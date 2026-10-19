from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import httpx2
from google.adk.sessions import DatabaseSessionService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from provider_sim.app import create_app as create_sim
from provider_sim.db import ensure_schema as ensure_sim_schema
from recon_domain.connector import FakeClock
from recon_mcp.server import ReadService
from recon_mcp.server import create_app as create_mcp
from recon_provider.http import HttpRefundReader
from recon_retrieval.embed import embedder_from_env
from recon_storage.bootstrap import bootstrap
from recon_storage.db import DbNames, admin_url, make_engine, sessions, url
from recon_verifier.db import ensure_schema as ensure_eval_schema

# business time is pinned so every case, label and policy window is the same on every run
T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OPERATOR = "bench-operator"
RUN_KEY = b"bench-run-token-key"
MCP_URL = "http://localhost:8200/mcp"
SIM_TABLES = "accounts, payment_intents, refunds, idempotency, faults, requests_log, events"


@dataclass
class World:
    names: DbNames
    admins: dict[str, AsyncEngine]
    db: async_sessionmaker
    sim: httpx.AsyncClient
    op: dict[str, str]
    evals: AsyncEngine
    verifier: dict[str, AsyncEngine]
    mcp: ReadService
    adk: DatabaseSessionService
    clock: FakeClock
    read_keys: dict[str, str] = field(default_factory=dict)

    def http_factory(self, headers=None, timeout=None, auth=None) -> httpx2.AsyncClient:
        # agents reach mcp-read in-process, same code path as over the network
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.mcp),
            headers=headers,
            timeout=timeout,
            auth=auth,
            base_url="http://localhost:8200",
        )

    def reader_for(self, tenant_id: str) -> HttpRefundReader:
        return HttpRefundReader(self.sim, self.read_keys[tenant_id])

    async def reset(self) -> None:
        # every case starts from empty databases and a fresh simulator, nothing leaks between cases
        async with self.admins[self.names.recon].begin() as c:
            tables = (
                await c.execute(
                    text(
                        "SELECT string_agg(quote_ident(tablename), ', ') FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
            ).scalar_one()
            await c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        async with self.admins[self.names.sim].begin() as c:
            await c.execute(text(f"TRUNCATE {SIM_TABLES} RESTART IDENTITY"))
            await c.execute(
                text(
                    "UPDATE sim_state SET frozen_at = NULL, offset_seconds = 0, retention_seconds = 86400, refund_seq = 0"
                )
            )
        async with self.admins[self.names.eval].begin() as c:
            await c.execute(text("TRUNCATE labels, verdicts"))
        self.read_keys.clear()


@asynccontextmanager
async def world(env: str = "bench") -> AsyncIterator[World]:
    names = await bootstrap(env, fresh=True)
    await ensure_sim_schema(url("sim_app", names.sim))
    await ensure_eval_schema(url("recon_eval", names.eval))
    admins = {d: make_engine(admin_url(d)) for d in (names.recon, names.sim, names.eval)}
    app_engine = make_engine(url("recon_app", names.recon), pool_size=20, max_overflow=10)
    verifier = {
        "recon": make_engine(url("recon_eval", names.recon)),
        "sim": make_engine(url("recon_eval", names.sim)),
        "eval": make_engine(url("recon_eval", names.eval)),
    }
    adk_engine = create_async_engine(
        url("recon_app", names.recon), connect_args={"server_settings": {"search_path": "adk"}}
    )
    sim_app = create_sim(url("sim_app", names.sim), OPERATOR)
    clock = FakeClock(T)
    holder: dict[str, Any] = {}
    mcp = create_mcp(
        url("recon_mcp_read", names.recon),
        RUN_KEY,
        lambda t: holder["w"].reader_for(t),
        clock,
        host="localhost",
        embedder=embedder_from_env(),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sim_app), base_url="http://sim") as sim:
        w = World(
            names=names,
            admins=admins,
            db=sessions(app_engine),
            sim=sim,
            op={"x-sim-operator": OPERATOR},
            evals=verifier["eval"],
            verifier=verifier,
            mcp=mcp,
            adk=DatabaseSessionService(db_engine=adk_engine),
            clock=clock,
        )
        holder["w"] = w
        async with serving(mcp):
            try:
                yield w
            finally:
                for e in (
                    *admins.values(),
                    app_engine,
                    *verifier.values(),
                    adk_engine,
                    mcp.engine,
                    sim_app.state.engine,
                ):
                    await e.dispose()


@asynccontextmanager
async def serving(service: ReadService) -> AsyncIterator[ReadService]:
    # the mcp lifespan's task group has to open and close in one task
    ready, stop = asyncio.Event(), asyncio.Event()

    async def hold():
        async with service.lifespan():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(hold())
    await ready.wait()
    try:
        yield service
    finally:
        stop.set()
        await task
