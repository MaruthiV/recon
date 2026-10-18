from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class Status(StrEnum):
    FOUND = "found"
    NONE_FOUND = "none_found"
    QUERY_FAILED = "query_failed"


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool


class Result[T](BaseModel):
    status: Status
    source_ids: list[str] = []
    source_version: str | None = None
    observed_at: datetime
    correlation_id: str
    data: T | None = None
    error: ToolError | None = None


class MessageView(BaseModel):
    author: str
    body: str
    created_at: datetime


class CaseView(BaseModel):
    case_id: str
    case_version: int
    state: str
    subject: str
    customer_id: str
    order_id: str | None
    created_at: datetime
    messages: list[MessageView]


class LineView(BaseModel):
    line_id: str
    sku: str
    description: str
    kind: str
    quantity: int
    unit_minor: int


class ReturnView(BaseModel):
    return_id: str
    ref: str
    line_id: str
    quantity: int
    requested_at: datetime
    received_at: datetime | None


class SubscriptionView(BaseModel):
    subscription_id: str
    ref: str
    plan_name: str
    period_start: datetime
    period_end: datetime
    price_minor: int
    cancelled_at: datetime | None


class OrderView(BaseModel):
    order_id: str
    version: int
    customer_id: str
    total_minor: int
    currency: str
    placed_at: datetime
    lines: list[LineView] = []
    returns: list[ReturnView] = []
    subscription: SubscriptionView | None = None


class PaymentView(BaseModel):
    payment_id: str
    ref: str
    version: int
    order_id: str
    provider_payment_id: str
    amount_minor: int
    currency: str
    status: str
    captured_at: datetime
    refunded_minor: int
    reserved_minor: int


class PaymentList(BaseModel):
    order_id: str
    payments: list[PaymentView]


class RefundView(BaseModel):
    refund_id: str
    payment_id: str
    provider_payment_id: str
    amount_minor: int
    currency: str
    status: str
    recon_command_id: str | None
    failure_reason: str | None


class RefundList(BaseModel):
    payment_id: str
    refunds: list[RefundView]


class PolicySection(BaseModel):
    ref: str
    policy_id: str
    version: int
    authority_class: str
    section_id: str
    heading: str
    text: str
    applies_to_case: bool


class PolicySearch(BaseModel):
    query: str
    applicable_policy_ids: list[str]
    hits: list[PolicySection]


class QuoteView(BaseModel):
    payment_id: str
    eligible: bool
    reasons: list[str]
    amount_minor: int | None
    currency: str | None
    entitlement_ref: str | None
    claim_id: str | None
    terms_policy_id: str | None
