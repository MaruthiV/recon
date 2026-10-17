from __future__ import annotations

import json
import os
import secrets
from datetime import datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from recon_domain.connector import Clock, FakeClock, SystemClock
from recon_provider.http import HttpConnector
from recon_storage.db import make_engine, sessions, url

from .exec_client import McpExecGateway
from .executor import Executor
from .faults import Faults, enabled_from_env
from .pipeline import Pipeline, Stage
from .webhooks import Webhooks

TOKEN_HEADER = "x-recon-task-token"


class AdvanceBody(BaseModel):
    run_id: str
    stage: Stage
    event_id: str = ""


def create_app(pipeline: Pipeline, token: str, webhooks: Webhooks | None = None) -> FastAPI:
    app = FastAPI(title="recon-worker")
    app.state.pipeline = pipeline

    @app.post("/webhooks/provider/{tenant_id}")
    async def provider_webhook(tenant_id: str, request: Request, background: BackgroundTasks):
        if webhooks is None:
            return JSONResponse({"detail": "webhooks not configured"}, status_code=404)
        status = await webhooks.receive(tenant_id, await request.body(), request.headers.get("stripe-signature", ""))
        if status == 200:
            # ack first, process after, like the provider asks
            background.add_task(webhooks.process_pending)
        return JSONResponse({"received": status == 200}, status_code=status)

    @app.post("/internal/tasks/advance")
    async def advance(body: AdvanceBody, request: Request):
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            return JSONResponse({"detail": "bad task token"}, status_code=401)
        r = await pipeline.advance(body.run_id, body.stage, body.event_id)
        return JSONResponse({"detail": r.detail, "next": r.next}, status_code=r.status)

    return app


def no_provider_key(_tenant: str):
    raise RuntimeError("this worker has no provider key, money moves through mcp-exec")


def clock_from_env() -> Clock:
    # tests pin business time so a worker process and the test agree on "now"
    fixed = os.environ.get("RECON_NOW")
    return FakeClock(datetime.fromisoformat(fixed)) if fixed else SystemClock()


def create_from_env() -> FastAPI:
    db = sessions(make_engine(os.environ.get("RECON_DATABASE_URL") or url("recon_app", "recon")))
    clock = clock_from_env()
    keys = json.loads(os.environ.get("RECON_PROVIDER_KEYS", "{}"))
    faults = Faults(db, enabled=enabled_from_env())
    exec_url = os.environ.get("RECON_EXEC_URL")
    if exec_url:
        # the deployed shape: money moves only through mcp-exec, this process never sees a provider key
        gateway = McpExecGateway(exec_url, os.environ["RECON_EXEC_KEY"].encode(), clock)
        executor = Executor(db, clock, no_provider_key, faults=faults, gateway_for=lambda _t: gateway)
    else:
        sim = httpx.AsyncClient(base_url=os.environ.get("RECON_SIM_URL", "http://localhost:8100"))
        connectors = {t: HttpConnector(sim, k["secret"]) for t, k in keys.items()}
        executor = Executor(db, clock, connectors.__getitem__, faults=faults)
    pipeline = Pipeline(
        db, clock, executor, lease_seconds=int(os.environ.get("RECON_LEASE_SECONDS", "60")), faults=faults
    )
    hooks = Webhooks(db, clock, executor.ledger, lambda t: keys.get(t, {}).get("webhook_secret"))
    return create_app(pipeline, os.environ.get("RECON_WORKER_TOKEN", "dev-worker-token"), hooks)
