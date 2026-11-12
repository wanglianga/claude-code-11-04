"""测试与种子脚本共用的场景装载器：按阶段装载，返回句柄供断言。"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import events as demo
from ..enums import DiscrepancyStatus
from ..models import BusinessEvent, Discrepancy, MonthlyReportVersion, SettlementBatch
from ..services.corrections import regenerate_closed_reports
from ..services.ledger import ingest_raw_event, process_event
from ..services.reconciliation import run_reconciliation
from ..services.workflow import (
    claim, close_batch, close_discrepancy, post_adjustment, suggest_adjustment,
)

OPERATOR = "finance.chen"


async def ingest(db: AsyncSession, event):
    raw, dup = await ingest_raw_event(db, event)
    be = None
    if not dup:
        be = await process_event(db, raw)
    return raw, dup, be


async def load_base_events(db: AsyncSession) -> dict:
    """阶段 1：汇率 + 订单/支付 + 银行流水 + 结算单 v1。"""
    counts = {"raw": 0, "duplicate": 0, "business": 0}
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        _, dup, be = await ingest(db, ev)
        counts["raw"] += 1
        counts["duplicate"] += int(dup)
        counts["business"] += int(be is not None)
    for ev in demo.bank_events():
        await ingest(db, ev)
    _, _, sb1_be = await ingest(db, demo.settlement_v1_event())
    await db.commit()
    batch = (
        await db.execute(
            select(SettlementBatch).where(
                SettlementBatch.batch_ref == "SB-202610",
                SettlementBatch.version == 1,
            )
        )
    ).scalar_one()
    return {"counts": counts, "batch": batch}


async def resolve_and_close(db: AsyncSession, batch: SettlementBatch) -> dict:
    """阶段 2：人工处置全部差异并关闭批次。"""
    result = await run_reconciliation(db, batch.id)
    discs = (
        await db.execute(
            select(Discrepancy).where(
                Discrepancy.batch_id == batch.id,
                Discrepancy.status != DiscrepancyStatus.CLOSED,
            )
        )
    ).scalars().all()
    for d in discs:
        await claim(db, d.id, OPERATOR, f"认领 {d.title}")
        await suggest_adjustment(
            db, d.id, OPERATOR,
            adjust_amount=-d.diff_amount if d.diff_amount != 0 else Decimal("5.00"),
            account="MANUAL_ADJUSTMENT", comment=f"调账建议：{d.title}",
        )
        await post_adjustment(db, d.id, OPERATOR, "复核后过账")
        await close_discrepancy(db, d.id, OPERATOR, "关闭确认")
    closed = await close_batch(db, batch.id, OPERATOR)
    await db.commit()
    return {"reconcile": result, "closed": closed, "resolved": len(discs)}


async def load_revision_and_close(db: AsyncSession) -> tuple[SettlementBatch, dict]:
    """阶段 3：结算单修订 v2 -> 对账 -> 处置 -> 关闭。"""
    _, _, _ = await ingest(db, demo.settlement_v2_event())
    await db.commit()
    batch = (
        await db.execute(
            select(SettlementBatch).where(
                SettlementBatch.batch_ref == "SB-202610",
                SettlementBatch.version == 2,
            )
        )
    ).scalar_one()
    info = await resolve_and_close(db, batch)
    return batch, info


async def load_fx_correction(db: AsyncSession) -> BusinessEvent:
    """阶段 4：汇率更正。"""
    _, _, be = await ingest(db, demo.fx_correction_event())
    await db.commit()
    return be


async def load_late_refund(db: AsyncSession):
    """阶段 5：关闭周期补单 + 报表新版本。"""
    _, _, be = await ingest(db, demo.late_october_refund_event())
    await db.commit()
    versions = await regenerate_closed_reports(db)
    await db.commit()
    return be, versions


async def report_versions(db: AsyncSession) -> list[MonthlyReportVersion]:
    return list(
        (await db.execute(
            select(MonthlyReportVersion).order_by(MonthlyReportVersion.version)
        )).scalars().all()
    )
