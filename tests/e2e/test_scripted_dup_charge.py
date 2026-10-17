import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from recon_domain.connector import FakeClock
from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_domain.validation import Decision
from recon_fixtures.dup_charge import scripted_plan, seed_duplicate_charge
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Command, CommandAttempt, Payment, PaymentReservation, RefundClaim
from recon_verifier.checks import verify_case
from recon_worker.executor import Executor

T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def world(app_sessions, flaky_sim, op, verifier_engines):
    clock = FakeClock(T)
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    conn = HttpConnector(flaky_sim, sd.api_key)
    ex = Executor(app_sessions, clock, lambda _tenant: conn)

    async def verify(case_id=None):
        e = verifier_engines
        return await verify_case(e["recon"], e["sim"], e["eval"], case_id or sd.case_id)

    async def sim_refunds():
        state = (await flaky_sim.get("/sim/state", headers=op)).json()
        return [
            r for r in state["refunds"] if r["payment_intent"] in (sd.duplicate_provider_id, sd.original_provider_id)
        ]

    return SimpleNamespace(
        clock=clock, sd=sd, ex=ex, sim=flaky_sim, op=op, sessions=app_sessions, verify=verify, sim_refunds=sim_refunds
    )


async def scalar(sessions, q):
    async with sessions() as s:
        return (await s.execute(q)).scalar_one()


async def test_scripted_duplicate_charge_refund_succeeds(world):
    out = await world.ex.run(world.sd.tenant_id, world.sd.case_id, scripted_plan(world.sd))
    assert (out.case_state, out.resolution, out.command_state) == (S.CLOSED, "REFUNDED", C.CONFIRMED_SUCCESS)
    refunds = await world.sim_refunds()
    assert len(refunds) == 1 and refunds[0]["amount"] == 4900
    assert refunds[0]["payment_intent"] == world.sd.duplicate_provider_id
    verdict = await world.verify()
    assert verdict.passed, verdict.failed()


async def test_verifier_rejects_false_done(world):
    async with world.sessions() as s, s.begin():
        await s.execute(update(Case).where(Case.id == world.sd.case_id).values(state="CLOSED", resolution="REFUNDED"))
    verdict = await world.verify()
    assert not verdict.passed
    failed = {c.name for c in verdict.checks if not c.ok}
    assert {"1_identity", "3_amount", "4_provider_effect", "5_local_state"} <= failed
    assert "2_disposition" not in failed  # the lie itself looks right, the ground truth doesn't


async def test_duplicate_prepare_converges_to_one_command(world):
    plan = scripted_plan(world.sd)
    plan_id, verdict = await world.ex.record_plan(world.sd.tenant_id, world.sd.case_id, plan, "scripted")
    assert verdict.decision is Decision.AUTHORIZE
    results = await asyncio.gather(
        *[world.ex.ledger.prepare(world.sd.tenant_id, world.sd.case_id, plan_id, plan) for _ in range(10)]
    )
    ids = {r.command_id for r in results}
    assert len(ids) == 1 and None not in ids
    assert sum(not r.converged for r in results) == 1
    assert await scalar(world.sessions, select(func.count()).select_from(Command)) == 1
    assert await scalar(world.sessions, select(func.count()).select_from(PaymentReservation)) == 1
    pay = await scalar(world.sessions, select(Payment).where(Payment.id == world.sd.duplicate_payment_id))
    assert pay.reserved_minor == 4900


async def test_two_tickets_one_entitlement_one_refund(world):
    from recon_storage.models import CaseMessage

    second = "case_second_ticket"
    async with world.sessions() as s, s.begin():
        s.add(
            Case(
                id=second,
                tenant_id=world.sd.tenant_id,
                customer_id=world.sd.customer_id,
                order_id=world.sd.order_id,
                subject="double charge??",
                state="OPEN",
                created_at=T,
                updated_at=T,
            )
        )
        await s.flush()
        s.add(
            CaseMessage(
                id="msg_2",
                tenant_id=world.sd.tenant_id,
                case_id=second,
                author="customer",
                body="you charged me twice",
                created_at=T,
            )
        )
    outs = await asyncio.gather(
        world.ex.run(world.sd.tenant_id, world.sd.case_id, scripted_plan(world.sd)),
        world.ex.run(world.sd.tenant_id, second, scripted_plan(world.sd, case_id=second)),
    )
    assert len(await world.sim_refunds()) == 1
    assert await scalar(world.sessions, select(func.count()).select_from(RefundClaim)) == 1
    assert await scalar(world.sessions, select(func.count()).select_from(Command)) == 1
    assert outs[0].command_id == outs[1].command_id
    assert S.CLOSED in {o.case_state for o in outs}
    assert all(o.command_state in (C.CONFIRMED_SUCCESS, C.SUBMITTING) for o in outs)


