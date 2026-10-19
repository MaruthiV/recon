from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .balances import ClaimBalance, PaymentBalance, remaining_minor
from .entitlements import duplicate_capture_ref, prorate_ref, return_ref
from .money import Currency, Money
from .plan import MONEY_DISPOSITIONS, Disposition, ReasonCode, ResolutionPlan
from .policy import AmbiguousPolicy, OperationalLimits, RefundTerms, applicable
from .proration import Rounding, prorate


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
class LineFact:
    id: str
    order_id: str
    sku: str
    kind: str  # item | shipping
    quantity: int
    unit: Money


@dataclass(frozen=True, slots=True)
class ReturnFact:
    id: str
    order_id: str
    line_id: str
    quantity: int
    requested_at: datetime
    received_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class SubscriptionFact:
    id: str
    order_id: str
    period_start: datetime
    period_end: datetime
    price: Money
    cancelled_at: datetime | None
    version: int


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
    lines: tuple[LineFact, ...] = ()
    returns: tuple[ReturnFact, ...] = ()
    subscription: SubscriptionFact | None = None
    # payment id -> statuses of the refunds a fresh provider read saw, None when nobody looked
    provider_refunds: Mapping[str, tuple[str, ...]] | None = None


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


@dataclass(frozen=True, slots=True)
class Entitlement:
    ref: str
    permitted_minor: int


@dataclass(frozen=True, slots=True)
class Refused:
    decision: Decision
    why: str


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


