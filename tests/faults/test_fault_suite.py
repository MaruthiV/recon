# e3 executor-level fault suite: 10 fault families x 10 money-moving instances, each paired with a no-fault control.
# plans are the rules planner's (deterministic, no model), RECON_FAULT_SUITE=full runs all 200, ci runs one each
import asyncio
import json
import os
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from recon_domain.states import CaseState as S
from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import TENANTS, materialize
from recon_fixtures.manifests import specs
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Command, Payment
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.faults import Faults, SimulatedCrash, raise_crash
from recon_worker.pipeline import Pipeline, Stage
from recon_worker.rules_planner import plan_by_rules
from tests.conftest import T

LEASE = 1
FAULTS = ["FP2", "FP3", "FP4", "FP5", "FP6", "FP7", "FP9", "FP10", "lost_response", "reset_before_send"]


def owed(s) -> int:
    # upper bound on the refund, enough to keep the over-limit ones (they wait for a person) out of this suite
    return {"refund": s.amount, "received": s.amount * s.extra["qty"], "two_lines": 1500}.get(s.variant, s.amount)


def spread(n: int) -> list:
    # refunds that need no approval, taken round robin over the three money-moving workflows
    pools: dict[str, list] = {}
    for s in specs("dev"):
        if (
            s.variant in ("refund", "received", "two_lines", "mid_period")
            and owed(s) <= TENANTS[s.tenant].approve_limit
        ):
            pools.setdefault(s.family, []).append(s)
    out: list = []
    while len(out) < n and any(pools.values()):
        for pool in pools.values():
            if pool and len(out) < n:
                out.append(pool.pop(0))
    return out


INSTANCES = spread(10)
FULL = os.environ.get("RECON_FAULT_SUITE") == "full"
RESULTS = Path(__file__).resolve().parents[2] / "results" / "faults" / "e3.jsonl"
TRIALS = [(fp, inst) for fp in FAULTS for inst in (INSTANCES if FULL else INSTANCES[:1])]


@pytest.fixture
async def fw(app_sessions, flaky_sim, op, verifier_engines, clock):
    names = count(1)

    async def seed(spec, tag):
        sd = await materialize(spec, app_sessions, flaky_sim, op, verifier_engines["eval"], T, "dev", seeded_ids(tag))
        async with app_sessions() as s:
            plan = await plan_by_rules(s, sd.tenant_id, sd.case_id, T)
        assert plan.recommended_disposition.value == "REFUND", plan
        return sd, plan

    def worker(sd, faults=None, name=None, backoff=0):
        conn = HttpConnector(flaky_sim, sd.api_key)
        f = faults or Faults(app_sessions, enabled=True, on_hit=raise_crash)
        return Pipeline(
            app_sessions,
            clock,
            Executor(app_sessions, clock, lambda _t: conn, faults=f),
            worker_id=name or f"w{next(names)}",
            lease_seconds=LEASE,
            faults=f,
            reconcile_backoff_seconds=backoff,
        )

    def dispatcher(*workers, duplicate=False):
        turn = count()

        async def deliver(d):
            w = workers[next(turn) % len(workers)]
            return (await w.advance(d.run_id, d.stage, d.event_id)).status

        return Dispatcher(app_sessions, deliver, visibility_seconds=LEASE, duplicate=duplicate)

    async def until_done(disp, sd, rounds=200):
        for _ in range(rounds):
            try:
                await disp.pump_once()
            except SimulatedCrash:
                pass
            async with app_sessions() as s:
                state = (await s.execute(select(Case.state).where(Case.id == sd.case_id))).scalar_one()
            if state in (S.CLOSED.value, S.ESCALATED.value):
                return
            await asyncio.sleep(0.05)

    async def arm(point):
        async with app_sessions() as s, s.begin():
            await s.execute(text("INSERT INTO fault_points (point) VALUES (:p)"), {"p": point})

    async def sim_fault(sd, kind, n=1, **params):
        r = await flaky_sim.post(
            "/sim/faults",
            json={"account": f"acct_{sd.tenant_id.removeprefix('t_')}", "kind": kind, "count": n, "params": params},
            headers=op,
        )
        r.raise_for_status()

    async def outcome(sd):
        e = verifier_engines
        v = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
        state = (await flaky_sim.get("/sim/state", headers=op)).json()
        refunds = [
            r
            for r in state["refunds"]
            if r["payment_intent"] in sd.label["case_payment_intents"] and r["status"] != "failed"
        ]
        async with app_sessions() as s:
            case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
            cmds = (await s.execute(select(Command).where(Command.case_id == sd.case_id))).scalars().all()
        return v, refunds, case, cmds

    return SimpleNamespace(
        seed=seed,
        worker=worker,
        dispatcher=dispatcher,
        until_done=until_done,
        arm=arm,
        sim_fault=sim_fault,
        outcome=outcome,
        sim=flaky_sim,
        sessions=app_sessions,
    )


