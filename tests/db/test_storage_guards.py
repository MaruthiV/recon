from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from recon_domain.connector import FakeClock
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_storage.db import make_engine, url
from recon_worker.connector import HttpConnector
from recon_worker.executor import Executor
from tests.conftest import NAMES
from tests.e2e.test_scripted_dup_charge import scripted_plan

T = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def refunded(app_sessions, sim, op, verifier_engines, app_engine):
    sd = await seed_duplicate_charge(app_sessions, sim, op, verifier_engines["eval"], T)
    ex = Executor(app_sessions, FakeClock(T), lambda _t: HttpConnector(sim, sd.api_key))
    out = await ex.run(sd.tenant_id, sd.case_id, scripted_plan(sd))
    return sd, out.command_id


async def exec_as(engine, sql, **params):
    async with engine.begin() as c:
        return await c.execute(text(sql), params)


async def test_command_payload_is_immutable(refunded, app_engine):
    _, cmd = refunded
    with pytest.raises(DBAPIError, match="immutable"):
        await exec_as(app_engine, "UPDATE commands SET amount_minor = 1 WHERE id = :id", id=cmd)
    with pytest.raises(DBAPIError, match="already bound"):
        await exec_as(app_engine, "UPDATE commands SET provider_refund_id = 're_other' WHERE id = :id", id=cmd)
    await exec_as(app_engine, "UPDATE commands SET updated_at = now() WHERE id = :id", id=cmd)


async def test_audit_is_append_only(refunded, app_engine):
    with pytest.raises(DBAPIError):
        await exec_as(app_engine, "UPDATE audit_events SET kind = 'x'")
    with pytest.raises(DBAPIError):
        await exec_as(app_engine, "DELETE FROM audit_events")


async def test_app_cannot_delete_money_rows(refunded, app_engine):
    for table in ("commands", "payment_reservations", "payments", "refund_claims"):
        with pytest.raises(DBAPIError, match="permission denied"):
            await exec_as(app_engine, f"DELETE FROM {table}")


async def test_one_live_command_per_entitlement(refunded, app_engine):
    _, cmd = refunded
    with pytest.raises(IntegrityError):
        await exec_as(
            app_engine,
            "INSERT INTO commands (id, tenant_id, case_id, plan_id, claim_id, entitlement_ref, op_seq, payment_id, "
            "provider_payment_id, amount_minor, currency, payload_hash, idempotency_key, state, created_at, updated_at) "
            "SELECT 'cmd_x', tenant_id, case_id, plan_id, claim_id, entitlement_ref, 2, payment_id, provider_payment_id, "
            "amount_minor, currency, 'h', 'k_x', 'PREPARED', now(), now() FROM commands WHERE id = :id",
            id=cmd,
        )


async def test_payment_cannot_be_overdrawn(refunded, app_engine):
    sd, _ = refunded
    with pytest.raises(IntegrityError, match="not_overdrawn"):
        await exec_as(app_engine, "UPDATE payments SET reserved_minor = 1 WHERE id = :id", id=sd.duplicate_payment_id)


async def test_mcp_read_role_sees_facts_not_ledger(refunded, dbs):
    engine = make_engine(url("recon_mcp_read", NAMES.recon))
    try:
        await exec_as(engine, "SELECT count(*) FROM payments")
        await exec_as(engine, "SELECT count(*) FROM policy_documents")
        await exec_as(engine, "SELECT count(*) FROM policy_chunks")
        ledger = ("commands", "payment_reservations", "command_attempts", "audit_events", "plans", "tenants")
        for table in (*ledger, "runs", "model_usage"):
            with pytest.raises(DBAPIError, match="permission denied"):
                await exec_as(engine, f"SELECT 1 FROM {table}")
        with pytest.raises(DBAPIError, match="permission denied"):
            await exec_as(engine, "UPDATE payments SET status = 'x'")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("role", ["recon_app", "recon_mcp_read"])
@pytest.mark.parametrize("db", [NAMES.sim, NAMES.eval])
async def test_agent_reachable_roles_cannot_reach_ground_truth(dbs, role, db):
    engine = make_engine(url(role, db))
    try:
        # refused at connect time, surfaces as the raw asyncpg error
        with pytest.raises(Exception, match="CONNECT privilege"):
            await exec_as(engine, "SELECT 1")
    finally:
        await engine.dispose()


async def test_model_usage_is_append_only_for_the_app(app_sessions):
    async with app_sessions() as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("DELETE FROM model_usage"))
    async with app_sessions() as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("UPDATE model_usage SET est_cost_micros = 0"))
