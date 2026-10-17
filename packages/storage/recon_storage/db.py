from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

HOST = os.environ.get("RECON_DB_HOST", "localhost:5442")


@dataclass(frozen=True)
class DbNames:
    recon: str = "recon"
    sim: str = "provider_sim"
    eval: str = "recon_eval"

    @classmethod
    def for_env(cls, env: str) -> DbNames:
        return cls() if env == "dev" else cls(f"recon_{env}", f"provider_sim_{env}", f"recon_eval_{env}")


def url(role: str, db: str, password: str | None = None) -> str:
    # dev passwords match role names, see infra/db/init.sql
    return f"postgresql+asyncpg://{role}:{password or role}@{HOST}/{db}"


def admin_url(db: str = "postgres") -> str:
    return url("postgres", db, os.environ.get("RECON_DB_ADMIN_PASSWORD", "postgres"))


def make_engine(dsn: str, **kw) -> AsyncEngine:
    return create_async_engine(dsn, pool_pre_ping=True, **kw)


def sessions(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"
