from datetime import timedelta

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY, ROLE_TOOLS
from recon_domain.tokens import MCP_READ, RUN_TOKEN_HEADER, mint
from recon_fixtures.dup_charge import seed_duplicate_charge
from recon_mcp.server import TOOLS
from tests.conftest import MCP_URL, RUN_KEY, T


def token(sd, role=INVESTIGATOR, **kw):
    claims = {
        "aud": MCP_READ,
        "tenant": sd.tenant_id,
        "case_id": sd.case_id,
        "run_id": "run_test",
        "role": role,
        "tools": list(ROLE_TOOLS[role]),
        "exp": int((T + timedelta(minutes=5)).timestamp()),
        **kw,
    }
    return mint(claims, RUN_KEY)


def client(service, tok):
    headers = {RUN_TOKEN_HEADER: tok} if tok else {}
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=service), headers=headers, base_url="http://localhost:8200"
    )


async def call(service, tok, name, **args):
    async with client(service, tok) as http, Client(streamable_http_client(MCP_URL, http_client=http)) as c:
        r = await c.call_tool(name, args)
    assert not r.is_error, r.content
    return r.structured_content


async def tool_list(service, tok):
    async with client(service, tok) as http, Client(streamable_http_client(MCP_URL, http_client=http)) as c:
        return (await c.list_tools()).tools


@pytest.fixture
async def two_tenants(app_sessions, flaky_sim, op, verifier_engines, mcp_read):
    acme = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, tenant="acme")
    globex = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, tenant="globex")
    mcp_read.read_keys.update({acme.tenant_id: acme.read_key, globex.tenant_id: globex.read_key})
    return acme, globex, mcp_read.service


async def test_tool_cannot_cross_tenant(two_tenants, flaky_sim):
    acme, globex, svc = two_tenants
    r = await flaky_sim.post(
        "/v1/refunds",
        data={"payment_intent": globex.duplicate_provider_id, "amount": "100"},
        headers={"Authorization": f"Bearer {globex.api_key}", "Idempotency-Key": "k_globex"},
    )
    globex_refund = r.json()["id"]

    tok = token(acme)
    # acme's run asks for globex rows by guessing ids, every answer looks like "doesn't exist"
    for name, args in [
        ("get_payment", {"payment_id": globex.duplicate_payment_id}),
        ("list_refunds", {"payment_id": globex.duplicate_payment_id}),
        ("get_order", {"order_id": globex.order_id}),
        ("list_payments", {"order_id": globex.order_id}),
        ("get_refund", {"refund_id": globex_refund}),
    ]:
        out = await call(svc, tok, name, **args)
        assert out["status"] == "none_found", (name, out)
        assert out["data"] is None

    q = await call(
        svc,
        token(acme, COORDINATOR),
        "compute_refund_quote",
        payment_id=globex.duplicate_payment_id,
        reason_code="DUPLICATE_CAPTURE",
    )
    assert q["data"]["eligible"] is False

    found = await call(svc, token(acme, POLICY), "search_policy", query="duplicate charge refund")
    assert found["status"] == "found"
    assert {h["policy_id"] for h in found["data"]["hits"]} <= {acme.terms_policy_id, acme.limits_policy_id}
    sect = await call(
        svc, token(acme, POLICY), "get_policy_section", policy_id=globex.terms_policy_id, section_id="dup"
    )
    assert sect["status"] == "none_found"

    # the case comes from the signed token, no argument can point it elsewhere
    case = await call(svc, tok, "get_case")
    assert case["data"]["case_id"] == acme.case_id


async def test_no_tool_takes_tenant_or_case_arguments(two_tenants):
    acme, _, svc = two_tenants
    tools = await tool_list(svc, token(acme))
    assert sorted(t.name for t in tools) == sorted(TOOLS)
    for t in tools:
        props = set((t.input_schema or {}).get("properties", {}))
        assert not props & {"tenant", "tenant_id", "case_id", "customer_id", "account"}, (t.name, props)
        assert t.annotations is not None and t.annotations.read_only_hint is True


