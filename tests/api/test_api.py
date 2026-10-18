from types import SimpleNamespace

import httpx
import pytest

from recon_api.app import create_app
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline
from tests.conftest import T

KEY = b"test-session-key"


@pytest.fixture
async def api(app_sessions, flaky_sim, op, verifier_engines, clock):
    acme = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, tenant="acme")
    globex = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, tenant="globex")
    app = create_app(app_sessions, clock, KEY, dev=True, faults_enabled=True)
    conn = HttpConnector(flaky_sim, acme.api_key)
    worker = Pipeline(
        app_sessions, clock, Executor(app_sessions, clock, lambda _t: conn), worker_id="w", reconcile_backoff_seconds=0
    )

    async def drain():
        disp = Dispatcher(app_sessions, lambda d: _adv(worker, d))
        for _ in range(10):
            if await disp.pump_once() == 0:
                return

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
        yield SimpleNamespace(c=c, acme=acme, globex=globex, drain=drain, app=app)


async def _adv(w, d):
    return (await w.advance(d.run_id, d.stage, d.event_id)).status


async def login(c, tenant="t_acme", role="operator"):
    r = await c.post("/v1/login", json={"user": "maruthi", "tenant_id": tenant, "role": role})
    assert r.status_code == 200


async def test_everything_needs_a_session(api):
    for path in ("/v1/me", "/v1/cases", f"/v1/cases/{api.acme.case_id}", f"/v1/cases/{api.acme.case_id}/events"):
        assert (await api.c.get(path)).status_code == 401


async def test_cases_are_tenant_scoped_and_guessed_ids_404(api):
    await login(api.c)
    cases = (await api.c.get("/v1/cases")).json()
    assert [c["id"] for c in cases] == [api.acme.case_id]
    assert (await api.c.get(f"/v1/cases/{api.globex.case_id}")).status_code == 404
    assert (await api.c.get(f"/v1/cases/{api.globex.case_id}/events")).status_code == 404
    r = await api.c.post(f"/v1/cases/{api.globex.case_id}/investigations", json={"mode": "rules"})
    assert r.status_code == 404
    detail = (await api.c.get(f"/v1/cases/{api.acme.case_id}")).json()
    assert len(detail["payments"]) == 2 and detail["plan"] is None and detail["commands"] == []


async def test_rules_investigation_runs_once_and_the_timeline_follows(api):
    await login(api.c)
    cid = api.acme.case_id
    r = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules"})
    assert r.status_code == 202
    again = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules"})
    assert again.status_code == 409  # a double click can't start a second run
    await api.drain()

    detail = (await api.c.get(f"/v1/cases/{cid}")).json()
    assert (detail["case"]["state"], detail["case"]["resolution"]) == ("CLOSED", "REFUNDED")
    assert detail["plan"]["source"] == "rules" and detail["plan"]["decision"] == "AUTHORIZE"
    [cmd] = detail["commands"]
    assert cmd["state"] == "CONFIRMED_SUCCESS" and cmd["provider_refund_id"]

    first = (await api.c.get(f"/v1/cases/{cid}/events")).json()
    kinds = [e["kind"] for e in first["events"]]
    assert "command_confirmed_success" in kinds
    assert [e["id"] for e in first["events"]] == sorted(e["id"] for e in first["events"])
    rest = (await api.c.get(f"/v1/cases/{cid}/events", params={"after": first["cursor"]})).json()
    assert rest["events"] == [] and rest["cursor"] == first["cursor"]


def sse_ids(body: str) -> list[int]:
    return [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]


async def test_sse_resumes_from_last_event_id(api):
    await login(api.c)
    cid = api.acme.case_id
    await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules"})
    await api.drain()
    url = f"/v1/cases/{cid}/events/stream"
    everything = sse_ids((await api.c.get(url, params={"until_idle": True})).text)
    assert len(everything) > 3
    mid = everything[len(everything) // 2]
    resumed = sse_ids((await api.c.get(url, params={"until_idle": True}, headers={"Last-Event-ID": str(mid)})).text)
    assert resumed == [i for i in everything if i > mid]


async def test_login_and_fault_controls_are_dev_only(app_sessions, clock, api):
    public = create_app(app_sessions, clock, KEY, dev=False, faults_enabled=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://api") as c:
        assert (await c.post("/v1/login", json={"user": "x", "tenant_id": "t_acme"})).status_code == 404
    await login(api.c)
    assert (await api.c.post("/internal/demo/faults", json={"point": "FP4"})).status_code == 200
    quiet = create_app(app_sessions, clock, KEY, dev=True, faults_enabled=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=quiet), base_url="http://api") as c:
        await login(c)
        assert (await c.post("/internal/demo/faults", json={"point": "FP4"})).status_code == 404


async def test_only_a_supervisor_approves_and_only_the_plan_they_saw(
    app_sessions, flaky_sim, op, verifier_engines, clock
):
    from recon_fixtures.dup_charge import scripted_plan

    sd = await seed_duplicate_charge(
        app_sessions, flaky_sim, op, verifier_engines["eval"], T, tenant="hooli", amount_minor=25_000
    )
    app = create_app(app_sessions, clock, KEY, dev=True, faults_enabled=False)
    conn = HttpConnector(flaky_sim, sd.api_key)
    worker = Pipeline(app_sessions, clock, Executor(app_sessions, clock, lambda _t: conn), worker_id="w")
    await worker.start_scripted(sd.tenant_id, sd.case_id, scripted_plan(sd, amount_minor=25_000))
    disp = Dispatcher(app_sessions, lambda d: _adv(worker, d))
    for _ in range(10):
        await disp.pump_once()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
        await login(c, sd.tenant_id)
        plan = (await c.get(f"/v1/cases/{sd.case_id}")).json()["plan"]
        assert plan["decision"] == "NEEDS_APPROVAL"
        body = {"decision": "APPROVED", "plan_hash": plan["material_hash"]}
        assert (await c.post(f"/v1/plans/{plan['id']}/approvals", json=body)).status_code == 403
        await login(c, sd.tenant_id, role="supervisor")
        stale = {"decision": "APPROVED", "plan_hash": "0" * 64}
        assert (await c.post(f"/v1/plans/{plan['id']}/approvals", json=stale)).status_code == 409
        assert (await c.post("/v1/plans/plan_nope/approvals", json=body)).status_code == 404
        assert (await c.post(f"/v1/plans/{plan['id']}/approvals", json=body)).status_code == 202
    for _ in range(10):
        await disp.pump_once()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
        await login(c, sd.tenant_id)
        case = (await c.get(f"/v1/cases/{sd.case_id}")).json()["case"]
    assert (case["state"], case["resolution"]) == ("CLOSED", "REFUNDED")
