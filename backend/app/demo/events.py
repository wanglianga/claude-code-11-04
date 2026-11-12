"""模拟事件数据集（2026-10 结算周期，渠道 STRIPE / 商户 M1）。

覆盖 8 类场景：
  S1 CDC 重放（同消息重投 + 轮询补拉同业务事实）
  S2 webhook 乱序（捕获先于授权到达；旧订单状态迟到试图回退）
  S3 支付后延迟入账（银行 11-15 才到，产生时间差）
  S4 部分退款（ORD-005 退 40/100）
  S5 拒付反转（ORD-006 拒付后撤销）
  S6 结算单修订（v1 关闭后 v2 补手续费）
  S7 汇率更正（USD/CNY 7.20 -> 7.25，只追加更正账）
  S8 多笔合并入账（BK-001 一笔流水合并 3 个订单结算款）
另含：金额差（ORD-007）、汇率差（ORD-009 EUR 订单渠道汇率不同）、
      未知差（幽灵结算行 + 银行流水残差）、关闭后补单。
"""
from datetime import datetime, timezone
from decimal import Decimal

from ..schemas import RawEventIn

CHANNEL = "STRIPE"
MERCHANT = "M1"
BOOK_CCY = "CNY"
RATE_USD_CNY = Decimal("7.20")
RATE_EUR_USD = Decimal("1.08")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _raw(key: str, source: str, occurred: str, topic: str, payload: dict) -> RawEventIn:
    payload = dict(payload)
    payload.setdefault("event_time", occurred)
    payload.setdefault("channel", CHANNEL)
    payload.setdefault("merchant_id", MERCHANT)
    payload.setdefault("book_currency", BOOK_CCY)
    payload.setdefault("decimal_places", 2)
    return RawEventIn(
        idempotency_key=key, source=source,
        event_occurred_at=_dt(occurred), topic=topic, payload=payload,
    )


def _money_payload(base: dict, amount: Decimal, fee: Decimal = Decimal("0"),
                   txn_ccy: str = "USD", settle_ccy: str = "USD") -> dict:
    """补齐三币种金额与原始金额。"""
    book_amount = amount * RATE_USD_CNY if settle_ccy == "USD" else amount
    base.update({
        "amount": str(amount),
        "raw_amount": str(amount),
        "fee_amount": str(fee),
        "book_amount": str(book_amount),
        "book_fee_amount": str(fee * RATE_USD_CNY),
        "txn_currency": txn_ccy,
        "settle_currency": settle_ccy,
    })
    return base


# ---------------------------------------------------------------- 汇率
def fx_events() -> list[RawEventIn]:
    return [
        _raw("FX-INIT-USDCNY", "MANUAL", "2026-10-01T00:00:00+00:00", "fx.rate", {
            "event_type": "FX_RATE_PUBLISHED", "entity_type": "FX_RATE",
            "entity_ref": "FX-USDCNY-1",
            "business_key": "FX_RATE_PUBLISHED:USDCNY:2026-10-01:v1",
            "from_currency": "USD", "to_currency": "CNY",
            "rate": str(RATE_USD_CNY), "rate_source": "CHANNEL_RATE",
            "effective_from": "2026-10-01T00:00:00+00:00", "event_version": 1,
            "txn_currency": "USD", "amount": "0",
        }),
        _raw("FX-INIT-EURUSD", "MANUAL", "2026-10-01T00:00:00+00:00", "fx.rate", {
            "event_type": "FX_RATE_PUBLISHED", "entity_type": "FX_RATE",
            "entity_ref": "FX-EURUSD-1",
            "business_key": "FX_RATE_PUBLISHED:EURUSD:2026-10-01:v1",
            "from_currency": "EUR", "to_currency": "USD",
            "rate": str(RATE_EUR_USD), "rate_source": "CHANNEL_RATE",
            "effective_from": "2026-10-01T00:00:00+00:00", "event_version": 1,
            "txn_currency": "EUR", "amount": "0",
        }),
    ]


