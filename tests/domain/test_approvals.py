from dataclasses import replace
from datetime import timedelta

import pytest

from recon_domain.approvals import ApprovalFact, approval_gap, executable
from recon_domain.plan import material_hash
from recon_domain.validation import Decision, validate
from tests.domain.test_validation import LIMITS, T0, facts, pay, plan

BIG = 25_000
F = facts(payments=(pay("pay_a", 0, amount=BIG), pay("pay_b", 3, amount=BIG)))
P = plan(amount_minor=BIG)
V = validate(P, F)
NOW = F.now


def approved(**kw):
    base = ApprovalFact(
        material_hash(P, V.entitlement_ref),
        "pay_b",
        "dup:ord_1:pay_b",
        BIG,
        "usd",
        "APPROVED",
        NOW + timedelta(hours=24),
    )
    return replace(base, **kw)


def test_over_the_limit_needs_an_approval_that_matches():
    assert V.decision is Decision.NEEDS_APPROVAL and LIMITS.auto_approve_limit_minor < BIG
    assert not executable(V, P, None, NOW)
    assert approval_gap(approved(), P, V, NOW) is None
    assert executable(V, P, approved(), NOW)


@pytest.mark.parametrize(
    ("a", "why"),
    [
        (approved(decision="REJECTED"), "said no"),
        (approved(expires_at=T0), "expired"),
        (approved(plan_hash="0" * 64), "plan changed"),
        (approved(amount_minor=BIG - 1), "now means"),
        (approved(entitlement_ref="dup:ord_1:pay_a"), "now means"),
    ],
)
def test_an_approval_covers_exactly_what_was_approved(a, why):
    gap = approval_gap(a, P, V, NOW)
    assert gap is not None and why in gap
    assert not executable(V, P, a, NOW)


def test_rewording_the_explanation_keeps_the_approval():
    reworded = plan(amount_minor=BIG, explanation="same refund, different words")
    assert approval_gap(approved(), reworded, validate(reworded, F), NOW) is None


def test_changing_the_cited_evidence_needs_a_fresh_approval():
    bigger = plan(amount_minor=BIG, evidence_refs=["payment:pay_b:v1", "policy:pol_terms_v1:dup"])
    assert approval_gap(approved(), bigger, validate(bigger, F), NOW) == "the plan changed after it was approved"
