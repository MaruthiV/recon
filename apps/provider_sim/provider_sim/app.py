from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from . import db
from .db import accounts, events, faults, idempotency, payment_intents, refunds, requests_log, sim_state

REFUND_PARAMS = {"payment_intent", "charge", "amount", "reason", "metadata"}
REASONS = {"duplicate", "fraudulent", "requested_by_customer"}
COUNTED = ("succeeded", "pending", "requires_action")


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _error(
    status: int,
    type_: str,
    message: str,
    code: str | None = None,
    param: str | None = None,
    should_retry: bool | None = None,
) -> JSONResponse:
    err: dict[str, Any] = {"type": type_, "message": message}
    if code:
        err["code"] = code
        err["doc_url"] = f"https://docs.stripe.com/error-codes/{code.replace('_', '-')}"
    if param:
        err["param"] = param
    headers = {"Request-Id": _id("req")}
    if should_retry is not None:
        headers["Stripe-Should-Retry"] = "true" if should_retry else "false"
    return JSONResponse({"error": err}, status_code=status, headers=headers)


def refund_obj(r: Any) -> dict[str, Any]:
    out = {
        "id": r.id,
        "object": "refund",
        "amount": r.amount,
        "balance_transaction": None,
        "charge": r.charge,
        "created": r.created,
        "currency": r.currency,
        "metadata": r.metadata,
        "payment_intent": r.payment_intent,
        "reason": r.reason,
        "receipt_number": None,
        "source_transfer_reversal": None,
        "status": r.status,
        "transfer_reversal": None,
    }
    if r.failure_reason:
        out["failure_reason"] = r.failure_reason
    if r.pending_reason:
        out["pending_reason"] = r.pending_reason
    return out


def pi_obj(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "object": "payment_intent",
        "amount": p.amount,
        "amount_received": p.amount_received,
        "currency": p.currency,
        "status": p.status,
        "latest_charge": p.latest_charge,
        "customer": p.customer,
        "metadata": p.metadata,
        "created": p.created,
    }


def parse_form(body: bytes) -> tuple[dict[str, str], dict[str, str]]:
    plain: dict[str, str] = {}
    meta: dict[str, str] = {}
    for k, v in parse_qsl(body.decode(), keep_blank_values=True):
        if k.startswith("metadata[") and k.endswith("]"):
            meta[k[9:-1]] = v
        else:
            plain[k] = v
    return plain, meta


def sign(secret: str, t: int, body: bytes) -> str:
    # stripe scheme: t=<ts>,v1=hex(hmac_sha256(secret, "<ts>.<raw body>"))
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


async def emit(c: AsyncConnection, account: str, type_: str, refund_row: Any) -> None:
    t = await db.now(c)
    evt = _id("evt")
    payload = {
        "id": evt,
        "object": "event",
        "type": type_,
        "created": int(t.timestamp()),
        "data": {"object": refund_obj(refund_row)},
    }
    await c.execute(events.insert().values(id=evt, account=account, type=type_, payload=payload))


async def settle_due(c: AsyncConnection) -> None:
    now = await db.now(c)
    due = (
        (
            await c.execute(
                update(refunds)
                .where(refunds.c.status == "pending", refunds.c.settle_at <= now)
                .values(status=refunds.c.settle_to, settle_at=None)
                .returning(refunds.c.id)
            )
        )
        .scalars()
        .all()
    )
    for rid in due:
        r = (await c.execute(select(refunds).where(refunds.c.id == rid))).one()
        if r.status == "failed":
            await c.execute(
                update(refunds).where(refunds.c.id == rid).values(failure_reason="expired_or_canceled_card")
            )
            r = (await c.execute(select(refunds).where(refunds.c.id == rid))).one()
        await emit(c, r.account, "refund.updated", r)