# ---------------------------------------------------------------- S1/S2 订单与支付
def order_payment_events() -> list[RawEventIn]:
    ev: list[RawEventIn] = []

    # ---- S1: ORD-001 CDC 重放
    def cdc_order_001():
        return [
            _raw("CDC-LSN-1001", "CDC", "2026-10-01T09:00:00+00:00", "order.order", {
                "event_type": "ORDER_CREATED", "entity_type": "ORDER",
                "entity_ref": "ORD-001", "order_ref": "ORD-001",
                "business_key": "ORDER_CREATED:ORD-001:v1",
                "order_status": "CREATED", "amount": "100", "txn_currency": "USD",
            }),
            _raw("CDC-LSN-1002", "CDC", "2026-10-02T09:00:00+00:00", "order.order", {
                "event_type": "ORDER_PAID", "entity_type": "ORDER",
                "entity_ref": "ORD-001", "order_ref": "ORD-001",
                "business_key": "ORDER_PAID:ORD-001:v1",
                "order_status": "PAID", "amount": "100", "txn_currency": "USD",
            }),
        ]

    ev += cdc_order_001()
    ev += [
        _raw("WH-P-001-CREATED", "WEBHOOK", "2026-10-02T10:00:00+00:00", "payment", {
            "event_type": "PAYMENT_CREATED", "entity_type": "PAYMENT_ATTEMPT",
            "entity_ref": "P-001", "order_ref": "ORD-001", "channel_ref": "CH-P001",
            "business_key": "PAYMENT_CREATED:P-001:v1",
            "amount": "100", "txn_currency": "USD",
        }),
        _raw("WH-P-001-AUTH", "WEBHOOK", "2026-10-02T10:05:00+00:00", "payment", {
            "event_type": "PAYMENT_AUTHORIZED", "entity_type": "AUTHORIZATION",
            "entity_ref": "P-001", "order_ref": "ORD-001", "channel_ref": "CH-P001",
            "business_key": "PAYMENT_AUTHORIZED:P-001:v1",
            "amount": "100", "txn_currency": "USD",
        }),
        _raw("WH-P-001-CAP", "WEBHOOK", "2026-10-03T08:00:00+00:00", "payment",
             _money_payload({
                 "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                 "entity_ref": "P-001", "order_ref": "ORD-001", "channel_ref": "CH-P001",
                 "business_key": "PAYMENT_CAPTURED:P-001:v1",
             }, Decimal("100"), Decimal("3"))),
    ]
    # CDC 重放：同样的消息再次投递（同 source+idempotency_key）
    ev += cdc_order_001()
    # 轮询补拉：不同来源消息 ID，但同一 business_key -> 账本不得重复
    ev.append(_raw("POLL-CURSOR-7781:P-001", "POLL", "2026-10-03T09:00:00+00:00", "payment.poll",
                   _money_payload({
                       "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                       "entity_ref": "P-001", "order_ref": "ORD-001", "channel_ref": "CH-P001",
                       "business_key": "PAYMENT_CAPTURED:P-001:v1",
                   }, Decimal("100"), Decimal("3"))))

    # ---- S2: ORD-002 webhook 乱序（先喂捕获，再喂授权），订单旧状态迟到
    ev.append(_raw("CDC-ORD-002-CREATE", "CDC", "2026-10-03T09:00:00+00:00", "order.order", {
        "event_type": "ORDER_CREATED", "entity_type": "ORDER",
        "entity_ref": "ORD-002", "order_ref": "ORD-002",
        "business_key": "ORDER_CREATED:ORD-002:v1",
        "order_status": "CREATED", "amount": "120", "txn_currency": "USD",
    }))
    ev.append(_raw("WH-P-002-CREATE", "WEBHOOK", "2026-10-03T10:00:00+00:00", "payment", {
        "event_type": "PAYMENT_CREATED", "entity_type": "PAYMENT_ATTEMPT",
        "entity_ref": "P-002", "order_ref": "ORD-002", "channel_ref": "CH-P002",
        "business_key": "PAYMENT_CREATED:P-002:v1",
        "amount": "120", "txn_currency": "USD",
    }))
    # 捕获（发生时间 10-04）先到
    ev.append(_raw("WH-P-002-CAP", "WEBHOOK", "2026-10-04T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                       "entity_ref": "P-002", "order_ref": "ORD-002", "channel_ref": "CH-P002",
                       "business_key": "PAYMENT_CAPTURED:P-002:v1",
                   }, Decimal("120"), Decimal("3.6"))))
    # 授权（发生时间 10-03）晚到
    ev.append(_raw("WH-P-002-AUTH", "WEBHOOK", "2026-10-03T10:05:00+00:00", "payment", {
        "event_type": "PAYMENT_AUTHORIZED", "entity_type": "AUTHORIZATION",
        "entity_ref": "P-002", "order_ref": "ORD-002", "channel_ref": "CH-P002",
        "business_key": "PAYMENT_AUTHORIZED:P-002:v1",
        "amount": "120", "txn_currency": "USD",
    }))
    ev.append(_raw("CDC-ORD-002-PAID", "CDC", "2026-10-04T11:00:00+00:00", "order.order", {
        "event_type": "ORDER_PAID", "entity_type": "ORDER",
        "entity_ref": "ORD-002", "order_ref": "ORD-002",
        "business_key": "ORDER_PAID:ORD-002:v1",
        "order_status": "PAID", "amount": "120", "txn_currency": "USD",
    }))
    # 旧状态事件（10-02 的 PROCESSING）10-06 才重放到达：不得把 PAID 打回
    ev.append(_raw("CDC-LSN-0999-LATE", "CDC", "2026-10-02T15:00:00+00:00", "order.order", {
        "event_type": "ORDER_STATUS_CHANGED", "entity_type": "ORDER",
        "entity_ref": "ORD-002", "order_ref": "ORD-002",
        "business_key": "ORDER_STATUS_CHANGED:ORD-002:PROCESSING:v1",
        "order_status": "PROCESSING", "amount": "120", "txn_currency": "USD",
    }))

    # ---- 常规订单 ORD-003 / ORD-004（S3 延迟入账）
    for ref, pref, cref, amt, fee, day in [
        ("ORD-003", "P-003", "CH-P003", "60", "1.8", "08"),
        ("ORD-004", "P-004", "CH-P004", "50", "1.5", "10"),
    ]:
        ev.append(_raw(f"CDC-{ref}", "CDC", f"2026-10-{day}T09:00:00+00:00", "order.order", {
            "event_type": "ORDER_PAID", "entity_type": "ORDER",
            "entity_ref": ref, "order_ref": ref,
            "business_key": f"ORDER_PAID:{ref}:v1",
            "order_status": "PAID", "amount": amt, "txn_currency": "USD",
        }))
        ev.append(_raw(f"WH-{pref}-CAP", "WEBHOOK", f"2026-10-{day}T10:00:00+00:00", "payment",
                       _money_payload({
                           "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                           "entity_ref": pref, "order_ref": ref, "channel_ref": cref,
                           "business_key": f"PAYMENT_CAPTURED:{pref}:v1",
                       }, Decimal(amt), Decimal(fee))))

    # ---- S4: ORD-005 部分退款
    ev.append(_raw("CDC-ORD-005", "CDC", "2026-10-12T09:00:00+00:00", "order.order", {
        "event_type": "ORDER_PAID", "entity_type": "ORDER",
        "entity_ref": "ORD-005", "order_ref": "ORD-005",
        "business_key": "ORDER_PAID:ORD-005:v1",
        "order_status": "PAID", "amount": "100", "txn_currency": "USD",
    }))
    ev.append(_raw("WH-P-005-CAP", "WEBHOOK", "2026-10-12T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                       "entity_ref": "P-005", "order_ref": "ORD-005", "channel_ref": "CH-P005",
                       "business_key": "PAYMENT_CAPTURED:P-005:v1",
                   }, Decimal("100"), Decimal("3"))))
    ev.append(_raw("WH-P-005-REF-REQ", "WEBHOOK", "2026-10-19T10:00:00+00:00", "payment", {
        "event_type": "REFUND_REQUESTED", "entity_type": "REFUND",
        "entity_ref": "R-005", "order_ref": "ORD-005", "channel_ref": "CH-P005",
        "business_key": "REFUND_REQUESTED:R-005:v1",
        "amount": "40", "txn_currency": "USD",
    }))
    ev.append(_raw("WH-P-005-REF-OK", "WEBHOOK", "2026-10-20T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "REFUND_SUCCEEDED", "entity_type": "REFUND",
                       "entity_ref": "R-005", "order_ref": "ORD-005", "channel_ref": "CH-P005",
                       "business_key": "REFUND_SUCCEEDED:R-005:v1",
                   }, Decimal("40"), Decimal("1.2"))))  # 等比例退还手续费

    # ---- S5: ORD-006 拒付后反转
    ev.append(_raw("CDC-ORD-006", "CDC", "2026-10-14T09:00:00+00:00", "order.order", {
        "event_type": "ORDER_PAID", "entity_type": "ORDER",
        "entity_ref": "ORD-006", "order_ref": "ORD-006",
        "business_key": "ORDER_PAID:ORD-006:v1",
        "order_status": "PAID", "amount": "80", "txn_currency": "USD",
    }))
    ev.append(_raw("WH-P-006-CAP", "WEBHOOK", "2026-10-14T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                       "entity_ref": "P-006", "order_ref": "ORD-006", "channel_ref": "CH-P006",
                       "business_key": "PAYMENT_CAPTURED:P-006:v1",
                   }, Decimal("80"), Decimal("2.4"))))
    ev.append(_raw("WH-P-006-CB", "WEBHOOK", "2026-10-18T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "CHARGEBACK_OPENED", "entity_type": "CHARGEBACK",
                       "entity_ref": "CB-006", "order_ref": "ORD-006", "channel_ref": "CH-P006",
                       "business_key": "CHARGEBACK_OPENED:CB-006:v1",
                   }, Decimal("80"))))
    ev.append(_raw("WH-P-006-CB-REV", "WEBHOOK", "2026-10-25T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "CHARGEBACK_REVERSED", "entity_type": "CHARGEBACK",
                       "entity_ref": "CB-006", "order_ref": "ORD-006", "channel_ref": "CH-P006",
                       "business_key": "CHARGEBACK_REVERSED:CB-006:v1",
                   }, Decimal("80"))))

    # ---- 金额差：ORD-007 账面 90，结算行只给 88
    ev.append(_raw("WH-P-007-CAP", "WEBHOOK", "2026-10-15T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                       "entity_ref": "P-007", "order_ref": "ORD-007", "channel_ref": "CH-P007",
                       "business_key": "PAYMENT_CAPTURED:P-007:v1",
                   }, Decimal("90"), Decimal("2.7"))))

    # ---- 干净订单 ORD-008
    ev.append(_raw("WH-P-008-CAP", "WEBHOOK", "2026-10-16T10:00:00+00:00", "payment",
                   _money_payload({
                       "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
                       "entity_ref": "P-008", "order_ref": "ORD-008", "channel_ref": "CH-P008",
                       "business_key": "PAYMENT_CAPTURED:P-008:v1",
                   }, Decimal("70"), Decimal("2.1"))))

    # ---- 汇率差：ORD-009 EUR 交易，内部汇率 1.08，渠道结算用 1.09
    ev.append(_raw("WH-P-009-CAP", "WEBHOOK", "2026-10-17T10:00:00+00:00", "payment", {
        "event_type": "PAYMENT_CAPTURED", "entity_type": "CAPTURE",
        "entity_ref": "P-009", "order_ref": "ORD-009", "channel_ref": "CH-P009",
        "business_key": "PAYMENT_CAPTURED:P-009:v1",
        "amount": "100", "raw_amount": "100",
        "fee_amount": "3", "book_amount": str(Decimal("108") * RATE_USD_CNY),
        "book_fee_amount": str(Decimal("3") * RATE_USD_CNY),
        "txn_currency": "EUR", "settle_currency": "USD", "book_currency": "CNY",
        "decimal_places": 2,
    }))
    return ev


