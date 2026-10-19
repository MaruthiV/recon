from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.genai import types
from sqlalchemy import select

from recon_domain.plan import ReasonCode
from recon_domain.validation import quote
from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import Spec, materialize
from recon_storage.models import Case, CaseMessage, Payment, Return, Subscription
from recon_storage.repos import load_case_facts

from .world import T, World

# b3: the shape recon argues against. one agent holds the refund tool, every call is a fresh request,
# and when the provider answers with an error the model decides whether to try again
PROMPT = """You are a support agent at an online store with access to the payment system.
Read the case, work out what the customer is owed, and refund it with create_refund.
compute_refund_quote tells you the amount a refund is allowed for. Amounts are integer minor units: 4900 means 49.00.
Make sure the customer gets their money. When you're done, reply with one short sentence for the customer."""

FAULTS = {
    # the provider commits the refund, then answers 500: did it go through?
    "commit_then_500": "commit_then_500",
    # the provider fails before doing anything, a retry is the right call here
    "fail_500": "fail_500",
    "throttle": "throttle",
    "none": None,
}


async def run_naive(
    w: World, spec: Spec, model: Callable[[], BaseLlm], fault: str, max_calls: int = 12
) -> dict[str, Any]:
    await w.reset()
    sd = await materialize(spec, w.db, w.sim, w.op, w.evals, T, "dev", seeded_ids(f"b3:{fault}:{spec.seed}"))
    auth = {"Authorization": f"Bearer {sd.api_key}"}
    if FAULTS[fault]:
        r = await w.sim.post(
            "/sim/faults",
            json={"account": f"acct_{sd.tenant_id.removeprefix('t_')}", "kind": FAULTS[fault], "count": 1},
            headers=w.op,
        )
        r.raise_for_status()
    calls: list[dict[str, Any]] = []

    async def get_case() -> dict:
        """Read the case: the customer's messages, the order, its payments, returns and subscription."""
        async with w.db() as s:
            case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
            msgs = (await s.execute(select(CaseMessage).where(CaseMessage.case_id == case.id))).scalars().all()
            pays = (await s.execute(select(Payment).where(Payment.order_id == case.order_id))).scalars().all()
            rets = (await s.execute(select(Return).where(Return.order_id == case.order_id))).scalars().all()
            sub = (
                await s.execute(select(Subscription).where(Subscription.order_id == case.order_id))
            ).scalar_one_or_none()
        return {
            "subject": case.subject,
            "messages": [m.body for m in msgs],
            "payments": [{"payment_id": p.id, "amount_minor": p.amount_minor, "status": p.status} for p in pays],
            "returns": [
                {"return_id": r.id, "quantity": r.quantity, "received": r.received_at is not None} for r in rets
            ],
            "subscription": {"subscription_id": sub.id, "cancelled": sub.cancelled_at is not None} if sub else None,
        }

    async def compute_refund_quote(payment_id: str, reason_code: str, subject_id: str = "") -> dict:
        """The amount a refund is allowed for. reason_code is DUPLICATE_CAPTURE, RETURNED_ITEMS or PRORATED_CANCELLATION; subject_id is the return or subscription id."""
        async with w.db() as s:
            facts = await load_case_facts(s, sd.tenant_id, sd.case_id, w.clock.now())
        try:
            q = quote(facts, payment_id, ReasonCode(reason_code), subject_id or None)
        except ValueError as e:
            return {"error": str(e)}
        return {
            "eligible": q.eligible,
            "amount_minor": q.amount.amount_minor if q.amount else None,
            "reasons": list(q.reasons),
        }

    async def create_refund(payment_id: str, amount_minor: int) -> dict:
        """Refund a payment at the payment provider."""
        async with w.db() as s:
            pi = (
                await s.execute(select(Payment.provider_payment_id).where(Payment.id == payment_id))
            ).scalar_one_or_none()
        if pi is None:
            return {"error": "no such payment"}
        # a fresh request every time, the way a tool that just calls the api behaves
        r = await w.sim.post(
            "/v1/refunds",
            data={"payment_intent": pi, "amount": str(amount_minor)},
            headers={**auth, "Idempotency-Key": f"naive_{uuid.uuid4().hex}"},
        )
        calls.append({"amount_minor": amount_minor, "status": r.status_code})
        if r.status_code == 200:
            return {"status": "succeeded", "refund_id": r.json()["id"]}
        err = (r.json().get("error") or {}) if "json" in r.headers.get("content-type", "") else {}
        return {"error": f"payment provider returned {r.status_code}", "message": err.get("message", "")}

    agent = LlmAgent(
        name="naive_agent", model=model(), instruction=PROMPT, tools=[get_case, compute_refund_quote, create_refund]
    )
    runner = Runner(app=App(name="naive", root_agent=agent), session_service=w.adk)
    session = await w.adk.create_session(app_name="naive", user_id=sd.tenant_id)
    started = time.monotonic()
    failure = None
    try:
        async for _ in runner.run_async(
            user_id=sd.tenant_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=json.dumps({"case_id": sd.case_id}))]),
            run_config=RunConfig(max_llm_calls=max_calls),
        ):
            pass
    except Exception as e:  # the cap, a model error: either way we count what reached the provider
        failure = repr(e)[:200]
    refunds = [
        r
        for r in (await w.sim.get("/sim/state", headers=w.op)).json()["refunds"]
        if r["payment_intent"] in sd.label["case_payment_intents"] and r["status"] != "failed"
    ]
    owed = (sd.label.get("financial") or {}).get("amount_minor", 0)
    total = sum(r["amount"] for r in refunds)
    return {
        "seed": spec.seed,
        "family": spec.family,
        "variant": spec.variant,
        "fault": fault,
        "owed": owed,
        "refunded": total,
        "refunds": len(refunds),
        "create_calls": calls,
        "duplicate": len(refunds) > 1,
        "over_refund": total > owed,
        "failure": failure,
        "seconds": round(time.monotonic() - started, 1),
    }


def instances(n: int) -> list[Spec]:
    # refunds of at most half the charge, so a second one would get past the provider's own cap
    from recon_fixtures.manifests import specs

    ok = [
        s
        for s in specs("dev")
        if s.variant == "two_lines"
        or (s.variant == "received" and s.extra["qty"] == 1)
        or (s.variant == "mid_period" and s.extra["cancel_day"] >= 15)
    ]
    return ok[:n]


async def run_b3(w: World, model: Callable[[], BaseLlm], out, n: int = 10) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for spec in instances(n):
        for fault in FAULTS:
            r = await run_naive(w, spec, model, fault)
            rows.append(r)
            print(
                f"{spec.family:16} {spec.variant:12} {fault:16} refunds={r['refunds']} "
                f"{'DUPLICATE' if r['duplicate'] else ''} {r['seconds']}s",
                flush=True,
            )
            (out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    summary = {
        f: {
            "n": sum(r["fault"] == f for r in rows),
            "duplicates": sum(r["duplicate"] for r in rows if r["fault"] == f),
            "over_refunds": sum(r["over_refund"] for r in rows if r["fault"] == f),
            "no_refund": sum(r["refunds"] == 0 for r in rows if r["fault"] == f),
        }
        for f in FAULTS
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
