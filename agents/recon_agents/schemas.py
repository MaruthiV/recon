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


class PaymentFacts(BaseModel):
    case_id: str
    case_version: int
    customer_id: str
    order_id: str | None = None  # adk drops nulls on the way out, so nullable needs a default
    customer_request: str
    payments: list[PaymentObservation]
    duplicate_payment_ids: list[str]
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
