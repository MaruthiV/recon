from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from recon_domain.money import Currency, Money
from recon_domain.plan import Disposition, ReasonCode, ResolutionPlan, material_hash
from recon_domain.policy import AuthorityClass, OperationalLimits, PolicyVersion, RefundTerms
from recon_domain.proration import Rounding
from recon_domain.validation import CaseFacts, ClaimFact, Decision, PaymentFact, quote, validate

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


DISTINCT = facts(payments=(pay("pay_a", 0), pay("pay_b", 3, amount=1200)))


def deny(**kw):
    return plan(recommended_disposition="DENY", reason_code="INELIGIBLE", amount_minor=None, currency=None, **kw)


def test_non_money_dispositions_move_nothing():
    v = validate(deny(), DISTINCT)
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


QUOTE_CASES = {
    "fresh": facts(),
    "partly_refunded": facts(payments=(pay("pay_a", 0), pay("pay_b", 3, refunded_minor=1200))),
    "claim_reserved": facts(claims={"dup:ord_1:pay_b": ClaimFact("claim_1", "dup:ord_1:pay_b", 4900, 900, 0)}),
    "over_limit": facts(payments=(pay("pay_a", 0, amount=25_000), pay("pay_b", 3, amount=25_000))),
}


@pytest.mark.parametrize("name", sorted(QUOTE_CASES))
def test_quote_is_exactly_what_validate_allows(name):
    f = QUOTE_CASES[name]
    q = quote(f, "pay_b", ReasonCode.DUPLICATE_CAPTURE)
    assert q.eligible and q.amount is not None, q.reasons
    claim_id = q.claim_id
    at = validate(plan(amount_minor=q.amount.amount_minor, claim_id=claim_id), f)
    assert at.decision in (Decision.AUTHORIZE, Decision.NEEDS_APPROVAL), at.reasons
    over = validate(plan(amount_minor=q.amount.amount_minor + 1, claim_id=claim_id), f)
    assert over.decision is Decision.REJECT


@pytest.mark.parametrize(
    ("f", "pid", "reason", "why"),
    [
        (facts(), "pay_a", ReasonCode.DUPLICATE_CAPTURE, "no earlier matching capture"),
        (facts(), "pay_zz", ReasonCode.DUPLICATE_CAPTURE, "isn't on this case's order"),
        (facts(), "pay_b", ReasonCode.RETURNED_ITEMS, "doesn't refund returns"),
        (facts(now=T0 + timedelta(days=31)), "pay_b", ReasonCode.DUPLICATE_CAPTURE, "refund window"),
        (
            facts(payments=(pay("pay_a", 0), pay("pay_b", 3, refunded_minor=4900))),
            "pay_b",
            ReasonCode.DUPLICATE_CAPTURE,
            "nothing left",
        ),
    ],
)
def test_quote_says_why_not(f, pid, reason, why):
    q = quote(f, pid, reason)
    assert not q.eligible and q.amount is None
    assert why in q.reasons[0]


def test_rules_plan_refunds_the_duplicate_and_passes_validation():
    from recon_domain.rules import rules_plan

    f = facts()
    p = rules_plan(f)
    assert (p.recommended_disposition, p.payment_id, p.amount_minor) == ("REFUND", "pay_b", 4900)
    assert validate(p, f).decision is Decision.AUTHORIZE


@pytest.mark.parametrize(
    ("f", "disposition"),
    [
        (facts(payments=(pay("pay_a", 0),)), "REQUEST_INFORMATION"),
        (facts(payments=(pay("pay_a", 0), pay("pay_b", 3, refunded_minor=4900))), "ALREADY_REFUNDED"),
        (facts(now=T0 + timedelta(days=40)), "DENY"),
        (facts(payments=(pay("pay_a", 0), pay("pay_b", 3, amount=5000))), "DENY"),
    ],
)
def test_rules_plan_moves_no_money_when_it_shouldnt(f, disposition):
    from recon_domain.rules import rules_plan

    p = rules_plan(f)
    assert p.recommended_disposition == disposition
    assert validate(p, f).decision is not Decision.AUTHORIZE


