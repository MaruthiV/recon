import httpx
import pytest
from sqlalchemy import text

from provider_sim.app import create_app
from provider_sim.db import ensure_schema as ensure_sim_schema
from recon_storage.bootstrap import bootstrap
from recon_storage.db import DbNames, admin_url, make_engine, sessions, url
from recon_verifier.db import ensure_schema as ensure_eval_schema

NAMES = DbNames.for_env("test")
OPERATOR = "test-operator"
SIM_TABLES = "accounts, payment_intents, refunds, idempotency, faults, requests_log"


@pytest.fixture(scope="session")
async def dbs():
    await bootstrap("test", fresh=True)
    await ensure_sim_schema(url("sim_app", NAMES.sim))
    await ensure_eval_schema(url("recon_eval", NAMES.eval))
    admins = {db: make_engine(admin_url(db)) for db in (NAMES.recon, NAMES.sim, NAMES.eval)}
    yield admins
    for e in admins.values():
        await e.dispose()


@pytest.fixture
async def clean(dbs):
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
        self.posts = 0

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
        return await self.inner.handle_async_request(request)


@pytest.fixture
async def flaky_sim(sim_asgi, clean):
    transport = FlakyTransport(httpx.ASGITransport(app=sim_asgi))
    async with httpx.AsyncClient(transport=transport, base_url="http://sim") as c:
        c.flaky = transport  # type: ignore[attr-defined]
        yield c
