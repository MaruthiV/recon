import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from recon_api.app import create_app
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_verifier.checks import verify_case
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

    async with client(app) as c:
        yield SimpleNamespace(c=c, acme=acme, globex=globex, drain=drain, app=app)


async def _adv(w, d):
    return (await w.advance(d.run_id, d.stage, d.event_id)).status


async def with_key(request: httpx.Request) -> None:
    # every write gets a fresh key unless the test set one (even an empty one) itself
    if request.method == "POST" and "idempotency-key" not in request.headers:
        request.headers["Idempotency-Key"] = f"test-{uuid.uuid4()}"


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", event_hooks={"request": [with_key]}
    )


async def version(c, cid) -> int:
    return (await c.get(f"/v1/cases/{cid}")).json()["case"]["version"]


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
    r = await api.c.post(f"/v1/cases/{api.globex.case_id}/investigations", json={"mode": "rules", "case_version": 1})
    assert r.status_code == 404
    detail = (await api.c.get(f"/v1/cases/{api.acme.case_id}")).json()
    assert len(detail["payments"]) == 2 and detail["plan"] is None and detail["commands"] == []


async def test_rules_investigation_runs_once_and_the_timeline_follows(api):
    await login(api.c)
    cid = api.acme.case_id
    r = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 1})
    assert r.status_code == 202
    again = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 1})
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
    await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 1})
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
    async with client(quiet) as c:
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

    async with client(app) as c:
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
    async with client(app) as c:
        await login(c, sd.tenant_id)
        case = (await c.get(f"/v1/cases/{sd.case_id}")).json()["case"]
    assert (case["state"], case["resolution"]) == ("CLOSED", "REFUNDED")


async def test_a_reply_with_the_order_picks_a_waiting_case_back_up(api, app_sessions, verifier_engines):
    cid, order = api.acme.case_id, api.acme.order_id
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE cases SET order_id = NULL WHERE id = :c"), {"c": cid})
    await login(api.c)
    # an open case just takes the message, nothing starts
    r = await api.c.post(f"/v1/cases/{cid}/messages", json={"body": "hello?", "case_version": 1})
    assert (r.status_code, r.json()["resumed"]) == (202, False)

    await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 2})
    await api.drain()
    detail = (await api.c.get(f"/v1/cases/{cid}")).json()
    assert detail["case"]["state"] == "AWAITING_INFO"
    assert detail["plan"]["payload"]["recommended_disposition"] == "REQUEST_INFORMATION"

    for wrong in ("ord_nope", api.globex.order_id):
        body = {"body": "here", "order_id": wrong, "mode": "rules", "case_version": 2}
        r = await api.c.post(f"/v1/cases/{cid}/messages", json=body)
        assert r.status_code == 409, wrong
    body = {"body": f"it's {order}", "order_id": order, "mode": "rules", "case_version": 2}
    r = await api.c.post(f"/v1/cases/{cid}/messages", json=body)
    assert (r.status_code, r.json()["resumed"]) == (202, True)
    await api.drain()

    detail = (await api.c.get(f"/v1/cases/{cid}")).json()
    assert (detail["case"]["state"], detail["case"]["resolution"]) == ("CLOSED", "REFUNDED")
    assert detail["plan"]["payload"]["case_version"] == 3  # seed, hello, the order (the wrong ones rolled back)
    e = verifier_engines
    assert (await verify_case(e["recon"], e["sim"], e["eval"], cid)).passed
    closed = await api.c.post(f"/v1/cases/{cid}/messages", json={"body": "thanks", "case_version": 3})
    assert closed.status_code == 409


async def test_writes_need_a_key_and_a_retry_gets_the_first_answer(api, app_sessions):
    await login(api.c)
    cid = api.acme.case_id
    url, body = f"/v1/cases/{cid}/investigations", {"mode": "rules", "case_version": 1}
    assert (await api.c.post(url, json=body, headers={"Idempotency-Key": ""})).status_code == 428

    first = await api.c.post(url, json=body, headers={"Idempotency-Key": "retry-me-123"})
    # the client never saw the answer and sends the same request again
    again = await api.c.post(url, json=body, headers={"Idempotency-Key": "retry-me-123"})
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    async with app_sessions() as s:
        assert (await s.execute(text("SELECT count(*) FROM runs"))).scalar_one() == 1

    other = {"mode": "agent", "case_version": 1}
    assert (await api.c.post(url, json=other, headers={"Idempotency-Key": "retry-me-123"})).status_code == 422
    # a fresh key is a fresh request, and that one is refused on the case's own state
    assert (await api.c.post(url, json=body)).status_code == 409


