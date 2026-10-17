from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .balances import ClaimBalance, PaymentBalance, remaining_minor
from .entitlements import duplicate_capture_ref
from .money import Currency, Money
from .plan import MONEY_DISPOSITIONS, ReasonCode, ResolutionPlan
from .policy import AmbiguousPolicy, OperationalLimits, RefundTerms, applicable


class Decision(StrEnum):
    AUTHORIZE = "AUTHORIZE"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    NO_MONEY = "NO_MONEY"
    REJECT = "REJECT"
    STALE = "STALE"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True, slots=True)
class PaymentFact:
    id: str
    order_id: str
    customer_id: str
    provider_payment_id: str
    amount: Money
    status: str
    captured_at: datetime
    version: int
    refunded_minor: int
    reserved_minor: int


@dataclass(frozen=True, slots=True)
class ClaimFact:
    id: str
    entitlement_ref: str
    permitted_minor: int
    reserved_minor: int
    consumed_minor: int


@dataclass(frozen=True, slots=True)
class CaseFacts:
    tenant_id: str
    case_id: str
    case_version: int
    customer_id: str
    order_id: str
    order_placed_at: datetime
    order_currency: Currency
    payments: tuple[PaymentFact, ...]
    claims: dict[str, ClaimFact]
    refund_terms: tuple[RefundTerms, ...]
    limits: tuple[OperationalLimits, ...]
    now: datetime


@dataclass(frozen=True, slots=True)
class Verdict:
    decision: Decision
    reasons: tuple[str, ...] = ()
    entitlement_ref: str | None = None
    payment: PaymentFact | None = None
    amount: Money | None = None
    permitted_minor: int | None = None
    policy_ids: tuple[str, ...] = field(default=())

    @property
    def can_execute(self) -> bool:
        return self.decision is Decision.AUTHORIZE


def _reject(*reasons: str) -> Verdict:
    return Verdict(Decision.REJECT, reasons)


def _original_capture(payment: PaymentFact, payments: tuple[PaymentFact, ...]) -> PaymentFact | None:
    # earliest other succeeded capture on the same order for the same amount
    twins = [
        p
        for p in payments
        if p.id != payment.id
        and p.status == "succeeded"
        and p.order_id == payment.order_id
        and p.amount == payment.amount
        and (p.captured_at, p.id) < (payment.captured_at, payment.id)
    ]
    return min(twins, key=lambda p: (p.captured_at, p.id)) if twins else None


def _check_evidence(
    plan: ResolutionPlan, facts: CaseFacts, payment: PaymentFact, policy_ids: set[str]
) -> Verdict | None:
    by_id = {p.id: p for p in facts.payments}
    cites_target = False
    for ref in plan.evidence_refs:
        kind, _, rest = ref.partition(":")
        if kind == "payment":
            pid, _, ver = rest.partition(":")
            found = by_id.get(pid)
            if found is None:
                return _reject(f"evidence {ref} not on this case's order")
            if ver != f"v{found.version}":
                return Verdict(Decision.STALE, (f"evidence {ref} superseded by v{found.version}",))
            cites_target |= pid == payment.id
        elif kind == "policy":
            pol, _, _section = rest.partition(":")
            if pol not in policy_ids:
                return _reject(f"evidence {ref} isn't an applicable policy version")
        else:
            return _reject(f"unknown evidence kind in {ref}")
    if not cites_target:
        return _reject("plan doesn't cite the payment it refunds")
    return None


def validate(plan: ResolutionPlan, facts: CaseFacts) -> Verdict:
    if plan.case_id != facts.case_id:
        return _reject("plan is for a different case")
    if plan.case_version != facts.case_version:
        return Verdict(Decision.STALE, (f"plan case v{plan.case_version}, current v{facts.case_version}",))
    if plan.recommended_disposition not in MONEY_DISPOSITIONS:
        return Verdict(Decision.NO_MONEY, (f"{plan.recommended_disposition} moves no money",))

    payment = next((p for p in facts.payments if p.id == plan.payment_id), None)
    if payment is None:
        return _reject("payment isn't on this case's order")
    if payment.customer_id != facts.customer_id:
        return _reject("payment belongs to a different customer")
    if plan.amount_minor is None or plan.currency is None:
        return _reject("money plan without amount or currency")
    if plan.currency != payment.amount.currency:
        return _reject(f"currency {plan.currency} doesn't match payment {payment.amount.currency}")
    amount = Money(plan.amount_minor, plan.currency)
    if not amount.is_positive():
        return _reject("amount must be positive")
    if payment.status != "succeeded":
        return _reject(f"payment status is {payment.status}")

    if plan.reason_code is not ReasonCode.DUPLICATE_CAPTURE:
        return Verdict(Decision.ESCALATE, (f"{plan.reason_code} isn't automated yet",))
    if _original_capture(payment, facts.payments) is None:
        return _reject("no earlier matching capture, charges look distinct")
    ref = duplicate_capture_ref(facts.order_id, payment.id)

    try:
        terms = applicable(facts.refund_terms, facts.order_placed_at)
        limits = applicable(facts.limits, facts.now)
    except AmbiguousPolicy as e:
        return Verdict(Decision.ESCALATE, (str(e),))
    if terms is None or limits is None:
        return Verdict(Decision.ESCALATE, ("no applicable policy version",))
    policy_ids = {terms.policy.id, limits.policy.id}
    if terms.policy.id not in plan.policy_version_ids:
        return _reject(f"plan doesn't cite applicable refund terms {terms.policy.id}")
    if not terms.duplicate_refund_allowed:
        return _reject("policy doesn't allow duplicate-capture refunds")
    if facts.now - payment.captured_at > timedelta(days=terms.refund_window_days):
        return _reject(f"outside the {terms.refund_window_days}-day refund window")

    bad = _check_evidence(plan, facts, payment, policy_ids)
    if bad is not None:
        return bad

    claim = facts.claims.get(ref)
    if claim is not None and plan.claim_id is not None and plan.claim_id != claim.id:
        return _reject(f"plan claim {plan.claim_id} doesn't match {claim.id}")
    permitted = claim.permitted_minor if claim else payment.amount.amount_minor
    left = remaining_minor(
        ClaimBalance(permitted, claim.reserved_minor if claim else 0, claim.consumed_minor if claim else 0),
        PaymentBalance(payment.amount.amount_minor, payment.refunded_minor, payment.reserved_minor),
    )
    if amount.amount_minor > left:
        return _reject(f"amount {amount.amount_minor} exceeds remaining {left}")

    decided = Verdict(
        Decision.AUTHORIZE,
        (),
        ref,
        payment,
        amount,
        permitted,
        tuple(sorted(policy_ids)),
    )
    if amount.amount_minor > limits.auto_approve_limit_minor:
        return Verdict(
            Decision.NEEDS_APPROVAL,
            (f"over auto-approve limit {limits.auto_approve_limit_minor}",),
            ref,
            payment,
            amount,
            permitted,
            decided.policy_ids,
        )
    return decided
