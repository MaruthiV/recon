import asyncio
import json
import os
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from recon_domain.connector import Clock, SystemClock
from recon_domain.rules import rules_plan
from recon_domain.tokens import BadToken, mint, verify
from recon_storage.db import make_engine, sessions, url
from recon_storage.models import Case, CaseMessage, Command, Order, Payment, Plan, Run, Tenant
from recon_storage.repos import load_case_facts
from recon_worker.executor import Executor
from recon_worker.pipeline import NotStartable, Pipeline

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


class Start(BaseModel):
    mode: Literal["rules", "agent"]


class Arm(BaseModel):
    point: Literal["FP2", "FP3", "FP4"]


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
                "exp": int((now + timedelta(hours=8)).timestamp()),
            },
            session_key,
        )
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=8 * 3600)
        return {"user": body.user, "tenant_id": body.tenant_id}

    @app.post("/v1/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/v1/me")
    async def me(claims: Claims):
        return {"user": claims["user"], "tenant_id": claims["tenant"], "dev": dev, "faults": faults_enabled}

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
                        .order_by(Plan.created_at)
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
                    .order_by(Plan.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            run = (
                await s.execute(
                    select(Run)
                    .where(Run.tenant_id == tenant, Run.case_id == case_id)
                    .order_by(Run.created_at.desc())
                    .limit(1)
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

    @app.post("/v1/cases/{case_id}/investigations", status_code=202)
    async def start(case_id: str, body: Start, tenant: TenantId):
        case = await own_case(tenant, case_id)
        if case.state != "OPEN":
            raise HTTPException(409, f"case is {case.state}, already started")
        try:
            if body.mode == "agent":
                run_id = await pipeline.start_agent(tenant, case_id)
            else:
                async with db() as s:
                    facts = await load_case_facts(s, tenant, case_id, clock.now())
                run_id = await pipeline.start_scripted(tenant, case_id, rules_plan(facts), source="rules")
        except NotStartable as e:
            raise HTTPException(409, str(e)) from e
        return {"run_id": run_id, "mode": body.mode}

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
