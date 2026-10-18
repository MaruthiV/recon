from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from recon_domain.plan import ResolutionPlan
from recon_storage.db import new_id
from recon_storage.models import (
    Case,
    CaseMessage,
    Customer,
    Order,
    Payment,
    PolicyChunk,
    PolicyDocument,
    PolicyRule,
    Tenant,
)
from recon_verifier.db import labels

MESSAGE = "I was charged twice for the same order. Please return the extra payment."
TERMS_SECTIONS = (
    (
        "dup",
        "Duplicate charges",
        "If an order was captured more than once for the same amount, the extra capture is refunded in full.",
    ),
    ("window", "Refund window", "Refund requests are accepted within 30 days of the charge."),
    (
        "returns",
        "Returned items",
        "Returned items are refunded at the item price once the return is received. Shipping is not refunded.",
    ),
)
LIMITS_SECTIONS = (
    (
        "auto_approve",
        "Automatic approval",
        "Refunds up to 100.00 USD are approved automatically. Larger refunds need a supervisor.",
    ),
)


def body_of(sections: tuple[tuple[str, str, str], ...]) -> str:
    return "\n\n".join(f"{heading}. {text}" for _, heading, text in sections)


def seeded_ids(seed: str) -> Callable[[str], str]:
    # deterministic ids so recorded model runs replay against the same rows
    n = itertools.count()
    return lambda prefix: f"{prefix}_{hashlib.sha256(f'{seed}:{prefix}:{next(n)}'.encode()).hexdigest()[:24]}"


@dataclass(frozen=True)
class SeededCase:
    tenant_id: str
    api_key: str
    read_key: str
    account: str
    case_id: str
    customer_id: str
    order_id: str
    original_payment_id: str
    duplicate_payment_id: str
    duplicate_provider_id: str
    original_provider_id: str
    terms_policy_id: str
    limits_policy_id: str


