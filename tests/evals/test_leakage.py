import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import FAMILIES, make_specs, materialize
from recon_storage.db import make_engine, url
from tests.conftest import NAMES, T

# words that only the grader's labels should know. "proration" and "ineligible" are also plain domain words
# (a policy section id, a reason code), so they're not label-only
LABEL_ONLY = {
    *(f for f in FAMILIES if f not in ("proration", "ineligible")),
    "never_issued",
    "not_received",
    "two_lines",
    "after_period",
    "not_allowed",
    "returns_off",
    "wrong_customer",
    "no_order",
    "over_limit",
    "expects_information",
    "requires_approval",
    "also_accept",
    "preexisting_refunds",
}


async def test_the_agent_role_cant_reach_the_labels_database(dbs):
    e = make_engine(url("recon_mcp_read", NAMES.eval))
    with pytest.raises((DBAPIError, OSError, Exception)):
        async with e.connect() as c:
            await c.execute(text("SELECT count(*) FROM labels"))
    await e.dispose()


async def test_nothing_the_agent_can_read_carries_a_label(app_sessions, flaky_sim, op, verifier_engines):
    ids = seeded_ids("leakage")
    for spec in make_specs("dev", per_family=5, seed=1):
        await materialize(spec, app_sessions, flaky_sim, op, verifier_engines["eval"], T, "dev", ids)
    e = make_engine(url("recon_mcp_read", NAMES.recon))
    found = []
    async with e.connect() as c:
        tables = (
            (
                await c.execute(
                    text(
                        "SELECT table_name FROM information_schema.role_table_grants "
                        "WHERE grantee = 'recon_mcp_read' AND privilege_type = 'SELECT' AND table_schema = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {"cases", "payments", "policy_chunks", "returns", "subscriptions"} <= set(tables)
        for t in tables:
            rows = (await c.execute(text(f'SELECT t::text FROM "{t}" t'))).scalars().all()
            for row in rows:
                hit = [w for w in LABEL_ONLY if w in row.lower()]
                if hit:
                    found.append((t, hit, row[:120]))
    await e.dispose()
    assert not found, found[:5]
