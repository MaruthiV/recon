from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from recon_domain.connector import Clock, PaymentConnector
from recon_domain.plan import Disposition, ResolutionPlan, material_hash
from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_domain.validation import Decision, Verdict, validate
from recon_storage.models import Plan
from recon_storage.repos import audit, get_case, load_case_facts, move_case, new_id

from .ledger import SAFE_WINDOW, Ledger

NO_MONEY_OUTCOME = {
    Disposition.DENY: (S.CLOSED, "DENIED"),
    Disposition.ALREADY_REFUNDED: (S.CLOSED, "ALREADY_REFUNDED"),
    Disposition.REQUEST_INFORMATION: (S.AWAITING_INFO, None),
    Disposition.ESCALATE: (S.ESCALATED, None),
}


@dataclass
class Outcome:
    case_state: S
    resolution: str | None
    verdict: Verdict | None = None
    command_id: str | None = None
    command_state: C | None = None
    problems: list[str] = field(default_factory=list)


def verdict_json(v: Verdict) -> dict:
    return {
        "decision": v.decision.value,
        "reasons": list(v.reasons),
        "entitlement_ref": v.entitlement_ref,
        "payment_id": v.payment.id if v.payment else None,
        "amount_minor": v.amount.amount_minor if v.amount else None,
        "permitted_minor": v.permitted_minor,
        "policy_ids": list(v.policy_ids),
    }


class Executor:
    """direct-call stage runner, the queue and leases arrive in m3"""

    def __init__(
        self,
        sessions: async_sessionmaker,
        clock: Clock,
        connector_for: Callable[[str], PaymentConnector],
        safe_window: timedelta = SAFE_WINDOW,
        max_reconcile_rounds: int = 5,
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.ledger = Ledger(sessions, clock, connector_for, safe_window)
        self.max_rounds = max_reconcile_rounds

    async def _move(self, tenant_id: str, case_id: str, dst: S, resolution: str | None = None, **why) -> S:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            src = S((await get_case(s, tenant_id, case_id)).state)
            if src is dst:
                return dst
            await move_case(s, tenant_id, case_id, src, dst, now, resolution)
            audit(s, tenant_id, "case_state", now, case_id=case_id, src=src.value, dst=dst.value, **why)
        return dst

    async def record_plan(
        self, tenant_id: str, case_id: str, plan: ResolutionPlan, source: str, run_id: str | None = None
    ) -> tuple[str, Verdict]:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            case = await get_case(s, tenant_id, case_id)
            if case.state == S.OPEN.value:
                await move_case(s, tenant_id, case_id, S.OPEN, S.INVESTIGATING, now)
                case.state = S.INVESTIGATING.value
            await move_case(s, tenant_id, case_id, S(case.state), S.VALIDATING, now)
            verdict = validate(plan, await load_case_facts(s, tenant_id, case_id, now))
            plan_id = new_id("plan")
            s.add(
                Plan(
                    id=plan_id,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    case_version=plan.case_version,
                    run_id=run_id,
                    source=source,
                    payload=plan.model_dump(mode="json"),
                    material_hash=material_hash(plan, verdict.entitlement_ref),
                    entitlement_ref=verdict.entitlement_ref,
                    decision=verdict.decision.value,
                    verdict=verdict_json(verdict),
                    created_at=now,
                )
            )
            audit(s, tenant_id, "plan_validated", now, case_id=case_id, plan_id=plan_id, **verdict_json(verdict))
        return plan_id, verdict

    async def _not_executed(self, tenant_id: str, case_id: str, plan: ResolutionPlan, v: Verdict) -> Outcome:
        match v.decision:
            case Decision.NEEDS_APPROVAL:
                dst, res = S.AWAITING_APPROVAL, None
            case Decision.NO_MONEY:
                dst, res = NO_MONEY_OUTCOME[plan.recommended_disposition]
            case Decision.STALE:
                dst, res = S.INVESTIGATING, None
            case _:
                dst, res = S.ESCALATED, None
        await self._move(tenant_id, case_id, dst, res, decision=v.decision.value)
        return Outcome(dst, res, v)

    async def run(
        self,
        tenant_id: str,
        case_id: str,
        plan: ResolutionPlan,
        source: str = "scripted",
        run_id: str | None = None,
    ) -> Outcome:
        plan_id, verdict = await self.record_plan(tenant_id, case_id, plan, source, run_id)
        if verdict.decision is not Decision.AUTHORIZE:
            return await self._not_executed(tenant_id, case_id, plan, verdict)
        prepared = await self.ledger.prepare(tenant_id, case_id, plan_id, plan)
        if prepared.command_id is None:
            return await self._not_executed(tenant_id, case_id, plan, prepared.verdict)
        await self._move(tenant_id, case_id, S.EXECUTING, command_id=prepared.command_id)
        out = await self.drive(tenant_id, case_id, prepared.command_id)
        out.verdict = verdict
        return out

    async def drive(self, tenant_id: str, case_id: str, command_id: str, recover: bool = False) -> Outcome:
        state = await self.ledger.submit(command_id)
        rounds = 0
        # submitting may just be another runner on the wire, only crash recovery gets to call it unknown
        while (
            state in (C.UNKNOWN, C.PENDING_PROVIDER) or (recover and state is C.SUBMITTING)
        ) and rounds < self.max_rounds:
            await self._move(tenant_id, case_id, S.RECONCILING, command_id=command_id)
            if state is C.SUBMITTING:
                state = await self.ledger.mark_unknown(command_id)
            state = await self.ledger.reconcile(command_id)
            rounds += 1

        if state is C.CONFIRMED_SUCCESS:
            await self._move(tenant_id, case_id, S.VERIFYING, command_id=command_id)
            problems = await self.ledger.verify(command_id)
            if problems:
                await self._move(tenant_id, case_id, S.ESCALATED, problems=problems)
                return Outcome(S.ESCALATED, None, None, command_id, state, problems)
            await self._move(tenant_id, case_id, S.CLOSED, "REFUNDED", command_id=command_id)
            return Outcome(S.CLOSED, "REFUNDED", None, command_id, state)
        if state in (C.CONFIRMED_FAILURE, C.MANUAL_REVIEW):
            await self._move(tenant_id, case_id, S.ESCALATED, command_id=command_id, command_state=state.value)
            return Outcome(S.ESCALATED, None, None, command_id, state)
        # still unresolved, the sweeper picks it up later (m3)
        return Outcome(S.RECONCILING, None, None, command_id, state)