async def inject(fw, fp, sd, plan):
    w1 = fw.worker(sd, name="first")
    if fp == "FP2":
        await fw.arm("FP2")
        await w1.start_scripted(sd.tenant_id, sd.case_id, plan)
        with pytest.raises(SimulatedCrash):
            await fw.dispatcher(w1).pump_once()
    elif fp in ("FP3", "FP4"):
        await w1.start_scripted(sd.tenant_id, sd.case_id, plan)
        disp = fw.dispatcher(w1)
        await disp.pump_once()  # PREPARE
        await fw.arm(fp)
        with pytest.raises(SimulatedCrash):
            await disp.pump_once()  # SUBMIT dies before (FP3) or after (FP4) the provider commits
    elif fp == "FP5":
        # the provider holds the request, the client gives up first, recon reconciles and resubmits the same key
        await fw.sim_fault(sd, "slow", seconds=2)
        fw.sim.flaky.give_up_early = 1
        w = fw.worker(sd, name="slowpoke", backoff=1)
        await w.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(w), sd)
        await asyncio.gather(*fw.sim.flaky.background)
        return
    elif fp == "FP6":
        a, b = fw.worker(sd, name="a"), fw.worker(sd, name="b")
        await a.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(a, b, duplicate=True), sd)
        return
    elif fp == "FP7":
        stalled, resume = asyncio.Event(), asyncio.Event()

        async def stall(point):
            stalled.set()
            await resume.wait()

        old = fw.worker(sd, Faults(fw.sessions, enabled=True, on_hit=lambda p: None, before_hit=stall), name="old")
        run_id = await old.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.dispatcher(old).pump_once()
        await fw.arm("FP7")
        stale = asyncio.create_task(old.advance(run_id, Stage.SUBMIT))
        await asyncio.wait_for(stalled.wait(), 10)
        await asyncio.sleep(LEASE + 0.2)
        new = fw.worker(sd, name="new")
        await new.advance(run_id, Stage.SUBMIT)
        resume.set()
        await stale
        await fw.until_done(fw.dispatcher(new), sd)
        return
    elif fp == "FP9":
        await fw.sim_fault(sd, "throttle", n=2)
        await w1.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(w1), sd)
        return
    elif fp == "FP10":
        # someone else holds the whole payment between prepare's unlocked check and its locks
        async def someone_else_reserves(point):
            async with fw.sessions() as s, s.begin():
                await s.execute(
                    update(Payment).where(Payment.id == plan.payment_id).values(reserved_minor=Payment.amount_minor)
                )

        w = fw.worker(sd, Faults(fw.sessions, enabled=True, on_hit=lambda p: None, before_hit=someone_else_reserves))
        await fw.arm("FP10")
        await w.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(w), sd)
        return
    elif fp == "lost_response":
        fw.sim.flaky.lose_after = 1  # the provider commits, the response never arrives
        await w1.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(w1), sd)
        return
    elif fp == "reset_before_send":
        fw.sim.flaky.drop_before = 1  # the connection drops before the request leaves
        await w1.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(w1), sd)
        return
    # crash families: a fresh worker picks it up once the dead one's lease runs out
    await asyncio.sleep(LEASE + 0.2)
    await fw.until_done(fw.dispatcher(fw.worker(sd, name="second")), sd)


@pytest.mark.parametrize(("fp", "inst"), TRIALS, ids=[f"{fp}-{i.seed}" for fp, i in TRIALS])
@pytest.mark.parametrize("faulted", [True, False], ids=["fault", "control"])
async def test_e3_fault_recovery(fw, fp, inst, faulted):
    sd, plan = await fw.seed(inst, f"e3:{fp}:{inst.seed}")
    if faulted:
        await inject(fw, fp, sd, plan)
    else:
        w = fw.worker(sd)
        await w.start_scripted(sd.tenant_id, sd.case_id, plan)
        await fw.until_done(fw.dispatcher(w), sd)
    v, refunds, case, cmds = await fw.outcome(sd)
    owed = sd.label["financial"]["amount_minor"]
    # never more than one provider refund, never more than the entitlement, whatever happened
    safe = len(refunds) <= 1 and sum(r["amount"] for r in refunds) <= owed
    if faulted and fp == "FP10":
        # recovered = refused under the locks: nothing sent, a person decides
        recovered = case.state == S.ESCALATED.value and refunds == [] and cmds == []
    else:
        # recovered = exactly one refund for the right amount, closed, independent verifier passes
        recovered = (case.state, case.resolution) == (S.CLOSED.value, "REFUNDED") and v.passed
    if FULL:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        row = {"fault": fp, "instance": inst.seed, "faulted": faulted, "recovered": recovered, "safe": safe}
        with RESULTS.open("a") as f:
            f.write(json.dumps(row) + "\n")
    assert safe, refunds
    assert recovered, (case.state, case.resolution, [c.state for c in cmds], v.failed())
