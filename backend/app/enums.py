"""事件类型与状态枚举（以字符串存储，避免数据库 ENUM 迁移成本）。"""


class EventSource:
    CDC = "CDC"              # 商城订单库变更捕获
    WEBHOOK = "WEBHOOK"      # 支付渠道异步推送
    POLL = "POLL"            # 渠道轮询补拉
    MANUAL = "MANUAL"        # 人工补单 / 手工调账


class EntityType:
    ORDER = "ORDER"
    PAYMENT_ATTEMPT = "PAYMENT_ATTEMPT"
    AUTHORIZATION = "AUTHORIZATION"
    CAPTURE = "CAPTURE"
    REFUND = "REFUND"
    CHARGEBACK = "CHARGEBACK"
    SETTLEMENT = "SETTLEMENT"
    BANK_TXN = "BANK_TXN"
    MANUAL_ADJ = "MANUAL_ADJ"
    FX_RATE = "FX_RATE"


class EventType:
    # 订单
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_PAID = "ORDER_PAID"
    ORDER_STATUS_CHANGED = "ORDER_STATUS_CHANGED"
    # 支付生命周期
    PAYMENT_CREATED = "PAYMENT_CREATED"
    PAYMENT_AUTHORIZED = "PAYMENT_AUTHORIZED"
    PAYMENT_AUTH_EXPIRED = "PAYMENT_AUTH_EXPIRED"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    REFUND_REQUESTED = "REFUND_REQUESTED"
    REFUND_SUCCEEDED = "REFUND_SUCCEEDED"
    CHARGEBACK_OPENED = "CHARGEBACK_OPENED"
    CHARGEBACK_REVERSED = "CHARGEBACK_REVERSED"
    # 结算 / 银行
    SETTLEMENT_BATCH_ISSUED = "SETTLEMENT_BATCH_ISSUED"
    SETTLEMENT_BATCH_REVISED = "SETTLEMENT_BATCH_REVISED"
    BANK_DEPOSIT_RECEIVED = "BANK_DEPOSIT_RECEIVED"
    # 人工 / 汇率
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    FX_RATE_PUBLISHED = "FX_RATE_PUBLISHED"
    FX_RATE_CORRECTED = "FX_RATE_CORRECTED"


class PaymentState:
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    CHARGEBACK = "CHARGEBACK"
    CHARGEBACK_REVERSED = "CAPTURED"  # 拒付反转后回到捕获态（复用状态，事件轨迹保留区别）
    FAILED = "FAILED"


class BatchStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class MatchStatus:
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"


class DiscrepancyKind:
    AMOUNT = "AMOUNT"      # 金额差
    FEE = "FEE"            # 手续费差
    FX = "FX"              # 汇率差
    TIMING = "TIMING"      # 时间差
    UNKNOWN = "UNKNOWN"    # 未知差（禁止自动核销）


class DiscrepancyStatus:
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    RESOLVED = "RESOLVED"   # 已创建调账建议/已调账，待批次关闭
    CLOSED = "CLOSED"


class ActionType:
    CLAIM = "CLAIM"
    ATTACH = "ATTACH"
    ADJUST_SUGGEST = "ADJUST_SUGGEST"
    ADJUST_POST = "ADJUST_POST"
    CLOSE = "CLOSE"
    REOPEN = "REOPEN"


class ReportStatus:
    DRAFT = "DRAFT"
    CLOSED = "CLOSED"


class VersionStatus:
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"


# 账本科目（双分录记账）
class Account:
    REVENUE = "REVENUE"                       # 收入（贷）
    AR = "AR"                                 # 应收账款（订单侧）
    SETTLEMENT_RECEIVABLE = "SETTLEMENT_RECEIVABLE"  # 渠道结算应收
    CASH = "CASH"                             # 银行资金
    PAYMENT_FEE = "PAYMENT_FEE"               # 手续费
    REFUND_PAYABLE = "REFUND_PAYABLE"         # 退款
    CHARGEBACK = "CHARGEBACK"                 # 拒付
    FX_GAIN_LOSS = "FX_GAIN_LOSS"             # 汇率更正损益
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"   # 手工调账
    ROUNDING = "ROUNDING"                     # 舍入差