# --- returns and proration (m4) ---

from recon_domain.validation import LineFact, ReturnFact, SubscriptionFact  # noqa: E402

FULL_TERMS = RefundTerms(
    policy("pol_terms_v1", AuthorityClass.REFUND_TERMS),
    30,
    True,
    Rounding.HALF_EVEN,
    returns_allowed=True,
    shipping_refundable=False,
    proration_allowed=True,
)
LINES = (
    LineFact("line_a", "ord_1", "SKU-A", "item", 2, Money(2500, Currency.USD)),
    LineFact("line_s", "ord_1", "SHIP", "shipping", 1, Money(500, Currency.USD)),
)


def ret(rid="ret_1", line="line_a", qty=1, received=True, version=1):
    return ReturnFact(
        rid, "ord_1", line, qty, T0 + timedelta(days=1), T0 + timedelta(days=3) if received else None, version
    )


def return_facts(*returns, terms=FULL_TERMS):
    return facts(
        payments=(pay("pay_a", 0, amount=5500),), lines=LINES, returns=returns or (ret(),), refund_terms=(terms,)
    )


def return_plan(amount, rid="ret_1", **kw):
    base = dict(
        payment_id="pay_a",
        amount_minor=amount,
        reason_code="RETURNED_ITEMS",
        evidence_refs=["payment:pay_a:v1", f"return:{rid}:v1", "policy:pol_terms_v1:returns"],
    )
    return plan(**{**base, **kw})


def test_return_refunds_exactly_the_returned_units():
    f = return_facts()
    q = quote(f, "pay_a", ReasonCode.RETURNED_ITEMS)
    assert q.eligible and q.amount == Money(2500, Currency.USD) and q.entitlement_ref == "return:ord_1:ret_1"
    assert validate(return_plan(2500), f).decision is Decision.AUTHORIZE
    assert validate(return_plan(2501), f).decision is Decision.REJECT


@pytest.mark.parametrize(
    ("f", "why"),
    [
        (return_facts(ret(received=False)), "hasn't been received"),
        (return_facts(ret(line="line_s")), "shipping isn't refundable"),
        (return_facts(terms=replace(FULL_TERMS, returns_allowed=False)), "doesn't refund returns"),
    ],
)
def test_returns_that_arent_refundable(f, why):
    q = quote(f, "pay_a", ReasonCode.RETURNED_ITEMS)
    assert not q.eligible and why in q.reasons[0]
    v = validate(return_plan(500 if "shipping" in why else 2500), f)
    assert v.decision is Decision.REJECT and why in v.reasons[0]


def test_the_cited_return_is_the_one_refunded():
    f = return_facts(ret("ret_1", qty=1), ret("ret_2", qty=2))
    assert "several returns" in quote(f, "pay_a", ReasonCode.RETURNED_ITEMS).reasons[0]
    assert quote(f, "pay_a", ReasonCode.RETURNED_ITEMS, "ret_2").amount == Money(5000, Currency.USD)
    assert validate(return_plan(5000, "ret_2"), f).decision is Decision.AUTHORIZE
    # the 2-unit amount on the 1-unit return is over its entitlement
    assert validate(return_plan(5000, "ret_1"), f).decision is Decision.REJECT


def sub(price=3000, days=30, cancel_after=timedelta(days=10, hours=12), cancelled=True):
    start = T0
    return SubscriptionFact(
        "sub_1",
        "ord_1",
        start,
        start + timedelta(days=days),
        Money(price, Currency.USD),
        start + cancel_after if cancelled else None,
        1,
    )


def prorate_facts(s=None, terms=FULL_TERMS):
    return facts(payments=(pay("pay_a", 0, amount=3000),), subscription=s or sub(), refund_terms=(terms,))


