from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import Column, DateTime, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

metadata = MetaData()

labels = Table(
    "labels",
    metadata,
    Column("case_id", String, primary_key=True),
    Column("tenant_id", String, nullable=False),
    Column("split", String, nullable=False),
    Column("family", String, nullable=False),
    Column("label", JSONB, nullable=False),
)

verdicts = Table(
    "verdicts",
    metadata,
    Column("id", String, primary_key=True),
    Column("case_id", String, nullable=False),
    Column("passed", String, nullable=False),
    Column("checks", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


async def ensure_schema(dsn: str) -> None:
    engine = create_async_engine(dsn)
    async with engine.begin() as c:
        await c.run_sync(metadata.create_all)
    await engine.dispose()


def default_dsn() -> str:
    host = os.environ.get("RECON_DB_HOST", "localhost:5442")
    return os.environ.get("RECON_EVAL_DATABASE_URL", f"postgresql+asyncpg://recon_eval:recon_eval@{host}/recon_eval")


if __name__ == "__main__":
    asyncio.run(ensure_schema(sys.argv[1] if len(sys.argv) > 1 else default_dsn()))
