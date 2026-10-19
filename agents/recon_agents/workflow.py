from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_toolset import BaseToolset
from google.adk.workflow import FunctionNode, JoinNode, Workflow

from recon_domain.plan import ResolutionPlan

from .roles import COORDINATOR, INVESTIGATOR, POLICY, ROLE_TOOLS, SINGLE, roles_of
from .schemas import PaymentFacts, PolicyEvidence

APP_NAME = "recon_investigate"
PROMPTS = Path(__file__).parent / "prompts"
OUTPUTS: dict[str, type] = {
    INVESTIGATOR: PaymentFacts,
    POLICY: PolicyEvidence,
    COORDINATOR: ResolutionPlan,
    SINGLE: ResolutionPlan,
}
GRAPH = "START -> (payment_investigator, policy_specialist) -> join -> brief -> coordinator"
SINGLE_GRAPH = "START -> single_agent"


def prompt(role: str) -> str:
    # a prompt variant under test can live outside the repo, its text still lands in workflow_version
    return (Path(os.environ.get("RECON_PROMPTS_DIR") or PROMPTS) / f"{role}.txt").read_text()


def model_id(m: BaseLlm | str) -> str:
    return m if isinstance(m, str) else m.model


def workflow_version(models: Mapping[str, BaseLlm | str]) -> str:
    # resume is only safe when prompts, schemas, graph and models all match
    roles = roles_of(models)
    blob = {
        "adk": version("google-adk"),
        "graph": SINGLE_GRAPH if roles == (SINGLE,) else GRAPH,
        "prompts": {r: prompt(r) for r in roles},
        "schemas": {r: OUTPUTS[r].model_json_schema() for r in roles},
        "tools": {r: list(ROLE_TOOLS[r]) for r in roles},
        "models": {r: model_id(models[r]) for r in roles},
    }
    digest = hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()
    return f"wf_{digest[:16]}"


def brief(ctx: Context, node_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "case": ctx.state.get("case"),
        "payment_facts": node_input.get(INVESTIGATOR),
        "policy_evidence": node_input.get(POLICY),
    }


def build_app(
    models: Mapping[str, BaseLlm | str],
    toolset_for: Callable[[str], BaseToolset],
    plugins: list[BasePlugin] | None = None,
) -> App:
    agents = {
        role: LlmAgent(
            name=role,
            model=models[role],
            instruction=prompt(role),
            tools=[toolset_for(role)],
            output_schema=OUTPUTS[role],
        )
        for role in roles_of(models)
    }
    if SINGLE in agents:
        return App(
            name=APP_NAME,
            root_agent=Workflow(name="investigate", edges=[("START", agents[SINGLE])]),
            plugins=plugins or [],
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
    graph = Workflow(
        name="investigate",
        edges=[
            (
                "START",
                (agents[INVESTIGATOR], agents[POLICY]),
                JoinNode(name="join"),
                FunctionNode(func=brief, name="brief"),
                agents[COORDINATOR],
            )
        ],
    )
    return App(
        name=APP_NAME,
        root_agent=graph,
        plugins=plugins or [],
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
