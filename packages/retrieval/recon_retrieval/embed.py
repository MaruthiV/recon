from __future__ import annotations

import os
from typing import Literal, Protocol

import httpx

Kind = Literal["query", "document"]
# nomic was trained with a task prefix on every input
NOMIC = {"query": "search_query: ", "document": "search_document: "}


class EmbedError(Exception):
    pass


class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        dim: int = 768,
        prefixes: dict[str, str] | None = None,
        timeout_s: int = 30,
    ) -> None:
        self.model, self.dim = model, dim
        self.prefixes = NOMIC if prefixes is None and model.startswith("nomic-embed") else (prefixes or {})
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        pre = self.prefixes.get(kind, "")
        try:
            r = await self._http.post("/api/embed", json={"model": self.model, "input": [pre + t for t in texts]})
            r.raise_for_status()
            out = r.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise EmbedError(f"{self.model}: {e}") from e
        if len(out) != len(texts) or any(len(v) != self.dim for v in out):
            raise EmbedError(f"{self.model}: wanted {len(texts)} x {self.dim}")
        return out

    async def aclose(self) -> None:
        await self._http.aclose()


def doc_text(heading: str, text: str) -> str:
    # same text the lexical index sees
    return f"{heading}. {text}"


def literal(v: list[float]) -> str:
    # pgvector's text form, sent with a cast so no driver codec is needed
    return "[" + ",".join(repr(x) for x in v) + "]"


def embedder_from_env() -> OllamaEmbedder | None:
    # hybrid policy search only when an embedding model is named, lexical otherwise
    model = os.environ.get("RECON_EMBED_MODEL")
    return OllamaEmbedder(os.environ.get("RECON_OLLAMA_URL", "http://127.0.0.1:11434"), model) if model else None
