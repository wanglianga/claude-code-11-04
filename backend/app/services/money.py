"""金额工具：精度规则统一在此处理。"""
from decimal import ROUND_HALF_UP, Decimal


def quantize(amount: Decimal, decimal_places: int = 2) -> Decimal:
    q = Decimal(1).scaleb(-decimal_places)
    return amount.quantize(q, rounding=ROUND_HALF_UP)
