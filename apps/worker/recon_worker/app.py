from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from recon_domain.connector import Clock, FakeClock, ReadFailed, SystemClock
from recon_provider.http import HttpConnector, HttpRefundReader
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


def investigator_from_env(db, clock: Clock):
    # RECON_AGENT=live:<model> or replay:<tape path>, unset means this worker can't run INVESTIGATE
    spec = os.environ.get("RECON_AGENT", "")
    if not spec:
        return None
    from google.adk.models.base_llm import BaseLlm
    from google.adk.sessions import DatabaseSessionService
    from sqlalchemy.ext.asyncio import create_async_engine

    from recon_agents.meter import Pricing
    from recon_agents.models import live_model
    from recon_agents.replay import Tape, replay_models
    from recon_agents.roles import ROLES

    from .investigate import AgentConfig, Investigator

    kind, _, arg = spec.partition(":")
    models: Mapping[str, BaseLlm | str]
    if kind == "replay":
        tape = Tape.load(Path(arg))
        models, mode, pricing = replay_models(tape), "replay", Pricing.load(tape.header.get("pricing", "2026-10-17"))
    elif kind == "live" and os.environ.get("RECON_LIVE") == "1":
        models, mode, pricing = {r: live_model(arg) for r in ROLES}, "live", Pricing.load("2026-10-17")
    else:
        raise RuntimeError("RECON_AGENT=live needs RECON_LIVE=1, or use replay:<tape>")
    adk = create_async_engine(
        os.environ.get("RECON_DATABASE_URL") or url("recon_app", "recon"),
        connect_args={"server_settings": {"search_path": "adk"}},
    )
    cfg = AgentConfig(
        models=models,
        mode=mode,
        pricing=pricing,
        mcp_url=os.environ.get("RECON_MCP_URL", "http://127.0.0.1:8103/mcp"),
        token_key=os.environ.get("RECON_RUN_TOKEN_KEY", "dev-run-token-key").encode(),
    )
    return Investigator(db, clock, cfg, DatabaseSessionService(db_engine=adk))


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
        # the deployed shape: money moves only through mcp-exec, this process only ever holds read keys
        gateway = McpExecGateway(exec_url, os.environ["RECON_EXEC_KEY"].encode(), clock)
        sim = httpx.AsyncClient(base_url=os.environ.get("RECON_SIM_URL", "http://localhost:8100"))
        readers = {t: HttpRefundReader(sim, k["read"]) for t, k in keys.items() if k.get("read")}

        def reader_for(tenant: str) -> HttpRefundReader:
            if tenant not in readers:
                raise ReadFailed(f"no read key for {tenant}")
            return readers[tenant]

        executor = Executor(
            db, clock, no_provider_key, faults=faults, gateway_for=lambda _t: gateway, reader_for=reader_for
        )
    else:
        sim = httpx.AsyncClient(base_url=os.environ.get("RECON_SIM_URL", "http://localhost:8100"))
        connectors = {t: HttpConnector(sim, k["secret"]) for t, k in keys.items()}
        executor = Executor(db, clock, connectors.__getitem__, faults=faults)
    pipeline = Pipeline(
        db,
        clock,
        executor,
        investigator_from_env(db, clock),
        lease_seconds=int(os.environ.get("RECON_LEASE_SECONDS", "60")),
        faults=faults,
    )
    hooks = Webhooks(db, clock, executor.ledger, lambda t: keys.get(t, {}).get("webhook_secret"))
    return create_app(pipeline, os.environ.get("RECON_WORKER_TOKEN", "dev-worker-token"), hooks)
