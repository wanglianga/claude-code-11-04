"""更正账与月度报表版本。

规则：
  - 历史事实不可改写：汇率更正/结算修订/补单一律追加新事件与新分录；
  - 已关闭周期收到更正：生成更正账（FX_GAIN_LOSS / MANUAL_ADJUSTMENT）并产出新报表版本，
    旧版本标记 SUPERSEDED 但原样保留；
  - 报表版本快照内容不变则复用当前版本，变化才追加新版本。
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..enums import Account, ReportStatus, VersionStatus
from ..models import (
    BusinessEvent, FxRate, LedgerEntry, MonthlyReport, MonthlyReportVersion,
    SettlementBatch,
)
from .money import quantize


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(v) -> datetime:
    return datetime.fromisoformat(v) if isinstance(v, str) else v


# ---------------------------------------------------------------- 月度报表快照
async def _build_snapshot(db: AsyncSession, channel: str, merchant_id: str,
                          book_currency: str, report_month: str) -> dict:
    """按只追加分录的生效期间聚合月度快照（记账币口径）。"""
    entries = (
        await db.execute(
            select(LedgerEntry).where(
                LedgerEntry.channel == channel,
                LedgerEntry.merchant_id == merchant_id,
                LedgerEntry.book_currency == book_currency,
                LedgerEntry.effective_month == report_month,
            )
        )
    ).scalars().all()

    by_account: dict[str, dict[str, Decimal]] = {}
    event_ids: set[int] = set()
    for e in entries:
        event_ids.add(e.business_event_id)
        bucket = by_account.setdefault(
            e.account, {"DR": Decimal("0"), "CR": Decimal("0")}
        )
        bucket[e.direction] += e.book_amount

    accounts = {
        acc: {
            "debit": str(quantize(vals["DR"])),
            "credit": str(quantize(vals["CR"])),
            "net": str(quantize(vals["DR"] - vals["CR"])),
        }
        for acc, vals in sorted(by_account.items())
    }
    total_dr = quantize(sum((v["DR"] for v in by_account.values()), Decimal("0")))
    total_cr = quantize(sum((v["CR"] for v in by_account.values()), Decimal("0")))
    return {
        "channel": channel,
        "merchant_id": merchant_id,
        "book_currency": book_currency,
        "report_month": report_month,
        "generated_at": _now().isoformat(),
        "business_event_count": len(event_ids),
        "accounts": accounts,
        "conservation_check": {
            "total_debit": str(total_dr),
            "total_credit": str(total_cr),
            "balanced": total_dr == total_cr,
        },
    }


def _fingerprint(snapshot: dict) -> str:
    payload = {k: v for k, v in snapshot.items() if k != "generated_at"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def ensure_monthly_report_version(
    db: AsyncSession, batch: SettlementBatch, reason: str
) -> dict:
    """批次关闭/更正后调用：内容变化才追加新版本。"""
    report_month = batch.period_end.strftime("%Y-%m")
    book_currency = batch.currency  # 演示中结算币=记账币口径 USD；报表快照同时含记账币 CNY
    report = (
        await db.execute(
            select(MonthlyReport).where(
                MonthlyReport.channel == batch.channel,
                MonthlyReport.merchant_id == batch.merchant_id,
                MonthlyReport.currency == book_currency,
                MonthlyReport.report_month == report_month,
            )
        )
    ).scalar_one_or_none()
    if report is None:
        report = MonthlyReport(
            channel=batch.channel, merchant_id=batch.merchant_id,
            currency=book_currency, report_month=report_month,
            status=ReportStatus.DRAFT, current_version=0,
        )
        db.add(report)
        await db.flush()

    snapshot = await _build_snapshot(
        db, batch.channel, batch.merchant_id, "CNY", report_month
    )
    snapshot["source_batch_ref"] = batch.batch_ref
    snapshot["source_batch_version"] = batch.version
    fp = _fingerprint(snapshot)

    current = (
        await db.execute(
            select(MonthlyReportVersion)
            .where(
                MonthlyReportVersion.report_id == report.id,
                MonthlyReportVersion.version_status == VersionStatus.CURRENT,
            )
        )
    ).scalar_one_or_none()
    if current is not None and current.snapshot.get("fingerprint") == fp:
        return {"report_id": report.id, "version": current.version, "reused": True}

    if current is not None:
        # 旧版本仅标记 SUPERSEDED，快照行永不更新/删除
        current.version_status = VersionStatus.SUPERSEDED
    next_version = report.current_version + 1
    snapshot["fingerprint"] = fp
    db.add(MonthlyReportVersion(
        report_id=report.id, version=next_version,
        version_status=VersionStatus.CURRENT,
        snapshot=snapshot, reason=reason,
    ))
    report.current_version = next_version
    report.status = (
        ReportStatus.CLOSED if batch.status == "CLOSED" else ReportStatus.DRAFT
    )
    if batch.status == "CLOSED":
        report.closed_at = _now()
    await db.flush()
    return {"report_id": report.id, "version": next_version, "reused": False}


# ---------------------------------------------------------------- 汇率更正账
async def generate_fx_correction_entries(
    db: AsyncSession, fx_event: BusinessEvent
) -> dict:
    """对受影响的历史分录计算汇率更正差额，追加更正账（不动任何历史行）。"""
    p = fx_event.payload
    from_ccy, to_ccy = p["from_currency"], p["to_currency"]
    new_rate = Decimal(str(p["rate"]))
    effective_from = _dt(p["effective_from"])

    # 取更正前的最新汇率版本（只追加事实，旧版本仍在）
    prior = (
        await db.execute(
            select(FxRate)
            .where(
                FxRate.from_currency == from_ccy,
                FxRate.to_currency == to_ccy,
                FxRate.effective_from == effective_from,
                FxRate.business_event_id != fx_event.id,
            )
            .order_by(FxRate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    old_rate = prior.rate if prior else None

    # 受影响分录：生效期不早于更正生效时点，且走了该币种对
    candidates = (
        await db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.rate_effective_at.is_not(None))
        )
    ).scalars().all()
    affected: list[LedgerEntry] = [
        e for e in candidates
        if (e.settle_currency == from_ccy and e.book_currency == to_ccy)
        or (e.txn_currency == from_ccy and e.settle_currency == to_ccy)
    ]

    grouped: dict[int, list[LedgerEntry]] = {}
    for e in affected:
        be = await db.get(BusinessEvent, e.business_event_id)
        if be is None or be.event_time < effective_from:
            continue
        grouped.setdefault(be.id, []).append(e)

    correction_rows: list[LedgerEntry] = []
    affected_events = 0
    closed_months: set[str] = set()

    for be_id, group in grouped.items():
        orig_be = await db.get(BusinessEvent, be_id)
        per_event_net = Decimal("0")
        for e in group:
            if e.settle_currency == from_ccy and e.book_currency == to_ccy:
                restated_book = quantize(e.settle_amount * new_rate)
            else:
                new_settle = quantize(e.txn_amount * new_rate)
                restated_book = quantize(new_settle * e.settle_to_book_rate)
            delta = quantize(restated_book - e.book_amount)
            if delta == 0:
                continue
            direction = e.direction if delta > 0 else (
                "CR" if e.direction == "DR" else "DR"
            )
            correction_rows.append(LedgerEntry(
                business_event_id=fx_event.id,
                account=e.account,
                direction=direction,
                txn_amount=Decimal("0"), txn_currency=e.txn_currency,
                settle_amount=Decimal("0"), settle_currency=e.settle_currency,
                book_amount=abs(delta), book_currency=e.book_currency,
                txn_to_settle_rate=e.txn_to_settle_rate,
                settle_to_book_rate=new_rate,
                rate_source=f"FX_CORRECTION({p['rate_source']})",
                rate_effective_at=effective_from,
                decimal_places=e.decimal_places,
                raw_amount=e.raw_amount,
                effective_month=orig_be.event_time.strftime("%Y-%m"),
                channel=fx_event.channel, merchant_id=fx_event.merchant_id,
                order_ref=e.order_ref,
            ))
            signed = abs(delta) if direction == "DR" else -abs(delta)
            per_event_net += signed

        # 平衡行：汇兑损益，保证更正事件整体借贷守恒
        bal = quantize(-per_event_net)
        if bal != 0:
            correction_rows.append(LedgerEntry(
                business_event_id=fx_event.id,
                account=Account.FX_GAIN_LOSS,
                direction="DR" if bal > 0 else "CR",
                txn_amount=Decimal("0"), txn_currency=to_ccy,
                settle_amount=Decimal("0"), settle_currency=to_ccy,
                book_amount=abs(bal), book_currency=to_ccy,
                settle_to_book_rate=new_rate,
                rate_source=f"FX_CORRECTION({p['rate_source']})",
                rate_effective_at=effective_from,
                decimal_places=2, raw_amount=Decimal("0"),
                effective_month=orig_be.event_time.strftime("%Y-%m"),
                channel=fx_event.channel, merchant_id=fx_event.merchant_id,
            ))
        if correction_rows:
            affected_events += 1
            closed_months.add(orig_be.event_time.strftime("%Y-%m"))

    db.add_all(correction_rows)
    await db.flush()

    # 已关闭月份触发新报表版本（快照不可变，只能追加版本）
    new_versions: list[dict] = []
    for month in sorted(closed_months):
        reports = (
            await db.execute(
                select(MonthlyReport).where(
                    MonthlyReport.channel == fx_event.channel,
                    MonthlyReport.report_month == month,
                    MonthlyReport.status == ReportStatus.CLOSED,
                )
            )
        ).scalars().all()
        for report in reports:
            batch = (
                await db.execute(
                    select(SettlementBatch)
                    .where(
                        SettlementBatch.channel == report.channel,
                        SettlementBatch.merchant_id == report.merchant_id,
                        SettlementBatch.status == "CLOSED",
                    )
                    .order_by(SettlementBatch.closed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if batch:
                new_versions.append(
                    await ensure_monthly_report_version(
                        db, batch,
                        reason=(
                            f"汇率更正 {from_ccy}->{to_ccy}@{new_rate}"
                            f"（事件 {fx_event.business_key}）"
                        ),
                    )
                )

    return {
        "fx_event_id": fx_event.id,
        "old_rate": str(old_rate) if old_rate else None,
        "new_rate": str(new_rate),
        "affected_business_events": affected_events,
        "correction_entries": len(correction_rows),
        "new_report_versions": new_versions,
    }


async def regenerate_closed_reports(db: AsyncSession,
                                    channel: str | None = None) -> list[dict]:
    """补单/事后事件入口：为已关闭报表重算快照，内容有变化才追加新版本。"""
    stmt = select(MonthlyReport).where(MonthlyReport.status == ReportStatus.CLOSED)
    if channel:
        stmt = stmt.where(MonthlyReport.channel == channel)
    reports = (await db.execute(stmt)).scalars().all()
    out: list[dict] = []
    for report in reports:
        batch = (
            await db.execute(
                select(SettlementBatch)
                .where(
                    SettlementBatch.channel == report.channel,
                    SettlementBatch.merchant_id == report.merchant_id,
                    SettlementBatch.status == "CLOSED",
                )
                .order_by(SettlementBatch.closed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if batch is None:
            continue
        r = await ensure_monthly_report_version(
            db, batch, reason="关闭周期补单/事后事件触发的新报表版本"
        )
        if not r.get("reused"):
            out.append(r)
    return out
