"""S2 乱序安全：状态由业务时间重放，不被到达顺序破坏。"""
from sqlalchemy import select

from app.demo import events as demo
from app.demo.loader import ingest
from app.models import OrderState, PaymentState


async def test_payment_captured_when_auth_arrives_late(db):
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        await ingest(db, ev)
    await db.commit()

    p2 = (await db.execute(
        select(PaymentState).where(PaymentState.payment_ref == "P-002")
    )).scalar_one()
    # 捕获先到、授权（更早业务时间）晚到：最终状态仍是 CAPTURED，金额不翻倍
    assert p2.state == "CAPTURED"
    assert str(p2.captured_amount) == "120.000000"
    assert str(p2.authorized_amount) == "120.000000"


async def test_late_old_order_status_does_not_roll_back(db):
    for ev in [*demo.fx_events(), *demo.order_payment_events()]:
        await ingest(db, ev)
    await db.commit()

    o2 = (await db.execute(
        select(OrderState).where(OrderState.order_ref == "ORD-002")
    )).scalar_one()
    # 10-02 的 PROCESSING 在 10-06 才重放到达，PAID 不允许回退
    assert o2.status == "PAID"
