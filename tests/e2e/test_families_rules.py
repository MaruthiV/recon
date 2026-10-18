from collections import Counter

import pytest
from sqlalchemy import select

from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import FAMILIES, make_specs, materialize
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Plan
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline
from recon_worker.rules_planner import plan_by_rules
from tests.conftest import T

# the rules baseline can't see these, by construction, so they're expected misses
RULES_BLIND = {
    ("missing_refund", "pending"): "a pending provider refund isn't in recon's own records",
}


@pytest.fixture
async def graded(app_sessions, flaky_sim, op, verifier_engines, clock):
    ids = seeded_ids("families-dev-1")
    specs = make_specs("dev", per_family=5, seed=1)
    seeded = [
        await materialize(sp, app_sessions, flaky_sim, op, verifier_engines["eval"], T, "dev", ids) for sp in specs
    ]
    keys = {s.tenant_id: s.api_key for s in seeded}
    conns = {t: HttpConnector(flaky_sim, k) for t, k in keys.items()}
    w = Pipeline(
        app_sessions,
        clock,
        Executor(app_sessions, clock, conns.__getitem__),
        worker_id="w",
        reconcile_backoff_seconds=0,
    )
    for sd in seeded:
        async with app_sessions() as s:
            plan = await plan_by_rules(s, sd.tenant_id, sd.case_id, clock.now())
        await w.start_scripted(sd.tenant_id, sd.case_id, plan, source="rules")

    async def deliver(d):
        return (await w.advance(d.run_id, d.stage, d.event_id)).status

    disp = Dispatcher(app_sessions, deliver)

    async def drain():
        for _ in range(200):
            if await disp.pump_once() == 0:
                return

    await drain()
    # a supervisor signs off whatever waits for approval, on the exact plan they're shown
    async with app_sessions() as s:
        waiting = (
            (await s.execute(select(Plan).join(Case, Case.id == Plan.case_id).where(Case.state == "AWAITING_APPROVAL")))
            .scalars()
            .all()
        )
    for p in waiting:
        await w.approve(p.tenant_id, p.id, p.material_hash, "sam", "supervisor", "APPROVED")
    await drain()
    e = verifier_engines
    return [(sd, await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)) for sd in seeded]


async def test_every_family_is_seeded_and_graded(graded):
    assert Counter(sd.family for sd, _ in graded) == dict.fromkeys(FAMILIES, 5)


async def test_rules_baseline_never_moves_money_it_shouldnt(graded):
    for sd, v in graded:
        effect = {c.name: c for c in v.checks}
        # identity, amount and provider effect are the money checks, they hold even when the answer is wrong
        assert effect["4_provider_effect"].ok or sd.label["financial"], (sd.family, sd.variant, v.failed())
        assert effect["3_amount"].ok or sd.label["financial"], (sd.family, sd.variant, v.failed())


async def test_rules_baseline_passes_everything_it_can_see(graded):
    unexpected = [
        (sd.family, sd.variant, v.failed())
        for sd, v in graded
        if not v.passed and (sd.family, sd.variant) not in RULES_BLIND
    ]
    assert not unexpected, unexpected
    blind = [(sd.family, sd.variant) for sd, v in graded if (sd.family, sd.variant) in RULES_BLIND]
    assert all(not v.passed for sd, v in graded if (sd.family, sd.variant) in RULES_BLIND), blind
