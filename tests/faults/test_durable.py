import asyncio
from itertools import count
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import AuditEvent, Case, Command, CommandAttempt, OutboxEvent, Run
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher, Sweeper
from recon_worker.executor import Executor
from recon_worker.faults import Faults, SimulatedCrash, raise_crash
from recon_worker.pipeline import Pipeline, Stage
from tests.conftest import T
from tests.e2e.test_scripted_dup_charge import scripted_plan

LEASE = 1


@pytest.fixture
async def pw(app_sessions, flaky_sim, op, verifier_engines, clock):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    conn = HttpConnector(flaky_sim, sd.api_key)
    names = count(1)

    def worker(faults=None, name=None):
        f = faults or Faults(app_sessions, enabled=True, on_hit=raise_crash)
        ex = Executor(app_sessions, clock, lambda _t: conn, faults=f)
        return Pipeline(
            app_sessions,
            clock,
            ex,
            worker_id=name or f"w{next(names)}",
            lease_seconds=LEASE,
            faults=f,
            reconcile_backoff_seconds=0,
        )

    def dispatcher(*workers, duplicate=False):
        turn = count()

        async def deliver(d):
            w = workers[next(turn) % len(workers)]
            return (await w.advance(d.run_id, d.stage, d.event_id)).status

        return Dispatcher(app_sessions, deliver, visibility_seconds=LEASE, duplicate=duplicate)

    async def drain(disp, rounds=30):
        for _ in range(rounds):
            if await disp.pump_once() == 0 and await pending() == 0:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("outbox never drained")

    async def pending():
        async with app_sessions() as s:
            q = text("SELECT count(*) FROM outbox_events WHERE dispatched_at IS NULL")
            return (await s.execute(q)).scalar_one()

    async def arm(point, scope=None, n=1):
        async with app_sessions() as s, s.begin():
            await s.execute(
                text("INSERT INTO fault_points (point, scope_id, remaining) VALUES (:p, :s, :n)"),
                {"p": point, "s": scope, "n": n},
            )

    async def rows(model, *where):
        async with app_sessions() as s:
            return list((await s.execute(select(model).where(*where))).scalars())

    async def refunds():
        return (await flaky_sim.get("/sim/state", headers=op)).json()["refunds"]

    async def refund_posts():
        log = (await flaky_sim.get("/sim/state", headers=op)).json()["requests_log"]
        return [r for r in log if r["method"] == "POST" and r["path"] == "/v1/refunds"]

    async def verify():
        e = verifier_engines
        return await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)

    async def case_state():
        [c] = await rows(Case, Case.id == sd.case_id)
        return S(c.state), c.resolution

    async def wait_out_lease():
        await asyncio.sleep(LEASE + 0.2)

    return SimpleNamespace(
        sd=sd,
        worker=worker,
        dispatcher=dispatcher,
        drain=drain,
        arm=arm,
        rows=rows,
        refunds=refunds,
        refund_posts=refund_posts,
        verify=verify,
        case_state=case_state,
        wait_out_lease=wait_out_lease,
        sim=flaky_sim,
        op=op,
    )


async def assert_one_refund_closed(pw):
    assert await pw.case_state() == (S.CLOSED, "REFUNDED")
    [refund] = await pw.refunds()
    assert (refund["payment_intent"], refund["amount"]) == (pw.sd.duplicate_provider_id, 4900)
    [cmd] = await pw.rows(Command)
    assert cmd.state == C.CONFIRMED_SUCCESS.value and cmd.provider_refund_id == refund["id"]
    [run] = await pw.rows(Run)
    assert run.stage == Stage.DONE.value and run.lease_owner is None
    verdict = await pw.verify()
    assert verdict.passed, verdict.failed()


async def test_pipeline_runs_a_scripted_case_end_to_end(pw):
    w = pw.worker()
    await w.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    await pw.drain(pw.dispatcher(w))

    await assert_one_refund_closed(pw)
    moves = [(a.data["src"], a.data["dst"]) for a in await pw.rows(AuditEvent, AuditEvent.kind == "stage_advanced")]
    assert moves == [("PREPARE", "SUBMIT"), ("SUBMIT", "VERIFY"), ("VERIFY", "DONE")]
    assert all(e.dispatched_at is not None for e in await pw.rows(OutboxEvent))