# ---------------------------------------------------------------- S8 银行流水（合并入账）
def bank_events() -> list[RawEventIn]:
    def bank(key, ref, amount, booked, memo):
        return _raw(key, "WEBHOOK", booked, "bank.deposit", {
            "event_type": "BANK_DEPOSIT_RECEIVED", "entity_type": "BANK_TXN",
            "entity_ref": ref, "business_key": f"BANK_DEPOSIT_RECEIVED:{ref}:v1",
            "bank_ref": ref, "currency": "USD",
            "amount": str(amount), "book_amount": str(amount * RATE_USD_CNY),
            "booked_at": booked, "memo": memo,
            "txn_currency": "USD",
        })

    return [
        # 合并 ORD-001/002/003 结算净额 97 + 116.4 + 58.2 = 271.6
        bank("BK-001-EVT", "BK-001", Decimal("271.60"),
             "2026-10-20T02:00:00+00:00", "STRIPE 合并入账 ORD-001/002/003"),
        # 含全部剩余订单的合并入账，故意多 10（未解释残差 -> 未知差）
        bank("BK-003-EVT", "BK-003", Decimal("400.00"),
             "2026-10-22T02:00:00+00:00", "STRIPE 合并入账（含未解释残差 10）"),
        # S3: ORD-004 延迟到 11-15 才入账
        bank("BK-002-EVT", "BK-002", Decimal("48.50"),
             "2026-11-15T02:00:00+00:00", "STRIPE 延迟入账 ORD-004"),
    ]


