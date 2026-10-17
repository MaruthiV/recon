import asyncio

import pytest

KEY = "sk_test_acme"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
async def pi(sim, op):
    await sim.post("/sim/accounts", json={"id": "acct_acme", "api_key": KEY}, headers=op)
    await sim.post("/sim/accounts", json={"id": "acct_other", "api_key": "sk_test_other"}, headers=op)
    await sim.post("/sim/clock", json={"freeze_at": "2026-10-01T12:00:00+00:00"}, headers=op)
    r = await sim.post("/sim/payment_intents", json={"account": "acct_acme", "amount": 4900}, headers=op)
    return r.json()["id"]


async def refund(sim, pi, key=None, **params):
    headers = {**AUTH, **({"Idempotency-Key": key} if key else {})}
    return await sim.post("/v1/refunds", data={"payment_intent": pi, **params}, headers=headers)


async def refunds_on(sim, op, pi):
    state = (await sim.get("/sim/state", headers=op)).json()
    return [r for r in state["refunds"] if r["payment_intent"] == pi]


async def test_sim_idempotency_semantics(sim, op, pi):
    first = await refund(sim, pi, "k1", amount="1000", **{"metadata[recon_command_id]": "cmd_1"})
    assert first.status_code == 200 and "Idempotent-Replayed" not in first.headers
    again = await refund(sim, pi, "k1", amount="1000", **{"metadata[recon_command_id]": "cmd_1"})
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    assert len(await refunds_on(sim, op, pi)) == 1

    mismatch = await refund(sim, pi, "k1", amount="999", **{"metadata[recon_command_id]": "cmd_1"})
    assert mismatch.status_code == 400 and mismatch.json()["error"]["type"] == "idempotency_error"
    assert len(await refunds_on(sim, op, pi)) == 1


async def test_concurrent_same_key_gets_409_then_replay(sim, op, pi):
    await sim.post(
        "/sim/faults",
        json={"account": "acct_acme", "kind": "slow", "idempotency_key": "k2", "params": {"seconds": 0.5}},
        headers=op,
    )
    slow = asyncio.create_task(refund(sim, pi, "k2", amount="500"))
    await asyncio.sleep(0.15)
    clash = await refund(sim, pi, "k2", amount="500")
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "idempotency_key_in_use"
    assert clash.headers["Stripe-Should-Retry"] == "true"
    done = await slow
    assert done.status_code == 200
    replay = await refund(sim, pi, "k2", amount="500")
    assert replay.headers.get("Idempotent-Replayed") == "true" and replay.json()["id"] == done.json()["id"]
    assert len(await refunds_on(sim, op, pi)) == 1


async def test_key_reuse_after_retention_creates_a_new_refund(sim, op, pi):
    a = await refund(sim, pi, "k3", amount="1000")
    await sim.post("/sim/clock", json={"advance_seconds": 86_400 + 1}, headers=op)
    b = await refund(sim, pi, "k3", amount="1000")
    # exactly why recon never resubmits outside the window
    assert b.status_code == 200 and "Idempotent-Replayed" not in b.headers
    assert b.json()["id"] != a.json()["id"]
    assert len(await refunds_on(sim, op, pi)) == 2


async def test_cached_500_replays_but_refund_exists(sim, op, pi):
    await sim.post(
        "/sim/faults", json={"account": "acct_acme", "kind": "commit_then_500", "idempotency_key": "k4"}, headers=op
    )
    meta = {"metadata[recon_command_id]": "cmd_4"}
    r = await refund(sim, pi, "k4", **meta)
    assert r.status_code == 500
    replay = await refund(sim, pi, "k4", **meta)
    assert replay.status_code == 500 and replay.headers["Idempotent-Replayed"] == "true"
    listed = (await sim.get("/v1/refunds", params={"payment_intent": pi}, headers=AUTH)).json()["data"]
    assert [x["metadata"] for x in listed] == [{"recon_command_id": "cmd_4"}]


async def test_fail_500_caches_failure_without_effect(sim, op, pi):
    await sim.post(
        "/sim/faults", json={"account": "acct_acme", "kind": "fail_500", "idempotency_key": "k5"}, headers=op
    )
    assert (await refund(sim, pi, "k5")).status_code == 500
    assert (await refund(sim, pi, "k5")).headers["Idempotent-Replayed"] == "true"
    assert await refunds_on(sim, op, pi) == []


async def test_validation_errors_are_not_cached(sim, op, pi):
    bad = await sim.post("/v1/refunds", data={"amount": "100"}, headers={**AUTH, "Idempotency-Key": "k6"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "parameter_missing"
    ok = await refund(sim, pi, "k6", amount="100")
    assert ok.status_code == 200 and "Idempotent-Replayed" not in ok.headers


async def test_over_refund_is_cached_400(sim, op, pi):
    r = await refund(sim, pi, "k7", amount="5000")
    assert r.status_code == 400 and r.json()["error"]["code"] == "amount_too_large"
    replay = await refund(sim, pi, "k7", amount="5000")
    assert replay.status_code == 400 and replay.headers["Idempotent-Replayed"] == "true"
    full = await refund(sim, pi, "k8")
    assert full.json()["amount"] == 4900
    assert (await refund(sim, pi, "k9")).json()["error"]["code"] == "charge_already_refunded"


async def test_accounts_are_isolated(sim, op, pi):
    rid = (await refund(sim, pi, "k10", amount="100")).json()["id"]
    other = {"Authorization": "Bearer sk_test_other"}
    assert (await sim.get(f"/v1/refunds/{rid}", headers=other)).status_code == 404
    assert (await sim.post("/v1/refunds", data={"payment_intent": pi}, headers=other)).status_code == 404
    assert (await sim.get(f"/v1/refunds/{rid}", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await sim.get("/sim/state")).status_code == 403


async def test_list_paginates_newest_first(sim, op, pi):
    ids = [(await refund(sim, pi, f"p{i}", amount="100")).json()["id"] for i in range(3)]
    page = (await sim.get("/v1/refunds", params={"payment_intent": pi, "limit": 2}, headers=AUTH)).json()
    assert page["has_more"] and [r["id"] for r in page["data"]] == ids[::-1][:2]
    rest = (
        await sim.get("/v1/refunds", params={"payment_intent": pi, "limit": 2, "starting_after": ids[1]}, headers=AUTH)
    ).json()
    assert not rest["has_more"] and [r["id"] for r in rest["data"]] == [ids[0]]


async def test_throttle_is_429_and_not_cached(sim, op, pi):
    await sim.post(
        "/sim/faults", json={"account": "acct_acme", "kind": "throttle", "idempotency_key": "k11"}, headers=op
    )
    r = await refund(sim, pi, "k11", amount="100")
    assert r.status_code == 429 and r.headers["Stripe-Should-Retry"] == "true"
    assert (await refund(sim, pi, "k11", amount="100")).status_code == 200
