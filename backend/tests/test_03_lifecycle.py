"""S3-S5 业务轨迹：延迟入账、部分退款、拒付反转与账本守恒。"""
from decimal import Decimal

from sqlalchemy import func, select

from app.demo import events as demo
from app.demo.loader import ingest
from app.enums import Account
from app.models import BusinessEvent, LedgerEntry, PaymentState


async def test_partial_refund_state_and_entries(db):
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        await ingest(db, ev)
    await db.commit()

    p5 = (await db.execute(
        select(PaymentState).where(PaymentState.payment_ref == "P-005")
    )).scalar_one()
    assert p5.state == "PARTIALLY_REFUNDED"
    assert p5.captured_amount == Decimal("100")
    assert p5.refunded_amount == Decimal("40")

    # 退款分录存在且为红字方向（收入 DR / 应收 CR）
    refund_entries = (await db.execute(
        select(LedgerEntry)
        .join(BusinessEvent, BusinessEvent.id == LedgerEntry.business_event_id)
        .where(LedgerEntry.order_ref == "ORD-005",
               LedgerEntry.account == Account.REVENUE)
    )).scalars().all()
    directions = {e.direction for e in refund_entries}
    assert directions == {"DR", "CR"}  # 捕获时 CR，退款时 DR 冲回


async def test_chargeback_reversal_restores_captured(db):
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        await ingest(db, ev)
    await db.commit()

    p6 = (await db.execute(
        select(PaymentState).where(PaymentState.payment_ref == "P-006")
    )).scalar_one()
    # 拒付撤销后：拒付余额清零，状态回到 CAPTURED
    assert p6.chargeback_amount == Decimal("0")
    assert p6.state == "CAPTURED"


async def test_ledger_conservation(db):
    """账本守恒：任意时点全部分录借贷相等（记账币）。"""
    for ev in [*demo.fx_events(), *demo.order_payment_events(), *demo.bank_events()]:
        await ingest(db, ev)
    await db.commit()

    dr = (await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.book_amount), 0)).where(
            LedgerEntry.direction == "DR"
        )
    )).scalar_one()
    cr = (await db.execute(
        select(func.coalesce(func.sum(LedgerEntry.book_amount), 0)).where(
            LedgerEntry.direction == "CR"
        )
    )).scalar_one()
    assert Decimal(dr) == Decimal(cr)


async def test_three_currency_and_rate_provenance(db):
    """三币种与汇率溯源：EUR 订单分录保留 EUR/USD/CNY 与汇率来源。"""
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        await ingest(db, ev)
    await db.commit()

    row = (await db.execute(
        select(LedgerEntry)
        .join(BusinessEvent, BusinessEvent.id == LedgerEntry.business_event_id)
        .where(BusinessEvent.order_ref == "ORD-009",
               LedgerEntry.account == Account.SETTLEMENT_RECEIVABLE,
               LedgerEntry.direction == "DR")
    )).scalar_one()
    assert row.txn_currency == "EUR"
    assert row.settle_currency == "USD"
    assert row.book_currency == "CNY"
    assert row.txn_to_settle_rate == Decimal("1.0800000000")
    assert row.rate_source is not None and "CHANNEL_RATE" in row.rate_source
    assert row.raw_amount == Decimal("100")