async def test_commit_then_500_reconciles_single_refund(world):
    await world.sim.post(
        "/sim/faults",
        json={"account": world.sd.account, "kind": "commit_then_500", "payment_intent": world.sd.duplicate_provider_id},
        headers=world.op,
    )
    out = await world.ex.run(world.sd.tenant_id, world.sd.case_id, scripted_plan(world.sd))
    assert (out.case_state, out.command_state) == (S.CLOSED, C.CONFIRMED_SUCCESS)
    assert len(await world.sim_refunds()) == 1
    assert await scalar(world.sessions, select(func.count()).select_from(CommandAttempt)) == 1
    verdict = await world.verify()
    assert verdict.passed, verdict.failed()


async def test_lost_response_after_commit_matches_by_metadata(world):
    world.sim.flaky.lose_after = 1
    out = await world.ex.run(world.sd.tenant_id, world.sd.case_id, scripted_plan(world.sd))
    assert out.command_state is C.CONFIRMED_SUCCESS
    assert world.sim.flaky.posts == 1  # found by metadata, never resubmitted
    assert len(await world.sim_refunds()) == 1
    assert (await world.verify()).passed


async def test_dropped_request_resubmits_same_key_once(world):
    world.sim.flaky.drop_before = 1
    out = await world.ex.run(world.sd.tenant_id, world.sd.case_id, scripted_plan(world.sd))
    assert out.command_state is C.CONFIRMED_SUCCESS
    assert world.sim.flaky.posts == 2
    attempts = await scalar(world.sessions, select(func.count()).select_from(CommandAttempt))
    assert attempts == 2
    keys = {r["idempotency_key"] for r in (await world.sim.get("/sim/state", headers=world.op)).json()["requests_log"]}
    assert len(keys) == 1  # same key both times
    assert len(await world.sim_refunds()) == 1
    assert (await world.verify()).passed


async def test_retention_window_expired_goes_manual_review_not_resubmit(world):
    await world.sim.post(
        "/sim/faults",
        json={"account": world.sd.account, "kind": "fail_500", "payment_intent": world.sd.duplicate_provider_id},
        headers=world.op,
    )
    out = await world.ex.run(world.sd.tenant_id, world.sd.case_id, scripted_plan(world.sd))
    # cached 500 on every replay, still inside the window: stays unresolved, money stays reserved
    assert (out.case_state, out.command_state) == (S.RECONCILING, C.UNKNOWN)
    posts_before = world.sim.flaky.posts

    world.clock.advance(hours=21)
    await world.sim.post("/sim/clock", json={"advance_seconds": 21 * 3600}, headers=world.op)
    out = await world.ex.drive(world.sd.tenant_id, world.sd.case_id, out.command_id)
    assert (out.case_state, out.command_state) == (S.ESCALATED, C.MANUAL_REVIEW)
    assert world.sim.flaky.posts == posts_before  # never resubmitted past the window
    assert await world.sim_refunds() == []
    res = await scalar(world.sessions, select(PaymentReservation))
    assert res.state == "ACTIVE"  # unknown outcome keeps the money reserved
    pay = await scalar(world.sessions, select(Payment).where(Payment.id == world.sd.duplicate_payment_id))
    assert (pay.reserved_minor, pay.refunded_minor) == (4900, 0)


async def test_over_limit_waits_for_approval_without_moving_money(app_sessions, flaky_sim, op, verifier_engines):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, amount_minor=15_000)
    ex = Executor(app_sessions, FakeClock(T), lambda _t: HttpConnector(flaky_sim, sd.api_key))
    out = await ex.run(sd.tenant_id, sd.case_id, scripted_plan(sd, amount_minor=15_000))
    assert out.case_state is S.AWAITING_APPROVAL and out.command_id is None
    assert flaky_sim.flaky.posts == 0


async def test_deny_plan_moves_no_money(world):
    plan = scripted_plan(world.sd, recommended_disposition="DENY", reason_code="INELIGIBLE", amount_minor=None)
    out = await world.ex.run(world.sd.tenant_id, world.sd.case_id, plan)
    assert (out.case_state, out.resolution) == (S.CLOSED, "DENIED")
    assert world.sim.flaky.posts == 0
    assert await scalar(world.sessions, select(func.count()).select_from(Command)) == 0
