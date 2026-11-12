"""建表与只追加触发器。

只追加表的证据列（payload、金额、业务键、事件时间等）禁止 UPDATE/DELETE；
raw_events 的处理生命周期列（status/processed_at/reject_reason）允许更新，
但不允许改动任何原始证据。
"""
from sqlalchemy import text

from .database import engine
from .models import Base, APPEND_ONLY_TABLES

APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION recon_reject_mutation() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'table % is append-only: TRUNCATE is not allowed', TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'table % is append-only: DELETE is not allowed', TG_TABLE_NAME;
    END IF;
    -- raw_events 仅允许处理生命周期列变化
    IF TG_TABLE_NAME = 'raw_events' THEN
        IF NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.source IS DISTINCT FROM OLD.source
           OR NEW.event_occurred_at IS DISTINCT FROM OLD.event_occurred_at
           OR NEW.topic IS DISTINCT FROM OLD.topic
           OR NEW.payload IS DISTINCT FROM OLD.payload
           OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
            RAISE EXCEPTION 'raw_events evidentiary columns are immutable';
        END IF;
        RETURN NEW;
    END IF;
    -- monthly_report_versions 仅允许版本状态列 CURRENT->SUPERSEDED，快照证据列禁改
    IF TG_TABLE_NAME = 'monthly_report_versions' THEN
        IF NEW.report_id IS DISTINCT FROM OLD.report_id
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.snapshot IS DISTINCT FROM OLD.snapshot
           OR NEW.reason IS DISTINCT FROM OLD.reason
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'monthly_report_versions snapshot columns are immutable';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'table % is append-only: UPDATE is not allowed', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER_TPL = """
DROP TRIGGER IF EXISTS trg_append_only_{tbl} ON {tbl};
CREATE TRIGGER trg_append_only_{tbl}
    BEFORE UPDATE OR DELETE ON {tbl}
    FOR EACH ROW EXECUTE FUNCTION recon_reject_mutation();
"""

TRUNCATE_TRIGGER_TPL = """
DROP TRIGGER IF EXISTS trg_block_truncate_{tbl} ON {tbl};
CREATE TRIGGER trg_block_truncate_{tbl}
    BEFORE TRUNCATE ON {tbl}
    FOR EACH STATEMENT EXECUTE FUNCTION recon_reject_mutation();
"""


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 函数定义是单条 CREATE 命令（$$ 内分号属于函数体），整条执行
        await conn.execute(text(APPEND_ONLY_FN))
        # 触发器模板含两条命令，asyncpg 预编译不支持多命令，逐句执行
        for tbl in APPEND_ONLY_TABLES:
            for tpl in (TRIGGER_TPL, TRUNCATE_TRIGGER_TPL):
                for stmt in (s.strip() for s in tpl.format(tbl=tbl).split(";") if s.strip()):
                    await conn.execute(text(stmt))
