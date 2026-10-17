from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from recon_storage.db import new_id
from recon_storage.models import (
    Case,
    CaseMessage,
    Customer,
    Order,
    Payment,
    PolicyDocument,
    PolicyRule,
    Tenant,
)
from recon_verifier.db import labels

MESSAGE = "I was charged twice for the same order. Please return the extra payment."
TERMS_TEXT = (
    "Refund terms v1. Section dup: if an order was captured more than once for the same amount, "
    "the extra capture is refunded in full within 30 days of the charge."
)
LIMITS_TEXT = "Operational limits v1. Refunds up to 100.00 USD are approved automatically."


@dataclass(frozen=True)
class SeededCase:
    tenant_id: str
    api_key: str
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
) -> SeededCase:
    tenant_id, account, api_key = f"t_{tenant}", f"acct_{tenant}", f"sk_test_{tenant}"
    await sim.post("/sim/accounts", json={"id": account, "api_key": api_key}, headers=sim_operator)
    await sim.post("/sim/clock", json={"freeze_at": now.isoformat()}, headers=sim_operator)
    pis = []
    for _ in range(2):
        r = await sim.post(
            "/sim/payment_intents", json={"account": account, "amount": amount_minor}, headers=sim_operator
        )
        r.raise_for_status()
        pis.append(r.json()["id"])

    placed = now - timedelta(days=2)
    cus, order = new_id("cus"), new_id("ord")
    pay_a, pay_b = new_id("pay"), new_id("pay")
    terms, limits, case_id = new_id("pol"), new_id("pol"), new_id("case")
    async with sessions() as s, s.begin():
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
        for pid, cls, title, body in (
            (terms, "refund_terms", "Refund terms", TERMS_TEXT),
            (limits, "operational_limits", "Operational limits", LIMITS_TEXT),
        ):
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
        s.add(
            PolicyRule(
                id=new_id("rule"),
                tenant_id=tenant_id,
                policy_document_id=terms,
                refund_window_days=30,
                duplicate_refund_allowed=True,
                rounding_mode="HALF_EVEN",
            )
        )
        s.add(
            PolicyRule(
                id=new_id("rule"), tenant_id=tenant_id, policy_document_id=limits, auto_approve_limit_minor=10_000
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
                id=new_id("msg"), tenant_id=tenant_id, case_id=case_id, author="customer", body=MESSAGE, created_at=now
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
        "requires_approval": False,
        "expects_information": False,
        "case_payment_intents": pis,
    }
    async with eval_engine.begin() as c:
        await c.execute(
            labels.insert().values(
                case_id=case_id, tenant_id=tenant_id, split=split, family="duplicate_charge", label=label
            )
        )
    return SeededCase(tenant_id, api_key, account, case_id, cus, order, pay_a, pay_b, pis[1], pis[0], terms, limits)
