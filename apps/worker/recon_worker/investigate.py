from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recon_agents.meter import Budget, Pricing, RunMeter, UsageRecord
from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY, ROLE_TOOLS, ROLES
from recon_agents.workflow import APP_NAME, build_app, model_id, workflow_version
from recon_domain.connector import Clock
from recon_domain.plan import ResolutionPlan
from recon_domain.states import CaseState as S
from recon_domain.tokens import MCP_READ, RUN_TOKEN_HEADER, mint
from recon_storage.models import ModelUsage, Run
from recon_storage.repos import audit, get_case, move_case, new_id

from .executor import Executor, Outcome


class NotInvestigable(Exception):
    pass


@dataclass(frozen=True)
class AgentConfig:
    models: Mapping[str, BaseLlm | str]
    mode: str  # live | replay | scripted
    pricing: Pricing
    mcp_url: str
    token_key: bytes
    budget: Budget = field(default_factory=Budget)
    token_ttl: timedelta = timedelta(minutes=5)
    http_factory: Any = None  # tests swap in an in-process transport


@dataclass
class Investigation:
    run_id: str
    status: str  # SUCCEEDED | BUDGET_EXHAUSTED | FAILED
    plan: ResolutionPlan | None = None
    failure: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


class Investigator:
    """INVESTIGATE stage: runs the adk graph, done only when a schema-valid plan comes back"""

    def __init__(
        self, sessions: async_sessionmaker, clock: Clock, cfg: AgentConfig, adk_sessions: BaseSessionService
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.cfg = cfg
        self.adk = adk_sessions

    def _toolset(self, tenant_id: str, case_id: str, run_id: str, role: str, now: datetime) -> McpToolset:
        claims = {
            "aud": MCP_READ,
            "tenant": tenant_id,
            "case_id": case_id,
            "run_id": run_id,
            "role": role,
            "tools": list(ROLE_TOOLS[role]),
            "iat": int(now.timestamp()),
            "exp": int((now + self.cfg.token_ttl).timestamp()),
        }
        token = mint(claims, self.cfg.token_key)
        extra = {"httpx_client_factory": self.cfg.http_factory} if self.cfg.http_factory else {}
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(url=self.cfg.mcp_url, **extra),
            tool_filter=list(ROLE_TOOLS[role]),
            header_provider=lambda _ctx: {RUN_TOKEN_HEADER: token},
        )

    async def _record_usage(self, tenant_id: str, run_id: str, rec: UsageRecord) -> None:
        snapshot = self.cfg.pricing.snapshot
        async with self.sessions() as s, s.begin():
            s.add(
                ModelUsage(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    agent=rec.agent,
                    model=rec.model,
                    invocation_id=rec.invocation_id,
                    prompt_tokens=rec.usage.prompt,
                    cached_tokens=rec.usage.cached,
                    output_tokens=rec.usage.output,
                    thoughts_tokens=rec.usage.thoughts,
                    tool_prompt_tokens=rec.usage.tool_prompt,
                    est_cost_micros=rec.est_cost_micros,
                    # replayed usage is the recorded run's, not new spend
                    pricing_snapshot=snapshot if self.cfg.mode == "live" else f"{self.cfg.mode}:{snapshot}",
                    latency_ms=rec.latency_ms,
                    raw=rec.raw,
                    created_at=self.clock.now(),
                )
            )

    async def begin(
        self, s: AsyncSession, tenant_id: str, case_id: str, run_id: str, now: datetime, stage: str = "INVESTIGATE"
    ) -> None:
        case = await get_case(s, tenant_id, case_id)
        state = S(case.state)
        if state is S.OPEN:
            await move_case(s, tenant_id, case_id, S.OPEN, S.INVESTIGATING, now)
        elif state is not S.INVESTIGATING:
            raise NotInvestigable(f"case {case_id} is {state}")
        wf = workflow_version(self.cfg.models)
        s.add(
            Run(
                id=run_id,
                tenant_id=tenant_id,
                case_id=case_id,
                stage=stage,
                investigation="RUNNING",
                workflow_version=wf,
                models={"mode": self.cfg.mode, **{r: model_id(self.cfg.models[r]) for r in ROLES}},
                budget=self.cfg.budget.as_dict(),
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        audit(s, tenant_id, "investigation_started", now, case_id=case_id, run_id=run_id, workflow_version=wf)

    async def _brief(self, tenant_id: str, case_id: str) -> dict[str, Any]:
        async with self.sessions() as s:
            case = await get_case(s, tenant_id, case_id)
        return {"case_id": case.id, "case_version": case.version, "subject": case.subject, "order_id": case.order_id}

    async def investigate(self, tenant_id: str, case_id: str, run_id: str | None = None) -> Investigation:
        now = self.clock.now()
        if run_id is None:
            run_id = new_id("run")
            # direct call, no stage machine behind it
            async with self.sessions() as s, s.begin():
                await self.begin(s, tenant_id, case_id, run_id, now, stage="DIRECT")
        brief = await self._brief(tenant_id, case_id)

        async def sink(rec: UsageRecord) -> None:
            await self._record_usage(tenant_id, run_id, rec)

        meter = RunMeter(
            self.cfg.budget, self.cfg.pricing, {r: model_id(self.cfg.models[r]) for r in ROLES}, now.date(), sink
        )
        toolsets = {role: self._toolset(tenant_id, case_id, run_id, role, now) for role in ROLES}
        runner = Runner(app=build_app(self.cfg.models, toolsets.__getitem__, [meter]), session_service=self.adk)
        session = await self.adk.create_session(app_name=APP_NAME, user_id=tenant_id, state={"case": brief})
        msg = types.Content(role="user", parts=[types.Part(text=json.dumps({"case": brief}))])

        outputs: dict[str, Any] = {}
        invocation_id: str | None = None
        status, failure = "SUCCEEDED", None
        assert run_id is not None
        try:
            async with asyncio.timeout(self.cfg.budget.max_seconds):
                async for ev in runner.run_async(user_id=tenant_id, session_id=session.id, new_message=msg):
                    invocation_id = invocation_id or ev.invocation_id
                    if ev.author in ROLES and getattr(ev, "output", None) is not None:
                        outputs[ev.author] = jsonable(ev.output)
        except TimeoutError:
            status, failure = "BUDGET_EXHAUSTED", f"wall time over {self.cfg.budget.max_seconds}s"
        except Exception as e:
            # adk wraps plugin errors, the meter remembers why it stopped
            status, failure = ("BUDGET_EXHAUSTED", meter.exhausted) if meter.exhausted else ("FAILED", repr(e)[:500])
        finally:
            for t in toolsets.values():
                await t.close()

        plan = None
        if status == "SUCCEEDED":
            raw = outputs.get(COORDINATOR)
            if raw is None:
                status, failure = "FAILED", "workflow ended without a plan"
            else:
                try:
                    plan = ResolutionPlan.model_validate(raw)
                except ValidationError as e:
                    status, failure = "FAILED", f"plan failed schema: {e.errors()[:3]}"
            # a broken mcp connection shows up as an empty toolset, not an error
            if plan is not None and meter.tool_calls == 0:
                status, failure, plan = "FAILED", "plan proposed without reading any evidence", None

        evidence = {r: outputs.get(r) for r in (INVESTIGATOR, POLICY)}
        later = self.clock.now()
        async with self.sessions() as s, s.begin():
            await s.execute(
                update(Run)
                .where(Run.tenant_id == tenant_id, Run.id == run_id)
                .values(
                    investigation=status,
                    failure=failure,
                    adk_session_id=session.id,
                    adk_invocation_id=invocation_id,
                    evidence=evidence,
                    model_calls=meter.model_calls,
                    tool_calls=meter.tool_calls,
                    est_cost_micros=meter.cost_micros,
                    updated_at=later,
                )
            )
            if plan is None:
                # no plan means nothing downstream runs, a human picks it up
                await move_case(s, tenant_id, case_id, S.INVESTIGATING, S.ESCALATED, later)
                audit(
                    s,
                    tenant_id,
                    "investigation_failed",
                    later,
                    case_id=case_id,
                    run_id=run_id,
                    status=status,
                    failure=failure,
                )
            else:
                audit(
                    s,
                    tenant_id,
                    "plan_proposed",
                    later,
                    case_id=case_id,
                    run_id=run_id,
                    disposition=plan.recommended_disposition.value,
                )
        return Investigation(run_id, status, plan, failure, evidence)


async def resolve_case(investigator: Investigator, executor: Executor, tenant_id: str, case_id: str) -> Outcome:
    inv = await investigator.investigate(tenant_id, case_id)
    if inv.plan is None:
        return Outcome(S.ESCALATED, None, problems=[inv.failure or inv.status])
    return await executor.run(tenant_id, case_id, inv.plan, source="agent", run_id=inv.run_id)
