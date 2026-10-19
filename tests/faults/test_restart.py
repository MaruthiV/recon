import asyncio
import json

import httpx
from sqlalchemy import text

from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_storage.db import url
from recon_verifier.checks import verify_case
from recon_worker.app import TOKEN_HEADER
from recon_worker.dispatch import Dispatcher
from tests.api.test_api import with_key
from tests.conftest import NAMES, OPERATOR, T
from tests.faults.test_fp4_process import WORKER_TOKEN, procs  # noqa: F401

SESSION_KEY = "test-restart-session-key"


async def test_waiting_cases_survive_killing_every_app_process(procs, app_sessions, op, verifier_engines):  # noqa: F811
    sim_srv = procs(
        "provider_sim.app:create_app", {"SIM_DATABASE_URL": url("sim_app", NAMES.sim), "SIM_OPERATOR_TOKEN": OPERATOR}
    )
    async with httpx.AsyncClient(base_url=sim_srv.url) as sim:
        big = await seed_duplicate_charge(app_sessions, sim, op, verifier_engines["eval"], T, amount_minor=25_000)
        quiet = await seed_duplicate_charge(app_sessions, sim, op, verifier_engines["eval"], T, tenant="globex")
    # the second customer never said which order, so the case will wait on them
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE cases SET order_id = NULL WHERE id = :c"), {"c": quiet.case_id})

    keys = {sd.tenant_id: {"secret": sd.api_key} for sd in (big, quiet)}
    worker_env = {
        "RECON_DATABASE_URL": url("recon_app", NAMES.recon),
        "RECON_SIM_URL": sim_srv.url,
        "RECON_PROVIDER_KEYS": json.dumps(keys),
        "RECON_NOW": T.isoformat(),
        "RECON_WORKER_TOKEN": WORKER_TOKEN,
    }
    api_env = {
        "RECON_DATABASE_URL": url("recon_app", NAMES.recon),
        "RECON_SESSION_KEY": SESSION_KEY,
        "RECON_NOW": T.isoformat(),
    }

    def stack():
        return procs("recon_worker.app:create_from_env", worker_env), procs("recon_api.app:create_from_env", api_env)

    async def pump(worker):
        async with httpx.AsyncClient() as http:

            async def over_http(d):
                r = await http.post(
                    f"{worker.url}/internal/tasks/advance",
                    json={"run_id": d.run_id, "stage": d.stage, "event_id": d.event_id},
                    headers={TOKEN_HEADER: WORKER_TOKEN},
                    timeout=30,
                )
                return r.status_code

            disp = Dispatcher(app_sessions, over_http)
            for _ in range(30):
                if await disp.pump_once() == 0:
                    return

    def browser(api):
        return httpx.AsyncClient(base_url=api.url, event_hooks={"request": [with_key]})

    async def login(c, tenant, role="operator"):
        r = await c.post("/v1/login", json={"user": "sam", "tenant_id": tenant, "role": role})
        assert r.status_code == 200

    worker, api = stack()
    for sd in (big, quiet):
        async with browser(api) as c:
            await login(c, sd.tenant_id)
            r = await c.post(f"/v1/cases/{sd.case_id}/investigations", json={"mode": "rules", "case_version": 1})
            assert r.status_code == 202, r.text
    await pump(worker)
    async with browser(api) as c:
        await login(c, big.tenant_id)
        assert (await c.get(f"/v1/cases/{big.case_id}")).json()["case"]["state"] == "AWAITING_APPROVAL"
        await login(c, quiet.tenant_id)
        assert (await c.get(f"/v1/cases/{quiet.case_id}")).json()["case"]["state"] == "AWAITING_INFO"

    # every app process dies at once, nothing in memory survives
    for p in (worker, api):
        p.proc.kill()
        assert p.proc.wait(timeout=10) == -9
    worker, api = stack()

    async with browser(api) as c:
        await login(c, big.tenant_id, role="supervisor")
        plan = (await c.get(f"/v1/cases/{big.case_id}")).json()["plan"]
        body = {"decision": "APPROVED", "plan_hash": plan["material_hash"]}
        assert (await c.post(f"/v1/plans/{plan['id']}/approvals", json=body)).status_code == 202
        await login(c, quiet.tenant_id)
        reply = {"body": f"it's order {quiet.order_id}", "order_id": quiet.order_id, "mode": "rules", "case_version": 1}
        r = await c.post(f"/v1/cases/{quiet.case_id}/messages", json=reply)
        assert (r.status_code, r.json()["resumed"]) == (202, True)
    await pump(worker)
    await asyncio.sleep(0)

    async with httpx.AsyncClient(base_url=sim_srv.url) as sim:
        refunds = (await sim.get("/sim/state", headers=op)).json()["refunds"]
    assert sorted(r["amount"] for r in refunds) == [4900, 25_000]
    e = verifier_engines
    for sd in (big, quiet):
        v = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
        assert v.passed, (sd.case_id, v.failed())
