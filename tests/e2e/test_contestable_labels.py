import pytest

from recon_domain.plan import ResolutionPlan
from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import make_specs, materialize
from recon_provider.http import HttpConnector
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.pipeline import Pipeline
from tests.conftest import T


@pytest.mark.parametrize(
    ("disposition", "reason", "passes"),
    [
        ("DENY", "INELIGIBLE", True),
        ("REQUEST_INFORMATION", "MISSING_EVIDENCE", True),
        ("ESCALATE", "MISSING_EVIDENCE", False),
    ],
)
async def test_a_return_not_yet_received_can_be_denied_or_waited_on(
    app_sessions, flaky_sim, op, verifier_engines, clock, disposition, reason, passes
):
    spec = next(s for s in make_specs("dev", per_family=3, seed=1) if s.variant == "not_received")
    sd = await materialize(spec, app_sessions, flaky_sim, op, verifier_engines["eval"], T, "dev", seeded_ids("contest"))
    conn = HttpConnector(flaky_sim, sd.api_key)
    w = Pipeline(app_sessions, clock, Executor(app_sessions, clock, lambda _t: conn), worker_id="w")
    plan = ResolutionPlan(
        case_id=sd.case_id,
        case_version=1,
        recommended_disposition=disposition,
        reason_code=reason,
        explanation="The return hasn't arrived yet.",
        missing_evidence=["return received"] if disposition == "REQUEST_INFORMATION" else [],
    )
    await w.start_scripted(sd.tenant_id, sd.case_id, plan)

    async def deliver(d):
        return (await w.advance(d.run_id, d.stage, d.event_id)).status

    disp = Dispatcher(app_sessions, deliver)
    for _ in range(10):
        if await disp.pump_once() == 0:
            break
    e = verifier_engines
    v = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
    assert v.passed is passes, v.failed()