async def seed_duplicate_charge(
    sessions: async_sessionmaker,
    sim: httpx.AsyncClient,
    sim_operator: dict[str, str],
    eval_engine: AsyncEngine,
    now: datetime,
    tenant: str = "acme",
    amount_minor: int = 4900,
    split: str = "dev",
    ids: Callable[[str], str] = new_id,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> SeededCase:
    tenant_id, account = f"t_{tenant}", f"acct_{tenant}"
    api_key, read_key = f"sk_test_{tenant}", f"rk_test_{tenant}"
    r = await sim.post(
        "/sim/accounts",
        json={
            "id": account,
            "api_key": api_key,
            "read_key": read_key,
            "webhook_url": webhook_url,
            "webhook_secret": webhook_secret,
        },
        headers=sim_operator,
    )
    r.raise_for_status()
    await sim.post("/sim/clock", json={"freeze_at": now.isoformat()}, headers=sim_operator)
    pis = []
    for _ in range(2):
        r = await sim.post(
            "/sim/payment_intents",
            json={"id": ids("pi"), "account": account, "amount": amount_minor},
            headers=sim_operator,
        )
        r.raise_for_status()
        pis.append(r.json()["id"])

    placed = now - timedelta(days=2)
    cus, order = ids("cus"), ids("ord")
    pay_a, pay_b = ids("pay"), ids("pay")
    terms, limits, case_id = ids("pol"), ids("pol"), ids("case")
    async with sessions() as s, s.begin():
        # a tenant seeded before keeps its policies, so the dev stack can add one demo case per start
        known = (await s.execute(select(Tenant.id).where(Tenant.id == tenant_id))).scalar() is not None
        if not known:
            s.add(Tenant(id=tenant_id, name=tenant.title(), provider_account=account, created_at=placed))
            await s.flush()
        s.add(Customer(id=cus, tenant_id=tenant_id, email=f"jo@{tenant}.example", name="Jo Rivera"))
        await s.flush()
        s.add(
            Order(
                id=order,
                tenant_id=tenant_id,
                customer_id=cus,
                total_minor=amount_minor,
                currency="usd",
                placed_at=placed,
            )
        )
        await s.flush()
        for pid, pi, minutes in ((pay_a, pis[0], 0), (pay_b, pis[1], 3)):
            s.add(
                Payment(
                    id=pid,
                    tenant_id=tenant_id,
                    order_id=order,
                    customer_id=cus,
                    provider_payment_id=pi,
                    amount_minor=amount_minor,
                    currency="usd",
                    status="succeeded",
                    captured_at=placed + timedelta(minutes=minutes),
                )
            )
        docs = (
            (terms, "refund_terms", "Refund terms", TERMS_SECTIONS),
            (limits, "operational_limits", "Operational limits", LIMITS_SECTIONS),
        )
        if known:
            terms, limits = [
                (
                    await s.execute(
                        select(PolicyDocument.id).where(
                            PolicyDocument.tenant_id == tenant_id,
                            PolicyDocument.authority_class == cls,
                            PolicyDocument.version == 1,
                        )
                    )
                ).scalar_one()
                for cls in ("refund_terms", "operational_limits")
            ]
            docs = ()
        for pid, cls, title, sections in docs:
            body = body_of(sections)
            s.add(
                PolicyDocument(
                    id=pid,
                    tenant_id=tenant_id,
                    authority_class=cls,
                    version=1,
                    title=title,
                    body=body,
                    effective_from=datetime(2026, 1, 1, tzinfo=now.tzinfo),
                    effective_to=None,
                    checksum=hashlib.sha256(body.encode()).hexdigest(),
                )
            )
        await s.flush()
        for pid, _, _, sections in docs:
            for section_id, heading, text in sections:
                s.add(
                    PolicyChunk(
                        id=ids("chunk"),
                        tenant_id=tenant_id,
                        policy_document_id=pid,
                        section_id=section_id,
                        heading=heading,
                        text=text,
                    )
                )
        if not known:
            s.add(
                PolicyRule(
                    id=ids("rule"),
                    tenant_id=tenant_id,
                    policy_document_id=terms,
                    refund_window_days=30,
                    duplicate_refund_allowed=True,
                    rounding_mode="HALF_EVEN",
                )
            )
            s.add(
                PolicyRule(
                    id=ids("rule"), tenant_id=tenant_id, policy_document_id=limits, auto_approve_limit_minor=10_000
                )
            )
        s.add(
            Case(
                id=case_id,
                tenant_id=tenant_id,
                customer_id=cus,
                order_id=order,
                subject="Charged twice",
                state="OPEN",
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            CaseMessage(
                id=ids("msg"), tenant_id=tenant_id, case_id=case_id, author="customer", body=MESSAGE, created_at=now
            )
        )

    label = {
        "tenant_id": tenant_id,
        "customer_id": cus,
        "order_id": order,
        "disposition": "REFUND",
        "resolution": "REFUNDED",
        "financial": {
            "payment_id": pay_b,
            "provider_payment_id": pis[1],
            "entitlement_ref": f"dup:{order}:{pay_b}",
            "amount_minor": amount_minor,
            "currency": "usd",
        },
        # the fixture's own copy of the 100.00 auto-approve limit it seeds
        "requires_approval": amount_minor > 10_000,
        "expects_information": False,
        "case_payment_intents": pis,
    }
    async with eval_engine.begin() as c:
        await c.execute(
            labels.insert().values(
                case_id=case_id, tenant_id=tenant_id, split=split, family="duplicate_charge", label=label
            )
        )
    return SeededCase(
        tenant_id, api_key, read_key, account, case_id, cus, order, pay_a, pay_b, pis[1], pis[0], terms, limits
    )


def scripted_plan(sd: SeededCase, **kw) -> ResolutionPlan:
    # the plan a careful human would write for this fixture, used where no model is involved
    base = dict(
        case_id=sd.case_id,
        case_version=1,
        recommended_disposition="REFUND",
        payment_id=sd.duplicate_payment_id,
        amount_minor=4900,
        currency="usd",
        policy_version_ids=[sd.terms_policy_id],
        evidence_refs=[
            f"payment:{sd.original_payment_id}:v1",
            f"payment:{sd.duplicate_payment_id}:v1",
            f"policy:{sd.terms_policy_id}:dup",
        ],
        reason_code="DUPLICATE_CAPTURE",
        explanation="Two captures of 4900 usd on one order, no refund exists for the second.",
    )
    return ResolutionPlan(**{**base, **kw})
