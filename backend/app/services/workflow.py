"""财务处置工作流与批次关闭。

硬性规则：
  - 自动任务只能生成 PROPOSED 匹配与 OPEN 差异，任何核销/关闭必须由人工 operator 触发；
  - UNKNOWN 差异禁止直接关闭：必须先过账调账（留痕 MANUAL_ADJUSTMENT 事件）；
  - 批次关闭 = 生成不可变快照并写入月度报表版本；关闭后收到更正只走新版本，不改快照。
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..enums import (
    ActionType, DiscrepancyKind, DiscrepancyStatus, EventType, MatchStatus,
)
from ..models import (
    BusinessEvent, Discrepancy, DiscrepancyAction, LedgerEntry,
    SettlementBatch, SettlementMatch,
)
from .corrections import ensure_monthly_report_version
from .money import quantize


class WorkflowError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _log(db: AsyncSession, discrepancy_id: int, action: str,
               operator: str, comment: str | None = None,
               attachment: dict | None = None) -> None:
    db.add(DiscrepancyAction(
        discrepancy_id=discrepancy_id, action_type=action,
        operator=operator, comment=comment, attachment=attachment,
    ))


async def claim(db: AsyncSession, discrepancy_id: int, operator: str,
                comment: str | None) -> Discrepancy:
    d = await db.get(Discrepancy, discrepancy_id)
    if d is None:
        raise WorkflowError("差异不存在")
    if d.status == DiscrepancyStatus.CLOSED:
        raise WorkflowError("差异已关闭，不可认领")
    d.status = DiscrepancyStatus.CLAIMED
    d.assignee = operator
    await _log(db, d.id, ActionType.CLAIM, operator, comment)
    await db.flush()
    return d


async def attach(db: AsyncSession, discrepancy_id: int, operator: str,
                 attachment: dict, comment: str | None) -> Discrepancy:
    d = await db.get(Discrepancy, discrepancy_id)
    if d is None:
        raise WorkflowError("差异不存在")
    await _log(db, d.id, ActionType.ATTACH, operator, comment, attachment)
    await db.flush()
    return d


async def suggest_adjustment(db: AsyncSession, discrepancy_id: int, operator: str,
                             adjust_amount: Decimal, account: str,
                             comment: str) -> Discrepancy:
    """创建调账建议：仅登记建议与处置轨迹，不过账、不动账本。"""
    d = await db.get(Discrepancy, discrepancy_id)
    if d is None:
        raise WorkflowError("差异不存在")
    if d.status == DiscrepancyStatus.CLOSED:
        raise WorkflowError("差异已关闭")
    d.status = DiscrepancyStatus.CLAIMED
    d.assignee = operator
    await _log(db, d.id, ActionType.ADJUST_SUGGEST, operator, comment, {
        "suggested_amount": str(adjust_amount),
        "account": account,
        "posted": False,
    })
    await db.flush()
    return d


async def post_adjustment(db: AsyncSession, discrepancy_id: int, operator: str,
                          comment: str | None) -> Discrepancy:
    """人工过账调账：生成 MANUAL_ADJUSTMENT 业务事件与平衡分录，差异转为 RESOLVED。"""
    d = await db.get(Discrepancy, discrepancy_id)
    if d is None:
        raise WorkflowError("差异不存在")
    if d.status == DiscrepancyStatus.CLOSED:
        raise WorkflowError("差异已关闭")
    if d.adjustment_event_id is not None:
        raise WorkflowError("该差异调账已过账，禁止重复过账")

    # 取最近一条调账建议作为过账依据；没有建议则按差异金额自动配对科目
    suggestion = None
    actions = (
        await db.execute(
            select(DiscrepancyAction)
            .where(
                DiscrepancyAction.discrepancy_id == d.id,
                DiscrepancyAction.action_type == ActionType.ADJUST_SUGGEST,
            )
            .order_by(DiscrepancyAction.id.desc())
        )
    ).scalars().first()
    if actions is not None:
        suggestion = actions.attachment

    amount = (
        Decimal(suggestion["suggested_amount"]) if suggestion
        else quantize(-d.diff_amount)  # 反向补足差异
    )
    account = suggestion["account"] if suggestion else "MANUAL_ADJUSTMENT"
    if amount == 0:
        raise WorkflowError("调账金额为 0，无法过账")

    # 差异口径为结算币，过账统一换算到记账币 CNY（按过账时点汇率）
    from .fx import get_rate  # 避免循环导入
    rate, rate_src, _, _ = await get_rate(db, d.currency, "CNY", _now())
    book_amount = quantize(abs(amount) * rate)
    payload = {
        "event_type": EventType.MANUAL_ADJUSTMENT,
        "entity_type": "MANUAL_ADJ",
        "entity_ref": f"ADJ-DISC-{d.id}",
        "business_key": f"{EventType.MANUAL_ADJUSTMENT}:DISC-{d.id}:v1",
        "channel": (await db.get(SettlementBatch, d.batch_id)).channel,
        "order_ref": d.order_ref,
        "event_time": _now().isoformat(),
        "amount": str(abs(amount)),
        "raw_amount": str(abs(amount)),
        "book_amount": str(book_amount),
        "book_currency": "CNY",
        "txn_currency": d.currency,
        "settle_currency": d.currency,
        "decimal_places": 2,
        # 差异为正（实际>账面）：借 对应科目 / 贷 调账；反之反向
        "debit_account": account if amount > 0 else "MANUAL_ADJUSTMENT",
        "credit_account": "MANUAL_ADJUSTMENT" if amount > 0 else account,
        "discrepancy_id": d.id,
        "comment": comment or "人工调账过账",
        "rate_source_hint": rate_src,
    }
    be = BusinessEvent(
        business_key=payload["business_key"],
        entity_type=payload["entity_type"],
        event_type=EventType.MANUAL_ADJUSTMENT,
        event_version=1,
        entity_ref=payload["entity_ref"],
        channel=payload["channel"],
        order_ref=d.order_ref,
        source="MANUAL",
        event_time=_now(),
        payload=payload,
    )
    db.add(be)
    await db.flush()

    # 延迟导入避免循环依赖
    from .ledger import _post, Posting  # noqa: WPS433
    await _post(db, be, payload, [
        Posting(payload["debit_account"], "DR", book_amount),
        Posting(payload["credit_account"], "CR", book_amount),
    ])

    d.adjustment_event_id = be.id
    d.status = DiscrepancyStatus.RESOLVED
    d.assignee = operator
    await _log(db, d.id, ActionType.ADJUST_POST, operator, comment, {
        "business_event_id": be.id,
        "amount": str(amount),
    })
    await db.flush()
    return d


async def close_discrepancy(db: AsyncSession, discrepancy_id: int,
                            operator: str, comment: str | None) -> Discrepancy:
    d = await db.get(Discrepancy, discrepancy_id)
    if d is None:
        raise WorkflowError("差异不存在")
    if d.status == DiscrepancyStatus.CLOSED:
        raise WorkflowError("差异已关闭")
    # 未知差必须先人工调账，系统绝不自动核销
    if d.kind == DiscrepancyKind.UNKNOWN and d.adjustment_event_id is None:
        raise WorkflowError(
            "UNKNOWN 未知差异禁止直接关闭：请先创建并过账人工调账"
        )
    d.status = DiscrepancyStatus.CLOSED
    await _log(db, d.id, ActionType.CLOSE, operator, comment)
    await db.flush()
    return d


async def confirm_match(db: AsyncSession, match_id: int, operator: str) -> SettlementMatch:
    m = await db.get(SettlementMatch, match_id)
    if m is None:
        raise WorkflowError("匹配不存在")
    m.status = MatchStatus.CONFIRMED
    m.match_method = "MANUAL"
    m.confirmed_at = _now()
    await db.flush()
    return m


async def close_batch(db: AsyncSession, batch_id: int, operator: str) -> dict:
    """关闭批次：前置校验 -> 快照 -> 月度报表版本。"""
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None:
        raise WorkflowError("结算批次不存在")
    if batch.status == "CLOSED":
        raise WorkflowError("批次已关闭；更正请基于新版本处理")

    open_discs = (
        await db.execute(
            select(Discrepancy).where(
                Discrepancy.batch_id == batch.id,
                Discrepancy.status != DiscrepancyStatus.CLOSED,
            )
        )
    ).scalars().all()
    if open_discs:
        raise WorkflowError(
            f"仍有 {len(open_discs)} 条未关闭差异，不可关闭批次"
        )

    batch.status = "CLOSED"
    batch.closed_at = _now()
    batch.closed_by = operator
    await db.flush()

    report = await ensure_monthly_report_version(
        db, batch, reason=f"批次 {batch.batch_ref} v{batch.version} 关闭快照"
    )
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "closed_at": batch.closed_at.isoformat(),
        "report_id": report["report_id"],
        "report_version": report["version"],
    }


async def batch_snapshot(db: AsyncSession, batch_id: int) -> dict:
    """读取批次关闭时口径的快照数据（从只追加事件/分录/报表版本还原，不依赖可更新列）。"""
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None:
        raise WorkflowError("结算批次不存在")
    entries = (
        await db.execute(
            select(LedgerEntry)
            .join(BusinessEvent, BusinessEvent.id == LedgerEntry.business_event_id)
            .where(BusinessEvent.event_time <= batch.period_end)
        )
    ).scalars().all()
    totals: dict[str, Decimal] = {}
    for e in entries:
        key = f"{e.account}:{e.direction}"
        totals[key] = totals.get(key, Decimal("0")) + e.book_amount
    discs = (
        await db.execute(select(Discrepancy).where(Discrepancy.batch_id == batch.id))
    ).scalars().all()
    return {
        "batch_ref": batch.batch_ref,
        "version": batch.version,
        "status": batch.status,
        "period": [batch.period_start.isoformat(), batch.period_end.isoformat()],
        "gross_amount": str(batch.gross_amount),
        "fee_amount": str(batch.fee_amount),
        "net_amount": str(batch.net_amount),
        "currency": batch.currency,
        "closed_at": batch.closed_at.isoformat() if batch.closed_at else None,
        "closed_by": batch.closed_by,
        "discrepancies": [
            {"id": d.id, "kind": d.kind, "status": d.status,
             "diff_amount": str(d.diff_amount), "title": d.title}
            for d in discs
        ],
        "ledger_totals_book": {k: str(quantize(v)) for k, v in sorted(totals.items())},
    }
