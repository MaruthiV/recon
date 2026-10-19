from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from recon_retrieval.embed import Embedder, EmbedError, literal

log = logging.getLogger(__name__)
Mode = Literal["lexical", "vector", "hybrid"]
RRF_K = 60
DEPTH = 20  # candidates per arm before fusing

LEXICAL = text(
    # any-term match, ranked, so long natural queries still hit
    "WITH x AS (SELECT replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery AS q) "
    "SELECT policy_document_id, section_id, heading, text FROM policy_chunks, x "
    "WHERE tenant_id = :t AND policy_document_id = ANY(:docs) AND tsv @@ x.q "
    "ORDER BY ts_rank(tsv, x.q) DESC, section_id LIMIT :k"
)
VECTOR = text(
    # exact scan on purpose, a tenant's corpus is small and ann + filters can under-return
    "SELECT policy_document_id, section_id, heading, text FROM policy_chunks "
    "WHERE tenant_id = :t AND policy_document_id = ANY(:docs) AND embedding_model = :m "
    "ORDER BY embedding <=> CAST(:v AS vector), section_id LIMIT :k"
)


@dataclass(frozen=True)
class Hit:
    policy_document_id: str
    section_id: str
    heading: str
    text: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.policy_document_id, self.section_id)


@dataclass(frozen=True)
class Found:
    hits: list[Hit]
    # what actually ran, a hybrid ask can fall back to lexical when the embedder is down
    mode: str


def rrf(rankings: Sequence[Sequence[tuple[str, str]]], k: int = RRF_K) -> list[tuple[str, str]]:
    score: dict[tuple[str, str], Fraction] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            score[key] = score.get(key, Fraction(0)) + Fraction(1, k + rank)
    return sorted(score, key=lambda key: (-score[key], key))


async def search(
    s: AsyncSession,
    tenant_id: str,
    doc_ids: Sequence[str],
    query: str,
    *,
    embedder: Embedder | None = None,
    mode: Mode = "hybrid",
    k: int = 5,
) -> Found:
    # doc_ids is the applicability filter, both arms only ever see those documents
    scope = {"t": tenant_id, "docs": list(doc_ids), "k": DEPTH}
    lexical: list[Hit] = []
    if mode != "vector":
        lexical = [Hit(*r) for r in (await s.execute(LEXICAL, {**scope, "q": query})).all()]
    if mode == "lexical" or embedder is None:
        return Found(lexical[:k], "lexical")
    try:
        [v] = await embedder.embed([query], "query")
    except EmbedError as e:
        log.warning("policy search fell back to lexical: %s", e)
        return Found(lexical[:k], "lexical_fallback")
    vector = [Hit(*r) for r in (await s.execute(VECTOR, {**scope, "m": embedder.model, "v": literal(v)})).all()]
    if mode == "vector":
        return Found(vector[:k], "vector")
    by_key = {h.key: h for h in lexical + vector}
    fused = rrf([[h.key for h in lexical], [h.key for h in vector]])
    return Found([by_key[key] for key in fused[:k]], "hybrid")
