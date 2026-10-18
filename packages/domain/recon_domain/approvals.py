from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .plan import ResolutionPlan, material_hash
from .validation import Decision, Verdict


@dataclass(frozen=True, slots=True)
class ApprovalFact:
    plan_hash: str
    payment_id: str | None
    entitlement_ref: str | None
    amount_minor: int | None
    currency: str | None
    decision: str
    expires_at: datetime


def approval_gap(approval: ApprovalFact, plan: ResolutionPlan, verdict: Verdict, now: datetime) -> str | None:
    """why this approval doesn't cover this plan on today's facts, None when it does"""
    if approval.decision != "APPROVED":
        return "the approver said no"
    if now >= approval.expires_at:
        return "the approval expired"
    if approval.plan_hash != material_hash(plan, verdict.entitlement_ref):
        return "the plan changed after it was approved"
    approved = (approval.payment_id, approval.entitlement_ref, approval.amount_minor, approval.currency)
    current = (
        verdict.payment.id if verdict.payment else None,
        verdict.entitlement_ref,
        verdict.amount.amount_minor if verdict.amount else None,
        verdict.amount.currency.value if verdict.amount else None,
    )
    if approved != current:
        return f"approved {approved}, the plan now means {current}"
    return None


def executable(verdict: Verdict, plan: ResolutionPlan, approval: ApprovalFact | None, now: datetime) -> bool:
    if verdict.decision is Decision.AUTHORIZE:
        return True
    return (
        verdict.decision is Decision.NEEDS_APPROVAL
        and approval is not None
        and approval_gap(approval, plan, verdict, now) is None
    )
