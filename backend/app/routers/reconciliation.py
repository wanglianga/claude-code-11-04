"""结算批次、匹配、差异处置与报表查询 API。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import (
    BankTransaction, Discrepancy, DiscrepancyAction, MonthlyReport,
    MonthlyReportVersion, SettlementBatch, SettlementLine, SettlementMatch,
)
from ..schemas import (
    AdjustPostIn, AdjustSuggestIn, AttachIn, BatchOut, ClaimIn,
    CloseBatchIn, CloseDiscrepancyIn, DiscrepancyOut, MatchConfirmIn,
)
from ..services.reconciliation import run_reconciliation
from ..services.workflow import (
    claim, attach, suggest_adjustment, post_adjustment,
    close_discrepancy, confirm_match, close_batch, batch_snapshot,
)

router = APIRouter(prefix="/api", tags=["reconciliation"])


# ---------------------------------------------------------------- 批次
@router.get("/batches", response_model=list[BatchOut])
async def list_batches(status: str | None = None,
                       db: AsyncSession = Depends(get_db)):
    stmt = select(SettlementBatch).order_by(
        SettlementBatch.period_end.desc(), SettlementBatch.version
    )
    if status:
        stmt = stmt.where(SettlementBatch.status == status)
    return (await db.execute(stmt)).scalars().all()


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: int, db: AsyncSession = Depends(get_db)):
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "批次不存在")
    lines = (
        await db.execute(
            select(SettlementLine).where(SettlementLine.batch_id == batch_id)
        )
    ).scalars().all()
    return {
        "batch": BatchOut.model_validate(batch),
        "lines": [
            {
                "id": l.id, "line_ref": l.line_ref, "order_ref": l.order_ref,
                "channel_ref": l.channel_ref, "line_type": l.line_type,
                "gross_amount": str(l.gross_amount),
                "fee_amount": str(l.fee_amount),
                "net_amount": str(l.net_amount),
                "currency": l.currency, "matched": l.matched,
            }
            for l in lines
        ],
    }


@router.post("/batches/{batch_id}/reconcile")
async def reconcile(batch_id: int, db: AsyncSession = Depends(get_db)):
    try:
        result = await run_reconciliation(db, batch_id)
        await db.commit()
        return result
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/batches/{batch_id}/close")
async def close(batch_id: int, data: CloseBatchIn,
                db: AsyncSession = Depends(get_db)):
    try:
        result = await close_batch(db, batch_id, data.operator)
        await db.commit()
        return result
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.get("/batches/{batch_id}/snapshot")
async def snapshot(batch_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await batch_snapshot(db, batch_id)
    except Exception as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------- 匹配
@router.get("/matches")
async def list_matches(level: str | None = None,
                       batch_id: int | None = None,
                       db: AsyncSession = Depends(get_db)):
    stmt = select(SettlementMatch).order_by(SettlementMatch.id)
    if level:
        stmt = stmt.where(SettlementMatch.match_level == level)
    if batch_id:
        stmt = stmt.where(SettlementMatch.settlement_batch_id == batch_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": m.id, "match_level": m.match_level,
            "settlement_batch_id": m.settlement_batch_id,
            "business_event_id": m.business_event_id,
            "settlement_line_id": m.settlement_line_id,
            "bank_transaction_id": m.bank_transaction_id,
            "allocated_amount": str(m.allocated_amount),
            "currency": m.currency, "status": m.status,
            "match_method": m.match_method,
            "confirmed_at": m.confirmed_at,
        }
        for m in rows
    ]


@router.post("/matches/{match_id}/confirm")
async def confirm(match_id: int, data: MatchConfirmIn,
                  db: AsyncSession = Depends(get_db)):
    try:
        m = await confirm_match(db, match_id, data.operator)
        await db.commit()
        return {"id": m.id, "status": m.status, "match_method": m.match_method}
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------- 银行流水
@router.get("/bank-transactions")
async def list_bank(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(BankTransaction).order_by(BankTransaction.booked_at))
    ).scalars().all()
    return [
        {
            "id": b.id, "bank_ref": b.bank_ref, "channel": b.channel,
            "currency": b.currency, "amount": str(b.amount),
            "booked_at": b.booked_at, "memo": b.memo,
        }
        for b in rows
    ]


# ---------------------------------------------------------------- 差异处置队列
@router.get("/discrepancies", response_model=list[DiscrepancyOut])
async def list_discrepancies(
    status: str | None = None, kind: str | None = None,
    batch_id: int | None = None, assignee: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Discrepancy).order_by(Discrepancy.id)
    if status:
        stmt = stmt.where(Discrepancy.status == status)
    if kind:
        stmt = stmt.where(Discrepancy.kind == kind)
    if batch_id:
        stmt = stmt.where(Discrepancy.batch_id == batch_id)
    if assignee:
        stmt = stmt.where(Discrepancy.assignee == assignee)
    return (await db.execute(stmt)).scalars().all()


@router.get("/discrepancies/{disc_id}")
async def get_discrepancy(disc_id: int, db: AsyncSession = Depends(get_db)):
    d = await db.get(Discrepancy, disc_id)
    if d is None:
        raise HTTPException(404, "差异不存在")
    actions = (
        await db.execute(
            select(DiscrepancyAction)
            .where(DiscrepancyAction.discrepancy_id == disc_id)
            .order_by(DiscrepancyAction.id)
        )
    ).scalars().all()
    return {
        "discrepancy": DiscrepancyOut.model_validate(d),
        "actions": [
            {
                "id": a.id, "action_type": a.action_type,
                "operator": a.operator, "comment": a.comment,
                "attachment": a.attachment, "created_at": a.created_at,
            }
            for a in actions
        ],
    }


@router.post("/discrepancies/{disc_id}/claim", response_model=DiscrepancyOut)
async def do_claim(disc_id: int, data: ClaimIn,
                   db: AsyncSession = Depends(get_db)):
    try:
        d = await claim(db, disc_id, data.operator, data.comment)
        await db.commit()
        return d
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/discrepancies/{disc_id}/attach", response_model=DiscrepancyOut)
async def do_attach(disc_id: int, data: AttachIn,
                    db: AsyncSession = Depends(get_db)):
    try:
        d = await attach(db, disc_id, data.operator, data.attachment, data.comment)
        await db.commit()
        return d
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/discrepancies/{disc_id}/adjust-suggest", response_model=DiscrepancyOut)
async def do_suggest(disc_id: int, data: AdjustSuggestIn,
                     db: AsyncSession = Depends(get_db)):
    try:
        d = await suggest_adjustment(
            db, disc_id, data.operator, data.adjust_amount,
            data.account, data.comment,
        )
        await db.commit()
        return d
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/discrepancies/{disc_id}/adjust-post", response_model=DiscrepancyOut)
async def do_post(disc_id: int, data: AdjustPostIn,
                  db: AsyncSession = Depends(get_db)):
    try:
        d = await post_adjustment(db, disc_id, data.operator, data.comment)
        await db.commit()
        return d
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/discrepancies/{disc_id}/close", response_model=DiscrepancyOut)
async def do_close(disc_id: int, data: CloseDiscrepancyIn,
                   db: AsyncSession = Depends(get_db)):
    try:
        d = await close_discrepancy(db, disc_id, data.operator, data.comment)
        await db.commit()
        return d
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------- 月度报表
@router.get("/reports")
async def list_reports(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(MonthlyReport))).scalars().all()
    return [
        {
            "id": r.id, "channel": r.channel, "merchant_id": r.merchant_id,
            "currency": r.currency, "report_month": r.report_month,
            "status": r.status, "current_version": r.current_version,
            "closed_at": r.closed_at,
        }
        for r in rows
    ]


@router.get("/reports/{report_id}/versions")
async def report_versions(report_id: int, db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(MonthlyReportVersion)
            .where(MonthlyReportVersion.report_id == report_id)
            .order_by(MonthlyReportVersion.version)
        )
    ).scalars().all()
    return [
        {
            "id": v.id, "version": v.version,
            "version_status": v.version_status,
            "reason": v.reason, "created_at": v.created_at,
            "snapshot": v.snapshot,
        }
        for v in rows
    ]
