from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recon_domain.connector import Clock
from recon_domain.plan import ResolutionPlan
from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_domain.validation import Decision, Verdict
from recon_storage.models import Command, OutboxEvent, Plan, Run
from recon_storage.repos import audit, get_case, move_case, new_id

from .executor import Executor
from .faults import NO_FAULTS, Faults
from .investigate import Investigator, NotInvestigable


class Stage(StrEnum):
    INVESTIGATE = "INVESTIGATE"
    PREPARE = "PREPARE"
    SUBMIT = "SUBMIT"
    RECONCILE = "RECONCILE"
    VERIFY = "VERIFY"
    DONE = "DONE"


UNRESOLVED = (C.UNKNOWN, C.PENDING_PROVIDER, C.SUBMITTING)


class StaleLease(Exception):
    pass


class NotStartable(Exception):
    pass


@dataclass(frozen=True)
class Advanced:
    status: int  # 200 advanced or nothing to do, 409 busy or fenced out, 500 stage blew up
    detail: str
    next: Stage | None = None


@dataclass(frozen=True)
class Claim:
    run_id: str
    tenant_id: str
    case_id: str
    generation: int
    plan_id: str | None
    command_id: str | None
    owns_command: bool
    reconcile_rounds: int
    took_over: bool  # last holder never released, so it died or stalled mid-stage


@dataclass(frozen=True)
class Step:
    next: Stage
    delay_seconds: int = 0
    plan: ResolutionPlan | None = None
    command_id: str | None = None
    owns_command: bool | None = None
    counts_as_round: bool = False


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"


