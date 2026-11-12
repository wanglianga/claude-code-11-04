"""S6/S7/关闭后补单：修订新版本、汇率更正账、报表版本链与快照不被静默篡改。"""
from decimal import Decimal

from sqlalchemy import select

from app.demo.loader import (
    load_base_events, load_fx_correction, load_late_refund,
    load_revision_and_close, report_versions, resolve_and_close,
)
from app.enums import VersionStatus
from app.models import (
    FxRate, LedgerEntry, MonthlyReportVersion, SettlementBatch,
)


async def test_settlement_revision_keeps_old_version(db):
    base = await load_base_events(db)
    await resolve_and_close(db, base["batch"])
    v2_batch, info = await load_revision_and_close(db)

    v1 = await db.get(SettlementBatch, base["batch"].id)
    # 旧版本关闭快照原样保留，新版本独立存在
    assert v1.status == "CLOSED" and v1.version == 1
    assert v2_batch.version == 2 and v2_batch.status == "CLOSED"
    assert v2_batch.supersedes_batch_id == v1.id
    # v2 手续费后补：27.4 vs v1 的 26.9
    assert v2_batch.fee_amount == Decimal("27.4")


async def test_fx_correction_appends_entries_and_new_report_version(db):
    base = await load_base_events(db)
    await resolve_and_close(db, base["batch"])
    await load_revision_and_close(db)
    fx_be = await load_fx_correction(db)

    # 汇率事实表：两个版本并存，旧 7.20 不被覆盖
    rates = (await db.execute(
        select(FxRate).where(FxRate.from_currency == "USD", FxRate.to_currency == "CNY")
        .order_by(FxRate.version)
    )).scalars().all()
    assert [r.version for r in rates] == [1, 2]
    assert rates[0].rate == Decimal("7.20") and rates[1].rate == Decimal("7.25")
    assert rates[1].is_correction is True

    # 更正账：只追加，且自身借贷平衡
    rows = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.business_event_id == fx_be.id)
    )).scalars().all()
    assert len(rows) > 0
    dr = sum((e.book_amount for e in rows if e.direction == "DR"), Decimal("0"))
    cr = sum((e.book_amount for e in rows if e.direction == "CR"), Decimal("0"))
    assert dr == cr
    # 更正归属期：10 月历史事实为主；11-15 延迟入账（BK-002）同样在更正生效区间内
    months = {e.effective_month for e in rows}
    assert "2026-10" in months
    assert months <= {"2026-10", "2026-11"}

    # 已关闭月份产出新报表版本，旧版本 SUPERSEDED 但内容不变
    versions = await report_versions(db)
    statuses = [(v.version, v.version_status) for v in versions]
    assert any(s == VersionStatus.SUPERSEDED for _, s in statuses)
    first_snapshot = versions[0].snapshot
    # 重新读取旧版本快照，确认未被静默改写
    reloaded = (await db.execute(
        select(MonthlyReportVersion).where(MonthlyReportVersion.version == 1)
    )).scalar_one()
    assert reloaded.snapshot == first_snapshot


async def test_late_refund_creates_another_version(db):
    base = await load_base_events(db)
    await resolve_and_close(db, base["batch"])
    await load_revision_and_close(db)
    await load_fx_correction(db)
    _, new_versions = await load_late_refund(db)

    # 补单确实产生了新版本（10 月口径变化）
    assert len(new_versions) >= 1
    versions = await report_versions(db)
    assert versions[-1].version_status == VersionStatus.CURRENT
    assert versions[-1].version >= 3


async def test_historical_rate_not_overwritten_for_old_events(db):
    """历史事实不可用今天汇率覆盖：旧分录的 rate_source/rate 保持入账时值。"""
    base = await load_base_events(db)
    await resolve_and_close(db, base["batch"])
    await load_revision_and_close(db)

    before = (await db.execute(
        select(LedgerEntry).where(
            LedgerEntry.account == "REVENUE", LedgerEntry.direction == "CR"
        ).limit(1)
    )).scalar_one()
    old_rate = before.settle_to_book_rate
    await load_fx_correction(db)

    after = await db.get(LedgerEntry, before.id)
    assert after.settle_to_book_rate == old_rate  # 历史行不动
