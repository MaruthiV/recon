import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from recon_domain.connector import Clock, SystemClock
from recon_domain.plan import canonical_hash
from recon_domain.states import CaseState
from recon_domain.tokens import BadToken, mint, verify
from recon_storage.db import make_engine, sessions, url
from recon_storage.models import ApiRequest, Case, CaseMessage, Command, Order, OutboxEvent, Payment, Plan, Run, Tenant
from recon_worker.executor import Executor
from recon_worker.pipeline import NotStartable, Pipeline
from recon_worker.rules_planner import plan_by_rules

COOKIE = "recon_session"
AUDIENCE = "recon-api"
PAGE = 200

# product-level labels, the raw kind stays visible in the engineering view
LABELS = {
    "investigation_queued": "Investigation queued",
    "investigation_started": "Agents started investigating",
    "plan_proposed": "Agents proposed a plan",
    "investigation_failed": "Investigation stopped without a plan",
    "plan_validated": "Plan checked against current facts and policy",
    "command_prepared": "Refund prepared and money reserved",
    "prepare_converged": "Refund already prepared, reusing it",
    "prepare_refused": "Refund refused at preparation",
    "submit_started": "Refund sent to the provider",
    "lease_taken_over": "A new worker picked the work up",
    "command_unknown": "Outcome unknown, asking the provider",
    "command_pending_provider": "Provider is still processing",
    "command_confirmed_success": "Provider confirmed the refund",
    "command_confirmed_failure": "Provider rejected the refund",
    "command_manual_review": "Needs a person to look at it",
    "stale_submit_result": "Ignored a late answer from a replaced worker",
    "reconcile_read_failed": "Couldn't read the provider, will retry",
}


class Login(BaseModel):
    user: str
    tenant_id: str
    role: Literal["operator", "supervisor"] = "operator"


