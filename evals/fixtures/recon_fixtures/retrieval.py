from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from recon_retrieval.embed import Embedder
from recon_retrieval.search import Mode, search
from recon_storage.models import PolicyDocument

# frozen 2026-10-18 before the first measurement, don't tune these against results
# (query, section it should find, style). "agent" is how the policy specialist searches, "customer" is plain words
QUERIES: tuple[tuple[str, str, str], ...] = (
    ("duplicate charge refund", "dup", "agent"),
    ("captured twice same order", "dup", "agent"),
    ("I was billed two times for one purchase", "dup", "customer"),
    ("my card got hit twice", "dup", "customer"),
    ("refund window days", "window", "agent"),
    ("refund time limit", "window", "agent"),
    ("how long after buying can I still get my money back", "window", "customer"),
    ("is it too late to ask for a refund", "window", "customer"),
    ("returned item refund", "returns", "agent"),
    ("return received refund item price", "returns", "agent"),
    ("I mailed the shirt back to you", "returns", "customer"),
    ("sent one thing back from my order", "returns", "customer"),
    ("shipping refund", "shipping", "agent"),
    ("delivery fee refundable", "shipping", "agent"),
    ("will you pay back what I paid for postage", "shipping", "customer"),
    ("subscription cancellation proration", "proration", "agent"),
    ("cancelled subscription unused days refund", "proration", "agent"),
    ("I stopped my membership halfway through the month", "proration", "customer"),
    ("quit my plan early, want the rest back", "proration", "customer"),
    ("refund approval limit", "auto_approve", "agent"),
    ("supervisor approval large refund", "auto_approve", "agent"),
    ("who has to sign off on a big payback", "auto_approve", "customer"),
    ("refund to original payment method", "method", "agent"),
    ("when will the money show up on my statement", "method", "customer"),
    ("exchange for different size", "exchanges", "agent"),
    ("can I swap it for a bigger one", "exchanges", "customer"),
    ("gift card refund", "gift_cards", "agent"),
    ("chargeback dispute", "disputes", "agent"),
    ("I told my bank the payment was wrong", "disputes", "customer"),
    ("item arrived broken", "damaged", "customer"),
)
KS = (1, 3, 5)


@dataclass(frozen=True)
class Row:
    mode: str
    style: str
    n: int
    recall: dict[int, float]
    mrr: float
    fallbacks: int


async def current_docs(sessions: async_sessionmaker, tenant_id: str) -> list[str]:
    async with sessions() as s:
        q = select(PolicyDocument.id).where(
            PolicyDocument.tenant_id == tenant_id, PolicyDocument.effective_to.is_(None)
        )
        return list((await s.execute(q)).scalars())


async def measure(
    sessions: async_sessionmaker,
    tenants: Sequence[str],
    embedder: Embedder | None,
    modes: Sequence[Mode] = ("lexical", "vector", "hybrid"),
) -> list[Row]:
    # every query against every tenant's current policies, scored on the expected section
    out = []
    for mode in modes:
        ranks: dict[str, list[int | None]] = {"agent": [], "customer": []}
        fallbacks = 0
        for tid in tenants:
            docs = await current_docs(sessions, tid)
            for query, want, style in QUERIES:
                async with sessions() as s:
                    found = await search(s, tid, docs, query, embedder=embedder, mode=mode, k=max(KS))
                fallbacks += found.mode != mode
                got = [h.section_id for h in found.hits]
                ranks[style].append(got.index(want) + 1 if want in got else None)
        for style, rs in (*ranks.items(), ("all", ranks["agent"] + ranks["customer"])):
            out.append(
                Row(
                    mode,
                    style,
                    len(rs),
                    {k: sum(1 for r in rs if r is not None and r <= k) / len(rs) for k in KS},
                    sum(1 / r for r in rs if r is not None) / len(rs),
                    fallbacks,
                )
            )
    return out


def table(rows: Sequence[Row]) -> str:
    head = f"{'mode':<8} {'style':<9} {'n':>3} " + " ".join(f"{'r@' + str(k):>5}" for k in KS) + f" {'mrr':>5}"
    lines = [head]
    for r in rows:
        lines.append(
            f"{r.mode:<8} {r.style:<9} {r.n:>3} " + " ".join(f"{r.recall[k]:>5.2f}" for k in KS) + f" {r.mrr:>5.2f}"
        )
    return "\n".join(lines)
