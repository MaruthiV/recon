import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, text, update

from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_domain.tokens import MCP_READ, mint
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_mcp.exec import create_exec_app
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Command
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.exec_client import McpExecGateway
from recon_worker.executor import Executor
from recon_worker.faults import Faults, SimulatedCrash, raise_crash
from recon_worker.pipeline import Pipeline
from tests.conftest import NAMES, RUN_KEY, T, serving
from tests.e2e.test_scripted_dup_charge import scripted_plan

EXEC_KEY = b"test-exec-capability-key"
EXEC_URL = "http://localhost:8300/mcp"


def no_provider_key(_tenant):
    raise AssertionError("the worker must not hold a provider key")


@pytest.fixture
async def ex(app_sessions, flaky_sim, op, verifier_engines, clock):
    from recon_storage.db import url

    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    svc = create_exec_app(
        url("recon_app", NAMES.recon), EXEC_KEY, lambda _t: HttpConnector(flaky_sim, sd.api_key), clock, "localhost"
    )

    def http():
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=svc), base_url="http://localhost:8300")

    gateway = McpExecGateway(EXEC_URL, EXEC_KEY, clock, http_factory=http)

    def worker(faults=None, name="w"):
        f = faults or Faults()
        executor = Executor(app_sessions, clock, no_provider_key, faults=f, gateway_for=lambda _t: gateway)
        return Pipeline(
            app_sessions, clock, executor, worker_id=name, lease_seconds=1, faults=f, reconcile_backoff_seconds=0
        )

    async def drain(w):
        disp = Dispatcher(app_sessions, lambda d: _status(w, d), visibility_seconds=1)
        for _ in range(30):
            if await disp.pump_once() == 0:
                async with app_sessions() as s:
                    left = (
                        await s.execute(text("SELECT count(*) FROM outbox_events WHERE dispatched_at IS NULL"))
                    ).scalar_one()
                if left == 0:
                    return
            await asyncio.sleep(0.05)

    async def call(tool, **args):
        async with http() as h, Client(streamable_http_client(EXEC_URL, http_client=h)) as c:
            return await c.call_tool(tool, args)

    async def refunds():
        return (await flaky_sim.get("/sim/state", headers=op)).json()["refunds"]

    async with serving(svc):
        yield SimpleNamespace(
            sd=sd,
            worker=worker,
            drain=drain,
            call=call,
            refunds=refunds,
            svc=svc,
            gateway=gateway,
            sessions=app_sessions,
            verifier=verifier_engines,
        )
    await svc.engine.dispose()


async def _status(w, d):
    return (await w.advance(d.run_id, d.stage, d.event_id)).status


async def closed_with_one_refund(ex):
    async with ex.sessions() as s:
        case = (await s.execute(select(Case))).scalar_one()
    assert (case.state, case.resolution) == (S.CLOSED.value, "REFUNDED")
    assert len(await ex.refunds()) == 1
    e = ex.verifier
    verdict = await verify_case(e["recon"], e["sim"], e["eval"], ex.sd.case_id)
    assert verdict.passed, verdict.failed()


async def test_pipeline_moves_money_only_through_mcp_exec(ex):
    w = ex.worker()
    await w.start_scripted(ex.sd.tenant_id, ex.sd.case_id, scripted_plan(ex.sd))
    await ex.drain(w)
    await closed_with_one_refund(ex)


async def test_exec_tools_take_no_money(ex):
    async with (
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ex.svc), base_url="http://localhost:8300") as h,
        Client(streamable_http_client(EXEC_URL, http_client=h)) as c,
    ):
        tools = {t.name: set(t.input_schema["properties"]) for t in (await c.list_tools()).tools}
    assert tools == {
        "execute_authorized_refund": {"command_id", "authorization_capability"},
        "reconcile_refund": {"command_id", "authorization_capability", "refund_id"},
    }


async def test_exec_refuses_anything_but_a_matching_capability(ex):
    w = ex.worker()
    await w.start_scripted(ex.sd.tenant_id, ex.sd.case_id, scripted_plan(ex.sd))
    await Dispatcher(ex.sessions, lambda d: _status(w, d)).pump_once()  # PREPARE only
    async with ex.sessions() as s, s.begin():
        cmd = (await s.execute(select(Command))).scalar_one()
        await s.execute(update(Command).where(Command.id == cmd.id).values(state=C.SUBMITTING.value, attempts=1))

    def cap(**kw):
        claims = {
            "aud": "mcp-exec",
            "tenant": ex.sd.tenant_id,
            "command_id": cmd.id,
            "payload_hash": cmd.payload_hash,
            "attempt": 1,
            "exp": int((T + timedelta(seconds=60)).timestamp()),
        }
        return mint({**claims, **kw}, EXEC_KEY)

    bad = {
        "agent run token": mint({"aud": MCP_READ, "tenant": ex.sd.tenant_id, "exp": 2**40}, RUN_KEY),
        "wrong key": mint({"aud": "mcp-exec", "command_id": cmd.id, "exp": 2**40}, b"nope"),
        "expired": cap(exp=int(T.timestamp())),
        "other command": cap(command_id="cmd_other"),
        "other payload": cap(payload_hash="0" * 64),
        "other tenant": cap(tenant="t_globex"),
        "older attempt": cap(attempt=0),
    }
    for why, capability in bad.items():
        r = await ex.call("execute_authorized_refund", command_id=cmd.id, authorization_capability=capability)
        assert r.structured_content["ok"] is False, why
    assert await ex.refunds() == []

    good = await ex.call("execute_authorized_refund", command_id=cmd.id, authorization_capability=cap())
    assert good.structured_content["ok"] is True and good.structured_content["kind"] == "APPLIED"
    assert len(await ex.refunds()) == 1


async def test_fp4_recovery_reconciles_through_mcp_exec(ex):
    crash = Faults(ex.sessions, enabled=True, on_hit=raise_crash)
    w1 = ex.worker(crash, name="w1")
    await w1.start_scripted(ex.sd.tenant_id, ex.sd.case_id, scripted_plan(ex.sd))
    disp = Dispatcher(ex.sessions, lambda d: _status(w1, d), visibility_seconds=1)
    await disp.pump_once()
    async with ex.sessions() as s, s.begin():
        await s.execute(text("INSERT INTO fault_points (point) VALUES ('FP4')"))
    with pytest.raises(SimulatedCrash):
        await disp.pump_once()
    assert len(await ex.refunds()) == 1

    await asyncio.sleep(1.2)
    await ex.drain(ex.worker(name="w2"))
    await closed_with_one_refund(ex)
