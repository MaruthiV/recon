from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from .db import DbNames, admin_url, make_engine, url
from .migrate import upgrade

ROLES = ("recon_owner", "recon_app", "recon_mcp_read", "sim_app", "recon_eval")


async def _run(dsn: str, statements: list[str]) -> None:
    engine = make_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as c:
            for s in statements:
                await c.execute(text(s))
    finally:
        await engine.dispose()


async def ensure_roles() -> None:
    engine = make_engine(admin_url(), isolation_level="AUTOCOMMIT")
    async with engine.connect() as c:
        have = set((await c.execute(text("SELECT rolname FROM pg_roles"))).scalars())
        for r in ROLES:
            if r not in have:
                await c.execute(text(f"CREATE ROLE {r} LOGIN PASSWORD '{r}'"))
    await engine.dispose()


async def ensure_databases(n: DbNames, fresh: bool = False) -> None:
    engine = make_engine(admin_url(), isolation_level="AUTOCOMMIT")
    async with engine.connect() as c:
        for db, owner in ((n.recon, "recon_owner"), (n.sim, "sim_app"), (n.eval, "recon_eval")):
            if fresh:
                await c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
            exists = (await c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db})).scalar()
            if not exists:
                await c.execute(text(f'CREATE DATABASE "{db}" OWNER {owner}'))
        await c.execute(text(f'REVOKE ALL ON DATABASE "{n.recon}", "{n.sim}", "{n.eval}" FROM PUBLIC'))
        await c.execute(text(f'GRANT CONNECT ON DATABASE "{n.recon}" TO recon_app, recon_mcp_read, recon_eval'))
        await c.execute(text(f'GRANT CONNECT ON DATABASE "{n.sim}" TO recon_eval'))
    await engine.dispose()

    await _run(
        admin_url(n.recon),
        [
            "CREATE EXTENSION IF NOT EXISTS vector",
            "GRANT USAGE ON SCHEMA public TO recon_app, recon_mcp_read, recon_eval",
            "ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO recon_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO recon_app",
            "ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public GRANT SELECT ON TABLES TO recon_eval",
            # mcp_read gets explicit per-table grants in migrations, deny by default
            "ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public REVOKE SELECT ON TABLES FROM recon_mcp_read",
            "CREATE SCHEMA IF NOT EXISTS adk AUTHORIZATION recon_app",
        ],
    )
    await _run(
        admin_url(n.sim),
        [
            "GRANT USAGE ON SCHEMA public TO recon_eval",
            "ALTER DEFAULT PRIVILEGES FOR ROLE sim_app IN SCHEMA public GRANT SELECT ON TABLES TO recon_eval",
        ],
    )


async def bootstrap(env: str, fresh: bool = False) -> DbNames:
    n = DbNames.for_env(env)
    await ensure_roles()
    await ensure_databases(n, fresh=fresh)
    await asyncio.to_thread(upgrade, url("recon_owner", n.recon))
    return n


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--env", default="dev")
    p.add_argument("--fresh", action="store_true")
    a = p.parse_args()
    print(asyncio.run(bootstrap(a.env, a.fresh)))
