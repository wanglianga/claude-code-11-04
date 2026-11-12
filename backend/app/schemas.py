"""API 入参/出参契约。"""
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 摄入
class RawEventIn(BaseModel):
    idempotency_key: str = Field(..., max_length=200, description="来源系统消息唯一 ID")
    source: str
    event_occurred_at: datetime
    topic: str
    payload: dict[str, Any]


class IngestResult(BaseModel):
    raw_event_id: int
    duplicate: bool
    business_event_id: int | None = None
    ledger_entry_ids: list[int] = []
    message: str


class BatchIngestIn(BaseModel):
    events: list[RawEventIn]


class BatchIngestResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: list[IngestResult]


# ---------------------------------------------------------------- 汇率
class FxRateIn(BaseModel):
    from_currency: str
    to_currency: str
    rate: Decimal
    rate_source: str
    effective_from: datetime
    correction: bool = False


# ---------------------------------------------------------------- 对账/处置
class ClaimIn(BaseModel):
    operator: str
    comment: str | None = None


class AttachIn(BaseModel):
    operator: str
    attachment: dict[str, Any]
    comment: str | None = None


class AdjustSuggestIn(BaseModel):
    operator: str
    adjust_amount: Decimal = Field(..., description="记账币调账金额，带方向")
    account: str
    comment: str


class AdjustPostIn(BaseModel):
    operator: str
    comment: str | None = None


class CloseDiscrepancyIn(BaseModel):
    operator: str
    comment: str | None = None


class CloseBatchIn(BaseModel):
    operator: str


class MatchConfirmIn(BaseModel):
    operator: str


# ---------------------------------------------------------------- 出参
class BusinessEventOut(BaseModel):
    id: int
    business_key: str
    entity_type: str
    event_type: str
    event_version: int
    entity_ref: str
    channel_ref: str | None
    channel: str | None
    merchant_id: str | None
    order_ref: str | None
    source: str
    event_time: datetime
    payload: dict[str, Any]
    supersedes_event_id: int | None

    model_config = {"from_attributes": True}


class LedgerEntryOut(BaseModel):
    id: int
    business_event_id: int
    account: str
    direction: str
    txn_amount: Decimal
    txn_currency: str
    settle_amount: Decimal
    settle_currency: str
    book_amount: Decimal
    book_currency: str
    txn_to_settle_rate: Decimal | None
    settle_to_book_rate: Decimal | None
    rate_source: str | None
    rate_effective_at: datetime | None
    decimal_places: int
    raw_amount: Decimal
    order_ref: str | None

    model_config = {"from_attributes": True}


class DiscrepancyOut(BaseModel):
    id: int
    batch_id: int
    kind: str
    status: str
    title: str
    order_ref: str | None
    channel_ref: str | None
    expected_amount: Decimal
    actual_amount: Decimal
    diff_amount: Decimal
    currency: str
    detail: dict[str, Any]
    assignee: str | None
    adjustment_event_id: int | None

    model_config = {"from_attributes": True}


class BatchOut(BaseModel):
    id: int
    batch_ref: str
    channel: str
    merchant_id: str
    period_start: datetime
    period_end: datetime
    currency: str
    version: int
    status: str
    gross_amount: Decimal
    fee_amount: Decimal
    net_amount: Decimal
    supersedes_batch_id: int | None
    closed_at: datetime | None

    model_config = {"from_attributes": True}
