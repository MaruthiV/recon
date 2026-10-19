from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.adk.models.base_llm import BaseLlm
from sqlalchemy import select

from recon_agents.meter import Budget, Pricing
from recon_agents.replay import RecordingLlm, Tape, replay_models
from recon_agents.roles import ROLES, SINGLE
from recon_agents.workflow import prompt, workflow_version
from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import Spec, materialize
from recon_fixtures.manifests import digest, frozen, specs
from recon_provider.http import HttpConnector
from recon_storage.models import AuditEvent, Case, Command, CommandAttempt, ModelUsage, Plan, Run
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.investigate import AgentConfig, Investigator
from recon_worker.pipeline import Pipeline
from recon_worker.rules_planner import plan_by_rules

from .world import MCP_URL, RUN_KEY, T, World

ARMS = ("rules", "graph", "single")
MONEY_CHECKS = ("3_amount", "4_provider_effect")


@dataclass
class Arm:
    name: str  # rules | graph | single
    model: str | None = None  # ollama:<tag>, a gemini id, or None for rules
    replay: Path | None = None  # a previous run dir, recorded traces instead of model calls
    budget: Budget = field(default_factory=Budget)
    factory: Callable[[str], BaseLlm] | None = None  # tests plug in a scripted model per role here
    price: Pricing | None = None

    def models(self, tape: Tape) -> Mapping[str, BaseLlm | str]:
        assert self.model is not None
        roles = (SINGLE,) if self.name == "single" else ROLES
        return {r: RecordingLlm(model=self.model, inner=self.inner(r), agent=r, tape=tape) for r in roles}

    def inner(self, role: str) -> BaseLlm:
        assert self.model is not None
        if self.factory is not None:
            return self.factory(role)
        if self.model.startswith("ollama:"):
            from recon_agents.ollama import OllamaLlm

            return OllamaLlm(model=self.model)
        from recon_agents.models import live_model

        return live_model(self.model)

    def pricing(self) -> Pricing:
        if self.price is not None:
            return self.price
        if self.replay is not None:
            # priced the way the recorded run was
            return Pricing.load(json.loads((self.replay / "manifest.json").read_text())["pricing"])
        return Pricing.load("local" if (self.model or "").startswith("ollama:") else "2026-10-17")


def sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


