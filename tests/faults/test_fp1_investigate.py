from sqlalchemy import select, text

from recon_agents.meter import Budget
from recon_agents.roles import COORDINATOR
from recon_domain.states import CaseState as S
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import Case, ModelUsage, Plan, Run
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.faults import SimulatedCrash
from recon_worker.investigate import AgentConfig, Investigator
from recon_worker.pipeline import Pipeline, Stage
from tests.agents.fakes import FAKE_PRICING, coordinator, fake_models
from tests.conftest import MCP_URL, RUN_KEY, T


def dies_on_first_call(script):
    calls = [0]

    def wrapped(req):
        calls[0] += 1
        if calls[0] == 1:
            raise SimulatedCrash("FP1")  # worker process gone mid-investigation
        return script(req)

    return wrapped


async def test_fp1_crash_mid_investigation_restarts_fresh_and_refunds_once(
    app_sessions, flaky_sim, op, verifier_engines, mcp_read, clock, adk_sessions
):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    mcp_read.read_keys[sd.tenant_id] = sd.read_key
    conn = HttpConnector(flaky_sim, sd.api_key)

    def worker(models, name):
        cfg = AgentConfig(
            models=models,
            mode="scripted",
            pricing=FAKE_PRICING,
            mcp_url=MCP_URL,
            token_key=RUN_KEY,
            budget=Budget(),
            http_factory=mcp_read.http_factory,
        )
        inv = Investigator(app_sessions, clock, cfg, adk_sessions)
        ex = Executor(app_sessions, clock, lambda _t: conn)
        return Pipeline(app_sessions, clock, ex, inv, worker_id=name, lease_seconds=30, reconcile_backoff_seconds=0)

    first = worker(fake_models(**{COORDINATOR: dies_on_first_call(coordinator())}), "first")
    run_id = await first.start_agent(sd.tenant_id, sd.case_id)

    async def via(w):
        async def deliver(d):
            try:
                return (await w.advance(d.run_id, d.stage, d.event_id)).status
            except SimulatedCrash:
                return 0

        return Dispatcher(app_sessions, deliver, visibility_seconds=30)

    await (await via(first)).pump_once()
    async with app_sessions() as s:
        run = (await s.execute(select(Run))).scalar_one()
        assert (run.stage, run.lease_owner) == (Stage.INVESTIGATE.value, "first")
        assert (await s.execute(select(Plan))).first() is None  # no half-made plan survives a crash
        crashed_calls = (await s.execute(select(ModelUsage))).scalars().all()
    assert crashed_calls  # the specialists' calls were billed and are kept

    # time passes, a fresh worker gets the redelivery
    async with app_sessions() as s, s.begin():
        await s.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second'"))
        await s.execute(text("UPDATE outbox_events SET available_at = now() WHERE dispatched_at IS NULL"))
    second = await via(worker(fake_models(), "second"))
    for _ in range(10):
        await second.pump_once()

    async with app_sessions() as s:
        case = (await s.execute(select(Case))).scalar_one()
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        plans = list((await s.execute(select(Plan))).scalars())
        usage = list((await s.execute(select(ModelUsage))).scalars())
    assert (case.state, case.resolution) == (S.CLOSED.value, "REFUNDED")
    assert (run.stage, run.investigation) == (Stage.DONE.value, "SUCCEEDED")
    assert [p.source for p in plans] == ["agent"]
    assert len(usage) > len(crashed_calls)  # both attempts' model calls are on the books
    assert len((await flaky_sim.get("/sim/state", headers=op)).json()["refunds"]) == 1
    e = verifier_engines
    verdict = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
    assert verdict.passed, verdict.failed()
