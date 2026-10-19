import pytest
from sqlalchemy import select

from recon_domain.states import CaseState as S
from recon_fixtures.dup_charge import scripted_plan, seed_duplicate_charge
from recon_fixtures.families import _provider_refund
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Command, Plan
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline
from tests.conftest import T


@pytest.fixture
async def ar(app_sessions, flaky_sim, op, verifier_engines, clock):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    conn = HttpConnector(flaky_sim, sd.api_key)

    async def resolve(plan, reader=True):
        ex = Executor(app_sessions, clock, lambda _t: conn)
        if not reader:
            ex.reader_for = None  # a worker with no read key at all
        w = Pipeline(app_sessions, clock, ex, worker_id="w", reconcile_backoff_seconds=0)
        await w.start_scripted(sd.tenant_id, sd.case_id, plan)

        async def deliver(d):
            return (await w.advance(d.run_id, d.stage, d.event_id)).status

        disp = Dispatcher(app_sessions, deliver)
        for _ in range(10):
            if await disp.pump_once() == 0:
                break
        async with app_sessions() as s:
            case = (await s.execute(select(Case))).scalar_one()
            row = (await s.execute(select(Plan))).scalar_one()
            cmds = (await s.execute(select(Command))).scalars().all()
        return case, row, cmds

    claim = scripted_plan(
        sd,
        recommended_disposition="ALREADY_REFUNDED",
        reason_code="EXISTING_REFUND",
        amount_minor=None,
        currency=None,
        explanation="The duplicate charge was already refunded.",
    )
    return sd, resolve, claim


async def test_a_made_up_refund_is_refused_not_told_to_the_customer(ar):
    _, resolve, claim = ar
    case, plan, cmds = await resolve(claim)
    assert plan.decision == "REJECT" and "no refund exists" in plan.verdict["reasons"][0]
    assert case.state == S.ESCALATED.value and cmds == []


async def test_a_refund_only_the_provider_knows_about_is_confirmed(ar, flaky_sim, op):
    sd, resolve, claim = ar
    t = {"account": sd.account, "api_key": sd.api_key}
    await _provider_refund(flaky_sim, op, t, sd.duplicate_provider_id, 4900, pending=True)
    case, plan, cmds = await resolve(claim)
    assert plan.decision == "NO_MONEY"
    assert (case.state, case.resolution) == (S.CLOSED.value, "ALREADY_REFUNDED") and cmds == []


async def test_with_no_way_to_look_it_goes_to_a_person(ar):
    _, resolve, claim = ar
    case, plan, _ = await resolve(claim, reader=False)
    assert plan.decision == "ESCALATE" and case.state == S.ESCALATED.value