# ---------------------------------------------------------------- 结算单
def _line(line_ref, line_type, gross, fee, net, order_ref=None, channel_ref=None):
    return {
        "line_ref": line_ref, "line_type": line_type,
        "gross_amount": str(gross), "fee_amount": str(fee),
        "net_amount": str(net), "currency": "USD",
        "order_ref": order_ref, "channel_ref": channel_ref,
    }


def settlement_v1_event() -> RawEventIn:
    # gross 737 / fee 26.9 / net 710.1
    lines = [
        _line("L-001", "CAPTURE", "100", "3", "97", "ORD-001", "CH-P001"),
        _line("L-002", "CAPTURE", "120", "3.6", "116.4", "ORD-002", "CH-P002"),
        _line("L-003", "CAPTURE", "60", "1.8", "58.2", "ORD-003", "CH-P003"),
        _line("L-004", "CAPTURE", "50", "1.5", "48.5", "ORD-004", "CH-P004"),
        _line("L-005", "CAPTURE", "100", "3", "97", "ORD-005", "CH-P005"),
        _line("L-005R", "REFUND", "-40", "-1.2", "-38.8", "ORD-005", "CH-P005"),
        _line("L-006", "CAPTURE", "80", "2.4", "77.6", "ORD-006", "CH-P006"),
        _line("L-006C", "CHARGEBACK", "-80", "0", "-80", "ORD-006", "CH-P006"),
        _line("L-006CR", "CHARGEBACK_REVERSAL", "80", "0", "80", "ORD-006", "CH-P006"),
        # 金额差：渠道少结 2
        _line("L-007", "CAPTURE", "88", "2.7", "85.3", "ORD-007", "CH-P007"),
        _line("L-008", "CAPTURE", "70", "2.1", "67.9", "ORD-008", "CH-P008"),
        # 汇率差：EUR100 渠道按 1.09 折 USD 109（内部 1.08 -> 108）
        _line("L-009", "CAPTURE", "109", "3", "106", "ORD-009", "CH-P009"),
        # 幽灵行：无订单事实 -> 未知差
        _line("L-UNKNOWN", "FEE", "0", "5", "-5"),
    ]
    return _raw("SB-202610-V1", "WEBHOOK", "2026-11-01T08:00:00+00:00", "settlement.batch", {
        "event_type": "SETTLEMENT_BATCH_ISSUED", "entity_type": "SETTLEMENT",
        "entity_ref": "SB-202610",
        "business_key": "SETTLEMENT_BATCH_ISSUED:SB-202610:v1",
        "batch_ref": "SB-202610", "currency": "USD",
        "period_start": "2026-10-01T00:00:00+00:00",
        "period_end": "2026-10-31T23:59:59+00:00",
        "gross_amount": "737", "fee_amount": "26.9", "net_amount": "710.1",
        "event_version": 1, "lines": lines,
        "txn_currency": "USD", "amount": "710.1",
    })