async def test_a_write_on_an_old_view_of_the_case_is_refused(api):
    await login(api.c)
    cid = api.acme.case_id
    assert (await api.c.post(f"/v1/cases/{cid}/messages", json={"body": "one", "case_version": 1})).status_code == 202
    # this tab still shows v1
    stale = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 1})
    assert stale.status_code == 409 and "v2" in stale.json()["detail"]
    late = await api.c.post(f"/v1/cases/{cid}/messages", json={"body": "two", "case_version": 1})
    assert late.status_code == 409
    assert await version(api.c, cid) == 2
    fresh = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 2})
    assert fresh.status_code == 202


async def test_a_supervisor_sees_and_requeues_dead_letters(api, app_sessions):
    await login(api.c)
    cid = api.acme.case_id
    await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 1})
    # the dispatcher gave up on the first stage before it ran
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE outbox_events SET dead_at = now(), attempts = 25, last_error = 'boom'"))
    assert (await api.c.get("/v1/ops/dead-letters")).status_code == 403

    await login(api.c, role="supervisor")
    [dead] = (await api.c.get("/v1/ops/dead-letters")).json()
    assert (dead["case_id"], dead["stage"], dead["last_error"]) == (cid, "PREPARE", "boom")
    url = f"/v1/ops/dead-letters/{dead['id']}/requeue"
    assert (await api.c.post(url, json={})).status_code == 202
    assert (await api.c.post(url, json={})).status_code == 409  # already back in the queue
    assert (await api.c.get("/v1/ops/dead-letters")).json() == []
    await api.drain()
    detail = (await api.c.get(f"/v1/cases/{cid}")).json()
    assert (detail["case"]["state"], detail["case"]["resolution"]) == ("CLOSED", "REFUNDED")
    assert len(detail["commands"]) == 1

    await login(api.c, tenant="t_globex", role="supervisor")
    assert (await api.c.post(url, json={})).status_code == 404  # someone else's event reads as missing


async def test_a_rules_already_refunded_close_starts_from_the_keyless_api(api, app_sessions):
    # recon's ledger already shows the duplicate refunded, rules closes it without any provider read
    async with app_sessions() as s, s.begin():
        await s.execute(
            text("UPDATE payments SET refunded_minor = 4900 WHERE id = :p"), {"p": api.acme.duplicate_payment_id}
        )
    await login(api.c)
    cid = api.acme.case_id
    r = await api.c.post(f"/v1/cases/{cid}/investigations", json={"mode": "rules", "case_version": 1})
    assert r.status_code == 202, r.text
    await api.drain()
    detail = (await api.c.get(f"/v1/cases/{cid}")).json()
    assert (detail["case"]["state"], detail["case"]["resolution"]) == ("CLOSED", "ALREADY_REFUNDED")


async def test_benchmarks_list_finished_runs_and_nothing_else(app_sessions, clock, tmp_path):
    import json as _json

    def run(name, report=True):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.json").write_text(_json.dumps({"started_at": f"2026-10-19T{name[-2:]}:00:00+00:00"}))
        if report:
            r = {
                "split": "val",
                "split_sha256": "abc",
                "arm": "rules",
                "model": None,
                "n": 70,
                "passed": 57,
                "wrong_closes": 10,
            }
            (d / "report.json").write_text(_json.dumps(r))

    run("run-01")
    run("run-02")
    run("run-03", report=False)  # still running, or killed
    async with app_sessions() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO tenants (id, name, provider_account, created_at) VALUES ('t_acme', 'Acme', 'acct_acme', now())"
            )
        )
    app = create_app(app_sessions, clock, KEY, dev=True, faults_enabled=False, results=tmp_path)
    async with client(app) as c:
        assert (await c.get("/v1/benchmarks")).status_code == 401
        await login(c)
        listed = (await c.get("/v1/benchmarks")).json()
        assert [b["id"] for b in listed] == ["run-02", "run-01"] and listed[0]["passed"] == 57
        detail = (await c.get("/v1/benchmarks/run-01")).json()
        assert detail["report"]["n"] == 70 and "started_at" in detail["manifest"]
        for bad in ("run-03", "..", "..%2F..%2Fetc", "nope"):
            assert (await c.get(f"/v1/benchmarks/{bad}")).status_code == 404, bad
