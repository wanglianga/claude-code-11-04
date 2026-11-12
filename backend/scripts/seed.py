"""一键装载模拟数据并演示完整处置流程。

执行顺序（时间线乱序由数据集本身保证）：
  1. 汇率 + 订单/支付事件（含 CDC 重放、webhook 乱序、退款、拒付反转）
  2. 10 月结算单 v1 -> 自动对账（只给建议）-> 人工逐条处置 -> 关闭 -> 报表 v1
  3. 验证只追加保护（直接 SQL 篡改应被触发器拒绝）
  4. 结算单修订 v2 -> 对账 -> 处置 -> 关闭 -> 报表 v2（v1 标记 SUPERSEDED）
  5. 汇率更正 -> 更正账 -> 报表 v3
  6. 关闭周期补单（10 月退款 11 月补录）-> 报表 v4
"""
import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

# 允许脱离 PYTHONPATH 直接 `python scripts/seed.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.future import select

from app.database import SessionLocal, engine
from app.demo import events as demo
from app.enums import DiscrepancyStatus
from app.init_db import init_db
from app.models import (
    Discrepancy, LedgerEntry, MonthlyReportVersion, SettlementBatch,
)
from app.services.ledger import ingest_raw_event, process_event
from app.services.reconciliation import run_reconciliation
from app.services.workflow import (
    claim, suggest_adjustment, post_adjustment, close_discrepancy, close_batch,
)
from app.services.corrections import regenerate_closed_reports

OPERATOR = "finance.chen"


async def _ingest(db, event):
    raw, dup = await ingest_raw_event(db, event)
    be = None
    if not dup:
        be = await process_event(db, raw)
    return raw, dup, be


async def _resolve_all(db, batch_id: int) -> dict:
    """模拟财务人员逐条处置该批次全部未关闭差异。"""
    discs = (
        await db.execute(
            select(Discrepancy).where(
                Discrepancy.batch_id == batch_id,
                Discrepancy.status != DiscrepancyStatus.CLOSED,
            )
        )
    ).scalars().all()
    summary: dict[str, int] = {}
    for d in discs:
        summary[d.kind] = summary.get(d.kind, 0) + 1
        await claim(db, d.id, OPERATOR, f"认领 {d.title}")
        # 全部走“调账建议 -> 过账 -> 关闭”，UNKNOWN 也必须留人工调账痕迹
        await suggest_adjustment(
            db, d.id, OPERATOR,
            adjust_amount=-d.diff_amount if d.diff_amount != 0 else Decimal("5.00"),
            account="MANUAL_ADJUSTMENT",
            comment=f"调账建议：{d.title}",
        )
        await post_adjustment(db, d.id, OPERATOR, "复核后过账")
        await close_discrepancy(db, d.id, OPERATOR, "材料齐全，关闭确认")
    return summary


