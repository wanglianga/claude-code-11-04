"""对账引擎。

两层匹配：
  LEVEL1 订单业务事件 <-> 结算行（按订单号/渠道参考号）
  LEVEL2 结算行/结算单 <-> 银行流水（渠道+币种+金额，支持多笔合并入账与部分分配）

五类差异：AMOUNT 金额差 / FEE 手续费差 / FX 汇率差 / TIMING 时间差 / UNKNOWN 未知差。
引擎只生成 PROPOSED 匹配与 OPEN 差异（建议性质）：
  - 不自动确认匹配、不自动核销任何差异，UNKNOWN 一律转人工；
  - 可重复执行，按逻辑键去重，幂等不产生重复差异。
"""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..enums import (
    Account, DiscrepancyKind, DiscrepancyStatus, EventType, MatchStatus,
)
from ..models import (
    BankTransaction, BusinessEvent, Discrepancy, LedgerEntry,
    SettlementBatch, SettlementLine, SettlementMatch,
)
from .money import quantize

TOLERANCE = Decimal(settings.rounding_tolerance)
# 银行入账晚于结算周期结束超过该天数 -> 时间差
TIMING_DAYS = 7

_LINE_EVENT = {
    "CAPTURE": EventType.PAYMENT_CAPTURED,
    "REFUND": EventType.REFUND_SUCCEEDED,
    "CHARGEBACK": EventType.CHARGEBACK_OPENED,
    "CHARGEBACK_REVERSAL": EventType.CHARGEBACK_REVERSED,
}


class ReconcileError(Exception):
    pass


async def _existing_discrepancy(db: AsyncSession, batch_id: int, dedup_key: str,
                                global_scope: bool = False):
    """global_scope=True 时跨批次去重（如银行残差，全系统只提示一次）。"""
    if global_scope:
        rows = (await db.execute(select(Discrepancy))).scalars().all()
    else:
        rows = (
            await db.execute(
                select(Discrepancy).where(Discrepancy.batch_id == batch_id)
            )
        ).scalars().all()
    for d in rows:
        if d.detail.get("dedup_key") == dedup_key:
            return d
    return None


async def _raise_discrepancy(
    db: AsyncSession, batch: SettlementBatch, kind: str, title: str,
    expected: Decimal, actual: Decimal, currency: str, dedup_key: str,
    detail: dict | None = None, order_ref: str | None = None,
    channel_ref: str | None = None, line: SettlementLine | None = None,
    bank: BankTransaction | None = None, global_scope: bool = False,
) -> Discrepancy | None:
    """超过容忍阈值才建差异；同键已存在则跳过（幂等重跑）。
    TIMING 时间差不是金额型差异，不受金额容忍阈值影响。"""
    diff = quantize(actual - expected)
    if kind != DiscrepancyKind.TIMING and abs(diff) <= TOLERANCE \
            and kind != DiscrepancyKind.UNKNOWN:
        return None
    existed = await _existing_discrepancy(db, batch.id, dedup_key, global_scope)
    if existed is not None:
        return existed
    d = Discrepancy(
        batch_id=batch.id, kind=kind, status=DiscrepancyStatus.OPEN, title=title,
        order_ref=order_ref, channel_ref=channel_ref,
        settlement_line_id=line.id if line else None,
        bank_transaction_id=bank.id if bank else None,
        expected_amount=quantize(expected), actual_amount=quantize(actual),
        diff_amount=diff, currency=currency,
        detail={**(detail or {}), "dedup_key": dedup_key},
    )
    db.add(d)
    await db.flush()
    return d


async def _event_settle_totals(db: AsyncSession, be_id: int) -> dict:
    """汇总某业务事件分录的结算币有符号金额。

    本金取 REVENUE / CHARGEBACK 科目，统一 CR 为正、DR 为负，
    与结算行口径一致：捕获/拒付反转为正，退款/拒付为负；
    手续费取 PAYMENT_FEE（DR 为正，退费 CR 为负）。
    """
    rows = (
        await db.execute(
            select(LedgerEntry).where(LedgerEntry.business_event_id == be_id)
        )
    ).scalars().all()
    gross = Decimal("0")
    fee = Decimal("0")
    txn_gross = Decimal("0")
    rate = None
    for e in rows:
        if e.account in (Account.REVENUE, Account.CHARGEBACK):
            sign = Decimal("1") if e.direction == "CR" else Decimal("-1")
            gross += sign * e.settle_amount
            txn_gross += sign * e.txn_amount
            rate = e.txn_to_settle_rate
        if e.account == Account.PAYMENT_FEE:
            sign = Decimal("1") if e.direction == "DR" else Decimal("-1")
            fee += sign * e.settle_amount
    return {"gross": gross, "fee": fee, "txn_gross": txn_gross, "rate": rate}