def fingerprint(path: str, plain: dict[str, str], meta: dict[str, str]) -> str:
    blob = json.dumps({"path": path, "params": plain, "metadata": meta}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def create_app(dsn: str | None = None, operator_token: str | None = None) -> FastAPI:
    engine: AsyncEngine = create_async_engine(dsn or db.default_dsn(), pool_pre_ping=True)
    op_token = operator_token or os.environ.get("SIM_OPERATOR_TOKEN", "sim-operator-dev")
    app = FastAPI(title="provider-sim")
    app.state.engine = engine
    # tests swap this for an in-process transport to recon
    app.state.webhook_client = httpx.AsyncClient(timeout=10)

    async def log(
        c: AsyncConnection,
        account: str | None,
        request: Request,
        key: str | None,
        status: int,
        replayed: bool = False,
        note: str | None = None,
    ) -> None:
        await c.execute(
            requests_log.insert().values(
                account=account,
                method=request.method,
                path=request.url.path,
                idempotency_key=key,
                status=status,
                replayed=replayed,
                note=note,
                at=await db.now(c),
            )
        )

    async def auth(request: Request, write: bool = False) -> str:
        header = request.headers.get("authorization", "")
        key = header.removeprefix("Bearer ").strip()
        async with engine.connect() as c:
            row = (
                await c.execute(
                    select(accounts.c.id, accounts.c.api_key).where(
                        or_(accounts.c.api_key == key, accounts.c.read_key == key)
                    )
                )
            ).first()
        if not key or row is None:
            raise HTTPException(401)
        if write and row.api_key != key:
            raise HTTPException(403, "restricted key")
        return row.id

    async def take_fault(account: str, kinds: tuple[str, ...], key: str | None, pi: str | None) -> dict | None:
        async with engine.begin() as c:
            row = (
                await c.execute(
                    text(
                        "UPDATE faults SET remaining = remaining - 1 WHERE id = ("
                        " SELECT id FROM faults WHERE account = :a AND remaining > 0 AND kind = ANY(:k)"
                        " AND (idempotency_key IS NULL OR idempotency_key = :key)"
                        " AND (payment_intent IS NULL OR payment_intent = :pi)"
                        # block, don't skip: concurrent requests must each see the fault until it's used up
                        " ORDER BY id LIMIT 1 FOR UPDATE) RETURNING kind, params"
                    ),
                    {"a": account, "k": list(kinds), "key": key, "pi": pi},
                )
            ).first()
        return {"kind": row.kind, "params": row.params} if row else None

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return _error(401, "invalid_request_error", "Invalid API Key provided.")
        if exc.status_code == 403 and exc.detail == "restricted key":
            return _error(
                403,
                "invalid_request_error",
                "The provided key does not have the required permissions for this endpoint.",
            )
        if exc.status_code == 403:
            return JSONResponse({"error": "operator only"}, status_code=403)
        return _error(exc.status_code, "invalid_request_error", str(exc.detail), code="resource_missing")

    @app.post("/v1/refunds")
    async def create_refund(request: Request):
        account = await auth(request, write=True)
        plain, meta = parse_form(await request.body())
        key = request.headers.get("idempotency-key")

        # pre-execution validation, never cached
        unknown = set(plain) - REFUND_PARAMS
        if unknown:
            return _error(
                400,
                "invalid_request_error",
                f"Received unknown parameter: {sorted(unknown)[0]}",
                "parameter_unknown",
                sorted(unknown)[0],
            )
        if "payment_intent" not in plain:
            return _error(
                400,
                "invalid_request_error",
                "Missing required param: payment_intent.",
                "parameter_missing",
                "payment_intent",
            )
        if "amount" in plain and not plain["amount"].isdigit():
            return _error(
                400, "invalid_request_error", "Invalid integer: amount", "parameter_invalid_integer", "amount"
            )
        if plain.get("reason") and plain["reason"] not in REASONS:
            return _error(400, "invalid_request_error", "Invalid reason", "parameter_invalid_empty", "reason")
        if key is not None and not 0 < len(key) <= 255:
            return _error(400, "invalid_request_error", "Invalid idempotency key")
        if await take_fault(account, ("throttle",), key, plain["payment_intent"]):
            async with engine.begin() as c:
                await log(c, account, request, key, 429, note="throttled")
            return _error(429, "invalid_request_error", "Too many requests", "rate_limit", should_retry=True)

        fp = fingerprint("/v1/refunds", plain, meta)
        if key is not None:
            async with engine.begin() as c:
                row = (
                    await c.execute(
                        select(idempotency)
                        .where(idempotency.c.account == account, idempotency.c.key == key)
                        .with_for_update()
                    )
                ).first()
                t = await db.now(c)
                retention = (await c.execute(select(sim_state.c.retention_seconds))).scalar_one()
                if row is not None and row.created_at + timedelta(seconds=retention) <= t:
                    # pruned, reuse now starts a brand new request
                    await c.execute(
                        delete(idempotency).where(idempotency.c.account == account, idempotency.c.key == key)
                    )
                    row = None
                if row is None:
                    await c.execute(
                        idempotency.insert().values(
                            account=account, key=key, fingerprint=fp, state="in_progress", created_at=t
                        )
                    )
                elif row.fingerprint != fp:
                    await log(c, account, request, key, 400, note="idempotency mismatch")
                    return _error(
                        400,
                        "idempotency_error",
                        "Keys for idempotent requests can only be used with the same parameters they were "
                        "first used with.",
                    )
                elif row.state == "in_progress":
                    await log(c, account, request, key, 409, note="key in use")
                    return _error(
                        409,
                        "idempotency_error",
                        "There is currently another in-progress request using this Idempotent Key.",
                        "idempotency_key_in_use",
                        should_retry=True,
                    )
                else:
                    await log(c, account, request, key, row.status_code, replayed=True)
                    return JSONResponse(
                        row.body,
                        status_code=row.status_code,
                        headers={"Idempotent-Replayed": "true", "Request-Id": _id("req")},
                    )

        slow = await take_fault(account, ("slow",), key, plain["payment_intent"])
        if slow:
            await asyncio.sleep(slow["params"].get("seconds", 1))

        status, body, cache = await execute_refund(account, plain, meta, key)
        async with engine.begin() as c:
            if key is not None:
                if cache:
                    await c.execute(
                        update(idempotency)
                        .where(
                            idempotency.c.account == account,
                            idempotency.c.key == key,
                        )
                        .values(state="done", status_code=status, body=body)
                    )
                else:
                    await c.execute(
                        delete(idempotency).where(idempotency.c.account == account, idempotency.c.key == key)
                    )
            await log(c, account, request, key, status)
        return JSONResponse(body, status_code=status, headers={"Request-Id": _id("req")})

    async def execute_refund(
        account: str, plain: dict[str, str], meta: dict[str, str], key: str | None
    ) -> tuple[int, dict, bool]:
        pi_id = plain["payment_intent"]
        fault = await take_fault(account, ("commit_then_500", "fail_500"), key, pi_id)
        delayed = await take_fault(account, ("pending",), key, pi_id)
        server_error = {"error": {"type": "api_error", "message": "An unknown error occurred."}}
        if fault and fault["kind"] == "fail_500":
            return 500, server_error, True
        async with engine.begin() as c:
            pi = (
                await c.execute(
                    select(payment_intents)
                    .where(payment_intents.c.id == pi_id, payment_intents.c.account == account)
                    .with_for_update()
                )
            ).first()
            if pi is None:
                return (
                    404,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "resource_missing",
                            "param": "payment_intent",
                            "message": f"No such payment_intent: '{pi_id}'",
                        }
                    },
                    False,
                )
            used = (
                await c.execute(
                    select(func.coalesce(func.sum(refunds.c.amount), 0)).where(
                        refunds.c.payment_intent == pi_id, refunds.c.status.in_(COUNTED)
                    )
                )
            ).scalar_one()
            left = pi.amount_received - used
            amount = int(plain["amount"]) if "amount" in plain else left
            if left <= 0:
                return (
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "charge_already_refunded",
                            "message": f"Charge {pi.latest_charge} has already been refunded.",
                        }
                    },
                    True,
                )
            if amount <= 0 or amount > left:
                return (
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "amount_too_large",
                            "param": "amount",
                            "message": f"Refund amount ({amount}) is greater than unrefunded amount ({left}).",
                        }
                    },
                    True,
                )
            t = await db.now(c)
            seq = (
                await c.execute(
                    update(sim_state).values(refund_seq=sim_state.c.refund_seq + 1).returning(sim_state.c.refund_seq)
                )
            ).scalar_one()
            row = (
                await c.execute(
                    refunds.insert()
                    .values(
                        id=_id("re"),
                        account=account,
                        payment_intent=pi_id,
                        charge=pi.latest_charge,
                        amount=amount,
                        currency=pi.currency,
                        status="pending" if delayed else "succeeded",
                        reason=plain.get("reason") or None,
                        metadata=meta,
                        created=int(t.timestamp()),
                        seq=seq,
                        settle_at=t + timedelta(seconds=delayed["params"].get("after_seconds", 30))
                        if delayed
                        else None,
                        settle_to=delayed["params"].get("then", "succeeded") if delayed else None,
                    )
                    .returning(*refunds.c)
                )
            ).one()
            await emit(c, account, "refund.created", row)
        if fault and fault["kind"] == "commit_then_500":
            # the refund exists, the client just never hears about it
            return 500, server_error, True
        return 200, refund_obj(row), True

    @app.get("/v1/refunds/{refund_id}")
    async def get_refund(refund_id: str, request: Request):
        account = await auth(request)
        async with engine.begin() as c:
            await settle_due(c)
        async with engine.connect() as c:
            r = (
                await c.execute(select(refunds).where(refunds.c.id == refund_id, refunds.c.account == account))
            ).first()
        if r is None:
            raise HTTPException(404, f"No such refund: '{refund_id}'")
        return refund_obj(r)

    @app.get("/v1/refunds")
    async def list_refunds(
        request: Request, payment_intent: str | None = None, limit: int = 10, starting_after: str | None = None
    ):
        account = await auth(request)
        if await take_fault(account, ("throttle_reads",), None, payment_intent):
            return _error(429, "invalid_request_error", "Too many requests", "rate_limit", should_retry=True)
        limit = max(1, min(limit, 100))
        async with engine.begin() as c:
            await settle_due(c)
        q = select(refunds).where(refunds.c.account == account).order_by(refunds.c.seq.desc())
        if payment_intent:
            q = q.where(refunds.c.payment_intent == payment_intent)
        async with engine.connect() as c:
            if starting_after:
                after = (await c.execute(select(refunds.c.seq).where(refunds.c.id == starting_after))).scalar()
                if after is None:
                    raise HTTPException(404, f"No such refund: '{starting_after}'")
                q = q.where(refunds.c.seq < after)
            rows = (await c.execute(q.limit(limit + 1))).all()
        return {
            "object": "list",
            "url": "/v1/refunds",
            "has_more": len(rows) > limit,
            "data": [refund_obj(r) for r in rows[:limit]],
        }

    @app.get("/v1/payment_intents/{pi_id}")
    async def get_payment_intent(pi_id: str, request: Request):
        account = await auth(request)
        async with engine.connect() as c:
            p = (
                await c.execute(
                    select(payment_intents).where(payment_intents.c.id == pi_id, payment_intents.c.account == account)
                )
            ).first()
        if p is None:
            raise HTTPException(404, f"No such payment_intent: '{pi_id}'")
        return pi_obj(p)

    # control plane, operator identity only, never reachable from agent tools

    def operator(request: Request) -> None:
        if not secrets.compare_digest(request.headers.get("x-sim-operator", ""), op_token):
            raise HTTPException(403)

    @app.post("/sim/reset")
    async def reset(request: Request):
        operator(request)
        async with engine.begin() as c:
            for t in db.DATA_TABLES:
                await c.execute(delete(t))
            await c.execute(
                update(sim_state).values(frozen_at=None, offset_seconds=0, retention_seconds=86400, refund_seq=0)
            )
        return {"ok": True}

    @app.post("/sim/accounts")
    async def add_account(request: Request):
        operator(request)
        body = await request.json()
        async with engine.begin() as c:
            await c.execute(
                accounts.insert().values(
                    id=body["id"],
                    api_key=body["api_key"],
                    read_key=body.get("read_key"),
                    webhook_url=body.get("webhook_url"),
                    webhook_secret=body.get("webhook_secret"),
                )
            )
        return {"id": body["id"]}

    @app.post("/sim/payment_intents")
    async def add_payment_intent(request: Request):
        operator(request)
        b = await request.json()
        if type(b["amount"]) is not int or b["amount"] <= 0:
            return JSONResponse({"error": "amount must be a positive int"}, status_code=400)
        async with engine.begin() as c:
            t = await db.now(c)
            row = (
                await c.execute(
                    payment_intents.insert()
                    .values(
                        id=b.get("id") or _id("pi"),
                        account=b["account"],
                        amount=b["amount"],
                        amount_received=b["amount"],
                        currency=b.get("currency", "usd"),
                        status="succeeded",
                        latest_charge=_id("ch"),
                        customer=b.get("customer"),
                        metadata=b.get("metadata", {}),
                        created=int(t.timestamp()),
                    )
                    .returning(*payment_intents.c)
                )
            ).one()
        return pi_obj(row)

    @app.get("/sim/clock")
    async def get_clock(request: Request):
        operator(request)
        async with engine.connect() as c:
            return {"now": (await db.now(c)).isoformat()}

    @app.post("/sim/clock")
    async def set_clock(request: Request):
        operator(request)
        b = await request.json()
        async with engine.begin() as c:
            if "freeze_at" in b:
                await c.execute(
                    update(sim_state).values(frozen_at=datetime.fromisoformat(b["freeze_at"]), offset_seconds=0)
                )
            if "advance_seconds" in b:
                await c.execute(
                    update(sim_state).values(offset_seconds=sim_state.c.offset_seconds + int(b["advance_seconds"]))
                )
            await settle_due(c)
            return {"now": (await db.now(c)).isoformat()}

    @app.post("/sim/config")
    async def set_config(request: Request):
        operator(request)
        b = await request.json()
        async with engine.begin() as c:
            await c.execute(update(sim_state).values(retention_seconds=int(b["retention_seconds"])))
        return {"ok": True}

    @app.post("/sim/faults")
    async def add_fault(request: Request):
        operator(request)
        b = await request.json()
        async with engine.begin() as c:
            fid = (
                await c.execute(
                    faults.insert()
                    .values(
                        account=b["account"],
                        kind=b["kind"],
                        idempotency_key=b.get("idempotency_key"),
                        payment_intent=b.get("payment_intent"),
                        remaining=b.get("count", 1),
                        params=b.get("params", {}),
                    )
                    .returning(faults.c.id)
                )
            ).scalar_one()
        return {"id": fid}

    @app.post("/sim/webhooks/flush")
    async def flush_webhooks(request: Request):
        # deliver undelivered events, optionally out of order / twice / with the first one lost
        operator(request)
        mode = set((await request.json()).get("mode", []))
        async with engine.connect() as c:
            rows = (
                await c.execute(
                    select(events, accounts.c.webhook_url, accounts.c.webhook_secret)
                    .join(accounts, accounts.c.id == events.c.account)
                    .where(events.c.deliveries == 0)
                    .order_by(events.c.seq)
                )
            ).all()
            t = int((await db.now(c)).timestamp())
        plan = list(rows)
        if "reverse" in mode:
            plan.reverse()
        dropped = plan[:1] if "drop_first" in mode else []
        plan = [r for r in plan if r not in dropped]
        if "duplicate" in mode:
            plan = [r for r in plan for _ in range(2)]
        out = []
        for r in plan:
            if not r.webhook_url or not r.webhook_secret:
                continue
            body = json.dumps(r.payload, separators=(",", ":")).encode()
            resp = await app.state.webhook_client.post(
                r.webhook_url,
                content=body,
                headers={"content-type": "application/json", "Stripe-Signature": sign(r.webhook_secret, t, body)},
            )
            async with engine.begin() as c:
                await c.execute(update(events).where(events.c.id == r.id).values(deliveries=events.c.deliveries + 1))
            out.append({"event": r.id, "type": r.type, "status": resp.status_code})
        return {"delivered": out, "dropped": [r.id for r in dropped]}

    @app.get("/sim/state")
    async def state(request: Request):
        operator(request)
        async with engine.connect() as c:
            out = {}
            for t in (payment_intents, refunds, requests_log):
                rows = (await c.execute(select(t))).mappings().all()
                out[t.name] = [
                    {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in rows
                ]
            out["now"] = (await db.now(c)).isoformat()
        return out

    return app