async def run_case(w: World, spec: Spec, split: str, arm: Arm, out: Path) -> dict[str, Any]:
    await w.reset()
    # ids from the spec seed, so one case can be re-run from its manifest alone
    ids = seeded_ids(f"bench:{spec.seed}")
    sd = await materialize(spec, w.db, w.sim, w.op, w.evals, T, split, ids)
    w.read_keys[sd.tenant_id] = sd.read_key
    initial = (await w.sim.get("/sim/state", headers=w.op)).json()
    conn = HttpConnector(w.sim, sd.api_key)
    started = time.monotonic()
    tape = None
    inv = None
    if arm.name != "rules":
        if arm.replay is not None:
            tape = Tape.load(arm.replay / "cases" / spec.seed.replace(":", "_") / "trace.json")
            models: Mapping[str, BaseLlm | str] = replay_models(tape)
            mode = "replay"
        else:
            tape = Tape({"case": sd.case_id, "clock": T.isoformat(), "models": {}})
            models = arm.models(tape)
            tape.header["models"] = {r: arm.model for r in models}
            tape.header["workflow_version"] = workflow_version(models)
            mode = "local" if (arm.model or "").startswith("ollama:") else "live"
        cfg = AgentConfig(
            models=models,
            mode=mode,
            pricing=arm.pricing(),
            mcp_url=MCP_URL,
            token_key=RUN_KEY,
            budget=arm.budget,
            http_factory=w.http_factory,
        )
        inv = Investigator(w.db, w.clock, cfg, w.adk)
    executor = Executor(w.db, w.clock, lambda _t: conn)
    if arm.replay is not None and inv is not None:
        # recorded replay is for debugging: rebuild the plan and its verdict, never touch the provider
        out_inv = await inv.investigate(sd.tenant_id, sd.case_id)
        verdict = None
        if out_inv.plan is not None:
            _, verdict = await executor.record_plan(sd.tenant_id, sd.case_id, out_inv.plan, "replay", out_inv.run_id)
        before = json.loads((arm.replay / "cases" / spec.seed.replace(":", "_") / "case_result.json").read_text())
        disposition = out_inv.plan.recommended_disposition.value if out_inv.plan else None
        result = {
            "seed": spec.seed,
            "family": spec.family,
            "variant": spec.variant,
            "replay_of": str(arm.replay),
            "observed": {"disposition": disposition, "decision": verdict.decision.value if verdict else None},
            "recorded": {k: before["observed"].get(k) for k in ("disposition", "decision")},
            "failure": out_inv.failure,
            "reproduced": disposition == before["observed"].get("disposition")
            and (verdict.decision.value if verdict else None) == before["observed"].get("decision"),
            "seconds": round(time.monotonic() - started, 1),
        }
        d = out / "cases" / spec.seed.replace(":", "_")
        d.mkdir(parents=True, exist_ok=True)
        (d / "case_result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        return result
    pipeline = Pipeline(w.db, w.clock, executor, inv, worker_id="bench", lease_seconds=1800)
    if arm.name == "rules":
        async with w.db() as s:
            plan = await plan_by_rules(s, sd.tenant_id, sd.case_id, w.clock.now())
        await pipeline.start_scripted(sd.tenant_id, sd.case_id, plan, source="rules")
    else:
        await pipeline.start_agent(sd.tenant_id, sd.case_id)

    async def deliver(d):
        return (await pipeline.advance(d.run_id, d.stage, d.event_id)).status

    disp = Dispatcher(w.db, deliver)

    async def drain():
        for _ in range(60):
            if await disp.pump_once() == 0:
                return

    await drain()
    approved = False
    async with w.db() as s:
        case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
        last = (
            await s.execute(select(Plan).where(Plan.case_id == sd.case_id).order_by(Plan.seq.desc()).limit(1))
        ).scalar_one_or_none()
    if case.state == "AWAITING_APPROVAL" and last is not None:
        # a supervisor signs off the exact plan on screen, same for every arm
        await pipeline.approve(sd.tenant_id, last.id, last.material_hash, "bench", "supervisor", "APPROVED")
        approved = True
        await drain()
    seconds = round(time.monotonic() - started, 1)

    e = w.verifier
    v = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
    async with w.db() as s:
        case = (await s.execute(select(Case).where(Case.id == sd.case_id))).scalar_one()
        plans = (await s.execute(select(Plan).where(Plan.case_id == sd.case_id).order_by(Plan.seq))).scalars().all()
        runs = (await s.execute(select(Run).where(Run.case_id == sd.case_id).order_by(Run.seq))).scalars().all()
        events = (
            (await s.execute(select(AuditEvent).where(AuditEvent.case_id == sd.case_id).order_by(AuditEvent.id)))
            .scalars()
            .all()
        )
        cmds = (await s.execute(select(Command).where(Command.case_id == sd.case_id))).scalars().all()
        attempts = (
            (await s.execute(select(CommandAttempt).where(CommandAttempt.command_id.in_([c.id for c in cmds]))))
            .scalars()
            .all()
        )
        usage = (
            (
                await s.execute(
                    select(ModelUsage).where(ModelUsage.run_id.in_([r.id for r in runs])).order_by(ModelUsage.id)
                )
            )
            .scalars()
            .all()
        )
    final = (await w.sim.get("/sim/state", headers=w.op)).json()
    agent_runs = [r for r in runs if r.workflow_version not in ("rules", "approval", "scripted")]
    plan = plans[-1] if plans else None
    result = {
        "seed": spec.seed,
        "family": spec.family,
        "variant": spec.variant,
        "tenant_id": sd.tenant_id,
        "case_id": sd.case_id,
        "expected": {
            k: sd.label.get(k) for k in ("disposition", "resolution", "case_state", "financial", "also_accept")
        },
        "observed": {
            "case_state": case.state,
            "resolution": case.resolution,
            "disposition": plan.payload.get("recommended_disposition") if plan else None,
            "decision": plan.decision if plan else None,
            "verdict": plan.verdict if plan else None,
        },
        "passed": v.passed,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in v.checks],
        "money_safe": all(c.ok for c in v.checks if c.name in MONEY_CHECKS) or bool(sd.label.get("financial")),
        "wrong_close": not v.passed and case.state == "CLOSED",
        "approved_by_supervisor": approved,
        "investigation": agent_runs[-1].investigation if agent_runs else None,
        "failure": agent_runs[-1].failure if agent_runs else None,
        "agent_runs": len(agent_runs),
        "model_calls": sum(r.model_calls for r in agent_runs),
        "tool_calls": sum(r.tool_calls for r in agent_runs),
        "cost_micros": sum(r.est_cost_micros for r in agent_runs),
        "seconds": seconds,
    }

    d = out / "cases" / spec.seed.replace(":", "_")
    d.mkdir(parents=True, exist_ok=True)
    dump = lambda name, obj: (d / name).write_text(json.dumps(obj, indent=2, default=str) + "\n")  # noqa: E731
    dump(
        "initial_fixture.json",
        {"spec": asdict(spec), "case_id": sd.case_id, "tenant_id": sd.tenant_id, "simulator": initial},
    )
    dump("final_simulator_state.json", final)
    dump("case_result.json", result)
    (d / "events.jsonl").write_text(
        "".join(
            json.dumps({"id": ev.id, "kind": ev.kind, "at": ev.created_at, "data": ev.data}, default=str) + "\n"
            for ev in events
        )
    )
    dump(
        "commands.json",
        [
            {
                "id": c.id,
                "state": c.state,
                "payload_hash": c.payload_hash,
                "idempotency_key": c.idempotency_key,
                "amount_minor": c.amount_minor,
                "currency": c.currency,
                "provider_refund_id": c.provider_refund_id,
                "attempts": [
                    {k: getattr(a, k) for k in ("attempt_no", "outcome", "http_status", "error_type", "replayed")}
                    for a in attempts
                    if a.command_id == c.id
                ],
            }
            for c in cmds
        ],
    )
    dump(
        "model_usage.json",
        [
            {
                "agent": u.agent,
                "model": u.model,
                "prompt_tokens": u.prompt_tokens,
                "cached_tokens": u.cached_tokens,
                "output_tokens": u.output_tokens,
                "thoughts_tokens": u.thoughts_tokens,
                "est_cost_micros": u.est_cost_micros,
                "pricing_snapshot": u.pricing_snapshot,
                "latency_ms": u.latency_ms,
                "raw": u.raw,
            }
            for u in usage
        ],
    )
    if tape is not None and arm.replay is None:
        tape.save(d / "trace.json")
    return result