def prorate_plan(amount, **kw):
    base = dict(
        payment_id="pay_a",
        amount_minor=amount,
        reason_code="PRORATED_CANCELLATION",
        evidence_refs=["payment:pay_a:v1", "subscription:sub_1:v1", "policy:pol_terms_v1:proration"],
    )
    return plan(**{**base, **kw})


def test_proration_counts_started_days_as_used():
    # 10.5 days in: 11 used, 19 of 30 unused, 3000 * 19/30 = 1900 exactly
    f = prorate_facts()
    q = quote(f, "pay_a", ReasonCode.PRORATED_CANCELLATION)
    assert q.amount == Money(1900, Currency.USD) and (q.entitlement_ref or "").startswith("prorate:sub_1:")
    assert validate(prorate_plan(1900), f).decision is Decision.AUTHORIZE
    assert validate(prorate_plan(1901), f).decision is Decision.REJECT


@pytest.mark.parametrize(("mode", "expected"), [("HALF_EVEN", 633), ("UP", 634), ("DOWN", 633)])
def test_proration_uses_the_policy_rounding(mode, expected):
    # 1000 * 19/30 = 633.33...
    f = prorate_facts(sub(price=1000), replace(FULL_TERMS, rounding=Rounding(mode)))
    assert quote(f, "pay_a", ReasonCode.PRORATED_CANCELLATION).amount == Money(expected, Currency.USD)


@pytest.mark.parametrize(
    ("f", "why"),
    [
        (prorate_facts(sub(cancelled=False)), "no cancelled subscription"),
        (prorate_facts(sub(cancel_after=timedelta(days=31))), "outside the paid period"),
        (prorate_facts(terms=replace(FULL_TERMS, proration_allowed=False)), "doesn't prorate"),
    ],
)
def test_proration_that_isnt_owed(f, why):
    q = quote(f, "pay_a", ReasonCode.PRORATED_CANCELLATION)
    assert not q.eligible and why in q.reasons[0]
    assert validate(prorate_plan(100), f).decision is Decision.REJECT


def test_a_ref_without_its_version_is_rejected_not_stale():
    v = validate(plan(evidence_refs=["payment:pay_a:v1", "payment:pay_b", "policy:pol_terms_v1:dup"]), facts())
    assert v.decision is Decision.REJECT and "has no version" in v.reasons[0]


@pytest.mark.parametrize(
    ("f", "args", "cites"),
    [
        (facts(), ("pay_b", ReasonCode.DUPLICATE_CAPTURE), ("payment:pay_a:v1", "payment:pay_b:v1")),
        (return_facts(), ("pay_a", ReasonCode.RETURNED_ITEMS), ("payment:pay_a:v1", "return:ret_1:v1")),
        (
            return_facts(ret("ret_1"), ret("ret_2", version=3)),
            ("pay_a", ReasonCode.RETURNED_ITEMS, "ret_2"),
            ("payment:pay_a:v1", "return:ret_2:v3"),
        ),
        (prorate_facts(), ("pay_a", ReasonCode.PRORATED_CANCELLATION), ("payment:pay_a:v1", "subscription:sub_1:v1")),
        # a refused quote still says what it looked at
        (
            return_facts(ret(received=False)),
            ("pay_a", ReasonCode.RETURNED_ITEMS),
            ("payment:pay_a:v1", "return:ret_1:v1"),
        ),
    ],
)
def test_a_quote_hands_back_the_refs_a_plan_must_cite(f, args, cites):
    q = quote(f, *args)
    assert q.cites == cites
    if q.eligible:
        assert q.amount is not None and q.terms_policy_id is not None
        p = plan(
            payment_id=args[0],
            amount_minor=q.amount.amount_minor,
            reason_code=args[1],
            evidence_refs=[*q.cites, f"policy:{q.terms_policy_id}:x"],
        )
        assert validate(p, f).decision is Decision.AUTHORIZE


