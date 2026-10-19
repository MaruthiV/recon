from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recon_domain.approvals import ApprovalFact, executable
from recon_domain.connector import (
    Clock,
    CommandRef,
    DirectGateway,
    PaymentConnector,
    ProviderGateway,
    ProviderRefund,
    ReadFailed,
    SubmitKind,
    SubmitResult,
    state_for_refund_status,
)
from recon_domain.entitlements import command_id_for, idempotency_key_for
from recon_domain.plan import ResolutionPlan, canonical_hash
from recon_domain.states import CommandState as C
from recon_domain.states import ReservationState as R
from recon_domain.states import check_command, check_reservation, reservation_for
from recon_domain.validation import Decision, Verdict, validate
from recon_storage.models import Command, CommandAttempt, Payment, PaymentReservation, RefundClaim
from recon_storage.repos import audit, load_case_facts, new_id

from .faults import NO_FAULTS, Faults

# stripe keeps keys >= 24h, stay well inside that
SAFE_WINDOW = timedelta(hours=20)


@dataclass(frozen=True)
class Prepared:
    command_id: str | None
    verdict: Verdict
    converged: bool = False


def provider_metadata(cmd: Command) -> dict[str, str]:
    return {"recon_command_id": cmd.id, "recon_tenant_id": cmd.tenant_id}


def command_ref(cmd: Command) -> CommandRef:
    return CommandRef(
        id=cmd.id,
        tenant_id=cmd.tenant_id,
        provider_payment_id=cmd.provider_payment_id,
        amount_minor=cmd.amount_minor,
        currency=cmd.currency,
        idempotency_key=cmd.idempotency_key,
        payload_hash=cmd.payload_hash,
        attempt=cmd.attempts,
        metadata=provider_metadata(cmd),
    )


