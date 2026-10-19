import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import TENANTS, ensure_tenant
from recon_fixtures.retrieval import QUERIES, measure, table
from recon_retrieval.embed import OllamaEmbedder
from recon_retrieval.index import index_chunks
from tests.retrieval.fakes import HashEmbedder

OUT = Path(__file__).resolve().parents[2] / "results" / "retrieval"


@pytest.fixture
async def corpus(app_sessions, flaky_sim, op):
    ids = seeded_ids("recall")
    async with app_sessions() as s, s.begin():
        for key in TENANTS:
            await ensure_tenant(s, flaky_sim, op, key, ids)
    return [f"t_{key}" for key in TENANTS]


async def test_the_harness_scores_every_query_in_every_mode(app_sessions, corpus):
    e = HashEmbedder()
    await index_chunks(app_sessions, e)
    rows = await measure(app_sessions, corpus, e)
    print("\n" + table(rows))
    assert {(r.mode, r.style) for r in rows} == {
        (m, st) for m in ("lexical", "vector", "hybrid") for st in ("agent", "customer", "all")
    }
    assert all(r.n == len(corpus) * sum(1 for q in QUERIES if st in (q[2], "all")) for r in rows for st in [r.style])
    assert all(r.fallbacks == 0 for r in rows)


@pytest.mark.local
async def test_recall_with_a_real_embedding_model(app_sessions, corpus):
    if os.environ.get("RECON_LOCAL") != "1":
        pytest.skip("local: needs RECON_LOCAL=1 and an ollama server")
    e = OllamaEmbedder(model=os.environ.get("RECON_EMBED_MODEL", "nomic-embed-text"))
    try:
        await index_chunks(app_sessions, e)
        rows = await measure(app_sessions, corpus, e)
    finally:
        await e.aclose()
    print("\n" + table(rows))
    assert all(r.fallbacks == 0 for r in rows)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"recall-{e.model}.json").write_text(
        json.dumps(
            {"model": e.model, "queries": len(QUERIES), "tenants": corpus, "rows": [asdict(r) for r in rows]}, indent=2
        )
        + "\n"
    )
