# spec load target: 25 investigations active at once across 5 tenants, through the real api, dispatcher and ledger.
# models are scripted with a fixed delay per call standing in for a model, so app overhead shows on its own.
# RECON_LOAD=1 runs it (make load), the numbers land in results/load/
import asyncio
import json
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from recon_agents.meter import Budget
from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY
from recon_api.app import create_app
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.db import admin_url, make_engine
from recon_storage.models import Case, Command, ModelUsage, OutboxEvent, Run
from recon_storage.users import ensure_demo_users
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.faults import Faults, SimulatedCrash, raise_crash
from recon_worker.investigate import AgentConfig, Investigator
from recon_worker.pipeline import Pipeline
from tests.agents.fakes import FAKE_MODEL, FAKE_PRICING, ScriptedLlm, coordinator, investigator, policy
from tests.api.test_api import KEY, client, login
from tests.conftest import MCP_URL, NAMES, RUN_KEY, T

TENANTS = ("acme", "globex", "hooli", "initech", "umbrella")
PER_TENANT = 5
WORKERS = 5  # worker instances, each takes deliveries concurrently
LOOPS = 25  # concurrent deliveries, what cloud tasks would fan out
DELAY = 1.0  # seconds per model call
LEASE = 30
CRASHES = 3  # fp4 under load: workers die right after the provider commits
RESULTS = Path(__file__).resolve().parents[2] / "results" / "load"

pytestmark = pytest.mark.skipif(os.environ.get("RECON_LOAD") != "1", reason="load test: RECON_LOAD=1 (make load)")


class SlowLlm(ScriptedLlm):
    delay: float = DELAY

    async def generate_content_async(self, llm_request, stream=False):
        await asyncio.sleep(self.delay)
        async for r in super().generate_content_async(llm_request, stream):
            yield r


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)


