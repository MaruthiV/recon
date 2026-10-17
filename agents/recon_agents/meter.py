from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

PRICING_DIR = Path(__file__).resolve().parents[2] / "pricing"


@dataclass(frozen=True)
class Budget:
    # first live run needed 9 calls (3 + 2 + 4) and ~70k µ$ at list price, see DECISIONS
    max_model_calls: int = 10
    max_tool_calls: int = 12
    max_seconds: int = 120
    max_cost_micros: int = 200_000

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class BudgetExhausted(Exception):
    pass


class UnpricedModel(Exception):
    pass


@dataclass(frozen=True)
class Price:
    # micro-usd per 1M tokens, ints so cost math never touches floats
    input: int
    output: int
    cached_input: int | None = None
    valid_until: date | None = None


@dataclass(frozen=True)
class Usage:
    prompt: int = 0
    cached: int = 0
    output: int = 0
    thoughts: int = 0
    tool_prompt: int = 0


@dataclass(frozen=True)
class Pricing:
    snapshot: str
    models: dict[str, Price]

    @classmethod
    def load(cls, name: str, root: Path = PRICING_DIR) -> Pricing:
        raw = json.loads((root / f"{name}.json").read_text())
        models = {
            m: Price(
                p["input"],
                p["output"],
                p.get("cached_input"),
                date.fromisoformat(p["valid_until"]) if p.get("valid_until") else None,
            )
            for m, p in raw["models"].items()
        }
        return cls(raw["snapshot"], models)

    def cost_micros(self, model: str, u: Usage, on: date) -> int:
        p = self.models.get(model)
        if p is None:
            raise UnpricedModel(f"{model} isn't in pricing snapshot {self.snapshot}")
        if p.valid_until is not None and on >= p.valid_until:
            raise UnpricedModel(f"{model} price in {self.snapshot} expired {p.valid_until}")
        # prompt includes cached, thoughts are billed as output and not inside candidates
        cached_price = p.input if p.cached_input is None else p.cached_input
        total = (
            (u.prompt - u.cached + u.tool_prompt) * p.input
            + u.cached * cached_price
            + (u.output + u.thoughts) * p.output
        )
        return -(-total // 1_000_000)  # ceil, int only


@dataclass(frozen=True)
class UsageRecord:
    agent: str
    model: str
    invocation_id: str | None
    usage: Usage
    est_cost_micros: int
    latency_ms: int | None
    raw: dict[str, Any] | None


def usage_of(resp: LlmResponse) -> tuple[Usage, dict[str, Any] | None]:
    u = resp.usage_metadata
    if u is None:
        return Usage(), None
    return (
        Usage(
            prompt=u.prompt_token_count or 0,
            cached=u.cached_content_token_count or 0,
            output=u.candidates_token_count or 0,
            thoughts=u.thoughts_token_count or 0,
            tool_prompt=u.tool_use_prompt_token_count or 0,
        ),
        u.model_dump(mode="json", exclude_none=True),
    )


class RunMeter(BasePlugin):
    """counts model + tool calls, prices usage, stops the run when the budget is spent"""

    def __init__(
        self,
        budget: Budget,
        pricing: Pricing,
        models: dict[str, str],
        on: date,
        sink: Callable[[UsageRecord], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(name="recon_run_meter")
        self.budget = budget
        self.pricing = pricing
        self.models = models
        self.on = on
        self.sink = sink
        self.model_calls = 0
        self.tool_calls = 0
        self.cost_micros = 0
        self.exhausted: str | None = None
        self._t0 = time.monotonic_ns()
        self._started: dict[str, int] = {}

    def _stop(self, why: str) -> None:
        self.exhausted = self.exhausted or why
        raise BudgetExhausted(why)

    def _check_time_and_cost(self) -> None:
        elapsed = (time.monotonic_ns() - self._t0) // 1_000_000_000
        if elapsed >= self.budget.max_seconds:
            self._stop(f"wall time {elapsed}s >= {self.budget.max_seconds}s")
        if self.cost_micros >= self.budget.max_cost_micros:
            self._stop(f"est spend {self.cost_micros} >= {self.budget.max_cost_micros} micro-usd")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        if self.exhausted:
            self._stop(self.exhausted)
        self._check_time_and_cost()
        if self.model_calls >= self.budget.max_model_calls:
            self._stop(f"model calls would exceed {self.budget.max_model_calls}")
        self.model_calls += 1
        self._started[callback_context.agent_name] = time.perf_counter_ns()
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        if llm_response.partial:
            return None
        agent = callback_context.agent_name
        model = self.models.get(agent, "unknown")
        usage, raw = usage_of(llm_response)
        cost = self.pricing.cost_micros(model, usage, self.on)
        self.cost_micros += cost
        start = self._started.pop(agent, None)
        latency = (time.perf_counter_ns() - start) // 1_000_000 if start is not None else None
        if self.sink is not None:
            await self.sink(UsageRecord(agent, model, callback_context.invocation_id, usage, cost, latency, raw))
        return None

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict | None:
        if self.exhausted:
            self._stop(self.exhausted)
        self._check_time_and_cost()
        # set_model_response is how the agent returns its answer, not a read
        if tool.name == "set_model_response":
            return None
        if self.tool_calls >= self.budget.max_tool_calls:
            self._stop(f"tool calls would exceed {self.budget.max_tool_calls}")
        self.tool_calls += 1
        return None
