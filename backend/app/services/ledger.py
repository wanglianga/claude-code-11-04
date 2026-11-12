"""事件摄入与标准账本服务。

两层幂等：
  L1 raw_events(source, idempotency_key) 唯一 —— CDC 重放/重复 webhook 直接丢弃；
  L2 business_events(business_key) 唯一 —— 同一业务事实多来源到达只入账一次。
乱序安全：所有投影按业务发生时间 event_time 重放，迟到的旧事件不能把状态打回旧状态。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..enums import Account, EventType, PaymentState as PS
from ..models import (
    BusinessEvent, LedgerEntry, OrderState, PaymentState, RawEvent,
    SettlementBatch, SettlementLine, BankTransaction, FxRate,
)
from ..schemas import RawEventIn
from .fx import get_rate
from .money import quantize


class IngestError(Exception):
    pass


@dataclass
class Posting:
    account: str
    direction: str  # DR / CR
    book_amount: Decimal  # 记账币金额（过账平衡以此为准）


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(v) -> datetime:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


# ---------------------------------------------------------------- 入口
async def ingest_raw_event(db: AsyncSession, data: RawEventIn) -> tuple[RawEvent, bool]:
    """写入 L1。返回 (raw_event, is_duplicate)。"""
    stmt = (
        pg_insert(RawEvent)
        .values(
            idempotency_key=data.idempotency_key,
            source=data.source,
            event_occurred_at=data.event_occurred_at,
            topic=data.topic,
            payload=data.payload,
            status="RECEIVED",
        )
        .on_conflict_do_nothing(index_elements=["source", "idempotency_key"])
        .returning(RawEvent)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = (
            await db.execute(
                select(RawEvent).where(
                    RawEvent.source == data.source,
                    RawEvent.idempotency_key == data.idempotency_key,
                )
            )
        ).scalar_one()
        return row, True
    return row, False


async def process_event(db: AsyncSession, raw: RawEvent) -> BusinessEvent | None:
    """归一化并过账。返回 business_event；重复业务键返回 None（账本保持不动）。"""
    p = raw.payload
    event_type = p["event_type"]
    entity_ref = p["entity_ref"]
    event_version = int(p.get("event_version", 1))
    business_key = p.get("business_key", f"{event_type}:{entity_ref}:v{event_version}")

    # L2 幂等：同一业务事实只入账一次
    existing = (
        await db.execute(
            select(BusinessEvent).where(BusinessEvent.business_key == business_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raw.status = "PROCESSED"
        raw.processed_at = _now()
        raw.reject_reason = "duplicate business_key, ledger untouched"
        return None

    be = BusinessEvent(
        business_key=business_key,
        entity_type=p["entity_type"],
        event_type=event_type,
        event_version=event_version,
        entity_ref=entity_ref,
        channel_ref=p.get("channel_ref"),
        channel=p.get("channel"),
        merchant_id=p.get("merchant_id"),
        order_ref=p.get("order_ref"),
        source=raw.source,
        source_raw_event_id=raw.id,
        event_time=raw.event_occurred_at,
        payload=p,
        supersedes_event_id=p.get("supersedes_event_id"),
    )
    db.add(be)
    await db.flush()

    handler = _HANDLERS.get(event_type)
    if handler is not None:
        await handler(db, be, p)

    # 汇率更正：追加更正账并为受影响的已关闭月份产出新报表版本（历史行不动）
    if event_type == EventType.FX_RATE_CORRECTED:
        from .corrections import generate_fx_correction_entries  # 避免循环导入
        await generate_fx_correction_entries(db, be)

    raw.status = "PROCESSED"
    raw.processed_at = _now()
    return be


# ---------------------------------------------------------------- 分录
async def _rate_ctx(db: AsyncSession, p: dict) -> dict:
    """取事件发生时点的适用汇率（历史事实，不用今日汇率覆盖）。"""
    at = _dt(p["event_time"])
    txn_ccy = p["txn_currency"]
    settle_ccy = p.get("settle_currency", txn_ccy)
    book_ccy = p.get("book_currency", "CNY")
    r1, src1, eff1, _ = await get_rate(db, txn_ccy, settle_ccy, at)
    r2, src2, eff2, _ = await get_rate(db, settle_ccy, book_ccy, at)
    return {
        "txn_ccy": txn_ccy, "settle_ccy": settle_ccy, "book_ccy": book_ccy,
        "r1": r1, "r2": r2,
        "rate_source": f"{src1} | {src2}", "rate_effective_at": eff2, "at": at,
    }


async def _post(db: AsyncSession, be: BusinessEvent, p: dict,
                postings: list[Posting]) -> list[LedgerEntry]:
    """把一组借贷平衡的 Posting 落为标准账本分录。

    平衡以记账币校验；结算币/交易币由记账币按事件时点汇率反推，保证同组分录
    在三种币种下各自守恒，汇率来源与时点逐行留痕。
    """
    total_dr = sum((x.book_amount for x in postings if x.direction == "DR"), Decimal("0"))
    total_cr = sum((x.book_amount for x in postings if x.direction == "CR"), Decimal("0"))
    dp = int(p.get("decimal_places", 2))
    if quantize(total_dr, dp) != quantize(total_cr, dp):
        raise IngestError(
            f"unbalanced postings for {be.business_key}: DR={total_dr} CR={total_cr}"
        )

    ctx = await _rate_ctx(db, p)
    raw_event_amt = Decimal(str(p.get("raw_amount", p["amount"])))
    entries: list[LedgerEntry] = []
    for po in postings:
        book_amt = quantize(po.book_amount, dp)
        settle_amt = quantize(book_amt / ctx["r2"], dp)
        txn_amt = quantize(settle_amt / ctx["r1"], dp)
        entries.append(LedgerEntry(
            business_event_id=be.id,
            account=po.account,
            direction=po.direction,
            txn_amount=txn_amt,
            txn_currency=ctx["txn_ccy"],
            settle_amount=settle_amt,
            settle_currency=ctx["settle_ccy"],
            book_amount=book_amt,
            book_currency=ctx["book_ccy"],
            txn_to_settle_rate=ctx["r1"],
            settle_to_book_rate=ctx["r2"],
            rate_source=ctx["rate_source"],
            rate_effective_at=ctx["rate_effective_at"],
            decimal_places=dp,
            # 原始金额：首条分录保留事件原始报文金额，其余保留反推原值，均不改写
            raw_amount=raw_event_amt if not entries else txn_amt,
            effective_month=ctx["at"].strftime("%Y-%m"),
            channel=be.channel,
            merchant_id=be.merchant_id,
            order_ref=be.order_ref,
        ))
    db.add_all(entries)
    await db.flush()
    return entries


def _book_amounts(p: dict) -> dict:
    return {
        "amount": Decimal(str(p["amount"])),
        "fee": Decimal(str(p.get("fee_amount", "0"))),
        "book_amount": Decimal(str(p.get("book_amount", p["amount"]))),
        "book_fee": Decimal(str(p.get("book_fee_amount", p.get("fee_amount", "0")))),
    }


# ---------------------------------------------------------------- 投影
async def _project_order(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    ref = be.order_ref or be.entity_ref
    st = (
        await db.execute(select(OrderState).where(OrderState.order_ref == ref))
    ).scalar_one_or_none()
    new_status = p["order_status"]
    if st is None:
        db.add(OrderState(
            order_ref=ref, channel=be.channel, merchant_id=be.merchant_id,
            status=new_status,
            amount=Decimal(str(p.get("amount", "0"))),
            currency=p.get("txn_currency", p.get("currency", "CNY")),
            last_event_id=be.id,
        ))
        return
    last = (await db.get(BusinessEvent, st.last_event_id)) if st.last_event_id else None
    # 乱序防护：旧事件晚到，不回退状态
    if last is not None and be.event_time < last.event_time:
        return
    st.status = new_status
    if p.get("amount") is not None:
        st.amount = Decimal(str(p["amount"]))
    st.last_event_id = be.id


async def _project_payment(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    # 退款/拒付事件的 entity_ref 是退款号/拒付号，需通过渠道参考号或订单号回找支付尝试
    st = (
        await db.execute(select(PaymentState).where(PaymentState.payment_ref == be.entity_ref))
    ).scalar_one_or_none()
    if st is None and be.channel_ref:
        st = (
            await db.execute(
                select(PaymentState).where(PaymentState.channel_ref == be.channel_ref)
            )
        ).scalar_one_or_none()
    if st is None and be.order_ref:
        st = (
            await db.execute(
                select(PaymentState).where(PaymentState.order_ref == be.order_ref)
            )
        ).scalar_one_or_none()
    if st is None:
        st = PaymentState(
            payment_ref=be.entity_ref, order_ref=be.order_ref, channel=be.channel,
            channel_ref=be.channel_ref, state=PS.CREATED,
            currency=p.get("txn_currency", "CNY"),
        )
        db.add(st)
        await db.flush()

    et = be.event_type
    amt = Decimal(str(p.get("amount", "0")))
    if et == EventType.PAYMENT_AUTHORIZED:
        st.authorized_amount = amt
    elif et == EventType.PAYMENT_CAPTURED:
        st.captured_amount += amt
    elif et == EventType.REFUND_SUCCEEDED:
        st.refunded_amount += amt
    elif et == EventType.CHARGEBACK_OPENED:
        st.chargeback_amount += amt
    elif et == EventType.CHARGEBACK_REVERSED:
        st.chargeback_amount = max(Decimal("0"), st.chargeback_amount - amt)

    # 状态由累计金额推导，而非事件到达顺序
    if et in (EventType.PAYMENT_FAILED, EventType.PAYMENT_AUTH_EXPIRED):
        new_state = PS.FAILED
    elif st.chargeback_amount > 0:
        new_state = PS.CHARGEBACK
    elif st.captured_amount > 0 and st.refunded_amount >= st.captured_amount:
        new_state = PS.REFUNDED
    elif st.refunded_amount > 0:
        new_state = PS.PARTIALLY_REFUNDED
    elif st.captured_amount > 0:
        new_state = PS.CAPTURED
    elif st.authorized_amount > 0:
        new_state = PS.AUTHORIZED
    else:
        new_state = PS.CREATED

    last = (await db.get(BusinessEvent, st.last_event_id)) if st.last_event_id else None
    # 乱序防护：旧事件晚到不覆盖当前状态（累计金额仍保留，因为事件本身唯一且只追加）
    if last is None or be.event_time >= last.event_time:
        st.state = new_state
        st.last_event_id = be.id


# ---------------------------------------------------------------- 事件处理器
async def _h_order_event(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    await _project_order(db, be, p)


async def _h_payment_lifecycle(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    await _project_payment(db, be, p)


async def _h_payment_captured(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    await _project_payment(db, be, p)
    a = _book_amounts(p)
    # 借 渠道结算应收（总额） / 贷 收入；手续费 借 费用 / 贷 应收
    postings = [
        Posting(Account.SETTLEMENT_RECEIVABLE, "DR", a["book_amount"]),
        Posting(Account.REVENUE, "CR", a["book_amount"]),
    ]
    if a["book_fee"] > 0:
        postings += [
            Posting(Account.PAYMENT_FEE, "DR", a["book_fee"]),
            Posting(Account.SETTLEMENT_RECEIVABLE, "CR", a["book_fee"]),
        ]
    await _post(db, be, p, postings)


async def _h_refund_succeeded(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    await _project_payment(db, be, p)
    a = _book_amounts(p)
    # 红字冲回收入与应收；渠道退还手续费则一并冲回
    postings = [
        Posting(Account.REVENUE, "DR", a["book_amount"]),
        Posting(Account.SETTLEMENT_RECEIVABLE, "CR", a["book_amount"]),
    ]
    if a["book_fee"] > 0:
        postings += [
            Posting(Account.SETTLEMENT_RECEIVABLE, "DR", a["book_fee"]),
            Posting(Account.PAYMENT_FEE, "CR", a["book_fee"]),
        ]
    await _post(db, be, p, postings)


async def _h_chargeback_opened(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    await _project_payment(db, be, p)
    a = _book_amounts(p)
    await _post(db, be, p, [
        Posting(Account.CHARGEBACK, "DR", a["book_amount"]),
        Posting(Account.SETTLEMENT_RECEIVABLE, "CR", a["book_amount"]),
    ])


async def _h_chargeback_reversed(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    await _project_payment(db, be, p)
    a = _book_amounts(p)
    await _post(db, be, p, [
        Posting(Account.SETTLEMENT_RECEIVABLE, "DR", a["book_amount"]),
        Posting(Account.CHARGEBACK, "CR", a["book_amount"]),
    ])


async def _h_fx_rate(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    """汇率发布/更正：只追加新版本，旧汇率事实保留。更正账由 corrections 服务另行生成。"""
    db.add(FxRate(
        from_currency=p["from_currency"],
        to_currency=p["to_currency"],
        rate=Decimal(str(p["rate"])),
        rate_source=p["rate_source"],
        effective_from=_dt(p["effective_from"]),
        effective_to=None,
        version=int(p.get("event_version", 1)),
        is_correction=be.event_type == EventType.FX_RATE_CORRECTED,
        business_event_id=be.id,
    ))


async def _h_settlement(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    version = int(p.get("event_version", 1))
    prior = (
        await db.execute(
            select(SettlementBatch)
            .where(SettlementBatch.batch_ref == p["batch_ref"])
            .order_by(SettlementBatch.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # 修订 = 新版本批次，旧版本（含关闭快照）原样保留
    batch = SettlementBatch(
        batch_ref=p["batch_ref"],
        channel=p["channel"],
        merchant_id=p["merchant_id"],
        period_start=_dt(p["period_start"]),
        period_end=_dt(p["period_end"]),
        currency=p["currency"],
        version=version,
        status="OPEN",
        gross_amount=Decimal(str(p["gross_amount"])),
        fee_amount=Decimal(str(p["fee_amount"])),
        net_amount=Decimal(str(p["net_amount"])),
        business_event_id=be.id,
        supersedes_batch_id=prior.id if prior else None,
    )
    db.add(batch)
    await db.flush()
    for line in p["lines"]:
        db.add(SettlementLine(
            batch_id=batch.id,
            line_ref=line["line_ref"],
            order_ref=line.get("order_ref"),
            channel_ref=line.get("channel_ref"),
            line_type=line["line_type"],
            gross_amount=Decimal(str(line["gross_amount"])),
            fee_amount=Decimal(str(line["fee_amount"])),
            net_amount=Decimal(str(line["net_amount"])),
            currency=line.get("currency", p["currency"]),
        ))


async def _h_bank_deposit(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    db.add(BankTransaction(
        bank_ref=p["bank_ref"],
        channel=p["channel"],
        currency=p["currency"],
        amount=Decimal(str(p["amount"])),
        booked_at=_dt(p["booked_at"]),
        memo=p.get("memo"),
        business_event_id=be.id,
    ))
    book_amount = Decimal(str(p.get("book_amount", p["amount"])))
    await _post(db, be, p, [
        Posting(Account.CASH, "DR", book_amount),
        Posting(Account.SETTLEMENT_RECEIVABLE, "CR", book_amount),
    ])


async def _h_manual_adjustment(db: AsyncSession, be: BusinessEvent, p: dict) -> None:
    amount = Decimal(str(p["book_amount"]))
    await _post(db, be, p, [
        Posting(p["debit_account"], "DR", amount),
        Posting(p["credit_account"], "CR", amount),
    ])


_HANDLERS = {
    EventType.ORDER_CREATED: _h_order_event,
    EventType.ORDER_PAID: _h_order_event,
    EventType.ORDER_STATUS_CHANGED: _h_order_event,
    EventType.PAYMENT_CREATED: _h_payment_lifecycle,
    EventType.PAYMENT_AUTHORIZED: _h_payment_lifecycle,
    EventType.PAYMENT_AUTH_EXPIRED: _h_payment_lifecycle,
    EventType.PAYMENT_FAILED: _h_payment_lifecycle,
    EventType.PAYMENT_CAPTURED: _h_payment_captured,
    EventType.REFUND_REQUESTED: _h_payment_lifecycle,
    EventType.REFUND_SUCCEEDED: _h_refund_succeeded,
    EventType.CHARGEBACK_OPENED: _h_chargeback_opened,
    EventType.CHARGEBACK_REVERSED: _h_chargeback_reversed,
    EventType.FX_RATE_PUBLISHED: _h_fx_rate,
    EventType.FX_RATE_CORRECTED: _h_fx_rate,
    EventType.SETTLEMENT_BATCH_ISSUED: _h_settlement,
    EventType.SETTLEMENT_BATCH_REVISED: _h_settlement,
    EventType.BANK_DEPOSIT_RECEIVED: _h_bank_deposit,
    EventType.MANUAL_ADJUSTMENT: _h_manual_adjustment,
}
