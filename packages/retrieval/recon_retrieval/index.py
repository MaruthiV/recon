from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from recon_retrieval.embed import Embedder, doc_text, embedder_from_env, literal

BATCH = 32


async def index_chunks(sessions: async_sessionmaker, embedder: Embedder) -> int:
    # fill in every chunk that has no vector from this model yet, safe to re-run
    async with sessions() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, heading, text FROM policy_chunks "
                    "WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM :m ORDER BY id"
                ),
                {"m": embedder.model},
            )
        ).all()
    for i in range(0, len(rows), BATCH):
        part = rows[i : i + BATCH]
        vecs = await embedder.embed([doc_text(r.heading, r.text) for r in part], "document")
        async with sessions() as s, s.begin():
            await s.execute(
                text("UPDATE policy_chunks SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
                [{"id": r.id, "v": literal(v), "m": embedder.model} for r, v in zip(part, vecs, strict=True)],
            )
    return len(rows)


async def index_from_env(sessions: async_sessionmaker) -> int | None:
    # a no-op unless RECON_EMBED_MODEL names a local model
    e = embedder_from_env()
    if e is None:
        return None
    try:
        return await index_chunks(sessions, e)
    finally:
        await e.aclose()
