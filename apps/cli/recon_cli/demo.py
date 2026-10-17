import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, text

from recon_domain.connector import FakeClock
from recon_fixtures.dup_charge import scripted_plan, seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.db import DbNames, make_engine, sessions, url
from recon_storage.models import Case, Command
from recon_verifier.checks import verify_case
from recon_worker.app import TOKEN_HEADER
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline

OPERATOR = os.environ.get("SIM_OPERATOR_TOKEN", "sim-operator-dev")


def say(step: str, msg: str) -> None:
    print(f"[{step:>8}] {msg}", flush=True)


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(factory: str, env: dict[str, str]) -> tuple[subprocess.Popen, str]:
    port = _port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", factory, "--port", str(port), "--log-level", "warning"],
        env={**os.environ, "GOOGLE_API_USE_CLIENT_CERTIFICATE": "false", **env},
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            httpx.get(base, timeout=0.5)
            return proc, base
        except httpx.TransportError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"{factory} didn't start")


async def dup_charge_fp4(names: DbNames) -> int:
    """spec §25: kill the worker right after the provider commits, recover to exactly one refund"""
    tenant = f"demo{secrets.token_hex(3)}"
    now = datetime.now(UTC).replace(microsecond=0)
    clock = FakeClock(now)
    app_engine = make_engine(url("recon_app", names.recon))
    db = sessions(app_engine)
    verifier = {
        k: make_engine(url("recon_eval", d))
        for k, d in (("recon", names.recon), ("sim", names.sim), ("eval", names.eval))
    }
    sim_proc, sim_url = _serve(
        "provider_sim.app:create_app", {"SIM_DATABASE_URL": url("sim_app", names.sim), "SIM_OPERATOR_TOKEN": OPERATOR}
    )
    worker_proc = None
    op = {"x-sim-operator": OPERATOR}
    try:
        async with httpx.AsyncClient(base_url=sim_url) as sim, httpx.AsyncClient() as http:
            sd = await seed_duplicate_charge(db, sim, op, verifier["eval"], now, tenant=tenant)
            say("seed", f"case {sd.case_id}: customer charged twice, 49.00 usd each, on order {sd.order_id}")
            worker_proc, worker_url = _serve(
                "recon_worker.app:create_from_env",
                {
                    "RECON_DATABASE_URL": url("recon_app", names.recon),
                    "RECON_SIM_URL": sim_url,
                    "RECON_PROVIDER_KEYS": json.dumps({sd.tenant_id: {"secret": sd.api_key}}),
                    "RECON_NOW": now.isoformat(),
                    "RECON_LEASE_SECONDS": "2",
                    "RECON_FAULTS": "1",
                    "RECON_WORKER_TOKEN": "demo",
                },
            )
            say("worker", f"worker process pid {worker_proc.pid}, faults armed: FP4 (die after the provider commits)")

            conn = HttpConnector(sim, sd.api_key)
            local = Pipeline(db, clock, Executor(db, clock, lambda _t: conn), worker_id="recovery", lease_seconds=2)
            run_id = await local.start_scripted(sd.tenant_id, sd.case_id, scripted_plan(sd))
            say("plan", f"run {run_id}: plan validated, refund 4900 usd on {sd.duplicate_payment_id}")

            async def remote(d):
                r = await http.post(
                    f"{worker_url}/internal/tasks/advance",
                    json={"run_id": d.run_id, "stage": d.stage, "event_id": d.event_id},
                    headers={TOKEN_HEADER: "demo"},
                    timeout=10,
                )
                return r.status_code

            to_worker = Dispatcher(db, remote, visibility_seconds=1)
            await to_worker.pump_once()
            async with db() as s:
                cmd = (await s.execute(select(Command).where(Command.case_id == sd.case_id))).scalar_one()
            say("prepare", f"command {cmd.id} prepared, 4900 reserved, idempotency key {cmd.idempotency_key}")
            async with db() as s, s.begin():
                await s.execute(text("INSERT INTO fault_points (point, scope_id) VALUES ('FP4', :c)"), {"c": cmd.id})
            await to_worker.pump_once()
            code = worker_proc.wait(timeout=10)
            refunds = [
                r
                for r in (await sim.get("/sim/state", headers=op)).json()["refunds"]
                if r["payment_intent"] == sd.duplicate_provider_id
            ]
            async with db() as s:
                cmd = (await s.execute(select(Command).where(Command.id == cmd.id))).scalar_one()
            say("crash", f"worker exited {code} after the provider committed {[r['id'] for r in refunds]}")
            say("crash", f"recon still thinks the command is {cmd.state}: it never heard back")

            say("recover", "waiting out the dead worker's lease...")
            await asyncio.sleep(2.5)

            async def here(d):
                return (await local.advance(d.run_id, d.stage, d.event_id)).status

            recovery = Dispatcher(db, here, visibility_seconds=1)
            for _ in range(20):
                await recovery.pump_once()
                async with db() as s:
                    case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
                if case.state in ("CLOSED", "ESCALATED"):
                    break
                await asyncio.sleep(0.2)
            async with db() as s:
                cmd = (await s.execute(select(Command).where(Command.id == cmd.id))).scalar_one()
                case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
            say(
                "recover",
                f"new worker took the lease, asked the provider by metadata: {cmd.state}, refund {cmd.provider_refund_id}",
            )
            say("close", f"case {case.state} {case.resolution or ''}".rstrip())

            state = (await sim.get("/sim/state", headers=op)).json()
            mine = [r for r in state["refunds"] if r["payment_intent"] == sd.duplicate_provider_id]
            posts = [
                r
                for r in state["requests_log"]
                if r["path"] == "/v1/refunds" and r["idempotency_key"] == cmd.idempotency_key
            ]
            verdict = await verify_case(verifier["recon"], verifier["sim"], verifier["eval"], sd.case_id)
            say("verify", f"provider refunds on the duplicate payment: {len(mine)}, refund POSTs sent: {len(posts)}")
            say("verify", f"independent verifier: {'PASS' if verdict.passed else 'FAIL ' + str(verdict.failed())}")
            return 0 if verdict.passed and len(mine) == 1 else 1
    finally:
        for p in (worker_proc, sim_proc):
            if p is not None and p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
        await app_engine.dispose()
        for e in verifier.values():
            await e.dispose()
