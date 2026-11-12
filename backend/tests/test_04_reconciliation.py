"""对账引擎：两层匹配 + 五类差异归因 + 自动任务不核销。"""
import pytest
from sqlalchemy import func, select

from app.demo.loader import load_base_events
from app.enums import DiscrepancyKind, MatchStatus
from app.models import BankTransaction, Discrepancy, SettlementMatch
from app.services.reconciliation import run_reconciliation


async def test_reconcile_produces_typed_discrepancies(db):
    base = await load_base_events(db)
    result = await run_reconciliation(db, base["batch"].id)
    await db.commit()

    kinds = result["by_kind"]
    # 金额差（ORD-007）、汇率差（ORD-009）、未知差（幽灵行+银行残差）、时间差（延迟入账）
    assert kinds[DiscrepancyKind.AMOUNT] >= 1
    assert kinds[DiscrepancyKind.FX] >= 1
    assert kinds[DiscrepancyKind.UNKNOWN] >= 2
    assert kinds[DiscrepancyKind.TIMING] >= 1


async def test_two_level_matches_created(db):
    base = await load_base_events(db)
    await run_reconciliation(db, base["batch"].id)
    await db.commit()

    l1 = (await db.execute(
        select(func.count()).select_from(SettlementMatch).where(
            SettlementMatch.match_level == "ORDER_TO_SETTLEMENT"
        )
    )).scalar_one()
    l2 = (await db.execute(
        select(func.count()).select_from(SettlementMatch).where(
            SettlementMatch.match_level == "SETTLEMENT_TO_BANK"
        )
    )).scalar_one()
    assert l1 >= 10  # 12 行结算（幽灵行除外均匹配）
    assert l2 >= 2   # BK-001 与 BK-003 两笔银行流水参与分配

    # 引擎只建议，不自动确认
    confirmed = (await db.execute(
        select(func.count()).select_from(SettlementMatch).where(
            SettlementMatch.status == MatchStatus.CONFIRMED
        )
    )).scalar_one()
    assert confirmed == 0


async def test_reconcile_is_idempotent(db):
    base = await load_base_events(db)
    r1 = await run_reconciliation(db, base["batch"].id)
    await db.commit()
    r2 = await run_reconciliation(db, base["batch"].id)
    await db.commit()
    # 重跑不产生重复差异
    assert r1["discrepancies_total"] == r2["discrepancies_total"]
    assert r2["new_matches_level1"] == 0 and r2["new_matches_level2"] == 0


async def test_unknown_discrepancy_cannot_auto_close(db):
    base = await load_base_events(db)
    await run_reconciliation(db, base["batch"].id)
    await db.commit()

    unknown = (await db.execute(
        select(Discrepancy).where(Discrepancy.kind == DiscrepancyKind.UNKNOWN).limit(1)
    )).scalar_one()

    from app.services.workflow import close_discrepancy
    with pytest.raises(Exception, match="UNKNOWN"):
        await close_discrepancy(db, unknown.id, "finance.chen", "尝试直接关闭")
    await db.rollback()


async def test_merged_bank_deposit_allocation(db):
    """S8 合并入账：BK-001 一笔流水被分配给批次，金额为部分净额。"""
    base = await load_base_events(db)
    await run_reconciliation(db, base["batch"].id)
    await db.commit()

    match = (await db.execute(
        select(SettlementMatch)
        .join(BankTransaction, BankTransaction.id == SettlementMatch.bank_transaction_id)
        .where(
            SettlementMatch.match_level == "SETTLEMENT_TO_BANK",
            BankTransaction.bank_ref == "BK-001",
        )
    )).scalar_one()
    assert match.allocated_amount > 0
    assert match.currency == "USD"