async def main(reset: bool = False) -> None:
    if reset:
        print("== 0. 重建 schema（--reset） ==")
        await engine.dispose()
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    await init_db()
    async with SessionLocal() as db:
        print("== 1. 摄入汇率与订单/支付事件 ==")
        raw_count = dup_count = be_count = 0
        for ev in [*demo.fx_events(), *demo.order_payment_events()]:
            _, dup, be = await _ingest(db, ev)
            raw_count += 1
            dup_count += int(dup)
            be_count += int(be is not None)
        await db.commit()
        print(f"   原始消息 {raw_count} 条，其中重复投递 {dup_count} 条，新增业务事件 {be_count} 条")

        print("== 2. 摄入银行流水（合并入账/延迟入账）与结算单 v1 ==")
        for ev in demo.bank_events():
            await _ingest(db, ev)
        await _ingest(db, demo.settlement_v1_event())
        await db.commit()
        sb1 = (
            await db.execute(
                select(SettlementBatch).where(
                    SettlementBatch.batch_ref == "SB-202610",
                    SettlementBatch.version == 1,
                )
            )
        ).scalar_one()

        print("== 3. 自动对账（引擎只生成建议，不核销） ==")
        r1 = await run_reconciliation(db, sb1.id)
        await db.commit()
        print("   " + json.dumps(r1["by_kind"], ensure_ascii=False))
        assert r1["discrepancies_open"] >= 4, "应至少产生金额/汇率/未知/时间差四类差异"

        print("== 4. 人工处置差异并关闭批次 v1 ==")
        summary = await _resolve_all(db, sb1.id)
        print(f"   人工处置差异分类统计：{summary}")
        closed1 = await close_batch(db, sb1.id, OPERATOR)
        await db.commit()
        print(f"   批次关闭：{closed1}")

        print("== 5. 只追加保护验证（预期触发器报错） ==")
        try:
            await db.execute(
                text("UPDATE ledger_entries SET book_amount = 0 WHERE id = (SELECT min(id) FROM ledger_entries)")
            )
            raise SystemExit("错误：只追加触发器未生效！")
        except Exception as exc:  # 预期异常
            assert "append-only" in str(exc)
            print(f"   账本篡改被拒绝：{exc.args[0][:80]}")
            await db.rollback()
        try:
            await db.execute(text("DELETE FROM business_events WHERE 1=1"))
            raise SystemExit("错误：业务事件可被删除！")
        except Exception as exc:
            assert "append-only" in str(exc)
            print(f"   事件删除被拒绝：{exc.args[0][:80]}")
            await db.rollback()

        print("== 6. 结算单修订 v2（关闭后更正不改旧快照） ==")
        await _ingest(db, demo.settlement_v2_event())
        await db.commit()
        sb2 = (
            await db.execute(
                select(SettlementBatch).where(
                    SettlementBatch.batch_ref == "SB-202610",
                    SettlementBatch.version == 2,
                )
            )
        ).scalar_one()
        r2 = await run_reconciliation(db, sb2.id)
        await db.commit()
        print(f"   v2 差异：{json.dumps(r2['by_kind'], ensure_ascii=False)}")
        await _resolve_all(db, sb2.id)
        closed2 = await close_batch(db, sb2.id, OPERATOR)
        await db.commit()
        print(f"   v2 关闭：{closed2}")

        print("== 7. 汇率更正 7.20 -> 7.25（只追加更正账） ==")
        _, _, fx_be = await _ingest(db, demo.fx_correction_event())
        await db.commit()
        correction_entries = (
            await db.execute(
                select(LedgerEntry).where(LedgerEntry.business_event_id == fx_be.id)
            )
        ).scalars().all()
        dr = sum((e.book_amount for e in correction_entries if e.direction == "DR"), Decimal("0"))
        cr = sum((e.book_amount for e in correction_entries if e.direction == "CR"), Decimal("0"))
        print(f"   更正分录 {len(correction_entries)} 行，借 {dr} / 贷 {cr}（平衡：{dr == cr}）")
        assert dr == cr

        print("== 8. 关闭周期补单（10 月退款 11 月补录） ==")
        _, _, late = await _ingest(db, demo.late_october_refund_event())
        await db.commit()
        new_versions = await regenerate_closed_reports(db)
        await db.commit()
        print(f"   补单触发新报表版本：{new_versions}")

        print("== 9. 最终汇总 ==")
        versions = (
            await db.execute(
                select(MonthlyReportVersion).order_by(MonthlyReportVersion.version)
            )
        ).scalars().all()
        for v in versions:
            print(
                f"   报表 v{v.version} [{v.version_status}] "
                f"借贷平衡={v.snapshot['conservation_check']['balanced']} "
                f"事件数={v.snapshot['business_event_count']} 原因={v.reason}"
            )
        from sqlalchemy import func
        balances = (
            await db.execute(
                select(
                    LedgerEntry.direction,
                    func.sum(LedgerEntry.book_amount),
                ).group_by(LedgerEntry.direction)
            )
        ).all()
        print(f"   全账本借贷：{balances}")
        print("SEED_DONE")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="装载差异处理中心模拟数据")
    parser.add_argument(
        "--reset", action="store_true",
        help="先 DROP/CREATE public schema 再装载（演示环境一键重置）",
    )
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset))
