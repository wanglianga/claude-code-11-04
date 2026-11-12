"""历史汇率服务：按事件发生时点取适用汇率，更正只产生新版本，不覆盖旧事实。"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FxRate


class FxError(Exception):
    pass


async def get_rate(
    db: AsyncSession, from_ccy: str, to_ccy: str, at: datetime
) -> tuple[Decimal, str, datetime, int]:
    """返回 (rate, rate_source, effective_from, version)。仅取事件过账时点已生效的最新版本。"""
    if from_ccy == to_ccy:
        return Decimal("1"), "IDENTITY", at, 1

    stmt = (
        select(FxRate)
        .where(
            FxRate.from_currency == from_ccy,
            FxRate.to_currency == to_ccy,
            FxRate.effective_from <= at,
            (FxRate.effective_to.is_(None)) | (FxRate.effective_to > at),
        )
        .order_by(FxRate.version.desc(), FxRate.effective_from.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row.rate, row.rate_source, row.effective_from, row.version

    # 反向汇率兜底
    rev = (
        select(FxRate)
        .where(
            FxRate.from_currency == to_ccy,
            FxRate.to_currency == from_ccy,
            FxRate.effective_from <= at,
            (FxRate.effective_to.is_(None)) | (FxRate.effective_to > at),
        )
        .order_by(FxRate.version.desc())
        .limit(1)
    )
    row = (await db.execute(rev)).scalar_one_or_none()
    if row is not None:
        return Decimal("1") / row.rate, f"INVERSE({row.rate_source})", row.effective_from, row.version

    raise FxError(f"no FX rate {from_ccy}->{to_ccy} effective at {at.isoformat()}")


async def resolve_book_amount(
    db: AsyncSession,
    txn_amount: Decimal,
    txn_ccy: str,
    settle_ccy: str,
    book_ccy: str,
    at: datetime,
) -> dict:
    """三币种换算：交易币 -> 结算币 -> 记账币，全程保留汇率溯源。"""
    r1, src1, eff1, v1 = await get_rate(db, txn_ccy, settle_ccy, at)
    r2, src2, eff2, v2 = await get_rate(db, settle_ccy, book_ccy, at)
    settle_amount = txn_amount * r1
    book_amount = settle_amount * r2
    return {
        "txn_amount": txn_amount,
        "txn_currency": txn_ccy,
        "settle_amount": settle_amount,
        "settle_currency": settle_ccy,
        "book_amount": book_amount,
        "book_currency": book_ccy,
        "txn_to_settle_rate": r1,
        "settle_to_book_rate": r2,
        "rate_source": f"{src1} | {src2}",
        "rate_effective_at": eff2,
    }