def spread(xs: list[float]) -> dict[str, float]:
    return {"p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "max": round(max(xs), 3) if xs else 0.0}


async def test_25_concurrent_investigations(
    app_sessions, flaky_sim, op, verifier_engines, mcp_read, clock, adk_sessions
):
    cases = []
    for name in TENANTS:
        for _ in range(PER_TENANT):
            sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, tenant=name)
            mcp_read.read_keys[sd.tenant_id] = sd.read_key
            cases.append(sd)
    await ensure_demo_users(app_sessions, T)
    async with app_sessions() as s, s.begin():
        await s.execute(text("INSERT INTO fault_points (point, remaining) VALUES ('FP4', :n)"), {"n": CRASHES})

    faults = Faults(app_sessions, enabled=True, on_hit=raise_crash)

    def conn(tenant: str) -> HttpConnector:
        return HttpConnector(flaky_sim, f"sk_test_{tenant.removeprefix('t_')}")

    def worker(i: int) -> Pipeline:
        models = {
            r: SlowLlm(model=FAKE_MODEL, script=s)
            for r, s in ((INVESTIGATOR, investigator), (POLICY, policy), (COORDINATOR, coordinator()))
        }
        cfg = AgentConfig(
            models=models,
            mode="scripted",
            pricing=FAKE_PRICING,
            mcp_url=MCP_URL,
            token_key=RUN_KEY,
            budget=Budget(max_seconds=120),
            http_factory=mcp_read.http_factory,
        )
        inv = Investigator(app_sessions, clock, cfg, adk_sessions)
        ex = Executor(app_sessions, clock, conn, faults=faults)
        return Pipeline(
            app_sessions,
            clock,
            ex,
            inv,
            worker_id=f"w{i}",
            lease_seconds=LEASE,
            faults=faults,
            reconcile_backoff_seconds=1,
        )

    workers = [worker(i) for i in range(WORKERS)]
    inflight: Counter[str] = Counter()
    peak: Counter[str] = Counter()
    deliveries: list[dict] = []
    turn = iter(range(10**9))

    async def deliver(d):
        w = workers[next(turn) % WORKERS]
        inflight[d.stage] += 1
        peak[d.stage] = max(peak[d.stage], inflight[d.stage])
        started, at = time.monotonic(), datetime.now(UTC)
        try:
            return (await w.advance(d.run_id, d.stage, d.event_id)).status
        finally:
            inflight[d.stage] -= 1
            deliveries.append(
                {"id": d.id, "stage": d.stage, "attempt": d.attempts, "at": at, "secs": time.monotonic() - started}
            )

    app = create_app(app_sessions, clock, KEY, dev=True, faults_enabled=False)
    admin = make_engine(admin_url(NAMES.recon))
    done = asyncio.Event()
    conns: list[int] = []
    api_start: list[float] = []
    api_read: list[float] = []
    closed_at: dict[str, float] = {}
    crashes = 0

    async def sample():
        while not done.is_set():
            async with admin.connect() as c:
                n = (
                    await c.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE datname = :d"), {"d": NAMES.recon}
                    )
                ).scalar_one()
            conns.append(n)
            await asyncio.sleep(0.25)

    async def loop():
        nonlocal crashes
        disp = Dispatcher(app_sessions, deliver, visibility_seconds=LEASE, batch=1)
        while not done.is_set():
            try:
                n = await disp.pump_once()
            except SimulatedCrash:
                crashes += 1  # this instance died mid-delivery, a fresh one takes its place
                n = 0
            if not n:
                await asyncio.sleep(0.05)

    async def browser(sd, c):
        # starts the investigation, then polls the case page like an operator would
        t0 = time.monotonic()
        r = await c.post(f"/v1/cases/{sd.case_id}/investigations", json={"mode": "agent", "case_version": 1})
        api_start.append(time.monotonic() - t0)
        assert r.status_code == 202, r.text
        while True:
            t1 = time.monotonic()
            state = (await c.get(f"/v1/cases/{sd.case_id}")).json()["case"]["state"]
            api_read.append(time.monotonic() - t1)
            if state in ("CLOSED", "ESCALATED"):
                closed_at[sd.case_id] = time.monotonic()
                return
            await asyncio.sleep(0.5)

    clients = {}
    for name in TENANTS:
        clients[f"t_{name}"] = client(app)
        await login(clients[f"t_{name}"], f"t_{name}")
    began = time.monotonic()
    bg = [asyncio.create_task(sample()), *(asyncio.create_task(loop()) for _ in range(LOOPS))]
    try:
        await asyncio.wait_for(asyncio.gather(*(browser(sd, clients[sd.tenant_id]) for sd in cases)), timeout=300)
    finally:
        done.set()
        await asyncio.gather(*bg, return_exceptions=True)
        for c in clients.values():
            await c.aclose()
        await admin.dispose()
    wall = time.monotonic() - began

    # correctness: one refund per case, on its own merchant's account, verifier agrees, nothing parked
    sim = (await flaky_sim.get("/sim/state", headers=op)).json()
    account_of = {pi["id"]: pi["account"] for pi in sim["payment_intents"]}
    async with app_sessions() as s:
        states = {c.id: (c.state, c.resolution) for c in (await s.execute(select(Case))).scalars()}
        cmds = (await s.execute(select(Command))).scalars().all()
        dead = (await s.execute(select(OutboxEvent).where(OutboxEvent.dead_at.is_not(None)))).scalars().all()
        events = {e.id: e for e in (await s.execute(select(OutboxEvent))).scalars()}
        runs = (await s.execute(select(Run))).scalars().all()
        usage = (await s.execute(select(ModelUsage))).scalars().all()
    verdicts = [
        await verify_case(verifier_engines["recon"], verifier_engines["sim"], verifier_engines["eval"], sd.case_id)
        for sd in cases
    ]
    per_case = []
    for sd in cases:
        mine = [r for r in sim["refunds"] if r["payment_intent"] in (sd.original_provider_id, sd.duplicate_provider_id)]
        per_case.append(
            {
                "refunds": len([r for r in mine if r["status"] != "failed"]),
                "accounts": {account_of[r["payment_intent"]] for r in mine},
            }
        )

    run_of = {r.id: r for r in runs}
    model_secs: dict[str, float] = {}
    for u in usage:
        cid = run_of[u.run_id].case_id
        model_secs[cid] = model_secs.get(cid, 0.0) + u.latency_ms / 1000
    # queue delay: from the event being written to a dispatcher starting on it, first try only (includes any backoff)
    queue = [
        max(0.0, (d["at"] - events[d["id"]].created_at).total_seconds())
        for d in deliveries
        if d["attempt"] == 1 and d["id"] in events
    ]
    report = {
        "at": datetime.now(UTC).isoformat(),
        "config": {
            "tenants": len(TENANTS),
            "cases": len(cases),
            "worker_instances": WORKERS,
            "concurrent_deliveries": LOOPS,
            "model": f"scripted, {DELAY}s per call",
            "lease_seconds": LEASE,
            "db_pool": "20 + 10 overflow, one process",
            "fp4_crashes_armed": CRASHES,
        },
        "peak_concurrent": dict(peak),
        "wall_seconds": round(wall, 1),
        "throughput_cases_per_min": round(len(cases) / wall * 60, 1),
        "api_start_seconds": spread(api_start),
        "api_read_seconds": spread(api_read),
        "queue_delay_seconds": spread(queue),
        "end_to_end_seconds": spread([closed_at[sd.case_id] - began for sd in cases]),
        "model_seconds_per_case_summed_over_agents": spread(list(model_secs.values())),
        "investigate_stage_seconds": spread([d["secs"] for d in deliveries if d["stage"] == "INVESTIGATE"]),
        "money_stage_seconds": spread([d["secs"] for d in deliveries if d["stage"] != "INVESTIGATE"]),
        "db_connections_max": max(conns) if conns else None,
        "crashes": crashes,
        "closed_refunded": sum(v == ("CLOSED", "REFUNDED") for v in states.values()),
        "verifier_pass": sum(v.passed for v in verdicts),
        "duplicate_refunds": sum(c["refunds"] > 1 for c in per_case),
        "crossed_tenants": sum(len(c["accounts"]) > 1 for c in per_case),
        "commands_not_confirmed": sum(c.state != "CONFIRMED_SUCCESS" for c in cmds),
        "dead_letters": len(dead),
    }
    print("\n" + json.dumps(report, indent=2))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{datetime.now(UTC):%Y%m%dT%H%M%S}.json").write_text(json.dumps(report, indent=2) + "\n")

    assert peak["INVESTIGATE"] == len(cases), peak
    assert report["closed_refunded"] == report["verifier_pass"] == len(cases)
    assert report["duplicate_refunds"] == report["crossed_tenants"] == report["commands_not_confirmed"] == 0
    assert report["dead_letters"] == 0 and len(cmds) == len(cases)
    assert crashes == CRASHES
