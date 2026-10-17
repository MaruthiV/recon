from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .money import Currency


class Disposition(StrEnum):
    REFUND = "REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    ALREADY_REFUNDED = "ALREADY_REFUNDED"
    DENY = "DENY"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    ESCALATE = "ESCALATE"


MONEY_DISPOSITIONS = frozenset({Disposition.REFUND, Disposition.PARTIAL_REFUND})


class ReasonCode(StrEnum):
    DUPLICATE_CAPTURE = "DUPLICATE_CAPTURE"
    RETURNED_ITEMS = "RETURNED_ITEMS"
    PRORATED_CANCELLATION = "PRORATED_CANCELLATION"
    EXISTING_REFUND = "EXISTING_REFUND"
    INELIGIBLE = "INELIGIBLE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class ResolutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    case_version: int = Field(strict=True, ge=0)
    recommended_disposition: Disposition
    payment_id: str | None = None
    claim_id: str | None = None
    amount_minor: int | None = Field(default=None, strict=True, ge=0)
    currency: Currency | None = None
    policy_version_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reason_code: ReasonCode
    explanation: str = ""
    missing_evidence: list[str] = Field(default_factory=list)

    @field_validator("currency", mode="before")
    @classmethod
    def _lower_currency(cls, v: object) -> object:
        return v.lower() if isinstance(v, str) else v


def canonical_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def material_hash(plan: ResolutionPlan, entitlement_ref: str | None) -> str:
    # A5: wording changes don't move the hash, anything financial does
    return canonical_hash(
        {
            "case_id": plan.case_id,
            "case_version": plan.case_version,
            "disposition": plan.recommended_disposition.value,
            "payment_id": plan.payment_id,
            "entitlement_ref": entitlement_ref,
            "amount_minor": plan.amount_minor,
            "currency": plan.currency.value if plan.currency else None,
            "reason_code": plan.reason_code.value,
            "policy_version_ids": sorted(plan.policy_version_ids),
            "evidence_refs": sorted(plan.evidence_refs),
        }
    )