async def run(
    w: World,
    split: str,
    arm: Arm,
    out_root: Path,
    families: set[str] | None = None,
    only: set[str] | None = None,
    limit: int | None = None,
) -> Path:
    if split in frozen() and digest(split) != frozen()[split]:
        raise SystemExit(f"{split} drifted from its frozen manifest, refusing to run")
    chosen = [s for s in specs(split) if (not families or s.family in families) and (not only or s.seed in only)]
    chosen = chosen[:limit] if limit else chosen
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    tag = "replay" if arm.replay else (arm.model or "").replace(":", "_").replace("/", "_")
    label = arm.name if arm.name == "rules" else f"{arm.name}-{tag}"
    out = out_root / f"{stamp}-{split}-{label}"
    out.mkdir(parents=True)
    wf = workflow_version(arm.models(Tape({}))) if arm.name != "rules" and arm.model and arm.replay is None else None
    roles = (SINGLE,) if arm.name == "single" else ROLES
    manifest = {
        "split": split,
        "split_sha256": digest(split),
        "cases": [s.seed for s in chosen],
        "arm": arm.name,
        "model": arm.model,
        "replay_of": str(arm.replay) if arm.replay else None,
        "workflow_version": wf,
        "prompt_sha256": {r: hashlib.sha256(prompt(r).encode()).hexdigest() for r in roles}
        if arm.name != "rules"
        else None,
        "budget": arm.budget.as_dict(),
        "pricing": arm.pricing().snapshot if arm.name != "rules" else None,
        "business_time": T.isoformat(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for i, spec in enumerate(chosen, 1):
        r = await run_case(w, spec, split, arm, out)
        if arm.replay is not None:
            print(f"{i:>3}/{len(chosen)} {spec.family:22} {spec.variant:16} reproduced={r['reproduced']}", flush=True)
            continue
        print(
            f"{i:>3}/{len(chosen)} {spec.family:22} {spec.variant:16} {'PASS' if r['passed'] else 'FAIL'}"
            f" {r['observed']['case_state']:>17} calls={r['model_calls']} {r['seconds']}s",
            flush=True,
        )
    return out