@pytest.mark.parametrize(
    ("disposition", "reason", "ok"),
    [
        ("ALREADY_REFUNDED", "INELIGIBLE", False),
        ("ALREADY_REFUNDED", "EXISTING_REFUND", True),
        ("DENY", "EXISTING_REFUND", False),
        ("DENY", "INELIGIBLE", True),
        ("REQUEST_INFORMATION", "MISSING_EVIDENCE", True),
        ("REQUEST_INFORMATION", "DUPLICATE_CAPTURE", False),
        ("ESCALATE", "DUPLICATE_CAPTURE", True),
        ("REFUND", "INELIGIBLE", False),
    ],
)
def test_a_plan_that_contradicts_itself_is_rejected(disposition, reason, ok):
    p = plan(
        recommended_disposition=disposition,
        reason_code=reason,
        **({} if disposition == "REFUND" else {"amount_minor": None, "currency": None}),
    )
    v = validate(p, DISTINCT)
    assert (v.decision is not Decision.REJECT) is ok, v.reasons


def refunded_claim(**kw):
    base = dict(
        recommended_disposition="ALREADY_REFUNDED",
        reason_code="EXISTING_REFUND",
        payment_id="pay_b",
        amount_minor=None,
        currency=None,
    )
    return plan(**{**base, **kw})


@pytest.mark.parametrize(
    ("f", "decision", "why"),
    [
        # recon's own ledger already shows money going back
        (facts(payments=(pay("pay_a", 0), pay("pay_b", 3, refunded_minor=4900))), Decision.NO_MONEY, "ledger"),
        # a refund only the provider knows about, pending counts
        (facts(provider_refunds={"pay_b": ("pending",)}), Decision.NO_MONEY, "provider shows"),
        # the model said refunded, nothing anywhere says so
        (facts(provider_refunds={"pay_b": ()}), Decision.REJECT, "no refund exists"),
        (facts(provider_refunds={"pay_b": ("failed",)}), Decision.REJECT, "no refund exists"),
        # nobody could look, so nobody can say
        (facts(), Decision.ESCALATE, "can't confirm"),
    ],
)
def test_an_already_refunded_claim_is_checked_like_money(f, decision, why):
    v = validate(refunded_claim(), f)
    assert v.decision is decision and any(why in r for r in v.reasons), v.reasons


def test_an_already_refunded_claim_has_to_name_a_payment_here():
    v = validate(refunded_claim(payment_id="pay_elsewhere"), facts(provider_refunds={"pay_elsewhere": ("succeeded",)}))
    assert v.decision is Decision.REJECT


@pytest.mark.parametrize(
    ("f", "decision", "why"),
    [
        # two different charges, the claimed duplicate is refused on the facts
        (DISTINCT, Decision.NO_MONEY, "every refund basis"),
        (return_facts(ret(received=False)), Decision.NO_MONEY, "every refund basis"),
        (facts(now=T0 + timedelta(days=90)), Decision.NO_MONEY, "every refund basis"),
        # the duplicate is owed, a denial would be a wrong answer to the customer
        (facts(), Decision.REJECT, "is owed"),
        (facts(payments=(pay("pay_a", 0), pay("pay_b", 3, refunded_minor=4900))), Decision.REJECT, "already refunded"),
        # nothing here was asked for in a way the facts can refuse
        (facts(payments=(pay("pay_a", 0),)), Decision.REJECT, "nothing on this order"),
    ],
)
def test_a_denial_has_to_refuse_something_real(f, decision, why):
    v = validate(deny(), f)
    assert v.decision is decision and any(why in r for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    ("minor", "shown"), [(4900, "49.00 USD"), (5, "0.05 USD"), (25000, "250.00 USD"), (-150, "-1.50 USD")]
)
def test_money_reads_like_money(minor, shown):
    assert Money(minor, Currency.USD).display() == shown
