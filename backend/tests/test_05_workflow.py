"""人工处置工作流与关闭快照。"""
import pytest
from sqlalchemy import select

from app.demo.loader import load_base_events, resolve_and_close
from app.enums import DiscrepancyStatus
from app.models import (
    BusinessEvent, Discrepancy, DiscrepancyAction, MonthlyReport,
    MonthlyReportVersion, SettlementBatch,
)
from app.services.reconciliation import run_reconciliation
from app.services.workflow import (
    claim, close_batch, close_discrepancy, post_adjustment,
    suggest_adjustment, batch_snapshot,
)


async def test_claim_attach_adjust_close_trail(db):
    base = await load_base_events(db)
    await run_reconciliation(db, base["batch"].id)
    await db.commit()

    d = (await db.execute(select(Discrepancy).limit(1))).scalar_one()
    await claim(db, d.id, "finance.chen", "我认领")
    await suggest_adjustment(
        db, d.id, "finance.chen", abs(d.diff_amount),
        "MANUAL_ADJUSTMENT", "建议调账",
    )
    await post_adjustment(db, d.id, "finance.chen", "过账")
    await close_discrepancy(db, d.id, "finance.chen", "关闭")
    await db.commit()

    refreshed = await db.get(Discrepancy, d.id)
    assert refreshed.status == DiscrepancyStatus.CLOSED
    assert refreshed.adjustment_event_id is not None
    actions = (await db.execute(
        select(DiscrepancyAction).where(DiscrepancyAction.discrepancy_id == d.id)
    )).scalars().all()
    assert {a.action_type for a in actions} >= {
        "CLAIM", "ADJUST_SUGGEST", "ADJUST_POST", "CLOSE"
    }
    # 调账生成了独立可追溯业务事件
    adj = await db.get(BusinessEvent, refreshed.adjustment_event_id)
    assert adj.event_type == "MANUAL_ADJUSTMENT"
    assert adj.source == "MANUAL"


async def test_batch_cannot_close_with_open_discrepancies(db):
    base = await load_base_events(db)
    await run_reconciliation(db, base["batch"].id)
    await db.commit()
    with pytest.raises(Exception, match="未关闭差异"):
        await close_batch(db, base["batch"].id, "finance.chen")
    await db.rollback()


async def test_close_produces_report_version_and_snapshot(db):
    base = await load_base_events(db)
    await resolve_and_close(db, base["batch"])

    batch = await db.get(SettlementBatch, base["batch"].id)
    assert batch.status == "CLOSED" and batch.closed_by == "finance.chen"

    report = (await db.execute(select(MonthlyReport))).scalar_one()
    assert report.status == "CLOSED" and report.current_version == 1
    versions = (await db.execute(
        select(MonthlyReportVersion).where(MonthlyReportVersion.report_id == report.id)
    )).scalars().all()
    assert len(versions) == 1
    snap = versions[0].snapshot
    assert snap["conservation_check"]["balanced"] is True

    # 快照可随时按批次还原
    snap2 = await batch_snapshot(db, batch.id)
    assert snap2["status"] == "CLOSED"
    assert "ledger_totals_book" in snap2


async def test_closed_batch_blocks_reconcile(db):
    base = await load_base_events(db)
    await resolve_and_close(db, base["batch"])
    with pytest.raises(Exception, match="已关闭"):
        await run_reconciliation(db, base["batch"].id)
    await db.rollback()
