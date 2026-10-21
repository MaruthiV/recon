from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from recon_storage.models import (
    Case,
    CaseMessage,
    Customer,
    Order,
    OrderLine,
    Payment,
    PolicyChunk,
    PolicyDocument,
    PolicyRule,
    Return,
    Subscription,
    Tenant,
)
from recon_verifier.db import labels

FAMILIES = (
    "duplicate_charge",
    "missing_refund",
    "partial_return",
    "proration",
    "ineligible",
    "missing_or_conflicting",
    # the record looks routine, what the customer wrote (and the policy text) changes the right answer
    "message_matters",
)
CUTOVER = datetime(2026, 7, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Terms:
    version: int
    effective_from: datetime
    effective_to: datetime | None
    window_days: int
    returns: bool
    shipping: bool
    proration: bool
    rounding: str


@dataclass(frozen=True)
class TenantSpec:
    key: str
    name: str
    terms: tuple[Terms, ...]
    approve_limit: int


# three merchants with different rules, acme changed its window mid-year so applicability matters
TENANTS = {
    "acme": TenantSpec(
        "acme",
        "Acme",
        (
            Terms(1, datetime(2026, 1, 1, tzinfo=UTC), CUTOVER, 30, True, False, True, "HALF_EVEN"),
            Terms(2, CUTOVER, None, 14, True, False, True, "HALF_EVEN"),
        ),
        10_000,
    ),
    "globex": TenantSpec(
        "globex", "Globex", (Terms(1, datetime(2026, 1, 1, tzinfo=UTC), None, 60, True, True, False, "DOWN"),), 5_000
    ),
    "initech": TenantSpec(
        "initech", "Initech", (Terms(1, datetime(2026, 1, 1, tzinfo=UTC), None, 30, False, False, True, "UP"),), 20_000
    ),
}


def terms_sections(t: Terms) -> tuple[tuple[str, str, str], ...]:
    # the policy text says exactly what the structured rule says, contradictions are their own fixtures
    out = [
        (
            "dup",
            "Duplicate charges",
            "If an order was captured more than once for the same amount, the extra capture is refunded in full.",
        ),
        ("window", "Refund window", f"Refund requests are accepted within {t.window_days} days of the charge."),
    ]
    out.append(
        ("returns", "Returned items", "Returned items are refunded at the item price once the return is received.")
        if t.returns
        else ("returns", "Returned items", "Returned items are not refunded. Store credit only.")
    )
    out.append(
        (
            "shipping",
            "Shipping",
            "Shipping charges are refunded with a returned item." if t.shipping else "Shipping is not refunded.",
        )
    )
    out.append(
        (
            "proration",
            "Cancellations",
            f"Cancelled subscriptions are refunded for unused whole days, rounded {t.rounding.lower().replace('_', ' ')}."
            if t.proration
            else "Subscriptions are not refunded after the period starts.",
        )
    )
    return (*out, *DISTRACTORS)


# real policies have sections no workflow needs, retrieval has to pick past them
DISTRACTORS = (
    (
        "method",
        "Refund method",
        "Refunds go back to the original payment method. Banks can take 5 to 10 business days to show them.",
    ),
    (
        "exchanges",
        "Exchanges",
        "Items can be swapped for another size or colour while stock lasts. An exchange is not a refund.",
    ),
    ("gift_cards", "Gift cards", "Gift cards and store credit can't be refunded or turned into cash."),
    (
        "disputes",
        "Card disputes",
        "If a charge is disputed with the bank, refunds on it are paused until the dispute is settled.",
    ),
    (
        "damaged",
        "Damaged items",
        "Report a damaged or faulty item within 7 days with a photo. We send a replacement first.",
    ),
    ("price_drops", "Price drops", "We don't refund the difference if an item goes on sale after you buy it."),
)


def limit_sections(limit: int) -> tuple[tuple[str, str, str], ...]:
    return (
        (
            "auto_approve",
            "Automatic approval",
            f"Refunds up to {limit // 100}.{limit % 100:02d} USD are approved automatically. Larger refunds need a supervisor.",
        ),
    )


def body_of(sections) -> str:
    return "\n\n".join(f"{h}. {t}" for _, h, t in sections)


def rounded(numer: int, denom: int, mode: str) -> int:
    # independent of recon_domain on purpose, the grader shouldn't share the code it grades
    q = {"HALF_EVEN": ROUND_HALF_EVEN, "UP": ROUND_UP, "DOWN": ROUND_DOWN}[mode]
    return int((Decimal(numer) / Decimal(denom)).quantize(Decimal(1), rounding=q))


@dataclass
class Spec:
    family: str
    variant: str
    tenant: str
    seed: str
    placed_days_ago: int
    amount: int
    message: str
    subject: str
    extra: dict[str, Any] = field(default_factory=dict)


def make_specs(split: str, per_family: int, seed: int) -> list[Spec]:
    rnd = random.Random(f"{split}:{seed}")
    variants = {
        "duplicate_charge": ["refund", "distinct", "already_refunded", "late", "over_limit"],
        "missing_refund": ["succeeded", "pending", "never_issued"],
        "partial_return": ["received", "not_received", "two_lines"],
        "proration": ["mid_period", "after_period", "not_allowed"],
        "ineligible": ["window", "returns_off"],
        "missing_or_conflicting": ["no_order", "wrong_customer"],
        "message_matters": ["disputed_with_bank", "quantity_conflict"],
    }
    specs = []
    for family in FAMILIES:
        for i in range(per_family):
            v = variants[family][i % len(variants[family])]
            tenant = {"not_allowed": "globex", "returns_off": "initech", "over_limit": "globex"}.get(
                v, rnd.choice(sorted(TENANTS))
            )
            if v in ("received", "two_lines", "not_received", "quantity_conflict") and tenant == "initech":
                tenant = "acme"  # initech doesn't take returns, keep the variant meaningful
            if v in ("mid_period", "after_period") and tenant == "globex":
                tenant = "acme"  # globex never prorates, which would hide what these variants test
            amount = rnd.choice([1900, 2500, 4900, 7400, 9900]) if v != "over_limit" else 8800
            window = min(t.window_days for t in TENANTS[tenant].terms)
            days = rnd.randint(2, window - 2) if v not in ("late", "window") else window + rnd.randint(70, 90)
            specs.append(
                Spec(
                    family,
                    v,
                    tenant,
                    f"{split}:{seed}:{family}:{i}",
                    days,
                    amount,
                    MESSAGES[(family, v)] if (family, v) in MESSAGES else MESSAGES[(family, "*")],
                    SUBJECTS[family],
                    {"cancel_day": rnd.randint(3, 25), "qty": rnd.choice([1, 2])},
                )
            )
    return specs


SUBJECTS = {
    "duplicate_charge": "Charged twice",
    "missing_refund": "Where is my refund?",
    "partial_return": "Refund for returned item",
    "proration": "Cancelled my subscription",
    "ineligible": "Refund request",
    "missing_or_conflicting": "Please refund me",
    "message_matters": "Refund request",
}
MESSAGES = {
    ("duplicate_charge", "*"): "I was charged twice for the same order. Please return the extra payment.",
    ("duplicate_charge", "distinct"): "I see two charges on my card for this order, one must be a mistake.",
    ("missing_refund", "*"): "You told me I'd get a refund but I can't see it. Where is it?",
    ("partial_return", "*"): "I returned part of my order. Please refund it.",
    ("proration", "*"): "I cancelled my subscription partway through the month. I'd like the unused part back.",
    ("ineligible", "*"): "I'd like a refund for the item I returned.",
    ("missing_or_conflicting", "*"): "Please refund my last purchase, it wasn't what I expected.",
    ("message_matters", "disputed_with_bank"): (
        "I was charged twice for the same order. I already disputed the second charge with my bank "
        "and they're looking into it."
    ),
    ("message_matters", "quantity_conflict"): (
        "I bought two of these but only sent one back. Please refund just the one I returned."
    ),
}


async def ensure_tenant(
    s, sim: httpx.AsyncClient, op: dict[str, str], key: str, ids: Callable[[str], str], prefix: str = ""
) -> dict[str, Any]:
    # prefix keeps a demo copy of a merchant apart from a tenant some other fixture already made
    spec = TENANTS[key]
    name = f"{prefix}{key}"
    tid = f"t_{name}"
    acct, sk, rk = f"acct_{name}", f"sk_test_{name}", f"rk_test_{name}"
    r = await sim.post("/sim/accounts", json={"id": acct, "api_key": sk, "read_key": rk}, headers=op)
    r.raise_for_status()
    if (await s.execute(select(Tenant.id).where(Tenant.id == tid))).scalar() is None:
        s.add(Tenant(id=tid, name=spec.name, provider_account=acct, created_at=spec.terms[0].effective_from))
        await s.flush()
        for t in spec.terms:
            await _policy(
                s,
                tid,
                "refund_terms",
                "Refund terms",
                t.version,
                t.effective_from,
                t.effective_to,
                terms_sections(t),
                ids,
                refund_window_days=t.window_days,
                duplicate_refund_allowed=True,
                rounding_mode=t.rounding,
                returns_allowed=t.returns,
                shipping_refundable=t.shipping,
                proration_allowed=t.proration,
            )
        await _policy(
            s,
            tid,
            "operational_limits",
            "Operational limits",
            1,
            spec.terms[0].effective_from,
            None,
            limit_sections(spec.approve_limit),
            ids,
            auto_approve_limit_minor=spec.approve_limit,
        )
    return {"tenant_id": tid, "account": acct, "api_key": sk, "read_key": rk}


async def _policy(s, tid, cls, title, version, start, end, sections, ids, **rule) -> None:
    pid = ids("pol")
    body = body_of(sections)
    s.add(
        PolicyDocument(
            id=pid,
            tenant_id=tid,
            authority_class=cls,
            version=version,
            title=title,
            body=body,
            effective_from=start,
            effective_to=end,
            checksum=hashlib.sha256(body.encode()).hexdigest(),
        )
    )
    await s.flush()
    for sid, heading, text in sections:
        s.add(
            PolicyChunk(
                id=ids("chunk"), tenant_id=tid, policy_document_id=pid, section_id=sid, heading=heading, text=text
            )
        )
    s.add(PolicyRule(id=ids("rule"), tenant_id=tid, policy_document_id=pid, **rule))


@dataclass(frozen=True)
class Seeded:
    case_id: str
    tenant_id: str
    family: str
    variant: str
    api_key: str
    read_key: str
    label: dict[str, Any]


def terms_at(tenant: str, at: datetime) -> Terms:
    return next(
        t for t in TENANTS[tenant].terms if t.effective_from <= at and (t.effective_to is None or at < t.effective_to)
    )


async def materialize(
    spec: Spec,
    sessions: async_sessionmaker,
    sim: httpx.AsyncClient,
    op: dict[str, str],
    evals: AsyncEngine,
    now: datetime,
    split: str,
    ids: Callable[[str], str],
    prefix: str = "",
) -> Seeded:
    v, amount = spec.variant, spec.amount
    placed = now - timedelta(days=spec.placed_days_ago)
    terms = terms_at(spec.tenant, placed)
    limit = TENANTS[spec.tenant].approve_limit

    async with sessions() as s, s.begin():
        t = await ensure_tenant(s, sim, op, spec.tenant, ids, prefix)
    tid = t["tenant_id"]
    await sim.post("/sim/clock", json={"freeze_at": now.isoformat()}, headers=op)

    async def pi(amt: int) -> str:
        r = await sim.post(
            "/sim/payment_intents", json={"id": ids("pi"), "account": t["account"], "amount": amt}, headers=op
        )
        r.raise_for_status()
        return r.json()["id"]

    cus, other, order, case_id = ids("cus"), ids("cus"), ids("ord"), ids("case")
    pays: list[tuple[str, str, int, int]] = []  # (payment_id, pi, amount, minutes after placing)
    lines: list[tuple[str, str, str, int, int]] = []  # (line_id, sku, kind, qty, unit)
    returns: list[tuple[str, str, int, bool]] = []  # (return_id, line_id, qty, received)
    sub: dict[str, Any] | None = None
    preexisting: list[str] = []
    settled: dict[str, int] = {}  # provider payment id -> amount a pre-existing refund already took
    fam = spec.family
    expect: dict[str, Any] = {"disposition": "DENY", "resolution": "DENIED", "case_state": "CLOSED", "financial": None}

    if fam == "duplicate_charge":
        second = amount if v != "distinct" else 1200
        pays = [(ids("pay"), await pi(amount), amount, 0), (ids("pay"), await pi(second), second, 3)]
        if v in ("refund", "over_limit"):
            expect = _refund(pays[1], amount, f"dup:{order}:{pays[1][0]}")
            if amount > limit:
                expect["requires_approval"] = True  # ends refunded, but only through an approval
        elif v == "already_refunded":
            preexisting.append(await _provider_refund(sim, op, t, pays[1][1], amount))
            settled[pays[1][1]] = amount
            expect = {
                "disposition": "ALREADY_REFUNDED",
                "resolution": "ALREADY_REFUNDED",
                "case_state": "CLOSED",
                "financial": None,
            }
    elif fam == "missing_refund":
        pays = [(ids("pay"), await pi(amount), amount, 0)]
        if v in ("succeeded", "pending"):
            preexisting.append(await _provider_refund(sim, op, t, pays[0][1], amount, pending=v == "pending"))
            if v == "succeeded":
                settled[pays[0][1]] = amount
                expect = {
                    "disposition": "ALREADY_REFUNDED",
                    "resolution": "ALREADY_REFUNDED",
                    "case_state": "CLOSED",
                    "financial": None,
                }
            else:
                # pending can still fail and nothing watches a refund recon didn't send, so not a close (spec §330)
                expect = {"disposition": "ESCALATE", "resolution": None, "case_state": "ESCALATED", "financial": None}
        else:
            expect = {
                "disposition": "REQUEST_INFORMATION",
                "resolution": None,
                "case_state": "AWAITING_INFO",
                "financial": None,
                "expects_information": True,
                # a refund promised by support only a person can check, so handing it to one is fair too (e1 audit)
                "also_accept": [{"disposition": "ESCALATE", "resolution": None, "case_state": "ESCALATED"}],
            }
    elif fam in ("partial_return", "ineligible"):
        unit = amount
        qty_bought = 2
        lines = [(ids("line"), "SKU-1", "item", qty_bought, unit), (ids("line"), "SHIP", "shipping", 1, 500)]
        if v == "two_lines":
            lines.append((ids("line"), "SKU-2", "item", 1, 1500))
        total = sum(q * u for _, _, _, q, u in lines)
        pays = [(ids("pay"), await pi(total), total, 0)]
        qty = spec.extra["qty"] if v != "two_lines" else 1
        target_line = lines[0] if v != "two_lines" else lines[2]
        returns = [(ids("ret"), target_line[0], qty, v != "not_received")]
        owed = target_line[4] * qty
        if fam == "partial_return" and v in ("received", "two_lines"):
            expect = _refund(pays[0], owed, f"return:{order}:{returns[0][0]}")
            if owed > limit:
                expect["requires_approval"] = True  # ends refunded, but only through an approval
        if v == "not_received":
            # nothing is owed yet: denying it or waiting for the parcel are both fair (decided 2026-10-19)
            expect["also_accept"] = [
                {"disposition": "REQUEST_INFORMATION", "resolution": None, "case_state": "AWAITING_INFO"}
            ]
        # not_received, window and returns_off all stay DENY: nothing is owed yet or at all
    elif fam == "proration":
        price = amount
        start = placed
        sub = {
            "id": ids("sub"),
            "start": start,
            "end": start + timedelta(days=30),
            "price": price,
            "cancelled": start + timedelta(days=spec.extra["cancel_day"], hours=6)
            if v != "after_period"
            else start + timedelta(days=31),
        }
        pays = [(ids("pay"), await pi(price), price, 0)]
        if v == "mid_period" and terms.proration:
            used = spec.extra["cancel_day"] + 1  # any started day counts as used
            owed = rounded(price * (30 - used), 30, terms.rounding)
            expect = _refund(pays[0], owed, f"prorate:{sub['id']}:{sub['end'].date().isoformat()}")
    elif fam == "message_matters":
        # escalate either way: a bank dispute pauses refunds by policy, a return that disagrees with the
        # customer needs a person. waiting on the customer is also fair
        expect = {
            "disposition": "ESCALATE",
            "resolution": None,
            "case_state": "ESCALATED",
            "financial": None,
            "also_accept": [{"disposition": "REQUEST_INFORMATION", "resolution": None, "case_state": "AWAITING_INFO"}],
        }
        if v == "disputed_with_bank":
            pays = [(ids("pay"), await pi(amount), amount, 0), (ids("pay"), await pi(amount), amount, 3)]
        else:
            lines = [(ids("line"), "SKU-1", "item", 2, amount), (ids("line"), "SHIP", "shipping", 1, 500)]
            total = sum(q * u for _, _, _, q, u in lines)
            pays = [(ids("pay"), await pi(total), total, 0)]
            # the warehouse logged both units, the customer says they sent one
            returns = [(ids("ret"), lines[0][0], 2, True)]
    elif fam == "missing_or_conflicting":
        pays = [(ids("pay"), await pi(amount), amount, 0)]
        if v == "no_order":
            expect = {
                "disposition": "REQUEST_INFORMATION",
                "resolution": None,
                "case_state": "AWAITING_INFO",
                "financial": None,
                "expects_information": True,
            }
        else:
            expect = {"disposition": "ESCALATE", "resolution": None, "case_state": "ESCALATED", "financial": None}

    async with sessions() as s, s.begin():
        s.add(Customer(id=cus, tenant_id=tid, email=f"{cus}@{spec.tenant}.example", name="Jo Rivera"))
        s.add(Customer(id=other, tenant_id=tid, email=f"{other}@{spec.tenant}.example", name="Sam Okafor"))
        await s.flush()
        # wrong_customer: the order and its payment belong to someone else than the person asking
        owner = other if v == "wrong_customer" else cus
        total = sum(a for _, _, a, _ in pays)
        s.add(Order(id=order, tenant_id=tid, customer_id=owner, total_minor=total, currency="usd", placed_at=placed))
        await s.flush()
        for pid, pi_id, amt, minutes in pays:
            s.add(
                Payment(
                    id=pid,
                    tenant_id=tid,
                    order_id=order,
                    customer_id=owner,
                    provider_payment_id=pi_id,
                    amount_minor=amt,
                    currency="usd",
                    status="succeeded",
                    captured_at=placed + timedelta(minutes=minutes),
                    refunded_minor=settled.get(pi_id, 0),
                )
            )
        for lid, sku, kind, qty, unit in lines:
            s.add(
                OrderLine(
                    id=lid,
                    tenant_id=tid,
                    order_id=order,
                    sku=sku,
                    description=sku.lower(),
                    kind=kind,
                    quantity=qty,
                    unit_minor=unit,
                    currency="usd",
                )
            )
        await s.flush()
        for rid, lid, qty, received in returns:
            s.add(
                Return(
                    id=rid,
                    tenant_id=tid,
                    order_id=order,
                    line_id=lid,
                    quantity=qty,
                    requested_at=now - timedelta(days=1),
                    received_at=now - timedelta(hours=6) if received else None,
                )
            )
        if sub:
            s.add(
                Subscription(
                    id=sub["id"],
                    tenant_id=tid,
                    customer_id=owner,
                    order_id=order,
                    plan_name="Monthly",
                    period_start=sub["start"],
                    period_end=sub["end"],
                    price_minor=sub["price"],
                    currency="usd",
                    cancelled_at=sub["cancelled"],
                )
            )
        s.add(
            Case(
                id=case_id,
                tenant_id=tid,
                customer_id=cus,
                order_id=None if v == "no_order" else order,
                subject=spec.subject,
                state="OPEN",
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            CaseMessage(
                id=ids("msg"), tenant_id=tid, case_id=case_id, author="customer", body=spec.message, created_at=now
            )
        )

    label = {
        "tenant_id": tid,
        "customer_id": cus,
        "order_id": None if v == "no_order" else order,
        "family": fam,
        "variant": v,
        **expect,
        "requires_approval": expect.get("requires_approval", False),
        "expects_information": expect.get("expects_information", False),
        "case_payment_intents": [p[1] for p in pays],
        "preexisting_refunds": preexisting,
    }
    async with evals.begin() as c:
        await c.execute(labels.insert().values(case_id=case_id, tenant_id=tid, split=split, family=fam, label=label))
    return Seeded(case_id, tid, fam, v, t["api_key"], t["read_key"], label)


def _refund(pay: tuple[str, str, int, int], amount: int, ref: str) -> dict[str, Any]:
    return {
        "disposition": "REFUND",
        "resolution": "REFUNDED",
        "case_state": "CLOSED",
        "financial": {
            "payment_id": pay[0],
            "provider_payment_id": pay[1],
            "entitlement_ref": ref,
            "amount_minor": amount,
            "currency": "usd",
        },
    }


async def _provider_refund(
    sim: httpx.AsyncClient, op: dict[str, str], t: dict[str, Any], pi_id: str, amount: int, pending: bool = False
) -> str:
    # a refund issued before the ticket, e.g. by support in the provider dashboard
    if pending:
        r = await sim.post(
            "/sim/faults",
            json={
                "account": t["account"],
                "kind": "pending",
                "count": 1,
                "payment_intent": pi_id,
                "params": {"after_seconds": 3 * 86400, "then": "succeeded"},
            },
            headers=op,
        )
        r.raise_for_status()
    r = await sim.post(
        "/v1/refunds",
        data={"payment_intent": pi_id, "amount": str(amount)},
        headers={"Authorization": f"Bearer {t['api_key']}", "Idempotency-Key": f"pre_{pi_id}"},
    )
    r.raise_for_status()
    return r.json()["id"]