async def _match_exists(db: AsyncSession, **filters) -> bool:
    stmt = select(SettlementMatch).filter_by(**filters)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


# ---------------------------------------------------------------- LEVEL 1
async def _reconcile_level1(db: AsyncSession, batch: SettlementBatch) -> int:
    created = 0
    lines = (
        await db.execute(
            select(SettlementLine).where(SettlementLine.batch_id == batch.id)
        )
    ).scalars().all()

    for line in lines:
        want_event = _LINE_EVENT.get(line.line_type)
        candidate = None
        if line.order_ref and want_event:
            candidate = (
                await db.execute(
                    select(BusinessEvent)
                    .where(
                        BusinessEvent.order_ref == line.order_ref,
                        BusinessEvent.event_type == want_event,
                    )
                    .order_by(BusinessEvent.event_time.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if candidate is None and line.channel_ref:
            candidate = (
                await db.execute(
                    select(BusinessEvent)
                    .where(BusinessEvent.channel_ref == line.channel_ref)
                    .order_by(BusinessEvent.event_time.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if candidate is None:
            # 结算行找不到订单侧事实：未知差，转人工
            await _raise_discrepancy(
                db, batch, DiscrepancyKind.UNKNOWN,
                f"结算行 {line.line_ref} 无匹配订单事件",
                Decimal("0"), line.net_amount, line.currency,
                dedup_key=f"L1-UNMATCHED-LINE:{line.id}",
                detail={"line_ref": line.line_ref, "line_type": line.line_type},
                order_ref=line.order_ref, channel_ref=line.channel_ref, line=line,
            )
            continue

        if not await _match_exists(
            db, match_level="ORDER_TO_SETTLEMENT",
            business_event_id=candidate.id, settlement_line_id=line.id,
        ):
            db.add(SettlementMatch(
                match_level="ORDER_TO_SETTLEMENT",
                business_event_id=candidate.id, settlement_line_id=line.id,
                allocated_amount=line.net_amount, currency=line.currency,
                status=MatchStatus.PROPOSED, match_method="AUTO",
            ))
            created += 1
        line.matched = True

        totals = await _event_settle_totals(db, candidate.id)
        # 1) 金额差与汇率差分解：仅跨币种交易（txn!=settle）才归因汇率差，
        #    同币种差异一律为金额差
        gross_diff = line.gross_amount - totals["gross"]
        pp = candidate.payload
        is_fx_pair = pp.get("txn_currency") != pp.get(
            "settle_currency", pp.get("txn_currency")
        )
        if totals["txn_gross"] > 0 and totals["rate"] and is_fx_pair:
            implied_rate = line.gross_amount / totals["txn_gross"]
            fx_diff = quantize(
                totals["txn_gross"] * (implied_rate - totals["rate"])
            )
        else:
            fx_diff = Decimal("0")
        amount_diff = quantize(gross_diff - fx_diff)

        if abs(amount_diff) > TOLERANCE:
            await _raise_discrepancy(
                db, batch, DiscrepancyKind.AMOUNT,
                f"订单 {line.order_ref or candidate.entity_ref} 结算金额差",
                totals["gross"], line.gross_amount, line.currency,
                dedup_key=f"L1-AMOUNT:{line.id}",
                detail={
                    "business_event_id": candidate.id,
                    "expected_gross_settle": str(totals["gross"]),
                    "line_gross": str(line.gross_amount),
                },
                order_ref=line.order_ref, channel_ref=line.channel_ref, line=line,
            )
        if abs(fx_diff) > TOLERANCE:
            await _raise_discrepancy(
                db, batch, DiscrepancyKind.FX,
                f"订单 {line.order_ref or candidate.entity_ref} 汇率差",
                # 金额口径传汇率差造成的结算币影响（0 与 fx_diff），
                # 原始汇率对比放 detail，避免用汇率点差做金额容忍判断
                Decimal("0"), fx_diff, line.currency,
                dedup_key=f"L1-FX:{line.id}",
                detail={
                    "business_event_id": candidate.id,
                    "event_time_rate": str(totals["rate"]),
                    "implied_settlement_rate": str(
                        line.gross_amount / totals["txn_gross"]
                    ) if totals["txn_gross"] else None,
                    "fx_diff_settle": str(fx_diff),
                },
                order_ref=line.order_ref, channel_ref=line.channel_ref, line=line,
            )

        # 2) 手续费差
        fee_diff = quantize(line.fee_amount - totals["fee"])
        if abs(fee_diff) > TOLERANCE:
            await _raise_discrepancy(
                db, batch, DiscrepancyKind.FEE,
                f"订单 {line.order_ref or candidate.entity_ref} 手续费差（后补手续费）",
                totals["fee"], line.fee_amount, line.currency,
                dedup_key=f"L1-FEE:{line.id}",
                detail={"business_event_id": candidate.id},
                order_ref=line.order_ref, channel_ref=line.channel_ref, line=line,
            )
    await db.flush()
    return created


# ---------------------------------------------------------------- LEVEL 2
async def _reconcile_level2(db: AsyncSession, batch: SettlementBatch) -> int:
    """结算单净额 vs 银行流水。一笔银行流水可覆盖多个结算单（合并入账），按时间贪心分配。"""
    created = 0
    # 同渠道同币种、周期前后各留 TIMING_DAYS 容差窗口的银行流水
    banks = (
        await db.execute(
            select(BankTransaction)
            .where(
                BankTransaction.channel == batch.channel,
                BankTransaction.currency == batch.currency,
                BankTransaction.booked_at >= batch.period_start - timedelta(days=TIMING_DAYS),
                BankTransaction.booked_at <= batch.period_end + timedelta(days=30),
            )
            .order_by(BankTransaction.booked_at)
        )
    ).scalars().all()

    # 已分配金额（流水可能被多个批次共享）
    allocated: dict[int, Decimal] = {}
    existing = (
        await db.execute(
            select(SettlementMatch).where(SettlementMatch.match_level == "SETTLEMENT_TO_BANK")
        )
    ).scalars().all()
    # 本批次历史轮次已分配的金额：重跑时必须扣除，否则会重复报“入账不足”
    already_for_batch = Decimal("0")
    for m in existing:
        allocated[m.bank_transaction_id] = allocated.get(
            m.bank_transaction_id, Decimal("0")
        ) + m.allocated_amount
        if m.settlement_batch_id == batch.id:
            already_for_batch += m.allocated_amount

    remaining_need = quantize(batch.net_amount - already_for_batch)
    for bank in banks:
        available = bank.amount - allocated.get(bank.id, Decimal("0"))
        if available <= TOLERANCE or remaining_need <= TOLERANCE:
            continue
        take = min(available, remaining_need)
        # 同批次+同流水幂等去重（流水仍可被其他批次继续分配）
        dup = (
            await db.execute(
                select(SettlementMatch).where(
                    SettlementMatch.match_level == "SETTLEMENT_TO_BANK",
                    SettlementMatch.settlement_batch_id == batch.id,
                    SettlementMatch.bank_transaction_id == bank.id,
                )
            )
        ).scalar_one_or_none()
        if dup is None:
            db.add(SettlementMatch(
                match_level="SETTLEMENT_TO_BANK",
                settlement_batch_id=batch.id,
                bank_transaction_id=bank.id,
                allocated_amount=quantize(take),
                currency=batch.currency,
                status=MatchStatus.PROPOSED, match_method="AUTO",
            ))
            created += 1
        allocated[bank.id] = allocated.get(bank.id, Decimal("0")) + take
        remaining_need = quantize(remaining_need - take)

        # 时间差：入账显著晚于结算周期
        lag = (bank.booked_at - batch.period_end).days
        if lag > TIMING_DAYS:
            await _raise_discrepancy(
                db, batch, DiscrepancyKind.TIMING,
                f"批次 {batch.batch_ref} 银行入账延迟 {lag} 天",
                Decimal("0"), Decimal("0"), batch.currency,
                dedup_key=f"L2-TIMING:{batch.id}:{bank.id}",
                detail={"lag_days": lag, "bank_ref": bank.bank_ref},
                bank=bank,
            )

    # 结算单未被银行流水完全覆盖：未知差（缺入账），禁止自动核销
    if remaining_need > TOLERANCE:
        await _raise_discrepancy(
            db, batch, DiscrepancyKind.UNKNOWN,
            f"批次 {batch.batch_ref} 银行入账不足，缺口 {remaining_need}",
            batch.net_amount, batch.net_amount - remaining_need, batch.currency,
            dedup_key=f"L2-SHORT:{batch.id}",
            detail={"missing_amount": str(remaining_need)},
        )
    await db.flush()
    return created


async def _check_bank_residuals(db: AsyncSession) -> int:
    """全局残差检查：所有 LEVEL2 分配完成后，银行流水仍有未解释余额才报未知差。"""
    created = 0
    allocations: dict[int, Decimal] = {}
    for m in (await db.execute(
        select(SettlementMatch).where(SettlementMatch.match_level == "SETTLEMENT_TO_BANK")
    )).scalars().all():
        allocations[m.bank_transaction_id] = allocations.get(
            m.bank_transaction_id, Decimal("0")
        ) + m.allocated_amount

    open_batches = (
        await db.execute(select(SettlementBatch).where(SettlementBatch.status == "OPEN"))
    ).scalars().all()
    for bank in (await db.execute(select(BankTransaction))).scalars().all():
        residual = quantize(bank.amount - allocations.get(bank.id, Decimal("0")))
        if residual <= TOLERANCE:
            continue
        for batch in open_batches:
            if batch.channel != bank.channel or batch.currency != bank.currency:
                continue
            d = await _raise_discrepancy(
                db, batch, DiscrepancyKind.UNKNOWN,
                f"银行流水 {bank.bank_ref} 存在未解释残差（合并入账多余金额）",
                Decimal("0"), residual, bank.currency,
                dedup_key=f"L2-RESIDUAL:{bank.id}",
                detail={"bank_ref": bank.bank_ref, "residual": str(residual)},
                bank=bank, global_scope=True,
            )
            if d is not None:
                created += 1
            break
    return created


async def run_reconciliation(db: AsyncSession, batch_id: int) -> dict:
    """对单个结算批次执行对账（建议生成），可安全重复执行。"""
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None:
        raise ReconcileError(f"batch {batch_id} not found")
    if batch.status == "CLOSED":
        raise ReconcileError(
            f"batch {batch.batch_ref} v{batch.version} 已关闭，对账请在新版本上进行"
        )
    m1 = await _reconcile_level1(db, batch)
    m2 = await _reconcile_level2(db, batch)
    residual_discs = await _check_bank_residuals(db)
    discs = (
        await db.execute(
            select(Discrepancy).where(Discrepancy.batch_id == batch.id)
        )
    ).scalars().all()
    open_discs = [d for d in discs if d.status != DiscrepancyStatus.CLOSED]
    return {
        "batch_id": batch.id,
        "batch_ref": batch.batch_ref,
        "version": batch.version,
        "new_matches_level1": m1,
        "new_matches_level2": m2,
        "new_residual_discrepancies": residual_discs,
        "discrepancies_total": len(discs),
        "discrepancies_open": len(open_discs),
        "by_kind": {
            k: len([d for d in open_discs if d.kind == k])
            for k in (DiscrepancyKind.AMOUNT, DiscrepancyKind.FEE,
                      DiscrepancyKind.FX, DiscrepancyKind.TIMING,
                      DiscrepancyKind.UNKNOWN)
        },
    }


async def run_all_open_batches(db: AsyncSession) -> list[dict]:
    """自动任务入口：只扫描并生成建议，不做任何核销动作。"""
    batches = (
        await db.execute(
            select(SettlementBatch).where(SettlementBatch.status == "OPEN")
        )
    ).scalars().all()
    return [await run_reconciliation(db, b.id) for b in batches]
