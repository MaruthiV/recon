import os

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text

from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Command, Payment, PaymentReservation, RefundClaim, Run
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.faults import Faults, SimulatedCrash, raise_crash
from recon_worker.pipeline import Pipeline, Stage
from tests.conftest import T, truncate_all
from tests.e2e.test_scripted_dup_charge import scripted_plan

ACTIONS = [
    "pump",
    "pump_twice_at_once",
    "crash_fp2",
    "crash_fp3",
    "crash_fp4",
    "expire_leases",
    "throttle",
    "lose_response",
    "drop_request",
]
EXAMPLES = int(os.environ.get("RECON_INTERLEAVINGS", "25"))
TERMINAL = {C.CONFIRMED_SUCCESS.value, C.CONFIRMED_FAILURE.value}


async def check_money(s, sim, op):
    refunds = (await sim.get("/sim/state", headers=op)).json()["refunds"]
    cmds = list((await s.execute(select(Command))).scalars())
    for cmd in cmds:
        mine = [r for r in refunds if r["metadata"].get("recon_command_id") == cmd.id]
        assert len(mine) <= 1, f"{cmd.id} has {len(mine)} provider refunds"
        res = (await s.execute(select(PaymentReservation).where(PaymentReservation.command_id == cmd.id))).scalar_one()
        # money stays held exactly while the outcome is open
        assert (res.state == "ACTIVE") == (cmd.state not in TERMINAL), (cmd.state, res.state)
        if cmd.state == C.CONFIRMED_SUCCESS.value:
            assert len(mine) == 1 and mine[0]["status"] == "succeeded" and cmd.provider_refund_id == mine[0]["id"]
    by_pi: dict[str, int] = {}
    for r in refunds:
        if r["status"] in ("succeeded", "pending"):
            by_pi[r["payment_intent"]] = by_pi.get(r["payment_intent"], 0) + r["amount"]
    for p in (await s.execute(select(Payment))).scalars():
        assert by_pi.get(p.provider_payment_id, 0) <= p.amount_minor
        assert p.refunded_minor + p.reserved_minor <= p.amount_minor
    for c in (await s.execute(select(RefundClaim))).scalars():
        assert c.reserved_minor + c.consumed_minor <= c.permitted_minor


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(schedule=st.lists(st.sampled_from(ACTIONS), min_size=3, max_size=14))
async def test_interleavings_keep_every_money_invariant(
    schedule, dbs, app_sessions, flaky_sim, op, verifier_engines, clock
):
    await truncate_all(dbs)
    flaky_sim.flaky.drop_before = flaky_sim.flaky.lose_after = flaky_sim.flaky.give_up_early = 0
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    conn = HttpConnector(flaky_sim, sd.api_key)
    faults = Faults(app_sessions, enabled=True, on_hit=raise_crash)

    def worker(name):
        ex = Executor(app_sessions, clock, lambda _t: conn, faults=faults)
        return Pipeline(
            app_sessions, clock, ex, worker_id=name, lease_seconds=30, faults=faults, reconcile_backoff_seconds=0
        )

    a, b = worker("a"), worker("b")
    turn = [0]

    async def deliver(d):
        w = (a, b)[turn[0] % 2]
        turn[0] += 1
        try:
            return (await w.advance(d.run_id, d.stage, d.event_id)).status
        except SimulatedCrash:
            return 0  # the worker process died, the dispatcher sees a reset connection

    single = Dispatcher(app_sessions, deliver, visibility_seconds=30)
    double = Dispatcher(app_sessions, deliver, visibility_seconds=30, duplicate=True)

    async def arm(point):
        async with app_sessions() as s, s.begin():
            await s.execute(text("INSERT INTO fault_points (point) VALUES (:p)"), {"p": point})

    async def expire():
        # time passes: leases and dispatcher visibility both run out
        async with app_sessions() as s, s.begin():
            await s.execute(
                text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE lease_owner IS NOT NULL")
            )
            await s.execute(text("UPDATE outbox_events SET available_at = now() WHERE dispatched_at IS NULL"))

    await a.start_scripted(sd.tenant_id, sd.case_id, scripted_plan(sd))
    for action in schedule:
        match action:
            case "pump":
                await single.pump_once()
            case "pump_twice_at_once":
                await double.pump_once()
            case "crash_fp2" | "crash_fp3" | "crash_fp4":
                await arm(action.split("_")[1].upper())
            case "expire_leases":
                await expire()
            case "throttle":
                await flaky_sim.post("/sim/faults", json={"account": sd.account, "kind": "throttle"}, headers=op)
            case "lose_response":
                flaky_sim.flaky.lose_after += 1
            case "drop_request":
                flaky_sim.flaky.drop_before += 1
        async with app_sessions() as s:
            await check_money(s, flaky_sim, op)

    # then let it run to rest: no new faults, time keeps passing
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE fault_points SET remaining = 0"))
    flaky_sim.flaky.drop_before = flaky_sim.flaky.lose_after = 0
    for _ in range(40):
        async with app_sessions() as s:
            run = (await s.execute(select(Run))).scalar_one()
        if run.stage == Stage.DONE.value:
            break
        await expire()
        await single.pump_once()

    async with app_sessions() as s:
        await check_money(s, flaky_sim, op)
        run = (await s.execute(select(Run))).scalar_one()
        case = (await s.execute(select(Case))).scalar_one()
        cmd = (await s.execute(select(Command))).scalar_one_or_none()
    assert run.stage == Stage.DONE.value, f"never came to rest: {run.stage}"
    event(f"ended {case.state} with command {cmd.state if cmd else None}")
    if case.state == S.CLOSED.value:
        assert case.resolution == "REFUNDED"
        e = verifier_engines
        verdict = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
        assert verdict.passed, verdict.failed()
    else:
        # escalated is allowed, but only with the money either settled or still held
        assert case.state == S.ESCALATED.value and cmd is not None, (case.state, schedule)
