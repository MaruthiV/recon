from __future__ import annotations

from .plan import Disposition, ReasonCode, ResolutionPlan
from .policy import AmbiguousPolicy, applicable
from .validation import CaseFacts, PaymentFact, _original_capture, quote


def _ref(p: PaymentFact) -> str:
    return f"payment:{p.id}:v{p.version}"


def rules_plan(facts: CaseFacts) -> ResolutionPlan:
    """rules baseline (B2), duplicate captures only: no model, structured fields in, a plan out"""
    base = {"case_id": facts.case_id, "case_version": facts.case_version}
    dups = [
        (p, orig)
        for p in facts.payments
        if p.status == "succeeded" and (orig := _original_capture(p, facts.payments)) is not None
    ]
    if not dups:
        return ResolutionPlan(
            **base,
            recommended_disposition=Disposition.ESCALATE,
            reason_code=ReasonCode.MISSING_EVIDENCE,
            explanation="No second capture of the same amount on this order, so this isn't a duplicate charge.",
            missing_evidence=["a duplicate capture"],
        )
    dup, orig = dups[0]
    q = quote(facts, dup.id, ReasonCode.DUPLICATE_CAPTURE)
    try:
        terms = applicable(facts.refund_terms, facts.order_placed_at)
    except AmbiguousPolicy:
        terms = None
    policy_ids = [terms.policy.id] if terms else []
    # the structured rule row is the citation, there's no retrieval in the rules baseline
    refs = [_ref(orig), _ref(dup), *(f"policy:{pid}:policy_rules" for pid in policy_ids)]
    if q.eligible and q.amount is not None:
        amount = q.amount.amount_minor
        return ResolutionPlan(
            **base,
            recommended_disposition=Disposition.REFUND,
            payment_id=dup.id,
            amount_minor=amount,
            currency=q.amount.currency,
            policy_version_ids=policy_ids,
            evidence_refs=refs,
            reason_code=ReasonCode.DUPLICATE_CAPTURE,
            explanation=(
                f"Payment {dup.id} captured {amount} {q.amount.currency.value} a second time on the order "
                f"after {orig.id}. Refunding the duplicate."
            ),
        )
    if q.reasons and q.reasons[0].startswith("nothing left"):
        return ResolutionPlan(
            **base,
            recommended_disposition=Disposition.ALREADY_REFUNDED,
            payment_id=dup.id,
            policy_version_ids=policy_ids,
            evidence_refs=refs,
            reason_code=ReasonCode.EXISTING_REFUND,
            explanation=f"The duplicate payment {dup.id} has already been refunded.",
        )
    return ResolutionPlan(
        **base,
        recommended_disposition=Disposition.DENY,
        payment_id=dup.id,
        policy_version_ids=policy_ids,
        evidence_refs=refs,
        reason_code=ReasonCode.INELIGIBLE,
        explanation=f"Not refundable: {'; '.join(q.reasons)}.",
    )
