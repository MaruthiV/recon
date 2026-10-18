from __future__ import annotations

from .plan import Disposition, ReasonCode, ResolutionPlan
from .policy import AmbiguousPolicy, applicable
from .validation import CaseFacts, PaymentFact, Quote, _original_capture, quote


def _ref(p: PaymentFact) -> str:
    return f"payment:{p.id}:v{p.version}"


def ask_for_order(case_id: str, case_version: int) -> ResolutionPlan:
    # no order on the case, nothing to look up yet
    return ResolutionPlan(
        case_id=case_id,
        case_version=case_version,
        recommended_disposition=Disposition.REQUEST_INFORMATION,
        reason_code=ReasonCode.MISSING_EVIDENCE,
        explanation="We need the order number for this charge before we can look into it.",
        missing_evidence=["order id"],
    )


def rules_plan(facts: CaseFacts) -> ResolutionPlan:
    """rules baseline (B2): structured fields in, a plan out, no model and no view of the provider"""
    base = {"case_id": facts.case_id, "case_version": facts.case_version}
    try:
        terms = applicable(facts.refund_terms, facts.order_placed_at)
    except AmbiguousPolicy:
        terms = None
    policy_ids = [terms.policy.id] if terms else []

    def no_money(disp: Disposition, reason: ReasonCode, why: str, payment: PaymentFact | None = None, refs=()):
        return ResolutionPlan(
            **base,
            recommended_disposition=disp,
            payment_id=payment.id if payment else None,
            policy_version_ids=policy_ids,
            evidence_refs=list(refs),
            reason_code=reason,
            explanation=why,
        )

    def from_quote(q: Quote, payment: PaymentFact, reason: ReasonCode, refs: list[str], what: str) -> ResolutionPlan:
        if q.eligible and q.amount is not None:
            return ResolutionPlan(
                **base,
                recommended_disposition=Disposition.REFUND,
                payment_id=payment.id,
                amount_minor=q.amount.amount_minor,
                currency=q.amount.currency,
                policy_version_ids=policy_ids,
                evidence_refs=refs,
                reason_code=reason,
                explanation=f"Refunding {q.amount.amount_minor} {q.amount.currency.value} for {what}.",
            )
        if q.reasons and q.reasons[0].startswith("nothing left"):
            return no_money(
                Disposition.ALREADY_REFUNDED,
                ReasonCode.EXISTING_REFUND,
                f"The {what} was already refunded.",
                payment,
                refs,
            )
        return no_money(
            Disposition.DENY, ReasonCode.INELIGIBLE, f"Not refundable: {'; '.join(q.reasons)}.", payment, refs
        )

    if any(p.customer_id != facts.customer_id for p in facts.payments):
        return no_money(
            Disposition.ESCALATE,
            ReasonCode.CONFLICTING_EVIDENCE,
            "The order's payments belong to a different customer than the one asking.",
        )
    policy_refs = [f"policy:{pid}:policy_rules" for pid in policy_ids]
    succeeded = [p for p in facts.payments if p.status == "succeeded"]

    dups = [(p, orig) for p in succeeded if (orig := _original_capture(p, facts.payments)) is not None]
    if dups:
        dup, orig = dups[0]
        q = quote(facts, dup.id, ReasonCode.DUPLICATE_CAPTURE)
        return from_quote(
            q, dup, ReasonCode.DUPLICATE_CAPTURE, [_ref(orig), _ref(dup), *policy_refs], "the duplicate charge"
        )

    if facts.returns and succeeded:
        ret = next((r for r in facts.returns if r.received_at is not None), facts.returns[0])
        pay = succeeded[0]
        q = quote(facts, pay.id, ReasonCode.RETURNED_ITEMS, ret.id)
        refs = [_ref(pay), f"return:{ret.id}:v{ret.version}", *policy_refs]
        return from_quote(q, pay, ReasonCode.RETURNED_ITEMS, refs, "the returned item")

    if facts.subscription is not None and facts.subscription.cancelled_at is not None and succeeded:
        pay, sub = succeeded[0], facts.subscription
        q = quote(facts, pay.id, ReasonCode.PRORATED_CANCELLATION, sub.id)
        refs = [_ref(pay), f"subscription:{sub.id}:v{sub.version}", *policy_refs]
        return from_quote(q, pay, ReasonCode.PRORATED_CANCELLATION, refs, "the unused part of the subscription")

    refunded = [p for p in facts.payments if p.refunded_minor > 0]
    if refunded:
        return no_money(
            Disposition.ALREADY_REFUNDED,
            ReasonCode.EXISTING_REFUND,
            f"Payment {refunded[0].id} was already refunded {refunded[0].refunded_minor}.",
            refunded[0],
            [_ref(refunded[0])],
        )
    if len(succeeded) > 1:
        return no_money(
            Disposition.DENY,
            ReasonCode.INELIGIBLE,
            "The charges on this order are for different amounts, so neither is a duplicate.",
            succeeded[-1],
            [_ref(p) for p in succeeded],
        )
    return ResolutionPlan(
        **base,
        recommended_disposition=Disposition.REQUEST_INFORMATION,
        reason_code=ReasonCode.MISSING_EVIDENCE,
        explanation="We can't find a refund or a refundable charge on this order. Can you send the refund confirmation?",
        missing_evidence=["refund confirmation"],
    )