def settlement_v2_event() -> RawEventIn:
    """S6 修订：ORD-001 手续费后补 3 -> 3.5（net 97 -> 96.5），净额 709.6。"""
    lines = [
        _line("L-001", "CAPTURE", "100", "3.5", "96.5", "ORD-001", "CH-P001"),
        _line("L-002", "CAPTURE", "120", "3.6", "116.4", "ORD-002", "CH-P002"),
        _line("L-003", "CAPTURE", "60", "1.8", "58.2", "ORD-003", "CH-P003"),
        _line("L-004", "CAPTURE", "50", "1.5", "48.5", "ORD-004", "CH-P004"),
        _line("L-005", "CAPTURE", "100", "3", "97", "ORD-005", "CH-P005"),
        _line("L-005R", "REFUND", "-40", "-1.2", "-38.8", "ORD-005", "CH-P005"),
        _line("L-006", "CAPTURE", "80", "2.4", "77.6", "ORD-006", "CH-P006"),
        _line("L-006C", "CHARGEBACK", "-80", "0", "-80", "ORD-006", "CH-P006"),
        _line("L-006CR", "CHARGEBACK_REVERSAL", "80", "0", "80", "ORD-006", "CH-P006"),
        _line("L-007", "CAPTURE", "88", "2.7", "85.3", "ORD-007", "CH-P007"),
        _line("L-008", "CAPTURE", "70", "2.1", "67.9", "ORD-008", "CH-P008"),
        _line("L-009", "CAPTURE", "109", "3", "106", "ORD-009", "CH-P009"),
        _line("L-UNKNOWN", "FEE", "0", "5", "-5"),
    ]
    return _raw("SB-202610-V2", "WEBHOOK", "2026-11-08T08:00:00+00:00", "settlement.batch", {
        "event_type": "SETTLEMENT_BATCH_REVISED", "entity_type": "SETTLEMENT",
        "entity_ref": "SB-202610",
        "business_key": "SETTLEMENT_BATCH_REVISED:SB-202610:v2",
        "batch_ref": "SB-202610", "currency": "USD",
        "period_start": "2026-10-01T00:00:00+00:00",
        "period_end": "2026-10-31T23:59:59+00:00",
        "gross_amount": "737", "fee_amount": "27.4", "net_amount": "709.6",
        "event_version": 2, "lines": lines,
        "txn_currency": "USD", "amount": "709.6",
    })


