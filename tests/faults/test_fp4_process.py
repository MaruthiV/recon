import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.db import url
from recon_storage.models import AuditEvent, Case, Command, CommandAttempt
from recon_verifier.checks import verify_case
from recon_worker.app import TOKEN_HEADER
from recon_worker.dispatch import Dispatcher
from recon_worker.exec_client import McpExecGateway
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline
from tests.conftest import NAMES, OPERATOR, T
from tests.e2e.test_scripted_dup_charge import scripted_plan

WORKER_TOKEN = "test-worker-token"
EXEC_KEY = "test-exec-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(factory: str, env: dict[str, str]) -> SimpleNamespace:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", factory, "--port", str(port), "--log-level", "warning"],
        env={**os.environ, "GOOGLE_API_USE_CLIENT_CERTIFICATE": "false", **env},
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            httpx.get(base, timeout=0.5)
            return SimpleNamespace(proc=proc, url=base)
        except httpx.TransportError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"{factory} didn't come up")


@pytest.fixture
def procs(dbs):
    started = []

    def start(factory, env):
        p = serve(factory, env)
        started.append(p)
        return p

    yield start
    for p in started:
        if p.proc.poll() is None:
            p.proc.terminate()
            p.proc.wait(timeout=10)


@pytest.mark.parametrize("topology", ["worker_holds_key", "via_mcp_exec"])
async def test_fp4_crash_after_provider_commit_reconciles_single_refund(
    procs, app_sessions, op, verifier_engines, clock, topology
):
    sim_srv = procs(
        "provider_sim.app:create_app", {"SIM_DATABASE_URL": url("sim_app", NAMES.sim), "SIM_OPERATOR_TOKEN": OPERATOR}
    )
    async with httpx.AsyncClient(base_url=sim_srv.url) as sim, httpx.AsyncClient() as http:
        sd = await seed_duplicate_charge(app_sessions, sim, op, verifier_engines["eval"], T)
        keys = json.dumps({sd.tenant_id: {"secret": sd.api_key}})
        env = {
            "RECON_DATABASE_URL": url("recon_app", NAMES.recon),
            "RECON_NOW": T.isoformat(),
            "RECON_LEASE_SECONDS": "1",
            "RECON_FAULTS": "1",
            "RECON_WORKER_TOKEN": WORKER_TOKEN,
        }
        gateway = None
        if topology == "via_mcp_exec":
            # three processes: the worker never sees the provider key
            exec_srv = procs(
                "recon_mcp.exec:create_exec_from_env",
                {
                    "RECON_EXEC_DATABASE_URL": url("recon_app", NAMES.recon),
                    "RECON_EXEC_KEY": EXEC_KEY,
                    "RECON_SIM_URL": sim_srv.url,
                    "RECON_PROVIDER_KEYS": keys,
                    "RECON_NOW": T.isoformat(),
                },
            )
            env |= {"RECON_EXEC_URL": f"{exec_srv.url}/mcp", "RECON_EXEC_KEY": EXEC_KEY}
            gateway = McpExecGateway(f"{exec_srv.url}/mcp", EXEC_KEY.encode(), clock)
        else:
            env |= {"RECON_SIM_URL": sim_srv.url, "RECON_PROVIDER_KEYS": keys}
        worker = procs("recon_worker.app:create_from_env", env)

        async def over_http(d):
            r = await http.post(
                f"{worker.url}/internal/tasks/advance",
                json={"run_id": d.run_id, "stage": d.stage, "event_id": d.event_id},
                headers={TOKEN_HEADER: WORKER_TOKEN},
                timeout=10,
            )
            return r.status_code

        conn = HttpConnector(sim, sd.api_key)
        local = Pipeline(
            app_sessions,
            clock,
            Executor(app_sessions, clock, lambda _t: conn, gateway_for=(lambda _t: gateway) if gateway else None),
            worker_id="recovery",
            lease_seconds=1,
            reconcile_backoff_seconds=0,
        )
        await local.start_scripted(sd.tenant_id, sd.case_id, scripted_plan(sd))
        remote = Dispatcher(app_sessions, over_http, visibility_seconds=1)
        assert await remote.pump_once() == 1  # PREPARE, on the worker process

        async with app_sessions() as s, s.begin():
            await s.execute(text("INSERT INTO fault_points (point) VALUES ('FP4')"))
        await remote.pump_once()  # SUBMIT: provider commits, then the worker process dies

        assert worker.proc.wait(timeout=10) == 137
        state = (await sim.get("/sim/state", headers=op)).json()
        assert len(state["refunds"]) == 1  # the money moved
        async with app_sessions() as s:
            cmd = (await s.execute(select(Command))).scalar_one()
        assert cmd.state == C.SUBMITTING.value  # and recon never heard about it

        # a fresh worker, once the dead one's lease and the dispatcher backoff run out
        await asyncio.sleep(2.5)

        async def in_process(d):
            return (await local.advance(d.run_id, d.stage, d.event_id)).status

        recovery = Dispatcher(app_sessions, in_process, visibility_seconds=1)
        for _ in range(20):
            if await recovery.pump_once() == 0:
                async with app_sessions() as s:
                    case = (await s.execute(select(Case))).scalar_one()
                if case.state in (S.CLOSED.value, S.ESCALATED.value):
                    break
            await asyncio.sleep(0.1)

        async with app_sessions() as s:
            case = (await s.execute(select(Case))).scalar_one()
            cmd = (await s.execute(select(Command))).scalar_one()
            attempts = list((await s.execute(select(CommandAttempt))).scalars())
            kinds = [a.kind for a in (await s.execute(select(AuditEvent))).scalars()]
        assert (case.state, case.resolution) == (S.CLOSED.value, "REFUNDED")
        assert cmd.state == C.CONFIRMED_SUCCESS.value
        state = (await sim.get("/sim/state", headers=op)).json()
        [refund] = state["refunds"]
        assert cmd.provider_refund_id == refund["id"]
        posts = [r for r in state["requests_log"] if r["method"] == "POST" and r["path"] == "/v1/refunds"]
        assert len(posts) == 1, "recovery must reconcile by metadata, not resend"
        assert len(attempts) == 1
        assert "lease_taken_over" in kinds and "command_unknown" in kinds

    e = verifier_engines
    verdict = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
    assert verdict.passed, verdict.failed()
