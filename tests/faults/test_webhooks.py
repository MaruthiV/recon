import json
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import AuditEvent, Case, Command, Payment, PaymentReservation, WebhookInbox
from recon_verifier.checks import verify_case
from recon_worker.app import create_app
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline
from recon_worker.webhooks import Webhooks, verify_signature
from tests.conftest import T
from tests.e2e.test_scripted_dup_charge import scripted_plan

SECRET = "whsec_test"


@pytest.fixture
async def wh(app_sessions, sim_asgi, op, verifier_engines, clock, clean):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sim_asgi), base_url="http://sim") as sim:
        sd = await seed_duplicate_charge(
            app_sessions,
            sim,
            op,
            verifier_engines["eval"],
            T,
            webhook_url="http://recon/webhooks/provider/t_acme",
            webhook_secret=SECRET,
        )
        conn = HttpConnector(sim, sd.api_key)
        ex = Executor(app_sessions, clock, lambda _t: conn)
        # polling backoff of an hour: in these tests only webhooks can move a pending refund along
        pipe = Pipeline(app_sessions, clock, ex, worker_id="w", lease_seconds=1, reconcile_backoff_seconds=3600)
        hooks = Webhooks(app_sessions, clock, ex.ledger, lambda t: SECRET if t == sd.tenant_id else None)
        recon = create_app(pipe, "tok", hooks)
        before = sim_asgi.state.webhook_client
        sim_asgi.state.webhook_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=recon), base_url="http://recon"
        )

        async def pump(times=10):
            disp = Dispatcher(app_sessions, lambda d: _adv(pipe, d), visibility_seconds=1)
            for _ in range(times):
                await disp.pump_once()

        async def one(model, *where):
            async with app_sessions() as s:
                return (await s.execute(select(model).where(*where))).scalar_one()

        async def many(model, *where):
            async with app_sessions() as s:
                return list((await s.execute(select(model).where(*where))).scalars())

        try:
            yield SimpleNamespace(
                sd=sd,
                sim=sim,
                op=op,
                pipe=pipe,
                hooks=hooks,
                recon=recon,
                pump=pump,
                one=one,
                many=many,
                verifier=verifier_engines,
            )
        finally:
            await sim_asgi.state.webhook_client.aclose()
            sim_asgi.state.webhook_client = before


async def _adv(pipe, d):
    return (await pipe.advance(d.run_id, d.stage, d.event_id)).status


async def pending_refund(wh, then):
    await wh.sim.post(
        "/sim/faults",
        json={"account": wh.sd.account, "kind": "pending", "count": 1, "params": {"after_seconds": 60, "then": then}},
        headers=wh.op,
    )
    await wh.pipe.start_scripted(wh.sd.tenant_id, wh.sd.case_id, scripted_plan(wh.sd))
    await wh.pump(2)  # PREPARE, SUBMIT
    cmd = await wh.one(Command)
    assert cmd.state == C.PENDING_PROVIDER.value
    await wh.sim.post("/sim/clock", json={"advance_seconds": 61}, headers=wh.op)
    return cmd


@pytest.mark.parametrize("mode", [["reverse", "duplicate"], ["duplicate"], ["drop_first"]])
async def test_fp8_webhook_dup_and_reorder(wh, mode):
    cmd = await pending_refund(wh, "succeeded")
    flushed = (await wh.sim.post("/sim/webhooks/flush", json={"mode": mode}, headers=wh.op)).json()
    assert all(d["status"] == 200 for d in flushed["delivered"])
    await wh.hooks.process_pending()
    await wh.pump()

    inbox = await wh.many(WebhookInbox)
    delivered = {d["event"] for d in flushed["delivered"]}
    assert len(inbox) == len(delivered)  # one row per event, however many times it arrived
    case = await wh.one(Case)
    assert (case.state, case.resolution) == (S.CLOSED.value, "REFUNDED")
    cmd = await wh.one(Command, Command.id == cmd.id)
    assert cmd.state == C.CONFIRMED_SUCCESS.value
    states = [a.kind for a in await wh.many(AuditEvent) if a.kind.startswith("command_")]
    # never went backwards, whatever order the events came in
    assert states == ["command_prepared", "command_pending_provider", "command_confirmed_success"]
    e = wh.verifier
    verdict = await verify_case(e["recon"], e["sim"], e["eval"], wh.sd.case_id)
    assert verdict.passed, verdict.failed()


async def test_fp8_failed_refund_releases_money_and_a_stale_event_cant_revive_it(wh):
    cmd = await pending_refund(wh, "failed")
    await wh.sim.post("/sim/webhooks/flush", json={"mode": ["reverse", "duplicate"]}, headers=wh.op)
    await wh.hooks.process_pending()
    await wh.pump()

    cmd = await wh.one(Command, Command.id == cmd.id)
    assert cmd.state == C.CONFIRMED_FAILURE.value
    res = await wh.one(PaymentReservation)
    assert res.state == "RELEASED"
    pay = await wh.one(Payment, Payment.id == wh.sd.duplicate_payment_id)
    assert (pay.refunded_minor, pay.reserved_minor) == (0, 0)
    assert (await wh.one(Case)).state == S.ESCALATED.value
    outcomes = [r.outcome for r in await wh.many(WebhookInbox)]
    assert outcomes[0].startswith("refreshed PENDING_PROVIDER -> CONFIRMED_FAILURE")
    assert outcomes[1] == "already CONFIRMED_FAILURE"


async def test_webhook_signature_tolerance_and_dedupe(wh):
    body = json.dumps({"id": "evt_1", "type": "refund.updated", "data": {"object": {"object": "refund"}}}).encode()
    now = int(T.timestamp())

    async def post(sig, payload=body):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wh.recon), base_url="http://recon") as c:
            r = await c.post("/webhooks/provider/t_acme", content=payload, headers={"Stripe-Signature": sig})
        return r.status_code

    from provider_sim.app import sign

    assert await post(sign("whsec_wrong", now, body)) == 400
    assert await post(sign(SECRET, now - 301, body)) == 400  # replayed past the 5 minute tolerance
    assert await post(sign(SECRET, now, body), body + b" ") == 400  # body changed after signing
    assert await post(f"t={now},v0=" + sign(SECRET, now, body).split("v1=")[1]) == 400  # only v1 counts
    assert await wh.many(WebhookInbox) == []

    assert await post(sign(SECRET, now, body)) == 200
    assert await post(sign(SECRET, now, body)) == 200
    assert len(await wh.many(WebhookInbox)) == 1


def test_verify_signature_accepts_any_matching_v1_during_secret_rollover():
    body, now = b"{}", int(time.time())
    from datetime import UTC, datetime

    from provider_sim.app import sign

    old, new = sign("whsec_old", now, body), sign(SECRET, now, body)
    header = f"t={now},v1={old.split('v1=')[1]},v1={new.split('v1=')[1]}"
    assert verify_signature(header, body, SECRET, datetime.fromtimestamp(now, UTC))