class Decide(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    plan_hash: str


class Start(BaseModel):
    mode: Literal["rules", "agent"]
    case_version: int  # the version the client was looking at


class Reply(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    case_version: int
    order_id: str | None = None
    mode: Literal["rules", "agent"] = "agent"  # how to re-investigate if the case was waiting on this


class Requeue(BaseModel):
    note: str = ""


class Arm(BaseModel):
    point: Literal["FP2", "FP3", "FP4"]


IDEMPOTENCY_KEY = "Idempotency-Key"
ABANDONED = timedelta(seconds=60)  # an unanswered key this old means the first try died


def no_provider_key(_tenant: str):
    raise RuntimeError("the api never talks to the provider")


def iso(d: Any) -> Any:
    return d.isoformat() if hasattr(d, "isoformat") else d


def session(request: Request) -> dict:
    st = request.app.state
    try:
        return verify(request.cookies.get(COOKIE, ""), st.session_key, AUDIENCE, st.clock.now())
    except BadToken as e:
        raise HTTPException(401, "not signed in") from e


Claims = Annotated[dict, Depends(session)]


def tenant_of(claims: Claims) -> str:
    return claims["tenant"]


TenantId = Annotated[str, Depends(tenant_of)]


def create_app(db: async_sessionmaker, clock: Clock, session_key: bytes, dev: bool, faults_enabled: bool) -> FastAPI:
    app = FastAPI(title="recon-api")

    async def once(request: Request, tenant: str, body: BaseModel, work: Callable[[], Awaitable[dict]]) -> JSONResponse:
        # a retried write with the same key gets the first answer back, a different body under that key is refused
        key = request.headers.get(IDEMPOTENCY_KEY, "")
        if not 8 <= len(key) <= 200:
            raise HTTPException(428, f"write requests need an {IDEMPOTENCY_KEY} header (8-200 chars)")
        route = request.url.path
        digest = canonical_hash({"route": route, "body": body.model_dump(mode="json")})
        now = clock.now()
        mine = (ApiRequest.tenant_id == tenant, ApiRequest.key == key)
        async with db() as s, s.begin():
            claimed = (
                await s.execute(
                    insert(ApiRequest)
                    .values(tenant_id=tenant, key=key, route=route, request_hash=digest, created_at=now)
                    .on_conflict_do_nothing()
                    .returning(ApiRequest.key)
                )
            ).scalar_one_or_none()
            if claimed is None:
                row = (await s.execute(select(ApiRequest).where(*mine).with_for_update())).scalar_one()
                if row.request_hash != digest:
                    raise HTTPException(422, f"this {IDEMPOTENCY_KEY} was already used for a different request")
                if row.status_code is not None:
                    return JSONResponse(row.response, row.status_code, headers={"Idempotent-Replayed": "true"})
                if now - row.created_at < ABANDONED:
                    raise HTTPException(409, "a request with this key is still in progress")
                row.created_at = now  # the first try died part way, this one takes over
        try:
            status, out = 202, await work()
        except HTTPException as e:
            if e.status_code >= 500:
                await forget(tenant, key)
                raise
            status, out = e.status_code, {"detail": e.detail}
        except Exception:
            await forget(tenant, key)
            raise
        async with db() as s, s.begin():
            await s.execute(update(ApiRequest).where(*mine).values(status_code=status, response=out))
        return JSONResponse(out, status)

    async def forget(tenant: str, key: str) -> None:
        # a server error isn't an answer, let the retry run for real
        async with db() as s, s.begin():
            await s.execute(delete(ApiRequest).where(ApiRequest.tenant_id == tenant, ApiRequest.key == key))

    app.state.session_key = session_key
    app.state.clock = clock
    pipeline = Pipeline(db, clock, Executor(db, clock, no_provider_key), worker_id="api")

    async def own_case(tenant: str, case_id: str) -> Case:
        async with db() as s:
            case = (
                await s.execute(select(Case).where(Case.tenant_id == tenant, Case.id == case_id))
            ).scalar_one_or_none()
        if case is None:
            raise HTTPException(404, "no such case")  # same as missing for other tenants' ids
        return case

    @app.post("/v1/login")
    async def login(body: Login, response: Response):
        if not dev:
            raise HTTPException(404)
        async with db() as s:
            if (await s.execute(select(Tenant.id).where(Tenant.id == body.tenant_id))).scalar() is None:
                raise HTTPException(400, "unknown tenant")
        now = clock.now()
        token = mint(
            {
                "aud": AUDIENCE,
                "user": body.user,
                "tenant": body.tenant_id,
                "role": body.role,
                "exp": int((now + timedelta(hours=8)).timestamp()),
            },
            session_key,
        )
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=8 * 3600)
        return {"user": body.user, "tenant_id": body.tenant_id, "role": body.role}

    @app.post("/v1/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/v1/me")
    async def me(claims: Claims):
        return {
            "user": claims["user"],
            "tenant_id": claims["tenant"],
            "role": claims.get("role", "operator"),
            "dev": dev,
            "faults": faults_enabled,
        }

    @app.get("/v1/cases")
    async def list_cases(tenant: TenantId, state: str | None = None):
        async with db() as s:
            q = select(Case).where(Case.tenant_id == tenant).order_by(Case.updated_at.desc(), Case.id)
            if state:
                q = q.where(Case.state == state)
            cases = list((await s.execute(q.limit(100))).scalars())
            plans = {
                p.case_id: p
                for p in (
                    await s.execute(
                        select(Plan)
                        .where(Plan.tenant_id == tenant, Plan.case_id.in_([c.id for c in cases]))
                        .order_by(Plan.seq)
                    )
                ).scalars()
            }
        return [
            {
                "id": c.id,
                "subject": c.subject,
                "state": c.state,
                "resolution": c.resolution,
                "updated_at": iso(c.updated_at),
                "proposed_amount_minor": (plans[c.id].payload.get("amount_minor") if c.id in plans else None),
                "needs_approval": c.id in plans and plans[c.id].decision == "NEEDS_APPROVAL",
            }
            for c in cases
        ]

    @app.get("/v1/cases/{case_id}")
    async def get_case(case_id: str, tenant: TenantId):
        case = await own_case(tenant, case_id)
        async with db() as s:
            msgs = (
                await s.execute(
                    select(CaseMessage)
                    .where(CaseMessage.tenant_id == tenant, CaseMessage.case_id == case_id)
                    .order_by(CaseMessage.created_at)
                )
            ).scalars()
            order = (
                await s.execute(select(Order).where(Order.tenant_id == tenant, Order.id == case.order_id))
            ).scalar_one_or_none()
            pays = (
                await s.execute(
                    select(Payment)
                    .where(Payment.tenant_id == tenant, Payment.order_id == case.order_id)
                    .order_by(Payment.captured_at)
                )
            ).scalars()
            cmds = (
                await s.execute(
                    select(Command)
                    .where(Command.tenant_id == tenant, Command.case_id == case_id)
                    .order_by(Command.created_at)
                )
            ).scalars()
            plan = (
                await s.execute(
                    select(Plan)
                    .where(Plan.tenant_id == tenant, Plan.case_id == case_id)
                    .order_by(Plan.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            run = (
                await s.execute(
                    select(Run).where(Run.tenant_id == tenant, Run.case_id == case_id).order_by(Run.seq.desc()).limit(1)
                )
            ).scalar_one_or_none()
        return {
            "case": {
                "id": case.id,
                "subject": case.subject,
                "state": case.state,
                "resolution": case.resolution,
                "version": case.version,
                "created_at": iso(case.created_at),
                "updated_at": iso(case.updated_at),
            },
            "messages": [{"author": m.author, "body": m.body, "created_at": iso(m.created_at)} for m in msgs],
            "order": order
            and {
                "id": order.id,
                "total_minor": order.total_minor,
                "currency": order.currency,
                "placed_at": iso(order.placed_at),
            },
            "payments": [
                {
                    "id": p.id,
                    "provider_payment_id": p.provider_payment_id,
                    "amount_minor": p.amount_minor,
                    "currency": p.currency,
                    "status": p.status,
                    "captured_at": iso(p.captured_at),
                    "refunded_minor": p.refunded_minor,
                    "reserved_minor": p.reserved_minor,
                }
                for p in pays
            ],
            "commands": [
                {
                    "id": c.id,
                    "state": c.state,
                    "amount_minor": c.amount_minor,
                    "currency": c.currency,
                    "payment_id": c.payment_id,
                    "provider_refund_id": c.provider_refund_id,
                    "attempts": c.attempts,
                    "idempotency_key": c.idempotency_key,
                }
                for c in cmds
            ],
            "plan": plan
            and {
                "id": plan.id,
                "source": plan.source,
                "decision": plan.decision,
                "material_hash": plan.material_hash,
                "payload": plan.payload,
                "verdict": plan.verdict,
                "created_at": iso(plan.created_at),
            },
            "run": run
            and {
                "id": run.id,
                "stage": run.stage,
                "investigation": run.investigation,
                "workflow_version": run.workflow_version,
                "models": run.models,
                "lease_owner": run.lease_owner,
                "generation": run.generation,
                "model_calls": run.model_calls,
                "tool_calls": run.tool_calls,
                "est_cost_micros": run.est_cost_micros,
                "evidence": run.evidence,
                "failure": run.failure,
            },
        }

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, tenant: TenantId):
        async with db() as s:
            run = (await s.execute(select(Run).where(Run.tenant_id == tenant, Run.id == run_id))).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, "no such run")
        return {
            "id": run.id,
            "case_id": run.case_id,
            "stage": run.stage,
            "investigation": run.investigation,
            "generation": run.generation,
        }

    async def events_after(tenant: str, case_id: str, after: int) -> list[dict]:
        async with db() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, kind, actor, created_at, command_id, data FROM audit_events"
                        " WHERE tenant_id = :t AND id > :after AND (case_id = :c OR command_id IN"
                        " (SELECT id FROM commands WHERE tenant_id = :t AND case_id = :c))"
                        " ORDER BY id LIMIT :n"
                    ),
                    {"t": tenant, "c": case_id, "after": after, "n": PAGE},
                )
            ).all()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "label": LABELS.get(r.kind)
                or (f"Case {r.data.get('src')} → {r.data.get('dst')}" if r.kind == "case_state" else r.kind),
                "engineering": r.kind not in LABELS and r.kind != "case_state",
                "actor": r.actor,
                "at": iso(r.created_at),
                "command_id": r.command_id,
                "data": r.data,
            }
            for r in rows
        ]

    @app.get("/v1/cases/{case_id}/events")
    async def poll_events(case_id: str, tenant: TenantId, after: int = 0):
        await own_case(tenant, case_id)
        evs = await events_after(tenant, case_id, after)
        return {"events": evs, "cursor": evs[-1]["id"] if evs else after}

    @app.get("/v1/cases/{case_id}/events/stream")
    async def stream_events(case_id: str, request: Request, tenant: TenantId, after: int = 0, until_idle: bool = False):
        await own_case(tenant, case_id)
        # resume where the browser left off, reading never restarts any work
        last = request.headers.get("last-event-id")
        cursor = int(last) if last and last.isdigit() else after

        async def gen():
            nonlocal cursor
            quiet = 0
            while not await request.is_disconnected():
                evs = await events_after(tenant, case_id, cursor)
                for e in evs:
                    cursor = e["id"]
                    yield f"id: {e['id']}\nevent: audit\ndata: {json.dumps(e, default=str)}\n\n"
                if not evs:
                    if until_idle:
                        return
                    quiet += 1
                    if quiet % 30 == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        # no-transform: proxies (next's rewrite included) would otherwise gzip and buffer the stream
        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    async def run(tenant: str, case_id: str, mode: str, version: int) -> str:
        if mode == "agent":
            return await pipeline.start_agent(tenant, case_id, expected_version=version)
        async with db() as s:
            plan = await plan_by_rules(s, tenant, case_id, clock.now())
        if plan.case_version != version:
            raise NotStartable(f"case changed since v{version}, it's v{plan.case_version} now")
        return await pipeline.start_scripted(tenant, case_id, plan, source="rules")

    @app.post("/v1/cases/{case_id}/investigations", status_code=202)
    async def start(case_id: str, body: Start, tenant: TenantId, request: Request):
        async def work() -> dict:
            case = await own_case(tenant, case_id)
            if case.state != "OPEN":
                raise HTTPException(409, f"case is {case.state}, already started")
            try:
                run_id = await run(tenant, case_id, body.mode, body.case_version)
            except NotStartable as e:
                raise HTTPException(409, str(e)) from e
            return {"run_id": run_id, "mode": body.mode}

        return await once(request, tenant, body, work)

    @app.post("/v1/cases/{case_id}/messages", status_code=202)
    async def reply(case_id: str, body: Reply, tenant: TenantId, request: Request):
        # a reply to a case that was waiting on the customer picks the investigation back up
        async def work() -> dict:
            await own_case(tenant, case_id)
            try:
                was = await pipeline.add_message(
                    tenant, case_id, body.body, body.order_id, expected_version=body.case_version
                )
                resume = was is CaseState.AWAITING_INFO
                run_id = await run(tenant, case_id, body.mode, body.case_version + 1) if resume else None
            except NotStartable as e:
                raise HTTPException(409, str(e)) from e
            return {"run_id": run_id, "resumed": run_id is not None}

        return await once(request, tenant, body, work)

    @app.post("/v1/plans/{plan_id}/approvals", status_code=202)
    async def decide(plan_id: str, body: Decide, claims: Claims, request: Request):
        # the client sends the hash of the plan it showed, a plan that changed since can't be approved by accident
        if claims.get("role") != "supervisor":
            raise HTTPException(403, "only a supervisor can approve refunds")

        async def work() -> dict:
            try:
                run_id = await pipeline.approve(
                    claims["tenant"], plan_id, body.plan_hash, claims["user"], claims["role"], body.decision
                )
            except NotStartable as e:
                raise HTTPException(404 if "no such plan" in str(e) else 409, str(e)) from e
            return {"decision": body.decision, "run_id": run_id}

        return await once(request, claims["tenant"], body, work)

    def supervisor(claims: dict) -> str:
        if claims.get("role") != "supervisor":
            raise HTTPException(403, "supervisors only")
        return claims["tenant"]

    @app.get("/v1/ops/dead-letters")
    async def dead_letters(claims: Claims):
        # work the dispatcher gave up on, for a person to look at and re-drive
        tenant = supervisor(claims)
        async with db() as s:
            rows = (
                await s.execute(
                    select(OutboxEvent, Run.case_id)
                    .join(Run, Run.id == OutboxEvent.run_id)
                    .where(OutboxEvent.tenant_id == tenant, OutboxEvent.dead_at.is_not(None))
                    .order_by(OutboxEvent.id)
                )
            ).all()
        return [
            {
                "id": ev.id,
                "case_id": case_id,
                "run_id": ev.run_id,
                "stage": ev.stage,
                "attempts": ev.attempts,
                "last_error": ev.last_error,
                "dead_at": iso(ev.dead_at),
            }
            for ev, case_id in rows
        ]

    @app.post("/v1/ops/dead-letters/{outbox_id}/requeue", status_code=202)
    async def requeue(outbox_id: int, body: Requeue, claims: Claims, request: Request):
        tenant = supervisor(claims)

        async def work() -> dict:
            try:
                return {"requeued": await pipeline.requeue_dead(tenant, outbox_id, claims["user"])}
            except NotStartable as e:
                raise HTTPException(404 if "no such" in str(e) else 409, str(e)) from e

        return await once(request, tenant, body, work)

    @app.post("/internal/demo/faults")
    async def arm(body: Arm, _: TenantId):
        # dev only, the public app has no way to reach the fault table
        if not (dev and faults_enabled):
            raise HTTPException(404)
        async with db() as s, s.begin():
            await s.execute(text("INSERT INTO fault_points (point) VALUES (:p)"), {"p": body.point})
        return {"armed": body.point}

    return app


def create_from_env() -> FastAPI:
    env = os.environ.get("RECON_ENV", "dev")
    db = sessions(make_engine(os.environ.get("RECON_DATABASE_URL") or url("recon_app", "recon")))
    return create_app(
        db,
        SystemClock(),
        os.environ.get("RECON_SESSION_KEY", "dev-session-key").encode(),
        dev=env != "public",
        faults_enabled=os.environ.get("RECON_FAULTS") == "1" and env != "public",
    )
