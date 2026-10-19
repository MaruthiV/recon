from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from sqlalchemy import select

from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import Spec, materialize
from recon_fixtures.manifests import specs
from recon_storage.models import (
    Case,
    CaseMessage,
    Order,
    OrderLine,
    Payment,
    PolicyDocument,
    PolicyRule,
    Return,
    Subscription,
)

from .world import T, World

# variants where a reasonable person could argue with the label, every one gets a look
TRICKY = (
    "not_received",
    "disputed_with_bank",
    "quantity_conflict",
    "pending",
    "never_issued",
    "wrong_customer",
    "after_period",
    "two_lines",
    "over_limit",
    "late",
)


def sample(split: str = "dev", n: int = 14, seed: int = 11) -> list[Spec]:
    pool = specs(split)
    picked = random.Random(seed).sample(pool, n)
    for v in TRICKY:
        if not any(s.variant == v for s in picked):
            picked.append(next(s for s in pool if s.variant == v))
    return picked


def money(m: int | None) -> str:
    return "none" if m is None else f"{m // 100}.{m % 100:02d}"


async def view(w: World, spec: Spec) -> dict[str, Any]:
    await w.reset()
    sd = await materialize(spec, w.db, w.sim, w.op, w.evals, T, "dev", seeded_ids(f"audit:{spec.seed}"))
    async with w.db() as s:
        case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
        msgs = (await s.execute(select(CaseMessage.body).where(CaseMessage.case_id == case.id))).scalars().all()
        order_id = sd.label.get("order_id")
        order = (await s.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none() if order_id else None
        pays = (
            (await s.execute(select(Payment).where(Payment.order_id == order_id))).scalars().all() if order_id else []
        )
        lines = (
            (await s.execute(select(OrderLine).where(OrderLine.order_id == order_id))).scalars().all()
            if order_id
            else []
        )
        rets = (await s.execute(select(Return).where(Return.order_id == order_id))).scalars().all() if order_id else []
        sub = (
            (await s.execute(select(Subscription).where(Subscription.order_id == order_id))).scalar_one_or_none()
            if order_id
            else None
        )
        docs = (await s.execute(select(PolicyDocument).where(PolicyDocument.tenant_id == sd.tenant_id))).scalars().all()
        rules = (await s.execute(select(PolicyRule).where(PolicyRule.tenant_id == sd.tenant_id))).scalars().all()
    provider = (await w.sim.get("/sim/state", headers=w.op)).json()["refunds"]
    by_doc = {r.policy_document_id: r for r in rules}
    return {
        "seed": spec.seed,
        "family": spec.family,
        "variant": spec.variant,
        "tenant": sd.tenant_id,
        "subject": case.subject,
        "messages": list(msgs),
        "case_customer": case.customer_id,
        "case_order": case.order_id,
        "order": order
        and {"customer": order.customer_id, "total": money(order.total_minor), "placed": order.placed_at.isoformat()},
        "lines": [{"sku": li.sku, "kind": li.kind, "qty": li.quantity, "unit": money(li.unit_minor)} for li in lines],
        "returns": [
            {
                "sku": next((li.sku for li in lines if li.id == r.line_id), "?"),
                "qty": r.quantity,
                "received": r.received_at is not None,
            }
            for r in rets
        ],
        "subscription": sub
        and {
            "start": sub.period_start.isoformat(),
            "end": sub.period_end.isoformat(),
            "price": money(sub.price_minor),
            "cancelled": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
        },
        "payments": [
            {
                "amount": money(p.amount_minor),
                "status": p.status,
                "captured": p.captured_at.isoformat(),
                "recon_refunded": money(p.refunded_minor),
            }
            for p in pays
        ],
        "provider_refunds": [{"amount": money(r["amount"]), "status": r["status"]} for r in provider],
        "policies": [
            {
                "class": d.authority_class,
                "version": d.version,
                "from": d.effective_from.date().isoformat(),
                "to": d.effective_to.date().isoformat() if d.effective_to else None,
                "rule": {
                    k: getattr(by_doc[d.id], k)
                    for k in (
                        "refund_window_days",
                        "returns_allowed",
                        "shipping_refundable",
                        "proration_allowed",
                        "rounding_mode",
                        "auto_approve_limit_minor",
                    )
                    if d.id in by_doc and getattr(by_doc[d.id], k) is not None
                },
            }
            for d in docs
        ],
        "business_time": T.isoformat(),
        "label": {
            k: sd.label.get(k)
            for k in ("disposition", "resolution", "case_state", "financial", "also_accept", "requires_approval")
        },
    }


async def collect(w: World, split: str = "dev", n: int = 14) -> list[dict[str, Any]]:
    return [await view(w, s) for s in sample(split, n)]


def save(views: list[dict[str, Any]], out: Path, split: str = "dev") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"audit-{split}.json"
    path.write_text(json.dumps(views, indent=2, default=str) + "\n")
    return path