# ---------------------------------------------------------------- S7 汇率更正
def fx_correction_event() -> RawEventIn:
    return _raw("FX-CORRECT-USDCNY", "MANUAL", "2026-11-10T10:00:00+00:00", "fx.rate", {
        "event_type": "FX_RATE_CORRECTED", "entity_type": "FX_RATE",
        "entity_ref": "FX-USDCNY-2",
        "business_key": "FX_RATE_CORRECTED:USDCNY:2026-10-01:v2",
        "from_currency": "USD", "to_currency": "CNY",
        "rate": "7.25", "rate_source": "CENTRAL_BANK_CORRECTION",
        "effective_from": "2026-10-01T00:00:00+00:00", "event_version": 2,
        "txn_currency": "USD", "amount": "0",
    })


# ---------------------------------------------------------------- 关闭后补单（10 月业务，11 月才补录）
def late_october_refund_event() -> RawEventIn:
    return _raw("MANUAL-LATE-REFUND-008", "MANUAL", "2026-11-10T14:00:00+00:00", "manual.refund",
                _money_payload({
                    "event_type": "REFUND_SUCCEEDED", "entity_type": "REFUND",
                    "entity_ref": "R-008-LATE", "order_ref": "ORD-008",
                    "channel_ref": "CH-P008",
                    "business_key": "REFUND_SUCCEEDED:R-008-LATE:v1",
                    # 业务发生在 10-28（已关闭周期），11-10 才人工补录
                    "event_time": "2026-10-28T10:00:00+00:00",
                }, Decimal("10"), Decimal("0.3")))
