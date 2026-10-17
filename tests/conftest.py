import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

# before anything imports adk: skip the gce mtls probe, use the stateless mcp protocol
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")
os.environ.setdefault("ADK_ENABLE_MCP_MODERN_PROTOCOL", "1")

import httpx
import httpx2
import pytest
from google.adk.sessions import DatabaseSessionService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from provider_sim.app import create_app
from provider_sim.db import ensure_schema as ensure_sim_schema
from recon_domain.connector import FakeClock
from recon_mcp.server import create_app as create_mcp_app
from recon_provider.http import HttpRefundReader
from recon_storage.bootstrap import bootstrap
from recon_storage.db import DbNames, admin_url, make_engine, sessions, url
from recon_verifier.db import ensure_schema as ensure_eval_schema

# live runs use their own dbs so they can't collide with a normal test session
TEST_ENV = os.environ.get("RECON_TEST_ENV", "test")
NAMES = DbNames.for_env(TEST_ENV)
OPERATOR = "test-operator"
RUN_KEY = b"test-run-token-key"
MCP_URL = "http://localhost:8200/mcp"
T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SIM_TABLES = "accounts, payment_intents, refunds, idempotency, faults, requests_log, events"


@pytest.fixture(scope="session")
async def dbs():
    await bootstrap(TEST_ENV, fresh=True)
    await ensure_sim_schema(url("sim_app", NAMES.sim))
    await ensure_eval_schema(url("recon_eval", NAMES.eval))
    admins = {db: make_engine(admin_url(db)) for db in (NAMES.recon, NAMES.sim, NAMES.eval)}
    yield admins
    for e in admins.values():
        await e.dispose()


async def truncate_all(dbs) -> None:
    async with dbs[NAMES.recon].begin() as c:
        tables = (
            await c.execute(
                text(
                    "SELECT string_agg(quote_ident(tablename), ', ') FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
        ).scalar_one()
        await c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with dbs[NAMES.sim].begin() as c:
        await c.execute(text(f"TRUNCATE {SIM_TABLES} RESTART IDENTITY"))
        await c.execute(
            text("UPDATE sim_state SET frozen_at = NULL, offset_seconds = 0, retention_seconds = 86400, refund_seq = 0")
        )
    async with dbs[NAMES.eval].begin() as c:
        await c.execute(text("TRUNCATE labels, verdicts"))


@pytest.fixture
async def clean(dbs):
    await truncate_all(dbs)
    yield


@pytest.fixture(scope="session")
async def app_engine(dbs):
    e = make_engine(url("recon_app", NAMES.recon), pool_size=20, max_overflow=10)
    yield e
    await e.dispose()


@pytest.fixture(scope="session")
async def eval_engine(dbs):
    e = make_engine(url("recon_eval", NAMES.recon))
    yield e
    await e.dispose()


@pytest.fixture
def app_sessions(app_engine, clean):
    return sessions(app_engine)


@pytest.fixture(scope="session")
async def sim_asgi(dbs):
    app = create_app(url("sim_app", NAMES.sim), OPERATOR)
    yield app
    await app.state.engine.dispose()


@pytest.fixture
async def sim(sim_asgi, clean):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sim_asgi), base_url="http://sim") as c:
        yield c


@pytest.fixture
def op():
    return {"x-sim-operator": OPERATOR}


@pytest.fixture(scope="session")
async def verifier_engines(dbs):
    engines = {
        "recon": make_engine(url("recon_eval", NAMES.recon)),
        "sim": make_engine(url("recon_eval", NAMES.sim)),
        "eval": make_engine(url("recon_eval", NAMES.eval)),
    }
    yield engines
    for e in engines.values():
        await e.dispose()


class FlakyTransport(httpx.AsyncBaseTransport):
    """wraps the sim: drop requests before they land, or lose responses after they commit"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.drop_before = 0
        self.lose_after = 0
        self.give_up_early = 0  # client times out while the provider is still working
        self.give_up_after = 0.2
        self.posts = 0
        self.background: list[asyncio.Task] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/refunds":
            self.posts += 1
            if self.drop_before:
                self.drop_before -= 1
                raise httpx.ConnectError("connection reset before send", request=request)
            if self.lose_after:
                self.lose_after -= 1
                await self.inner.handle_async_request(request)
                raise httpx.ReadTimeout("response lost after commit", request=request)
            if self.give_up_early:
                self.give_up_early -= 1
                task = asyncio.create_task(self.inner.handle_async_request(request))
                self.background.append(task)
                done, _ = await asyncio.wait({task}, timeout=self.give_up_after)
                if not done:
                    raise httpx.ReadTimeout("client gave up, provider still working", request=request)
                return task.result()
        return await self.inner.handle_async_request(request)


@pytest.fixture
async def flaky_sim(sim_asgi, clean):
    transport = FlakyTransport(httpx.ASGITransport(app=sim_asgi))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as c:
        c.flaky = transport  # type: ignore[attr-defined]
        yield c


@asynccontextmanager
async def serving(service):
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


class McpRead:
    """in-process mcp-read wired to the in-process sim, readers keyed by tenant"""

    def __init__(self, sim_client: httpx.AsyncClient) -> None:
        self.sim = sim_client
        self.read_keys: dict[str, str] = {}
        self.service: Any = None

    def reader_for(self, tenant_id: str) -> HttpRefundReader:
        return HttpRefundReader(self.sim, self.read_keys[tenant_id])

    def http_factory(self, headers=None, timeout=None, auth=None):
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.service),
            headers=headers,
            timeout=timeout,
            auth=auth,
            base_url="http://localhost:8200",
        )


@pytest.fixture
def clock():
    return FakeClock(T)


@pytest.fixture
async def mcp_read(flaky_sim, app_sessions, clock):
    m = McpRead(flaky_sim)
    m.service = create_mcp_app(url("recon_mcp_read", NAMES.recon), RUN_KEY, m.reader_for, clock, host="localhost")
    async with serving(m.service):
        yield m
    await m.service.engine.dispose()


@pytest.fixture(scope="session")
async def adk_sessions(dbs):
    engine = create_async_engine(
        url("recon_app", NAMES.recon), connect_args={"server_settings": {"search_path": "adk"}}
    )
    yield DatabaseSessionService(db_engine=engine)
    await engine.dispose()