class Pipeline:
    """durable stage machine: lease, run one stage, fenced advance + next outbox event"""

    def __init__(
        self,
        sessions: async_sessionmaker,
        clock: Clock,
        executor: Executor,
        investigator: Investigator | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        faults: Faults = NO_FAULTS,
        max_reconcile_rounds: int = 8,
        reconcile_backoff_seconds: int = 5,
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.executor = executor
        self.ledger = executor.ledger
        self.investigator = investigator
        self.worker_id = worker_id or worker_identity()
        self.lease = timedelta(seconds=lease_seconds)
        self.faults = faults
        self.max_rounds = max_reconcile_rounds
        self.backoff = reconcile_backoff_seconds

    def _enqueue(self, s: AsyncSession, tenant_id: str, run_id: str, stage: Stage, gen: int, delay: int = 0) -> None:
        s.add(
            OutboxEvent(
                tenant_id=tenant_id,
                run_id=run_id,
                stage=stage.value,
                event_id=f"{run_id}:{stage.value}:{gen}",
                available_at=func.now() + timedelta(seconds=delay),
            )
        )

    async def start_agent(self, tenant_id: str, case_id: str) -> str:
        # just queue it, the worker that runs INVESTIGATE stamps its own workflow version and models
        now = self.clock.now()
        run_id = new_id("run")
        async with self.sessions() as s, s.begin():
            case = await get_case(s, tenant_id, case_id)
            if case.state != S.OPEN.value:
                raise NotStartable(f"case {case_id} is {case.state}")
            await move_case(s, tenant_id, case_id, S.OPEN, S.INVESTIGATING, now)
            s.add(
                Run(
                    id=run_id,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    stage=Stage.INVESTIGATE.value,
                    investigation="QUEUED",
                    workflow_version="pending",
                    models={},
                    budget={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.flush()
            audit(s, tenant_id, "investigation_queued", now, case_id=case_id, run_id=run_id)
            self._enqueue(s, tenant_id, run_id, Stage.INVESTIGATE, 0)
        return run_id

    async def start_scripted(self, tenant_id: str, case_id: str, plan: ResolutionPlan, source: str = "scripted") -> str:
        now = self.clock.now()
        run_id = new_id("run")
        async with self.sessions() as s, s.begin():
            s.add(
                Run(
                    id=run_id,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    stage=Stage.PREPARE.value,
                    investigation="SKIPPED",
                    workflow_version=source,
                    models={"mode": source},
                    budget={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.flush()
            plan_id, _ = await self.executor.record_plan_in(s, tenant_id, case_id, plan, source, run_id)
            await s.execute(update(Run).where(Run.id == run_id).values(plan_id=plan_id))
            self._enqueue(s, tenant_id, run_id, Stage.PREPARE, 0)
        return run_id

    async def _claim(self, run_id: str, stage: Stage) -> Claim | Advanced:
        async with self.sessions() as s, s.begin():
            row = (
                await s.execute(
                    select(Run, (Run.lease_expires_at > func.now()).label("held"))
                    .where(Run.id == run_id)
                    .with_for_update(of=Run)
                    .execution_options(populate_existing=True)
                )
            ).first()
            if row is None:
                return Advanced(200, "unknown run, nothing to do")
            run, held = row
            if run.stage != stage.value:
                return Advanced(200, f"run is at {run.stage}, {stage} already done")
            if run.lease_owner is not None and held:
                return Advanced(409, f"lease held by {run.lease_owner}")
            took_over = run.lease_owner is not None
            gen = (
                await s.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        lease_owner=self.worker_id,
                        lease_expires_at=func.now() + self.lease,
                        generation=Run.generation + 1,
                    )
                    .returning(Run.generation)
                )
            ).scalar_one()
            if took_over:
                audit(
                    s,
                    run.tenant_id,
                    "lease_taken_over",
                    self.clock.now(),
                    case_id=run.case_id,
                    run_id=run_id,
                    stage=stage.value,
                    previous=run.lease_owner,
                    generation=gen,
                )
            return Claim(
                run_id,
                run.tenant_id,
                run.case_id,
                gen,
                run.plan_id,
                run.command_id,
                run.owns_command,
                run.reconcile_rounds,
                took_over,
            )

    async def advance(self, run_id: str, stage: Stage | str, event_id: str = "") -> Advanced:
        stage = Stage(stage)
        claim = await self._claim(run_id, stage)
        if isinstance(claim, Advanced):
            return claim
        try:
            step = await self._run(stage, claim)
            if stage is Stage.PREPARE:
                await self.faults.hit("FP2", run_id)
            await self._finish(stage, claim, step)
        except StaleLease:
            return Advanced(409, "fenced out, a newer lease owns this run")
        except Exception as e:
            await self._release(claim)
            return Advanced(500, f"{stage} failed: {e!r}"[:300])
        return Advanced(200, "advanced", step.next)

    async def _release(self, claim: Claim) -> None:
        # expire but keep the owner, so the next claim treats it like a takeover and reconciles first
        async with self.sessions() as s, s.begin():
            await s.execute(
                update(Run)
                .where(Run.id == claim.run_id, Run.generation == claim.generation)
                .values(lease_expires_at=func.now())
            )

    async def _finish(self, stage: Stage, claim: Claim, step: Step) -> None:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            values: dict = {"stage": step.next.value, "lease_owner": None, "lease_expires_at": None, "updated_at": now}
            if step.command_id is not None:
                values["command_id"] = step.command_id
            if step.owns_command is not None:
                values["owns_command"] = step.owns_command
            if step.counts_as_round:
                values["reconcile_rounds"] = Run.reconcile_rounds + 1
            res = await s.execute(
                update(Run)
                .where(Run.id == claim.run_id, Run.generation == claim.generation, Run.stage == stage.value)
                .values(**values)
            )
            if res.rowcount != 1:  # type: ignore[attr-defined]
                raise StaleLease(claim.run_id)
            if step.plan is not None:
                # the plan is persisted iff the stage advance commits (D6 guard)
                plan_id, _ = await self.executor.record_plan_in(
                    s, claim.tenant_id, claim.case_id, step.plan, "agent", claim.run_id
                )
                await s.execute(update(Run).where(Run.id == claim.run_id).values(plan_id=plan_id))
            if step.next is not Stage.DONE:
                self._enqueue(s, claim.tenant_id, claim.run_id, step.next, claim.generation, step.delay_seconds)
            audit(
                s,
                claim.tenant_id,
                "stage_advanced",
                now,
                case_id=claim.case_id,
                command_id=step.command_id or claim.command_id,
                run_id=claim.run_id,
                src=stage.value,
                dst=step.next.value,
                generation=claim.generation,
            )

    async def _run(self, stage: Stage, claim: Claim) -> Step:
        match stage:
            case Stage.INVESTIGATE:
                return await self._investigate(claim)
            case Stage.PREPARE:
                return await self._prepare(claim)
            case Stage.SUBMIT | Stage.RECONCILE:
                return await self._settle(stage, claim)
            case Stage.VERIFY:
                return await self._verify(claim)
            case _:
                return Step(Stage.DONE)

    async def _investigate(self, claim: Claim) -> Step:
        if self.investigator is None:
            raise RuntimeError("no investigator configured on this worker")
        try:
            inv = await self.investigator.investigate(claim.tenant_id, claim.case_id, claim.run_id)
        except NotInvestigable:
            return Step(Stage.DONE)
        if inv.plan is None:
            return Step(Stage.DONE)  # investigator already escalated the case
        return Step(Stage.PREPARE, plan=inv.plan)

    async def _prepare(self, claim: Claim) -> Step:
        async with self.sessions() as s:
            row = (await s.execute(select(Plan).where(Plan.id == claim.plan_id))).scalar_one()
        plan = ResolutionPlan.model_validate(row.payload)
        if row.decision != Decision.AUTHORIZE.value:
            verdict = Verdict(Decision(row.decision), tuple(row.verdict.get("reasons", ())))
            await self.executor._not_executed(claim.tenant_id, claim.case_id, plan, verdict)
            return Step(Stage.DONE)
        prepared = await self.ledger.prepare(claim.tenant_id, claim.case_id, row.id, plan)
        if prepared.command_id is None:
            await self.executor._not_executed(claim.tenant_id, claim.case_id, plan, prepared.verdict)
            return Step(Stage.DONE)
        async with self.sessions() as s:
            cmd = (await s.execute(select(Command).where(Command.id == prepared.command_id))).scalar_one()
        # a redelivered prepare converges on our own command, another ticket's command isn't ours to drive
        owns = cmd.plan_id == row.id
        await self.executor._move(claim.tenant_id, claim.case_id, S.EXECUTING, command_id=cmd.id)
        return Step(Stage.SUBMIT, command_id=cmd.id, owns_command=owns)

    async def _command_state(self, command_id: str) -> C:
        async with self.sessions() as s:
            return C((await s.execute(select(Command.state).where(Command.id == command_id))).scalar_one())

    async def _settle(self, stage: Stage, claim: Claim) -> Step:
        assert claim.command_id is not None
        cid = claim.command_id
        state = await self._command_state(cid)
        if claim.owns_command:
            if state is C.SUBMITTING and claim.took_over:
                # the last worker died somewhere around the provider call, ask the provider before anything else
                state = await self.ledger.mark_unknown(cid, "previous lease holder died mid-submit")
            if state is C.PREPARED:
                state = await self.ledger.submit(cid)
            elif state in (C.UNKNOWN, C.PENDING_PROVIDER):
                state = await self.ledger.reconcile(cid)
        return await self._route(stage, claim, state)

    async def _route(self, stage: Stage, claim: Claim, state: C) -> Step:
        t, c, cid = claim.tenant_id, claim.case_id, claim.command_id
        if state is C.CONFIRMED_SUCCESS:
            return Step(Stage.VERIFY)
        if state in (C.CONFIRMED_FAILURE, C.MANUAL_REVIEW):
            await self.executor._move(t, c, S.ESCALATED, command_id=cid, command_state=state.value)
            return Step(Stage.DONE)
        rounds = claim.reconcile_rounds + (1 if stage is Stage.RECONCILE else 0)
        if rounds >= self.max_rounds:
            # money stays reserved, a human (or the sweeper) takes it from here
            await self.executor._move(t, c, S.ESCALATED, command_id=cid, command_state=state.value, why="unresolved")
            return Step(Stage.DONE)
        await self.executor._move(t, c, S.RECONCILING, command_id=cid)
        return Step(
            Stage.RECONCILE, delay_seconds=self.backoff * (rounds + 1), counts_as_round=stage is Stage.RECONCILE
        )

    async def _verify(self, claim: Claim) -> Step:
        assert claim.command_id is not None
        t, c, cid = claim.tenant_id, claim.case_id, claim.command_id
        await self.executor._move(t, c, S.VERIFYING, command_id=cid)
        problems = await self.ledger.verify(cid)
        if problems:
            await self.executor._move(t, c, S.ESCALATED, problems=problems)
        else:
            await self.executor._move(t, c, S.CLOSED, "REFUNDED", command_id=cid)
        return Step(Stage.DONE)