async def test_fp2_prepared_command_executes_once(pw):
    await pw.arm("FP2")
    w1 = pw.worker()
    await w1.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    with pytest.raises(SimulatedCrash):
        await pw.dispatcher(w1).pump_once()
    [cmd] = await pw.rows(Command)
    assert cmd.state == C.PREPARED.value
    [run] = await pw.rows(Run)
    assert (run.stage, run.lease_owner) == ("PREPARE", "w1")

    await pw.wait_out_lease()
    await pw.drain(pw.dispatcher(pw.worker(name="w2")))

    await assert_one_refund_closed(pw)
    assert len(await pw.rows(Command)) == 1
    audits = [a.kind for a in await pw.rows(AuditEvent)]
    assert "lease_taken_over" in audits and "prepare_converged" in audits


async def test_fp3_crash_before_send_resubmits_same_key_once(pw):
    w1 = pw.worker()
    await w1.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    disp = pw.dispatcher(w1)
    await disp.pump_once()  # PREPARE
    await pw.arm("FP3")
    with pytest.raises(SimulatedCrash):
        await disp.pump_once()  # SUBMIT dies before the http call
    [cmd] = await pw.rows(Command)
    assert cmd.state == C.SUBMITTING.value
    assert await pw.refund_posts() == []

    await pw.wait_out_lease()
    await pw.drain(pw.dispatcher(pw.worker(name="w2")))

    await assert_one_refund_closed(pw)
    [post] = await pw.refund_posts()
    assert post["idempotency_key"] == cmd.idempotency_key
    assert len(await pw.rows(CommandAttempt)) == 2


async def test_fp4_crash_after_provider_commit_in_process(pw):
    w1 = pw.worker()
    await w1.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    disp = pw.dispatcher(w1)
    await disp.pump_once()
    await pw.arm("FP4")
    with pytest.raises(SimulatedCrash):
        await disp.pump_once()
    assert len(await pw.refunds()) == 1
    [cmd] = await pw.rows(Command)
    assert cmd.state == C.SUBMITTING.value

    await pw.wait_out_lease()
    await pw.drain(pw.dispatcher(pw.worker(name="w2")))

    await assert_one_refund_closed(pw)
    # matched by metadata, never re-sent
    assert len(await pw.refund_posts()) == 1
    assert len(await pw.rows(CommandAttempt)) == 1


async def test_fp6_concurrent_delivery_converges(pw):
    a, b = pw.worker(name="a"), pw.worker(name="b")
    await a.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    await pw.drain(pw.dispatcher(a, b, duplicate=True))

    await assert_one_refund_closed(pw)
    assert len(await pw.refund_posts()) == 1
    # every stage advanced exactly once despite double delivery
    moves = [(a.data["src"], a.data["dst"]) for a in await pw.rows(AuditEvent, AuditEvent.kind == "stage_advanced")]
    assert moves == [("PREPARE", "SUBMIT"), ("SUBMIT", "VERIFY"), ("VERIFY", "DONE")]


async def test_fp7_stale_lease_writes_rejected(pw, app_sessions):
    stalled, resume = asyncio.Event(), asyncio.Event()

    async def stall(point):
        stalled.set()
        await resume.wait()

    old = pw.worker(Faults(app_sessions, enabled=True, on_hit=lambda p: None, before_hit=stall), name="old")
    run_id = await old.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    await pw.dispatcher(old).pump_once()  # PREPARE
    await pw.arm("FP7")
    stale = asyncio.create_task(old.advance(run_id, Stage.SUBMIT))
    await asyncio.wait_for(stalled.wait(), 10)
    assert len(await pw.refunds()) == 1  # old worker's refund committed, now it's stalled

    await pw.wait_out_lease()
    new = pw.worker(name="new")
    took = await new.advance(run_id, Stage.SUBMIT)
    assert (took.status, took.next) == (200, Stage.VERIFY)

    resume.set()
    late = await stale
    assert late.status == 409 and "fenced" in late.detail

    await pw.drain(pw.dispatcher(new))
    await assert_one_refund_closed(pw)
    audits = [a.kind for a in await pw.rows(AuditEvent)]
    assert "stale_submit_result" in audits and "lease_taken_over" in audits
    verify_events = await pw.rows(OutboxEvent, OutboxEvent.stage == "VERIFY")
    assert len(verify_events) == 1


