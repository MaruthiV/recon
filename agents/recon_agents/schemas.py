from typing import Literal

from pydantic import BaseModel


class RefundObservation(BaseModel):
    refund_id: str
    amount_minor: int
    currency: str
    status: str
    recon_command_id: str | None = None


class PaymentObservation(BaseModel):
    payment_id: str
    ref: str
    provider_payment_id: str
    amount_minor: int
    currency: str
    status: str
    captured_at: str
    refunds_lookup: Literal["found", "none_found", "query_failed"]
    refunds: list[RefundObservation] = []


class ReturnObservation(BaseModel):
    return_id: str
    ref: str
    line_id: str
    quantity: int
    received: bool


class SubscriptionObservation(BaseModel):
    subscription_id: str
    ref: str
    period_start: str
    period_end: str
    cancelled_at: str | None = None


class PaymentFacts(BaseModel):
    case_id: str
    case_version: int
    customer_id: str
    order_id: str | None = None  # adk drops nulls on the way out, so nullable needs a default
    # no_order: the case names none. none_found: it names one this customer doesn't have
    order_lookup: Literal["found", "none_found", "query_failed", "no_order"]
    customer_request: str
    payments: list[PaymentObservation]
    duplicate_payment_ids: list[str]
    returns: list[ReturnObservation] = []
    subscription: SubscriptionObservation | None = None
    open_questions: list[str] = []


class PolicyCitation(BaseModel):
    ref: str
    policy_id: str
    version: int
    authority_class: str
    quote: str


class PolicyEvidence(BaseModel):
    citations: list[PolicyCitation]
    exclusions: list[str] = []
    contradictions: list[str] = []
