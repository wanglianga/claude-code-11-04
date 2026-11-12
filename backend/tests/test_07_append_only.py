"""只追加保护与原始证据不可篡改。"""
import pytest
from sqlalchemy import text

from app.demo.loader import load_base_events


async def test_ledger_update_rejected(db):
    await load_base_events(db)
    with pytest.raises(Exception, match="append-only"):
        await db.execute(
            text("UPDATE ledger_entries SET book_amount = 0 WHERE id = (SELECT min(id) FROM ledger_entries)")
        )
    await db.rollback()


async def test_ledger_delete_rejected(db):
    await load_base_events(db)
    with pytest.raises(Exception, match="append-only"):
        await db.execute(text("DELETE FROM ledger_entries WHERE 1=1"))
    await db.rollback()


async def test_ledger_truncate_rejected(db):
    await load_base_events(db)
    with pytest.raises(Exception, match="append-only"):
        await db.execute(text("TRUNCATE TABLE ledger_entries"))
    await db.rollback()


async def test_raw_evidence_immutable_but_status_mutable(db):
    await load_base_events(db)
    # 证据列不可改
    with pytest.raises(Exception, match="immutable"):
        await db.execute(
            text("UPDATE raw_events SET payload = '{\"tampered\": true}' WHERE id = (SELECT min(id) FROM raw_events)")
        )
    await db.rollback()
    # 处理生命周期列允许更新
    await db.execute(
        text("UPDATE raw_events SET reject_reason = 'recheck' WHERE id = (SELECT min(id) FROM raw_events)")
    )
    await db.commit()


async def test_business_events_append_only(db):
    await load_base_events(db)
    with pytest.raises(Exception, match="append-only"):
        await db.execute(text("DELETE FROM business_events WHERE 1=1"))
    await db.rollback()
    # business_events 被 fx_rates 等外键引用，TRUNCATE 会先被外键约束拦截；
    # UPDATE 由只追加触发器直接拒绝，足以证明业务事件不可改写
    with pytest.raises(Exception, match="append-only"):
        await db.execute(
            text("UPDATE business_events SET event_type = 'TAMPERED' WHERE id = (SELECT min(id) FROM business_events)")
        )
    await db.rollback()