def _whole_days_up(delta: timedelta) -> int:
    return -(-delta // timedelta(days=1))


def prorated_minor(sub: SubscriptionFact, rounding: Rounding) -> int:
    # day granularity: any started day counts as used
    assert sub.cancelled_at is not None
    total = (sub.period_end - sub.period_start) // timedelta(days=1)
    used = min(total, _whole_days_up(sub.cancelled_at - sub.period_start))
    return prorate(sub.price.amount_minor, total - used, total, rounding)


def _entitlement(
    reason: ReasonCode, payment: PaymentFact, facts: CaseFacts, terms: RefundTerms, subject_id: str | None
) -> Entitlement | Refused:
    # the one place that decides what is owed, validate() and quote() both go through here
    window = timedelta(days=terms.refund_window_days)
    match reason:
        case ReasonCode.DUPLICATE_CAPTURE:
            if not terms.duplicate_refund_allowed:
                return Refused(Decision.REJECT, "policy doesn't allow duplicate-capture refunds")
            if _original_capture(payment, facts.payments) is None:
                return Refused(Decision.REJECT, "no earlier matching capture, charges look distinct")
            if facts.now - payment.captured_at > window:
                return Refused(Decision.REJECT, f"outside the {terms.refund_window_days}-day refund window")
            return Entitlement(duplicate_capture_ref(facts.order_id, payment.id), payment.amount.amount_minor)
        case ReasonCode.RETURNED_ITEMS:
            if not terms.returns_allowed:
                return Refused(Decision.REJECT, "policy doesn't refund returns")
            mine = [r for r in facts.returns if r.order_id == facts.order_id]
            if subject_id is not None:
                ret = next((r for r in mine if r.id == subject_id), None)
            elif len(mine) > 1:
                return Refused(Decision.ESCALATE, "several returns on this order, cite the one to refund")
            else:
                ret = mine[0] if mine else None
            if ret is None:
                return Refused(Decision.REJECT, "no matching return on this order")
            if ret.received_at is None:
                return Refused(Decision.REJECT, "the return hasn't been received yet")
            line = next((li for li in facts.lines if li.id == ret.line_id), None)
            if line is None:
                return Refused(Decision.REJECT, "returned line isn't on this order")
            if line.kind == "shipping" and not terms.shipping_refundable:
                return Refused(Decision.REJECT, "shipping isn't refundable under policy")
            if line.unit.currency != payment.amount.currency:
                return Refused(Decision.REJECT, "returned line and payment currencies differ")
            if facts.now - payment.captured_at > window:
                return Refused(Decision.REJECT, f"outside the {terms.refund_window_days}-day refund window")
            return Entitlement(return_ref(facts.order_id, ret.id), line.unit.amount_minor * ret.quantity)
        case ReasonCode.PRORATED_CANCELLATION:
            if not terms.proration_allowed:
                return Refused(Decision.REJECT, "policy doesn't prorate cancellations")
            sub = facts.subscription
            if sub is None or sub.order_id != facts.order_id or sub.cancelled_at is None:
                return Refused(Decision.REJECT, "no cancelled subscription on this order")
            if not sub.period_start <= sub.cancelled_at < sub.period_end:
                return Refused(Decision.REJECT, "cancelled outside the paid period")
            if sub.price.currency != payment.amount.currency:
                return Refused(Decision.REJECT, "subscription and payment currencies differ")
            owed = prorated_minor(sub, terms.rounding)
            if owed == 0:
                return Refused(Decision.REJECT, "no unused time left in the period")
            return Entitlement(prorate_ref(sub.id, sub.period_end), owed)
        case _:
            return Refused(Decision.ESCALATE, f"{reason} isn't a reason to move money")


_REFUNDABLE = frozenset({ReasonCode.DUPLICATE_CAPTURE, ReasonCode.RETURNED_ITEMS, ReasonCode.PRORATED_CANCELLATION})
_ASKING = frozenset({ReasonCode.MISSING_EVIDENCE, ReasonCode.CONFLICTING_EVIDENCE})
# escalating is always allowed, whatever the reason
REASONS_FOR: dict[Disposition, frozenset[ReasonCode]] = {
    Disposition.REFUND: _REFUNDABLE,
    Disposition.PARTIAL_REFUND: _REFUNDABLE,
    Disposition.ALREADY_REFUNDED: frozenset({ReasonCode.EXISTING_REFUND}),
    Disposition.DENY: frozenset({ReasonCode.INELIGIBLE}),
    Disposition.REQUEST_INFORMATION: _ASKING,
}


def _cited(plan: ResolutionPlan, kind: str) -> list[str]:
    return [ref.split(":")[1] for ref in plan.evidence_refs if ref.startswith(f"{kind}:") and ref.count(":") >= 2]


def _check_evidence(
    plan: ResolutionPlan, facts: CaseFacts, payment: PaymentFact, policy_ids: set[str]
) -> Verdict | None:
    versions: dict[str, dict[str, int]] = {
        "payment": {p.id: p.version for p in facts.payments},
        "return": {r.id: r.version for r in facts.returns},
        "subscription": {facts.subscription.id: facts.subscription.version} if facts.subscription else {},
    }
    cites_target = False
    for ref in plan.evidence_refs:
        kind, _, rest = ref.partition(":")
        if kind in versions:
            rid, _, ver = rest.partition(":")
            if not ver:
                return _reject(f"evidence {ref} has no version, cite it as {kind}:<id>:v<n>")
            found = versions[kind].get(rid)
            if found is None:
                return _reject(f"evidence {ref} isn't on this case's order")
            if ver != f"v{found}":
                return Verdict(Decision.STALE, (f"evidence {ref} superseded by v{found}",))
            cites_target |= kind == "payment" and rid == payment.id
        elif kind == "policy":
            pol, _, _section = rest.partition(":")
            if pol not in policy_ids:
                return _reject(f"evidence {ref} isn't an applicable policy version")
        else:
            return _reject(f"unknown evidence kind in {ref}")
    if not cites_target:
        return _reject("plan doesn't cite the payment it refunds")
    return None


def _left(ent: Entitlement, claim: ClaimFact | None, payment: PaymentFact) -> int:
    permitted = claim.permitted_minor if claim else ent.permitted_minor
    return remaining_minor(
        ClaimBalance(permitted, claim.reserved_minor if claim else 0, claim.consumed_minor if claim else 0),
        PaymentBalance(payment.amount.amount_minor, payment.refunded_minor, payment.reserved_minor),
    )


@dataclass(frozen=True, slots=True)
class Quote:
    payment_id: str
    eligible: bool
    reasons: tuple[str, ...] = ()
    entitlement_ref: str | None = None
    claim_id: str | None = None
    amount: Money | None = None
    terms_policy_id: str | None = None
    # the exact versioned refs a plan built on this quote cites, so nobody rebuilds them by hand
    cites: tuple[str, ...] = ()


def _cites(facts: CaseFacts, payment: PaymentFact, reason: ReasonCode, subject_id: str | None) -> tuple[str, ...]:
    refs = [f"payment:{payment.id}:v{payment.version}"]
    if reason is ReasonCode.DUPLICATE_CAPTURE and (orig := _original_capture(payment, facts.payments)) is not None:
        refs.insert(0, f"payment:{orig.id}:v{orig.version}")
    mine = [r for r in facts.returns if r.order_id == facts.order_id]
    if reason is ReasonCode.RETURNED_ITEMS:
        ret = next((r for r in mine if r.id == subject_id), None) if subject_id else None
        ret = ret or (mine[0] if subject_id is None and len(mine) == 1 else None)
        if ret is not None:
            refs.append(f"return:{ret.id}:v{ret.version}")
    sub = facts.subscription
    if reason is ReasonCode.PRORATED_CANCELLATION and sub is not None and sub.order_id == facts.order_id:
        refs.append(f"subscription:{sub.id}:v{sub.version}")
    return tuple(refs)


def quote(facts: CaseFacts, payment_id: str, reason: ReasonCode, subject_id: str | None = None) -> Quote:
    # arithmetic only, validate() still decides, tests pin the two together
    payment = next((p for p in facts.payments if p.id == payment_id), None)
    if payment is None:
        return Quote(payment_id, False, ("payment isn't on this case's order",))
    cites = _cites(facts, payment, reason, subject_id)

    def no(why: str) -> Quote:
        return Quote(payment_id, False, (why,), cites=cites)

    if payment.status != "succeeded":
        return no(f"payment status is {payment.status}")
    try:
        terms = applicable(facts.refund_terms, facts.order_placed_at)
    except AmbiguousPolicy as e:
        return no(str(e))
    if terms is None:
        return no("no applicable refund terms")
    ent = _entitlement(reason, payment, facts, terms, subject_id)
    if isinstance(ent, Refused):
        return no(ent.why)
    claim = facts.claims.get(ent.ref)
    left = _left(ent, claim, payment)
    if left == 0:
        return Quote(payment_id, False, ("nothing left to refund",), ent.ref, claim.id if claim else None, cites=cites)
    return Quote(
        payment_id,
        True,
        (),
        ent.ref,
        claim.id if claim else None,
        Money(left, payment.amount.currency),
        terms.policy.id,
        cites,
    )


def _already_refunded(plan: ResolutionPlan, facts: CaseFacts) -> Verdict:
    # "the money already went back" is a claim about money, so it gets checked like one
    payment = next((p for p in facts.payments if p.id == plan.payment_id), None)
    if payment is None:
        return _reject("ALREADY_REFUNDED has to name a payment on this case's order")
    if payment.refunded_minor > 0:
        return Verdict(Decision.NO_MONEY, ("ALREADY_REFUNDED moves no money", "recon's ledger shows the refund"))
    seen = None if facts.provider_refunds is None else facts.provider_refunds.get(payment.id)
    if seen is None:
        return Verdict(Decision.ESCALATE, (f"can't confirm a refund on {payment.id} without a provider read",))
    if any(st not in ("failed", "canceled") for st in seen):
        return Verdict(
            Decision.NO_MONEY, ("ALREADY_REFUNDED moves no money", f"provider shows a refund on {payment.id}")
        )
    return _reject(f"no refund exists on {payment.id}")


def _deny(facts: CaseFacts) -> Verdict:
    # a denial has to refuse something that's on the order, and none of it may be owed or already paid back
    paid = [p for p in facts.payments if p.status == "succeeded"]
    asks: list[tuple[str, ReasonCode, str | None]] = []
    if len(paid) > 1:
        asks += [(p.id, ReasonCode.DUPLICATE_CAPTURE, None) for p in paid]
    asks += [
        (p.id, ReasonCode.RETURNED_ITEMS, r.id) for r in facts.returns if r.order_id == facts.order_id for p in paid
    ]
    sub = facts.subscription
    if sub is not None and sub.order_id == facts.order_id and sub.cancelled_at is not None:
        asks += [(p.id, ReasonCode.PRORATED_CANCELLATION, sub.id) for p in paid]
    if not asks:
        return _reject("nothing on this order to deny, ask for what's missing or escalate")
    for pid, reason, subject in asks:
        q = quote(facts, pid, reason, subject)
        if q.eligible:
            return _reject(f"{reason} on {pid} is owed, it can't be denied")
        if q.reasons and q.reasons[0].startswith("nothing left"):
            return _reject(f"{reason} on {pid} was already refunded, that's ALREADY_REFUNDED")
    return Verdict(Decision.NO_MONEY, ("DENY moves no money", "every refund basis on the order was refused"))


def validate(plan: ResolutionPlan, facts: CaseFacts) -> Verdict:
    if plan.case_id != facts.case_id:
        return _reject("plan is for a different case")
    if plan.case_version != facts.case_version:
        return Verdict(Decision.STALE, (f"plan case v{plan.case_version}, current v{facts.case_version}",))
    allowed = REASONS_FOR.get(plan.recommended_disposition)
    if allowed is not None and plan.reason_code not in allowed:
        # a plan that contradicts itself goes to a person, it doesn't close the case
        return _reject(f"{plan.recommended_disposition} doesn't go with reason {plan.reason_code}")
    if plan.recommended_disposition is Disposition.ALREADY_REFUNDED:
        return _already_refunded(plan, facts)
    if plan.recommended_disposition is Disposition.DENY:
        return _deny(facts)
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

    try:
        terms = applicable(facts.refund_terms, facts.order_placed_at)
        limits = applicable(facts.limits, facts.now)
    except AmbiguousPolicy as e:
        return Verdict(Decision.ESCALATE, (str(e),))
    if terms is None or limits is None:
        return Verdict(Decision.ESCALATE, ("no applicable policy version",))

    # the plan names which return or subscription it means through its evidence, never by a free id
    subject = next(iter(_cited(plan, "return") or _cited(plan, "subscription")), None)
    ent = _entitlement(plan.reason_code, payment, facts, terms, subject)
    if isinstance(ent, Refused):
        return Verdict(ent.decision, (ent.why,))

    policy_ids = {terms.policy.id, limits.policy.id}
    if terms.policy.id not in plan.policy_version_ids:
        return _reject(f"plan doesn't cite applicable refund terms {terms.policy.id}")
    bad = _check_evidence(plan, facts, payment, policy_ids)
    if bad is not None:
        return bad

    claim = facts.claims.get(ent.ref)
    if claim is not None and plan.claim_id is not None and plan.claim_id != claim.id:
        return _reject(f"plan claim {plan.claim_id} doesn't match {claim.id}")
    permitted = claim.permitted_minor if claim else ent.permitted_minor
    left = _left(ent, claim, payment)
    if amount.amount_minor > left:
        return _reject(f"amount {amount.amount_minor} exceeds remaining {left}")

    decision = Decision.AUTHORIZE
    reasons: tuple[str, ...] = ()
    if amount.amount_minor > limits.auto_approve_limit_minor:
        decision, reasons = Decision.NEEDS_APPROVAL, (f"over auto-approve limit {limits.auto_approve_limit_minor}",)
    return Verdict(decision, reasons, ent.ref, payment, amount, permitted, tuple(sorted(policy_ids)))