@pytest.mark.parametrize("bad", ["missing", "wrong_key", "wrong_audience", "expired"])
async def test_bad_run_token_is_rejected_before_any_tool(two_tenants, bad):
    acme, _, svc = two_tenants
    tok = {
        "missing": None,
        "wrong_key": mint({"aud": MCP_READ, "tenant": acme.tenant_id, "exp": 2**40}, b"nope"),
        "wrong_audience": token(acme, aud="mcp-exec"),
        "expired": token(acme, exp=int(T.timestamp())),
    }[bad]
    async with client(svc, tok) as http:
        r = await http.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


async def test_role_only_gets_its_tools(two_tenants):
    acme, _, svc = two_tenants
    out = await call(svc, token(acme, POLICY), "get_payment", payment_id=acme.duplicate_payment_id)
    assert out["status"] == "query_failed"
    assert out["error"]["code"] == "not_allowed" and out["error"]["retryable"] is False


async def test_list_refunds_separates_none_from_failed(two_tenants, flaky_sim, op):
    acme, _, svc = two_tenants
    tok = token(acme)
    none = await call(svc, tok, "list_refunds", payment_id=acme.duplicate_payment_id)
    assert none["status"] == "none_found" and none["error"]["retryable"] is False

    await flaky_sim.post(
        "/sim/faults", json={"account": acme.account, "kind": "throttle_reads", "count": 1}, headers=op
    )
    failed = await call(svc, tok, "list_refunds", payment_id=acme.duplicate_payment_id)
    assert failed["status"] == "query_failed"
    assert failed["error"]["retryable"] is True
    assert failed["source_ids"] == [f"payment:{acme.duplicate_payment_id}"]

    await flaky_sim.post(
        "/v1/refunds",
        data={"payment_intent": acme.duplicate_provider_id, "amount": "4900", "metadata[recon_command_id]": "cmd_x"},
        headers={"Authorization": f"Bearer {acme.api_key}", "Idempotency-Key": "k_acme"},
    )
    found = await call(svc, tok, "list_refunds", payment_id=acme.duplicate_payment_id)
    assert found["status"] == "found"
    [refund] = found["data"]["refunds"]
    assert (refund["amount_minor"], refund["recon_command_id"]) == (4900, "cmd_x")


async def test_envelope_carries_source_version_time_and_correlation(two_tenants):
    acme, _, svc = two_tenants
    out = await call(svc, token(acme), "get_payment", payment_id=acme.duplicate_payment_id)
    assert out["status"] == "found"
    assert out["source_ids"] == [f"payment:{acme.duplicate_payment_id}:v1"]
    assert out["source_version"] == "v1"
    assert out["observed_at"].startswith("2026-10-15T12:00:00")
    assert out["correlation_id"].startswith("req_")
    assert out["data"]["ref"] == f"payment:{acme.duplicate_payment_id}:v1"


async def test_quote_comes_from_the_domain(two_tenants):
    acme, _, svc = two_tenants
    q = await call(
        svc,
        token(acme, COORDINATOR),
        "compute_refund_quote",
        payment_id=acme.duplicate_payment_id,
        reason_code="DUPLICATE_CAPTURE",
    )
    assert q["data"]["eligible"] is True
    assert (q["data"]["amount_minor"], q["data"]["currency"]) == (4900, "usd")
    assert q["data"]["terms_policy_id"] == acme.terms_policy_id
    original = await call(
        svc,
        token(acme, COORDINATOR),
        "compute_refund_quote",
        payment_id=acme.original_payment_id,
        reason_code="DUPLICATE_CAPTURE",
    )
    assert original["data"]["eligible"] is False


async def test_read_key_cannot_move_money(two_tenants, flaky_sim, op):
    acme, _, _ = two_tenants
    r = await flaky_sim.post(
        "/v1/refunds",
        data={"payment_intent": acme.duplicate_provider_id, "amount": "4900"},
        headers={"Authorization": f"Bearer {acme.read_key}", "Idempotency-Key": "k_rk"},
    )
    assert r.status_code == 403
    state = (await flaky_sim.get("/sim/state", headers=op)).json()
    assert state["refunds"] == []
