"""S1 幂等性：CDC 重放、重复 webhook、轮询补拉均不得重复进账本。"""
from decimal import Decimal

from sqlalchemy import func, select

from app.demo import events as demo
from app.demo.loader import ingest
from app.models import BusinessEvent, LedgerEntry, RawEvent


async def test_duplicate_raw_event_dropped(db):
    ev = demo.fx_events()[0]
    _, dup1, _ = await ingest(db, ev)
    _, dup2, _ = await ingest(db, ev)
    assert dup1 is False and dup2 is True
    total = (await db.execute(
        select(func.count()).select_from(RawEvent)
    )).scalar_one()
    assert total == 1


async def test_cdc_replay_and_poll_share_one_ledger_fact(db):
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        await ingest(db, ev)
    await db.commit()

    # P-001 捕获来自 webhook + 轮询补拉两条 raw，但只有一个业务事件、一组捕获分录
    bes = (await db.execute(
        select(BusinessEvent).where(BusinessEvent.business_key == "PAYMENT_CAPTURED:P-001:v1")
    )).scalars().all()
    assert len(bes) == 1

    entries = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.business_event_id == bes[0].id)
    )).scalars().all()
    # 捕获分录：应收 DR/收入 CR + 手续费 DR/应收 CR = 4 行，重放后仍是 4 行
    assert len(entries) == 4
    receivable = sum(
        (e.book_amount for e in entries if e.account == "SETTLEMENT_RECEIVABLE" and e.direction == "DR"),
        Decimal("0"),
    )
    # 100 USD * 7.2 = 720 CNY，只记一次
    assert receivable == Decimal("720.00")


async def test_dataset_contains_replayed_messages(db):
    raw_keys = [e.idempotency_key for e in demo.order_payment_events()]
    # CDC-LSN-1001/1002 被重放一次
    assert raw_keys.count("CDC-LSN-1001") == 2
    assert raw_keys.count("CDC-LSN-1002") == 2
