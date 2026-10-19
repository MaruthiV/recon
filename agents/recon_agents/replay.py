from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import PrivateAttr


class ReplayDiverged(Exception):
    pass


def structure(obj: Any, depth: int = 4) -> Any:
    # field names all the way down, no values: a tool that grows or loses a field changes this
    if depth == 0:
        return "…"
    if isinstance(obj, dict):
        return {k: structure(v, depth - 1) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [structure(obj[0], depth - 1)] if obj else []
    return type(obj).__name__


def declarations(req: LlmRequest) -> str:
    # exactly what the model is told about each tool: name, description, input schema
    decls = [
        d.model_dump(mode="json", exclude_none=True)
        for t in ((req.config.tools if req.config else None) or [])
        for d in (getattr(t, "function_declarations", None) or [])
    ]
    blob = json.dumps(sorted(decls, key=lambda d: d.get("name", "")), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def shape(req: LlmRequest) -> dict[str, Any]:
    # what the model was shown, minus the volatile bits (ids, timestamps, values)
    parts = [p for c in req.contents for p in (c.parts or [])]
    responses = [p.function_response for p in parts if p.function_response]
    return {
        "contents": len(req.contents),
        "function_responses": [r.name for r in responses],
        "tools": sorted((req.tools_dict or {}).keys()),
        "tool_declarations": declarations(req),
        "response_structure": [
            [r.name, structure((r.response or {}).get("structuredContent", r.response))] for r in responses
        ],
    }


def same(now: dict[str, Any], recorded: dict[str, Any]) -> bool:
    # a tape recorded before a field existed is only held to what it recorded
    return all(now[k] == v for k, v in recorded.items() if k in now)


class Tape:
    def __init__(self, header: dict[str, Any], turns: list[dict[str, Any]] | None = None) -> None:
        self.header = header
        self.turns = turns or []

    @classmethod
    def load(cls, path: Path) -> Tape:
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return cls(lines[0], lines[1:])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.header, *self.turns]
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    def for_agent(self, agent: str) -> list[dict[str, Any]]:
        return [t for t in self.turns if t["agent"] == agent]


class RecordingLlm(BaseLlm):
    """passes through to the real model and writes every turn to a tape"""

    inner: BaseLlm
    agent: str
    tape: Any

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        seen = shape(llm_request)
        async for resp in self.inner.generate_content_async(llm_request, stream):
            if not resp.partial:
                self.tape.turns.append(
                    {"agent": self.agent, "shape": seen, "response": resp.model_dump(mode="json", exclude_none=True)}
                )
            yield resp


class ReplayLlm(BaseLlm):
    """plays a tape back in order per agent, refuses if the request no longer looks the same"""

    agent: str
    turns: list[dict[str, Any]]
    _cursor: int = PrivateAttr(default=0)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        if self._cursor >= len(self.turns):
            raise ReplayDiverged(f"{self.agent}: tape has only {len(self.turns)} turns")
        turn = self.turns[self._cursor]
        self._cursor += 1
        now = shape(llm_request)
        if not same(now, turn["shape"]):
            raise ReplayDiverged(f"{self.agent} turn {self._cursor}: request {now} != recorded {turn['shape']}")
        yield LlmResponse.model_validate(turn["response"])


def replay_models(tape: Tape) -> dict[str, BaseLlm]:
    return {
        agent: ReplayLlm(model=model, agent=agent, turns=tape.for_agent(agent))
        for agent, model in tape.header["models"].items()
    }
