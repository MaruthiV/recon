import pytest

from recon_agents.roles import POLICY
from recon_domain.connector import RefundReader
from recon_fixtures.dup_charge import seed_duplicate_charge, seeded_ids
from recon_fixtures.families import ensure_tenant
from recon_fixtures.retrieval import current_docs
from recon_mcp.server import create_app as create_mcp_app
from recon_retrieval.index import index_chunks
from recon_retrieval.search import rrf, search
from recon_storage.db import url
from tests.conftest import NAMES, RUN_KEY, T, serving
from tests.mcp.test_read_tools import call, token
from tests.retrieval.fakes import HashEmbedder


def no_reader(_tenant: str) -> RefundReader:
    raise AssertionError("policy search never reads the provider")


def test_rrf_puts_agreement_first():
    a, b, c, d = ("p", "a"), ("p", "b"), ("p", "c"), ("p", "d")
    assert rrf([[a, b, c], [c, a, d]]) == [a, c, b, d]


def test_rrf_breaks_ties_by_key():
    assert rrf([[("p", "y")], [("p", "x")]]) == [("p", "x"), ("p", "y")]


@pytest.fixture
async def tenants(app_sessions, flaky_sim, op):
    ids = seeded_ids("retrieval")
    async with app_sessions() as s, s.begin():
        for key in ("acme", "globex"):
            await ensure_tenant(s, flaky_sim, op, key, ids)
    return {key: await current_docs(app_sessions, f"t_{key}") for key in ("acme", "globex")}


async def test_indexing_is_idempotent_and_follows_the_model(app_sessions, tenants):
    first = await index_chunks(app_sessions, HashEmbedder())
    assert first > 0
    assert await index_chunks(app_sessions, HashEmbedder()) == 0
    # a new model means every vector is stale
    assert await index_chunks(app_sessions, HashEmbedder("hash-v2")) == first


@pytest.mark.parametrize("mode", ["lexical", "vector", "hybrid"])
async def test_every_arm_stays_inside_the_applicable_docs(app_sessions, tenants, mode):
    e = HashEmbedder()
    await index_chunks(app_sessions, e)
    acme = tenants["acme"]
    async with app_sessions() as s:
        found = await search(s, "t_acme", acme, "duplicate charge refund", embedder=e, mode=mode, k=20)
        # a tenant id with someone else's documents finds nothing
        stolen = await search(s, "t_acme", tenants["globex"], "duplicate charge refund", embedder=e, mode=mode)
    assert found.hits and {h.policy_document_id for h in found.hits} <= set(acme)
    assert stolen.hits == []


async def test_hybrid_fuses_both_arms(app_sessions, tenants):
    e = HashEmbedder()
    await index_chunks(app_sessions, e)
    async with app_sessions() as s:
        lex = await search(s, "t_acme", tenants["acme"], "zzz qqq", embedder=e, mode="lexical")
        hyb = await search(s, "t_acme", tenants["acme"], "zzz qqq", embedder=e, mode="hybrid")
        arms = [
            await search(s, "t_acme", tenants["acme"], "duplicate charge refund", embedder=e, mode=m, k=20)
            for m in ("lexical", "vector")
        ]
        dup = await search(s, "t_acme", tenants["acme"], "duplicate charge refund", embedder=e, mode="hybrid")
    # no words match, the vector arm still ranks something
    assert lex.hits == [] and len(hyb.hits) == 5 and hyb.mode == "hybrid"
    assert [h.key for h in dup.hits] == rrf([[h.key for h in a.hits] for a in arms])[:5]


async def test_a_down_embedder_falls_back_to_lexical_and_says_so(app_sessions, tenants):
    e = HashEmbedder()
    await index_chunks(app_sessions, e)
    e.down = True
    async with app_sessions() as s:
        lex = await search(s, "t_acme", tenants["acme"], "refund window", mode="lexical")
        got = await search(s, "t_acme", tenants["acme"], "refund window", embedder=e)
    assert got.mode == "lexical_fallback" and got.hits == lex.hits


async def test_unindexed_chunks_leave_hybrid_as_lexical(app_sessions, tenants):
    async with app_sessions() as s:
        lex = await search(s, "t_acme", tenants["acme"], "refund window", mode="lexical")
        got = await search(s, "t_acme", tenants["acme"], "refund window", embedder=HashEmbedder())
    assert got.hits == lex.hits


async def test_mcp_search_policy_reports_hybrid(app_sessions, flaky_sim, op, verifier_engines, clock):
    sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T)
    e = HashEmbedder()
    await index_chunks(app_sessions, e)
    svc = create_mcp_app(url("recon_mcp_read", NAMES.recon), RUN_KEY, no_reader, clock, host="localhost", embedder=e)
    async with serving(svc):
        found = await call(svc, token(sd, POLICY), "search_policy", query="duplicate charge refund")
    await svc.engine.dispose()
    assert found["status"] == "found" and found["data"]["retrieval"] == "hybrid"
    assert e.calls == 2  # one index batch, one query
