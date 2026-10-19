import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_retrieval.index import index_from_env
from recon_storage.db import DbNames, make_engine, sessions, url
from recon_worker.app import TOKEN_HEADER
from recon_worker.dispatch import Dispatcher, Sweeper

ROOT = Path(__file__).resolve().parents[3]
PORTS = {"sim": 8100, "worker": 8101, "exec": 8102, "read": 8103, "api": 8000, "web": 3000}
OPERATOR = os.environ.get("SIM_OPERATOR_TOKEN", "sim-operator-dev")
WORKER_TOKEN = "dev-worker-token"
TENANT = "acme"


def log(who: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [{who:>10}] {msg}", flush=True)


def py(factory: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", factory, "--port", str(port), "--log-level", "warning"],
        env={**os.environ, "GOOGLE_API_USE_CLIENT_CERTIFICATE": "false", "ADK_ENABLE_MCP_MODERN_PROTOCOL": "1", **env},
    )


def start_web() -> subprocess.Popen:
    return subprocess.Popen(
        ["pnpm", "--dir", str(ROOT / "apps" / "web"), "dev", "--port", str(PORTS["web"])],
        env={**os.environ, "RECON_API_URL": f"http://127.0.0.1:{PORTS['api']}"},
    )


def wait_up(base: str, proc: subprocess.Popen, seconds: int = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{base} exited with {proc.returncode}")
        try:
            httpx.get(base, timeout=0.5)
            return
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError(f"{base} didn't come up")


async def dev(names: DbNames, agent: str | None, web: bool) -> int:
    """local stack for the v0.1 demo: every piece in its own process, the worker restarts when it dies"""
    keys = {
        f"t_{TENANT}": {
            "secret": f"sk_test_{TENANT}",
            "read": f"rk_test_{TENANT}",
            "webhook_secret": "whsec_dev",
        }
    }
    common = {"RECON_PROVIDER_KEYS": json.dumps(keys), "RECON_SIM_URL": f"http://127.0.0.1:{PORTS['sim']}"}
    specs = {
        "sim": (
            "provider_sim.app:create_app",
            {"SIM_DATABASE_URL": url("sim_app", names.sim), "SIM_OPERATOR_TOKEN": OPERATOR},
        ),
        "exec": (
            "recon_mcp.exec:create_exec_from_env",
            common | {"RECON_EXEC_DATABASE_URL": url("recon_app", names.recon), "RECON_EXEC_KEY": "dev-exec-key"},
        ),
        "read": (
            "recon_mcp.server:create_read_from_env",
            common | {"RECON_MCP_DATABASE_URL": url("recon_mcp_read", names.recon)},
        ),
        "worker": (
            "recon_worker.app:create_from_env",
            {
                "RECON_DATABASE_URL": url("recon_app", names.recon),
                "RECON_EXEC_URL": f"http://127.0.0.1:{PORTS['exec']}/mcp",
                "RECON_EXEC_KEY": "dev-exec-key",
                "RECON_MCP_URL": f"http://127.0.0.1:{PORTS['read']}/mcp",
                # webhook secrets and the restricted read key, the worker can't create refunds in this shape
                "RECON_PROVIDER_KEYS": json.dumps(
                    {t: {"webhook_secret": k["webhook_secret"], "read": k["read"]} for t, k in keys.items()}
                ),
                "RECON_SIM_URL": f"http://127.0.0.1:{PORTS['sim']}",
                "RECON_FAULTS": "1",
                "RECON_LEASE_SECONDS": "5",
                "RECON_WORKER_TOKEN": WORKER_TOKEN,
                **({"RECON_AGENT": agent} if agent else {}),
            },
        ),
        "api": (
            "recon_api.app:create_from_env",
            {"RECON_DATABASE_URL": url("recon_app", names.recon), "RECON_FAULTS": "1"},
        ),
    }
    procs: dict[str, subprocess.Popen] = {}
    try:
        for name in ("sim", "exec", "read", "worker", "api"):
            factory, env = specs[name]
            procs[name] = py(factory, PORTS[name], env)
            wait_up(f"http://127.0.0.1:{PORTS[name]}", procs[name])
            log(name, f"up on :{PORTS[name]} (pid {procs[name].pid})")
        if web:
            procs["web"] = start_web()
            log("web", f"starting on :{PORTS['web']}")

        db = sessions(make_engine(url("recon_app", names.recon)))
        evals = make_engine(url("recon_eval", names.eval))
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORTS['sim']}") as sim:
            sd = await seed_duplicate_charge(
                db, sim, {"x-sim-operator": OPERATOR}, evals, datetime.now(UTC), tenant=TENANT
            )
        log("seed", f"new demo case {sd.case_id} for tenant {sd.tenant_id}")
        if (indexed := await index_from_env(db)) is not None:
            log("seed", f"embedded {indexed} policy chunks, mcp-read searches hybrid")
        log(
            "ready", f"http://localhost:{PORTS['web']}/cases/{sd.case_id}  (sign in as any name, tenant {sd.tenant_id})"
        )

        async with httpx.AsyncClient() as http:

            async def to_worker(d):
                r = await http.post(
                    f"http://127.0.0.1:{PORTS['worker']}/internal/tasks/advance",
                    json={"run_id": d.run_id, "stage": d.stage, "event_id": d.event_id},
                    headers={TOKEN_HEADER: WORKER_TOKEN},
                    timeout=30,
                )
                return r.status_code

            disp, sweeper = Dispatcher(db, to_worker, visibility_seconds=5), Sweeper(db)
            tick = 0
            while True:
                if procs["worker"].poll() is not None:
                    # what cloud run would do: a new instance, nothing carried over in memory
                    log("worker", f"exited {procs['worker'].returncode}, starting a fresh one")
                    factory, env = specs["worker"]
                    procs["worker"] = py(factory, PORTS["worker"], env)
                    await asyncio.to_thread(wait_up, f"http://127.0.0.1:{PORTS['worker']}", procs["worker"])
                    log("worker", f"back up (pid {procs['worker'].pid})")
                n = await disp.pump_once()
                if n:
                    log("dispatch", f"delivered {n} event(s)")
                tick += 1
                if tick % 25 == 0 and (fixed := await sweeper.sweep_once()):
                    log("sweeper", f"re-enqueued {fixed} stranded run(s)")
                await asyncio.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
