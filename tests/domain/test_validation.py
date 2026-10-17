from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from recon_domain.money import Currency, Money
from recon_domain.plan import Disposition, ReasonCode, ResolutionPlan, material_hash
from recon_domain.policy import AuthorityClass, OperationalLimits, PolicyVersion, RefundTerms
from recon_domain.proration import Rounding
from recon_domain.validation import CaseFacts, ClaimFact, Decision, PaymentFact, validate

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def pay(pid, minutes, amount=4900, version=1, **kw):
    base = dict(
        id=pid,
        order_id="ord_1",
        customer_id="cus_1",
        provider_payment_id=f"pi_{pid}",
        amount=Money(amount, Currency.USD),
        status="succeeded",
        captured_at=T0 + timedelta(minutes=minutes),
        version=version,
        refunded_minor=0,
        reserved_minor=0,
    )
    return PaymentFact(**{**base, **kw})


def policy(pid, cls):
    return PolicyVersion(pid, cls, 1, datetime(2026, 1, 1, tzinfo=UTC), None)


TERMS = RefundTerms(policy("pol_terms_v1", AuthorityClass.REFUND_TERMS), 30, True, Rounding.HALF_EVEN)
LIMITS = OperationalLimits(policy("pol_limits_v1", AuthorityClass.OPERATIONAL_LIMITS), 10_000)


def facts(**kw):
    base = dict(
        tenant_id="t_acme",
        case_id="case_1",
        case_version=1,
        customer_id="cus_1",
        order_id="ord_1",
        order_placed_at=T0 - timedelta(minutes=1),
        order_currency=Currency.USD,
        payments=(pay("pay_a", 0), pay("pay_b", 3)),
        claims={},
        refund_terms=(TERMS,),
        limits=(LIMITS,),
        now=T0 + timedelta(days=2),
    )
    return CaseFacts(**{**base, **kw})


def plan(**kw):
    base = dict(
        case_id="case_1",
        case_version=1,
        recommended_disposition="REFUND",
        payment_id="pay_b",
        amount_minor=4900,
        currency="USD",
        policy_version_ids=["pol_terms_v1"],
        evidence_refs=["payment:pay_a:v1", "payment:pay_b:v1", "policy:pol_terms_v1:dup"],
        reason_code="DUPLICATE_CAPTURE",
        explanation="two captures for one order",
    )
    return ResolutionPlan(**{**base, **kw})


def test_duplicate_capture_authorized():
    v = validate(plan(), facts())
    assert v.decision is Decision.AUTHORIZE, v.reasons
    assert v.entitlement_ref == "dup:ord_1:pay_b"
    assert v.amount == Money(4900, Currency.USD)


def test_refunding_the_original_is_rejected():
    v = validate(plan(payment_id="pay_a", evidence_refs=["payment:pay_a:v1", "policy:pol_terms_v1:x"]), facts())
    assert v.decision is Decision.REJECT


def test_distinct_amounts_are_not_duplicates():
    v = validate(plan(amount_minor=1500), facts(payments=(pay("pay_a", 0), pay("pay_b", 3, amount=1500))))
    assert v.decision is Decision.REJECT


def test_stale_case_version():
    assert validate(plan(case_version=0), facts()).decision is Decision.STALE


def test_superseded_payment_evidence_is_stale():
    v = validate(plan(), facts(payments=(pay("pay_a", 0), pay("pay_b", 3, version=2))))
    assert v.decision is Decision.STALE


def test_amount_over_remaining_rejected():
    claim = ClaimFact("clm_1", "dup:ord_1:pay_b", 4900, 0, 4000)
    assert validate(plan(), facts(claims={"dup:ord_1:pay_b": claim})).decision is Decision.REJECT
    assert validate(plan(amount_minor=900), facts(claims={"dup:ord_1:pay_b": claim})).decision is Decision.AUTHORIZE


def test_advisory_claim_id_must_match():
    claim = ClaimFact("clm_1", "dup:ord_1:pay_b", 4900, 0, 0)
    v = validate(plan(claim_id="clm_made_up"), facts(claims={"dup:ord_1:pay_b": claim}))
    assert v.decision is Decision.REJECT


def test_currency_mismatch_rejected_at_plan_boundary():
    with pytest.raises(ValidationError):
        plan(currency="EUR")
    with pytest.raises(ValidationError):
        plan(amount_minor=49.0)


def test_outside_window_rejected():
    assert validate(plan(), facts(now=T0 + timedelta(days=31))).decision is Decision.REJECT


def test_over_limit_needs_approval():
    low = OperationalLimits(LIMITS.policy, 1000)
    assert validate(plan(), facts(limits=(low,))).decision is Decision.NEEDS_APPROVAL


def test_missing_policy_escalates():
    assert validate(plan(), facts(refund_terms=())).decision is Decision.ESCALATE


def test_wrong_policy_citation_rejected():
    assert validate(plan(policy_version_ids=["pol_other"]), facts()).decision is Decision.REJECT
    bad_ref = ["payment:pay_b:v1", "policy:pol_other:dup"]
    assert validate(plan(evidence_refs=bad_ref), facts()).decision is Decision.REJECT


def test_other_customers_payment_rejected():
    v = validate(plan(), facts(payments=(pay("pay_a", 0), pay("pay_b", 3, customer_id="cus_2"))))
    assert v.decision is Decision.REJECT


def test_non_money_dispositions_move_nothing():
    v = validate(plan(recommended_disposition="DENY", reason_code="INELIGIBLE"), facts())
    assert v.decision is Decision.NO_MONEY and not v.can_execute


def test_material_hash_ignores_wording_only():
    a, b = plan(), plan(explanation="reworded")
    assert material_hash(a, "dup:ord_1:pay_b") == material_hash(b, "dup:ord_1:pay_b")
    assert material_hash(a, "dup:ord_1:pay_b") != material_hash(plan(amount_minor=4800), "dup:ord_1:pay_b")
    reordered = plan(evidence_refs=list(reversed(a.evidence_refs)))
    assert material_hash(a, "x") == material_hash(reordered, "x")


def test_plan_enums_parse():
    p = plan()
    assert p.recommended_disposition is Disposition.REFUND and p.reason_code is ReasonCode.DUPLICATE_CAPTURE
    assert replace(pay("x", 0), version=3).version == 3