async def test_fp9_throttle_backoff_same_command(pw):
    await pw.sim.post("/sim/faults", json={"account": pw.sd.account, "kind": "throttle", "count": 2}, headers=pw.op)
    w = pw.worker()
    await w.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    await pw.drain(pw.dispatcher(w))

    await assert_one_refund_closed(pw)
    [cmd] = await pw.rows(Command)
    posts = await pw.refund_posts()
    assert [p["status"] for p in posts] == [429, 429, 200]
    assert {p["idempotency_key"] for p in posts} == {cmd.idempotency_key}
    assert len(await pw.rows(CommandAttempt)) == 3


async def test_sweeper_repairs_a_run_with_no_pending_event(pw, app_sessions):
    w = pw.worker()
    await w.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    # lose the event: marked dispatched but nobody ever ran it
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE outbox_events SET dispatched_at = now()"))
    disp = pw.dispatcher(w)
    assert await disp.pump_once() == 0

    assert await Sweeper(app_sessions).sweep_once() == 1
    assert await Sweeper(app_sessions).sweep_once() == 0  # idempotent while the repair is pending
    await pw.drain(disp)
    await assert_one_refund_closed(pw)


async def test_fp5_late_commit_same_key_resubmit_dedups(pw, app_sessions, clock):
    # provider holds the request 2s, client gives up at 0.2s, recon reconciles at ~1s and resubmits the same key
    await pw.sim.post(
        "/sim/faults",
        json={"account": pw.sd.account, "kind": "slow", "count": 1, "params": {"seconds": 2}},
        headers=pw.op,
    )
    pw.sim.flaky.give_up_early = 1
    conn = HttpConnector(pw.sim, pw.sd.api_key)
    w = Pipeline(
        app_sessions,
        clock,
        Executor(app_sessions, clock, lambda _t: conn),
        worker_id="w",
        lease_seconds=LEASE,
        reconcile_backoff_seconds=1,
    )
    await w.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))
    disp = pw.dispatcher(w)
    for _ in range(200):
        await disp.pump_once()
        if (await pw.case_state())[0] in (S.CLOSED, S.ESCALATED):
            break
        await asyncio.sleep(0.05)
    await asyncio.gather(*pw.sim.flaky.background)

    await assert_one_refund_closed(pw)
    [cmd] = await pw.rows(Command)
    posts = await pw.refund_posts()
    assert {p["idempotency_key"] for p in posts} == {cmd.idempotency_key}
    # the resubmit hit the provider while the first was still in flight, and was turned away
    assert 409 in [p["status"] for p in posts]
    assert len(await pw.rows(CommandAttempt)) >= 2


async def test_an_event_that_never_succeeds_is_parked_not_retried_forever(pw, app_sessions):
    w = pw.worker()
    await w.start_scripted(pw.sd.tenant_id, pw.sd.case_id, scripted_plan(pw.sd))

    async def always_500(d):
        return 500

    disp = Dispatcher(app_sessions, always_500, visibility_seconds=0, max_attempts=3)
    for _ in range(3):
        async with app_sessions() as s, s.begin():
            await s.execute(text("UPDATE outbox_events SET available_at = now()"))
        assert await disp.pump_once() == 1
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE outbox_events SET available_at = now()"))
    assert await disp.pump_once() == 0  # parked, nobody claims it again
    [evt] = await pw.rows(OutboxEvent)
    assert evt.dead_at is not None and "dead after 3 attempts" in (evt.last_error or "")
    assert await Sweeper(app_sessions).sweep_once() == 0  # and the sweeper doesn't resurrect it
