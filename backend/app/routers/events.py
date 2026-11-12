"""事件摄入、事件与账本查询 API。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..enums import EventSource
from ..models import (
    BusinessEvent, LedgerEntry, OrderState, PaymentState, RawEvent, FxRate,
)
from ..schemas import (
    BatchIngestIn, BatchIngestResult, BusinessEventOut, IngestResult,
    LedgerEntryOut, RawEventIn, FxRateIn,
)
from ..services.ledger import ingest_raw_event, process_event
from ..services.reconciliation import run_all_open_batches
from ..services.corrections import regenerate_closed_reports

router = APIRouter(prefix="/api", tags=["events"])


@router.post("/events/ingest", response_model=IngestResult)
async def ingest_one(data: RawEventIn, db: AsyncSession = Depends(get_db)):
    try:
        raw, duplicate = await ingest_raw_event(db, data)
        be = None
        entry_ids: list[int] = []
        if not duplicate:
            be = await process_event(db, raw)
            if be is not None:
                entry_ids = [
                    r for (r,) in (
                        await db.execute(
                            select(LedgerEntry.id).where(
                                LedgerEntry.business_event_id == be.id
                            )
                        )
                    ).all()
                ]
        await db.commit()
        return IngestResult(
            raw_event_id=raw.id,
            duplicate=duplicate,
            business_event_id=be.id if be else None,
            ledger_entry_ids=entry_ids,
            message="duplicate raw event, ledger untouched" if duplicate else "accepted",
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/events/ingest-batch", response_model=BatchIngestResult)
async def ingest_batch(data: BatchIngestIn, db: AsyncSession = Depends(get_db)):
    accepted = duplicates = rejected = 0
    results: list[IngestResult] = []
    for item in data.events:
        try:
            raw, is_dup = await ingest_raw_event(db, item)
            be = None
            if not is_dup:
                be = await process_event(db, raw)
                accepted += 1
            else:
                duplicates += 1
            await db.commit()
            results.append(IngestResult(
                raw_event_id=raw.id, duplicate=is_dup,
                business_event_id=be.id if be else None,
                message="duplicate" if is_dup else "accepted",
            ))
        except Exception as exc:  # 单条失败不影响整批
            await db.rollback()
            rejected += 1
            results.append(IngestResult(
                raw_event_id=0, duplicate=False,
                message=f"rejected: {exc}",
            ))
    return BatchIngestResult(
        accepted=accepted, duplicates=duplicates,
        rejected=rejected, results=results,
    )


@router.post("/fx/rates")
async def publish_fx(data: FxRateIn, db: AsyncSession = Depends(get_db)):
    from ..enums import EventType
    version_row = (
        await db.execute(
            select(func.coalesce(func.max(FxRate.version), 0)).where(
                FxRate.from_currency == data.from_currency,
                FxRate.to_currency == data.to_currency,
                FxRate.effective_from == data.effective_from,
            )
        )
    ).scalar_one()
    next_version = int(version_row) + 1
    event_type = (
        EventType.FX_RATE_CORRECTED if data.correction else EventType.FX_RATE_PUBLISHED
    )
    raw_in = RawEventIn(
        idempotency_key=(
            f"FX:{data.from_currency}{data.to_currency}:"
            f"{data.effective_from.isoformat()}:v{next_version}"
        ),
        source=EventSource.MANUAL,
        event_occurred_at=data.effective_from,
        topic="fx.rate",
        payload={
            "event_type": event_type,
            "entity_type": "FX_RATE",
            "entity_ref": f"FX-{data.from_currency}{data.to_currency}-{next_version}",
            "business_key": f"{event_type}:{data.from_currency}{data.to_currency}:{data.effective_from.isoformat()}:v{next_version}",
            "from_currency": data.from_currency,
            "to_currency": data.to_currency,
            "rate": str(data.rate),
            "rate_source": data.rate_source,
            "effective_from": data.effective_from.isoformat(),
            "event_time": data.effective_from.isoformat(),
            "event_version": next_version,
            "txn_currency": data.from_currency,
            "book_currency": "CNY",
            "amount": "0",
        },
    )
    raw, dup = await ingest_raw_event(db, raw_in)
    if not dup:
        await process_event(db, raw)
    await db.commit()
    return {"version": next_version, "correction": data.correction, "duplicate": dup}


@router.get("/events/raw")
async def list_raw(limit: int = Query(100, le=500),
                   db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(RawEvent).order_by(RawEvent.id.desc()).limit(limit))
    ).scalars().all()
    return [
        {
            "id": r.id, "source": r.source,
            "idempotency_key": r.idempotency_key,
            "event_occurred_at": r.event_occurred_at,
            "received_at": r.received_at, "topic": r.topic,
            "status": r.status, "reject_reason": r.reject_reason,
        }
        for r in rows
    ]


@router.get("/events/business", response_model=list[BusinessEventOut])
async def list_business(
    order_ref: str | None = None, entity_ref: str | None = None,
    event_type: str | None = None, limit: int = Query(200, le=1000),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(BusinessEvent).order_by(BusinessEvent.event_time, BusinessEvent.id)
    if order_ref:
        stmt = stmt.where(BusinessEvent.order_ref == order_ref)
    if entity_ref:
        stmt = stmt.where(BusinessEvent.entity_ref == entity_ref)
    if event_type:
        stmt = stmt.where(BusinessEvent.event_type == event_type)
    return (await db.execute(stmt.limit(limit))).scalars().all()


@router.get("/ledger/entries", response_model=list[LedgerEntryOut])
async def list_ledger(order_ref: str | None = None,
                      account: str | None = None,
                      limit: int = Query(500, le=2000),
                      db: AsyncSession = Depends(get_db)):
    stmt = select(LedgerEntry).order_by(LedgerEntry.id)
    if order_ref:
        stmt = stmt.where(LedgerEntry.order_ref == order_ref)
    if account:
        stmt = stmt.where(LedgerEntry.account == account)
    return (await db.execute(stmt.limit(limit))).scalars().all()


@router.get("/ledger/balances")
async def ledger_balances(db: AsyncSession = Depends(get_db)):
    """账本守恒视图：按科目/方向汇总记账币金额并校验借贷平衡。"""
    rows = (
        await db.execute(
            select(
                LedgerEntry.account, LedgerEntry.direction,
                func.sum(LedgerEntry.book_amount),
            ).group_by(LedgerEntry.account, LedgerEntry.direction)
        )
    ).all()
    accounts: dict[str, dict[str, str]] = {}
    total_dr = total_cr = 0
    for account, direction, amount in rows:
        bucket = accounts.setdefault(account, {"DR": "0", "CR": "0"})
        bucket[direction] = str(amount)
        if direction == "DR":
            total_dr += float(amount)
        else:
            total_cr += float(amount)
    return {
        "accounts": accounts,
        "total_debit": round(total_dr, 2),
        "total_credit": round(total_cr, 2),
        "balanced": abs(total_dr - total_cr) < 0.001,
    }


@router.get("/states/payments")
async def payment_states(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PaymentState))).scalars().all()
    return [
        {
            "payment_ref": r.payment_ref, "order_ref": r.order_ref,
            "channel": r.channel, "channel_ref": r.channel_ref,
            "state": r.state, "currency": r.currency,
            "authorized_amount": str(r.authorized_amount),
            "captured_amount": str(r.captured_amount),
            "refunded_amount": str(r.refunded_amount),
            "chargeback_amount": str(r.chargeback_amount),
            "last_event_id": r.last_event_id,
        }
        for r in rows
    ]


@router.get("/states/orders")
async def order_states(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(OrderState))).scalars().all()
    return [
        {
            "order_ref": r.order_ref, "status": r.status,
            "amount": str(r.amount), "currency": r.currency,
            "channel": r.channel, "last_event_id": r.last_event_id,
        }
        for r in rows
    ]


@router.post("/reconcile/run-all")
async def run_all(db: AsyncSession = Depends(get_db)):
    """自动任务：只生成匹配建议与差异提示，不做核销。"""
    result = await run_all_open_batches(db)
    await db.commit()
    return result


@router.post("/reports/regenerate")
async def regenerate(channel: str | None = None, db: AsyncSession = Depends(get_db)):
    """关闭周期补单后触发新报表版本。"""
    result = await regenerate_closed_reports(db, channel)
    await db.commit()
    return {"new_versions": result}
