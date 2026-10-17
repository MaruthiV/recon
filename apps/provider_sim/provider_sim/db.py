from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

metadata = MetaData()
Ts = DateTime(timezone=True)

accounts = Table(
    "accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("api_key", String, nullable=False, unique=True),
)

payment_intents = Table(
    "payment_intents",
    metadata,
    Column("id", String, primary_key=True),
    Column("account", String, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("amount_received", BigInteger, nullable=False),
    Column("currency", String, nullable=False),
    Column("status", String, nullable=False),
    Column("latest_charge", String, nullable=False),
    Column("customer", String),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    Column("created", BigInteger, nullable=False),
)

refunds = Table(
    "refunds",
    metadata,
    Column("id", String, primary_key=True),
    Column("account", String, nullable=False),
    Column("payment_intent", String, nullable=False, index=True),
    Column("charge", String, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("currency", String, nullable=False),
    Column("status", String, nullable=False),
    Column("reason", String),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    Column("failure_reason", String),
    Column("pending_reason", String),
    Column("created", BigInteger, nullable=False),
    Column("seq", BigInteger, nullable=False),
    CheckConstraint("amount > 0", name="refund_positive"),
)

idempotency = Table(
    "idempotency",
    metadata,
    Column("account", String, nullable=False),
    Column("key", String, nullable=False),
    Column("fingerprint", String, nullable=False),
    Column("state", String, nullable=False),
    Column("status_code", Integer),
    Column("body", JSONB),
    Column("created_at", Ts, nullable=False),
    PrimaryKeyConstraint("account", "key"),
)

faults = Table(
    "faults",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("idempotency_key", String),
    Column("payment_intent", String),
    Column("remaining", Integer, nullable=False),
    Column("params", JSONB, nullable=False, server_default="{}"),
)

requests_log = Table(
    "requests_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("account", String),
    Column("method", String, nullable=False),
    Column("path", String, nullable=False),
    Column("idempotency_key", String),
    Column("status", Integer, nullable=False),
    Column("replayed", Boolean, nullable=False),
    Column("note", Text),
    Column("at", Ts, nullable=False),
)

sim_state = Table(
    "sim_state",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("frozen_at", Ts),
    Column("offset_seconds", BigInteger, nullable=False, server_default="0"),
    Column("retention_seconds", BigInteger, nullable=False, server_default="86400"),
    Column("refund_seq", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("id = 1", name="single_row"),
)

DATA_TABLES = (accounts, payment_intents, refunds, idempotency, faults, requests_log)


async def now(c: AsyncConnection) -> datetime:
    row = (await c.execute(select(sim_state.c.frozen_at, sim_state.c.offset_seconds))).one()
    base = row.frozen_at if row.frozen_at is not None else datetime.now(UTC)
    return base + timedelta(seconds=row.offset_seconds)


async def ensure_schema(dsn: str) -> None:
    engine = create_async_engine(dsn)
    async with engine.begin() as c:
        await c.run_sync(metadata.create_all)
        if (await c.execute(select(sim_state.c.id))).first() is None:
            await c.execute(sim_state.insert().values(id=1))
    await engine.dispose()


def default_dsn() -> str:
    host = os.environ.get("RECON_DB_HOST", "localhost:5442")
    return os.environ.get("SIM_DATABASE_URL", f"postgresql+asyncpg://sim_app:sim_app@{host}/provider_sim")


if __name__ == "__main__":
    asyncio.run(ensure_schema(sys.argv[1] if len(sys.argv) > 1 else default_dsn()))
