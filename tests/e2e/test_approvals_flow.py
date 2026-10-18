from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from recon_fixtures.dup_charge import scripted_plan, seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Command, Plan
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import NotStartable, Pipeline
from tests.conftest import T

BIG = 25_000  # over acme's 100.00 auto-approve limit


@pytest.fixture
async def ap(app_sessions, flaky_sim, op, verifier_engines, clock):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, amount_minor=BIG)
    conn = HttpConnector(flaky_sim, sd.api_key)
    w = Pipeline(
        app_sessions, clock, Executor(app_sessions, clock, lambda _t: conn), worker_id="w", reconcile_backoff_seconds=0
    )

    async def drain():
        disp = Dispatcher(app_sessions, lambda d: _adv(w, d))
        for _ in range(20):
            if await disp.pump_once() == 0:
                return

    async def one(model, *where):
        async with app_sessions() as s:
            return (await s.execute(select(model).where(*where))).scalar_one_or_none()

    async def refunds():
        return (await flaky_sim.get("/sim/state", headers=op)).json()["refunds"]

    async def verify():
        e = verifier_engines
        return await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)

    await w.start_scripted(sd.tenant_id, sd.case_id, scripted_plan(sd, amount_minor=BIG))
    await drain()
    plan = await one(Plan)
    return SimpleNamespace(sd=sd, w=w, drain=drain, one=one, refunds=refunds, verify=verify, plan=plan, clock=clock)


async def _adv(w, d):
    return (await w.advance(d.run_id, d.stage, d.event_id)).status


async def test_over_the_limit_waits_and_then_runs_on_an_exact_approval(ap):
    assert (await ap.one(Case)).state == "AWAITING_APPROVAL" and await ap.one(Command) is None
    with pytest.raises(NotStartable, match="changed since"):
        await ap.w.approve(ap.sd.tenant_id, ap.plan.id, "0" * 64, "sam", "supervisor", "APPROVED")
    await ap.w.approve(ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "sam", "supervisor", "APPROVED")
    await ap.drain()

    case = await ap.one(Case)
    assert (case.state, case.resolution) == ("CLOSED", "REFUNDED")
    [refund] = await ap.refunds()
    assert refund["amount"] == BIG
    verdict = await ap.verify()
    assert verdict.passed, verdict.failed()


async def test_a_no_closes_the_case_without_money(ap):
    await ap.w.approve(ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "sam", "supervisor", "REJECTED")
    await ap.drain()
    case = await ap.one(Case)
    assert (case.state, case.resolution) == ("CLOSED", "DENIED")
    assert await ap.one(Command) is None and await ap.refunds() == []


async def test_an_expired_approval_moves_nothing(ap):
    await ap.w.approve(
        ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "sam", "supervisor", "APPROVED", ttl=timedelta(seconds=0)
    )
    await ap.drain()
    # prepare re-checks the approval on fresh facts, an expired one sends it back to wait
    assert (await ap.one(Case)).state == "AWAITING_APPROVAL"
    assert await ap.one(Command) is None and await ap.refunds() == []


async def test_an_expired_approval_can_be_renewed(ap):
    await ap.w.approve(
        ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "sam", "supervisor", "APPROVED", ttl=timedelta(seconds=0)
    )
    await ap.drain()
    await ap.w.approve(ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "sam", "supervisor", "APPROVED")
    await ap.drain()
    assert (await ap.one(Case)).resolution == "REFUNDED"
    assert len(await ap.refunds()) == 1
    assert (await ap.verify()).passed


async def test_a_decided_case_cant_be_decided_again(ap):
    await ap.w.approve(ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "sam", "supervisor", "APPROVED")
    with pytest.raises(NotStartable):
        await ap.w.approve(ap.sd.tenant_id, ap.plan.id, ap.plan.material_hash, "kim", "supervisor", "REJECTED")