class Ledger:
    def __init__(
        self,
        sessions: async_sessionmaker,
        clock: Clock,
        connector_for: Callable[[str], PaymentConnector],
        safe_window: timedelta = SAFE_WINDOW,
        faults: Faults = NO_FAULTS,
        gateway_for: Callable[[str], ProviderGateway] | None = None,
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.gateway_for = gateway_for or (lambda tenant: DirectGateway(connector_for(tenant)))
        self.safe_window = safe_window
        self.faults = faults

    async def _lock(self, s: AsyncSession, model, *where):
        q = select(model).where(*where).with_for_update().execution_options(populate_existing=True)
        return (await s.execute(q)).scalar_one()

    async def _lock_command_set(self, s: AsyncSession, command_id: str) -> tuple[Command, Payment, RefundClaim]:
        ids = (
            await s.execute(
                select(Command.tenant_id, Command.payment_id, Command.claim_id).where(Command.id == command_id)
            )
        ).one()
        # always payment -> claim -> command, same as prepare
        pay = await self._lock(s, Payment, Payment.tenant_id == ids.tenant_id, Payment.id == ids.payment_id)
        claim = await self._lock(s, RefundClaim, RefundClaim.tenant_id == ids.tenant_id, RefundClaim.id == ids.claim_id)
        cmd = await self._lock(s, Command, Command.id == command_id)
        return cmd, pay, claim

    async def prepare(
        self, tenant_id: str, case_id: str, plan_id: str, plan: ResolutionPlan, approval: ApprovalFact | None = None
    ) -> Prepared:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            # a redelivered prepare for the same plan: its own reservation would fail validation, so check first
            mine = (
                await s.execute(select(Command).where(Command.tenant_id == tenant_id, Command.plan_id == plan_id))
            ).scalar_one_or_none()
            if mine is not None:
                audit(s, tenant_id, "prepare_converged", now, case_id=case_id, command_id=mine.id, plan_id=plan_id)
                again = Verdict(Decision.AUTHORIZE, ("already prepared for this plan",), mine.entitlement_ref)
                return Prepared(mine.id, again, converged=True)
        async with self.sessions() as s:
            first = validate(plan, await load_case_facts(s, tenant_id, case_id, now))
        if not executable(first, plan, approval, now):
            return Prepared(None, first)
        assert first.payment and first.entitlement_ref and first.amount and first.permitted_minor is not None
        ref = first.entitlement_ref
        # fp10: the gap where someone else can spend the balance before we lock
        await self.faults.hit("FP10", plan_id)

        async with self.sessions() as s, s.begin():
            pay = await self._lock(s, Payment, Payment.tenant_id == tenant_id, Payment.id == first.payment.id)
            await s.execute(
                pg_insert(RefundClaim)
                .values(
                    id=new_id("clm"),
                    tenant_id=tenant_id,
                    entitlement_ref=ref,
                    payment_id=pay.id,
                    currency=pay.currency,
                    permitted_minor=first.permitted_minor,
                    reserved_minor=0,
                    consumed_minor=0,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "entitlement_ref"])
            )
            claim = await self._lock(
                s, RefundClaim, RefundClaim.tenant_id == tenant_id, RefundClaim.entitlement_ref == ref
            )

            existing = (
                (
                    await s.execute(
                        select(Command)
                        .where(Command.tenant_id == tenant_id, Command.entitlement_ref == ref)
                        .order_by(Command.op_seq)
                    )
                )
                .scalars()
                .all()
            )
            live = next((c for c in existing if c.state != C.CONFIRMED_FAILURE), None)
            if live is not None and live.amount_minor == first.amount.amount_minor:
                # another ticket asking for the same refund rides on the command that's already there
                audit(s, tenant_id, "prepare_converged", now, case_id=case_id, command_id=live.id, plan_id=plan_id)
                return Prepared(live.id, first, converged=True)
            if live is not None:
                # one live command per entitlement: a different amount would close this ticket on someone else's
                # refund, so a person decides what's still owed
                why = f"{ref} already has a {live.amount_minor} refund ({live.state}), {first.amount.amount_minor} needs a person"
                audit(s, tenant_id, "prepare_refused", now, case_id=case_id, decision="ESCALATE", reasons=[why])
                return Prepared(None, Verdict(Decision.ESCALATE, (why,)))

            # decide for real on facts read under the locks
            verdict = validate(plan, await load_case_facts(s, tenant_id, case_id, now))
            if not executable(verdict, plan, approval, now) or verdict.entitlement_ref != ref:
                audit(
                    s,
                    tenant_id,
                    "prepare_refused",
                    now,
                    case_id=case_id,
                    decision=verdict.decision.value,
                    reasons=list(verdict.reasons),
                )
                return Prepared(None, verdict)
            assert verdict.amount is not None
            amount = verdict.amount.amount_minor

            op_seq = max((c.op_seq for c in existing), default=0) + 1
            cid = command_id_for(tenant_id, ref, op_seq)
            key = idempotency_key_for(cid)
            meta = {"recon_command_id": cid, "recon_tenant_id": tenant_id}
            payload_hash = canonical_hash(
                {
                    "payment_intent": pay.provider_payment_id,
                    "amount_minor": amount,
                    "currency": verdict.amount.currency.value,
                    "idempotency_key": key,
                    "metadata": meta,
                }
            )
            s.add(
                Command(
                    id=cid,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    plan_id=plan_id,
                    claim_id=claim.id,
                    entitlement_ref=ref,
                    op_seq=op_seq,
                    payment_id=pay.id,
                    provider_payment_id=pay.provider_payment_id,
                    amount_minor=amount,
                    currency=verdict.amount.currency.value,
                    payload_hash=payload_hash,
                    idempotency_key=key,
                    state=C.PREPARED.value,
                    attempts=0,
                    generation=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.flush()
            s.add(
                PaymentReservation(
                    id=new_id("res"),
                    tenant_id=tenant_id,
                    command_id=cid,
                    payment_id=pay.id,
                    claim_id=claim.id,
                    amount_minor=amount,
                    currency=pay.currency,
                    state=R.ACTIVE.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            pay.reserved_minor += amount
            claim.reserved_minor += amount
            audit(
                s,
                tenant_id,
                "command_prepared",
                now,
                case_id=case_id,
                command_id=cid,
                amount_minor=amount,
                entitlement_ref=ref,
                op_seq=op_seq,
            )
        return Prepared(cid, verdict)

    def in_window(self, cmd: Command, now: datetime) -> bool:
        return cmd.first_submitted_at is None or now - cmd.first_submitted_at < self.safe_window

    async def submit(self, command_id: str) -> C:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            cmd = await self._lock(s, Command, Command.id == command_id)
            state = C(cmd.state)
            if state not in (C.PREPARED, C.UNKNOWN) or (state is C.UNKNOWN and not self.in_window(cmd, now)):
                return state
            check_command(state, C.SUBMITTING)
            cmd.state = C.SUBMITTING.value
            cmd.attempts += 1
            cmd.first_submitted_at = cmd.first_submitted_at or now
            cmd.updated_at = now
            attempt_no = cmd.attempts
            s.add(
                CommandAttempt(
                    id=new_id("att"), tenant_id=cmd.tenant_id, command_id=cmd.id, attempt_no=attempt_no, started_at=now
                )
            )
            audit(
                s,
                cmd.tenant_id,
                "submit_started",
                now,
                case_id=cmd.case_id,
                command_id=cmd.id,
                attempt=attempt_no,
                resubmit=state is C.UNKNOWN,
            )
            ref = command_ref(cmd)
        await self.faults.hit("FP3", command_id)
        # no db transaction is open across the provider call
        result = await self.gateway_for(ref.tenant_id).submit(ref)
        await self.faults.hit("FP4", command_id)
        await self.faults.hit("FP7", command_id)
        return await self._record_submit(command_id, attempt_no, result)

    def _target(self, cmd: Command, result: SubmitResult) -> tuple[C, str]:
        match result.kind:
            case SubmitKind.APPLIED:
                assert result.refund is not None
                if not self._matches(cmd, result.refund):
                    return C.MANUAL_REVIEW, "provider refund doesn't match the command"
                return state_for_refund_status(result.refund.status), f"provider status {result.refund.status}"
            case SubmitKind.REJECTED:
                return C.CONFIRMED_FAILURE, f"provider rejected: {result.http_status} {result.error_code}"
            case SubmitKind.MISMATCH:
                return C.MANUAL_REVIEW, "idempotency key reused with different params"
            case SubmitKind.REFUSED:
                return C.MANUAL_REVIEW, f"execution boundary refused: {result.error_code}"
            case _:
                return C.UNKNOWN, f"ambiguous: http {result.http_status or 'no response'}"

    @staticmethod
    def _matches(cmd: Command, r: ProviderRefund) -> bool:
        return (
            r.metadata.get("recon_command_id") == cmd.id
            and r.amount_minor == cmd.amount_minor
            and r.currency == cmd.currency
            and r.payment_intent == cmd.provider_payment_id
        )

    async def _record_submit(self, command_id: str, attempt_no: int, result: SubmitResult) -> C:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            cmd, pay, claim = await self._lock_command_set(s, command_id)
            attempt = (
                await s.execute(
                    select(CommandAttempt).where(
                        CommandAttempt.command_id == command_id, CommandAttempt.attempt_no == attempt_no
                    )
                )
            ).scalar_one()
            attempt.finished_at = now
            attempt.outcome = result.kind.value
            attempt.http_status = result.http_status
            attempt.error_type = result.error_type
            attempt.error_code = result.error_code
            attempt.replayed = result.replayed
            attempt.provider_refund_id = result.refund.id if result.refund else None
            if cmd.state != C.SUBMITTING.value or cmd.attempts != attempt_no:
                # someone moved the command while we were on the wire, just keep the evidence
                audit(s, cmd.tenant_id, "stale_submit_result", now, command_id=cmd.id, attempt=attempt_no)
                return C(cmd.state)
            target, why = self._target(cmd, result)
            await self._apply(s, cmd, pay, claim, target, result.refund.id if result.refund else None, now, why)
            return target

    async def _apply(
        self,
        s: AsyncSession,
        cmd: Command,
        pay: Payment,
        claim: RefundClaim,
        target: C,
        refund_id: str | None,
        now: datetime,
        why: str,
    ) -> None:
        check_command(C(cmd.state), target)
        cmd.state = target.value
        cmd.updated_at = now
        if refund_id is not None and cmd.provider_refund_id is None:
            cmd.provider_refund_id = refund_id
        audit(
            s,
            cmd.tenant_id,
            f"command_{target.value.lower()}",
            now,
            case_id=cmd.case_id,
            command_id=cmd.id,
            reason=why,
            provider_refund_id=cmd.provider_refund_id,
        )
        settle = reservation_for(target)
        if settle is R.ACTIVE:
            return
        res = await self._lock(s, PaymentReservation, PaymentReservation.command_id == cmd.id)
        check_reservation(R(res.state), settle)
        res.state = settle.value
        res.updated_at = now
        pay.reserved_minor -= res.amount_minor
        claim.reserved_minor -= res.amount_minor
        if settle is R.CONSUMED:
            pay.refunded_minor += res.amount_minor
            claim.consumed_minor += res.amount_minor

    async def _transition(self, command_id: str, expect: C, target: C, refund_id: str | None, why: str) -> C:
        now = self.clock.now()
        async with self.sessions() as s, s.begin():
            cmd, pay, claim = await self._lock_command_set(s, command_id)
            if cmd.state != expect.value:
                return C(cmd.state)
            await self._apply(s, cmd, pay, claim, target, refund_id, now, why)
            return target

    async def mark_unknown(self, command_id: str, why: str = "worker lost track of an in-flight submit") -> C:
        return await self._transition(command_id, C.SUBMITTING, C.UNKNOWN, None, why)

    async def reconcile(self, command_id: str) -> C:
        now = self.clock.now()
        async with self.sessions() as s:
            cmd = (await s.execute(select(Command).where(Command.id == command_id))).scalar_one()
        state = C(cmd.state)
        gw, ref = self.gateway_for(cmd.tenant_id), command_ref(cmd)

        if state is C.PENDING_PROVIDER:
            assert cmd.provider_refund_id is not None
            try:
                r = await gw.refund(ref, cmd.provider_refund_id)
            except ReadFailed:
                return state
            if r is None:
                return await self._transition(command_id, state, C.MANUAL_REVIEW, None, "bound refund vanished")
            target = state_for_refund_status(r.status)
            if target is C.PENDING_PROVIDER:
                return state
            return await self._transition(command_id, state, target, r.id, f"provider status {r.status}")

        if state is not C.UNKNOWN:
            return state
        try:
            refunds = await gw.refunds_for(ref)
        except ReadFailed as e:
            # a failed read never means "no refund"
            async with self.sessions() as s, s.begin():
                audit(s, cmd.tenant_id, "reconcile_read_failed", now, command_id=cmd.id, error=str(e))
            return state
        mine = [r for r in refunds if r.metadata.get("recon_command_id") == cmd.id]
        if len(mine) > 1:
            return await self._transition(
                command_id, state, C.MANUAL_REVIEW, None, f"duplicate provider effect: {[r.id for r in mine]}"
            )
        if len(mine) == 1:
            r = mine[0]
            if not self._matches(cmd, r):
                return await self._transition(command_id, state, C.MANUAL_REVIEW, r.id, "matched refund disagrees")
            return await self._transition(
                command_id, state, state_for_refund_status(r.status), r.id, f"matched by metadata, status {r.status}"
            )
        if self.in_window(cmd, now):
            # same key, same payload: a late commit gets deduped by the provider
            return await self.submit(command_id)
        return await self._transition(
            command_id, state, C.MANUAL_REVIEW, None, "no provider record and the idempotency window has passed"
        )

    async def verify(self, command_id: str) -> list[str]:
        async with self.sessions() as s:
            cmd = (await s.execute(select(Command).where(Command.id == command_id))).scalar_one()
            res = (
                await s.execute(select(PaymentReservation).where(PaymentReservation.command_id == command_id))
            ).scalar_one()
        problems = []
        if cmd.state != C.CONFIRMED_SUCCESS.value:
            problems.append(f"command is {cmd.state}")
        if res.state != R.CONSUMED.value:
            problems.append(f"reservation is {res.state}")
        if cmd.provider_refund_id is None:
            return [*problems, "no provider refund bound"]
        try:
            r = await self.gateway_for(cmd.tenant_id).refund(command_ref(cmd), cmd.provider_refund_id)
        except ReadFailed as e:
            return [*problems, f"couldn't read provider refund: {e}"]
        if r is None or not self._matches(cmd, r) or r.status != "succeeded":
            problems.append("provider refund missing or doesn't match")
        return problems
