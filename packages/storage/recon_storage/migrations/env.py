import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from recon_storage.models import Base

config = context.config


def include_name(name, type_, parent_names):
    # adk owns its own schema, never touch it
    if type_ == "schema":
        return name in (None, "public")
    return True


def run(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def main():
    engine = create_async_engine(config.get_main_option("sqlalchemy.url") or "")
    async with engine.connect() as conn:
        await conn.run_sync(run)
    await engine.dispose()


asyncio.run(main())
