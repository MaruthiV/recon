import pytest
from sqlalchemy import select

from recon_agents.meter import Budget
from recon_agents.roles import COORDINATOR
from recon_domain.states import CaseState as S
from recon_fixtures.dup_charge import scripted_plan, seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import AuditEvent, Case, Command, Plan, Run
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.investigate import AgentConfig, Investigator
from recon_worker.pipeline import Pipeline
from tests.agents.fakes import FAKE_PRICING, coordinator, fake_models
from tests.conftest import MCP_URL, RUN_KEY, T


@pytest.fixture
async def stale(app_sessions, flaky_sim, op, verifier_engines, mcp_read, clock, adk_sessions):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    mcp_read.read_keys[sd.tenant_id] = sd.read_key
    conn = HttpConnector(flaky_sim, sd.api_key)

    def worker(models=None):
        inv = None
        if models is not None:
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
        return Pipeline(app_sessions, clock, ex, inv, worker_id="w", reconcile_backoff_seconds=0)

    async def drain(w):
        async def deliver(d):
            return (await w.advance(d.run_id, d.stage, d.event_id)).status

        disp = Dispatcher(app_sessions, deliver)
        for _ in range(30):
            if await disp.pump_once() == 0:
                return

    async def rows(model, *where):
        async with app_sessions() as s:
            return list((await s.execute(select(model).where(*where))).scalars())

    return sd, worker, drain, rows


def stale_first():
    # the first plan is built on an old case version, every later one is current
    old, fresh = coordinator(case_version=0), coordinator()
    answers = [0]

    def script(req):
        parts = (old if answers[0] == 0 else fresh)(req)
        if any(p.text or (p.function_call and p.function_call.name == "set_model_response") for p in parts):
            answers[0] += 1
        return parts

    return script


async def test_a_stale_agent_plan_gets_a_fresh_run_that_can_finish(stale):
    sd, worker, drain, rows = stale
    w = worker(fake_models(**{COORDINATOR: stale_first()}))
    await w.start_agent(sd.tenant_id, sd.case_id)
    await drain(w)

    [case] = await rows(Case)
    assert (case.state, case.resolution) == (S.CLOSED.value, "REFUNDED")
    assert [p.decision for p in sorted(await rows(Plan), key=lambda p: p.created_at)] == ["STALE", "AUTHORIZE"]
    assert len(await rows(Run)) == 2 and len(await rows(Command)) == 1
    assert len(await rows(AuditEvent, AuditEvent.kind == "reinvestigation_queued")) == 1


async def test_a_plan_stale_twice_goes_to_a_person_without_money(stale):
    sd, worker, drain, rows = stale
    w = worker(fake_models(**{COORDINATOR: coordinator(case_version=0)}))
    await w.start_agent(sd.tenant_id, sd.case_id)
    await drain(w)

    [case] = await rows(Case)
    # never stranded at INVESTIGATING, and never a third model run
    assert case.state == S.ESCALATED.value
    assert len(await rows(Run)) == 2 and await rows(Command) == []


async def test_a_stale_scripted_plan_escalates_instead_of_hanging(stale):
    sd, worker, drain, rows = stale
    w = worker()
    await w.start_scripted(sd.tenant_id, sd.case_id, scripted_plan(sd, case_version=0))
    await drain(w)
    [case] = await rows(Case)
    assert case.state == S.ESCALATED.value and await rows(Command) == []
