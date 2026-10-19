import hashlib
import math
import re

from recon_retrieval.embed import EmbedError


class HashEmbedder:
    """bag of hashed words, enough to give the vector arm a real ranking without a model server"""

    def __init__(self, model: str = "hash-v1", dim: int = 768) -> None:
        self.model, self.dim = model, dim
        self.down = False
        self.calls = 0

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        self.calls += 1
        if self.down:
            raise EmbedError("embedder is down")
        return [self._one(t) for t in texts]

    def _one(self, t: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z]+", t.lower()):
            v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]
