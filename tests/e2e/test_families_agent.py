import json
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from recon_agents.meter import Budget, Pricing
from recon_agents.replay import RecordingLlm, Tape
from recon_agents.roles import ROLES
from recon_agents.workflow import workflow_version
from recon_fixtures.dup_charge import seeded_ids
from recon_fixtures.families import FAMILIES, make_specs, materialize
from recon_provider.http import HttpConnector
from recon_storage.models import Case, Plan, Run
from recon_verifier.checks import verify_case
from recon_worker.dispatch import Dispatcher
from recon_worker.executor import Executor
from recon_worker.investigate import AgentConfig, Investigator
from recon_worker.pipeline import Pipeline
from tests.conftest import MCP_URL, RUN_KEY, T

OUT = Path(__file__).resolve().parents[2] / "results" / "families"
MONEY_CHECKS = ("3_amount", "4_provider_effect")


@pytest.mark.local
async def test_local_agent_arm_on_the_dev_families(
    app_sessions, flaky_sim, op, verifier_engines, mcp_read, clock, adk_sessions
):
    if os.environ.get("RECON_LOCAL") != "1":
        pytest.skip("local: needs RECON_LOCAL=1 and an ollama server")
    from recon_agents.ollama import OllamaLlm

    model = "ollama:" + os.environ.get("RECON_LOCAL_MODEL", "qwen3:4b-instruct")
    only = set(filter(None, os.environ.get("RECON_FAMILIES", "").split(","))) or set(FAMILIES)
    pricing = Pricing.load("local")
    # same ids and specs as the rules baseline, so the two arms grade the same 30 cases
    ids = seeded_ids("families-dev-1")
    specs = make_specs("dev", per_family=int(os.environ.get("RECON_PER_FAMILY", "5")), seed=1)
    seeded = [
        await materialize(sp, app_sessions, flaky_sim, op, verifier_engines["eval"], T, "dev", ids) for sp in specs
    ]
    seeded = [sd for sd in seeded if sd.family in only]
    for sd in seeded:
        mcp_read.read_keys[sd.tenant_id] = sd.read_key
    conns = {sd.tenant_id: HttpConnector(flaky_sim, sd.api_key) for sd in seeded}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out_dir = OUT / f"{stamp}-{model.replace(':', '_').replace('/', '_')}"
    (out_dir / "tapes").mkdir(parents=True, exist_ok=True)

    # same caps as gemini by default, a bigger call budget is its own logged arm
    budget = Budget(
        max_model_calls=int(os.environ.get("RECON_BUDGET_CALLS", "10")),
        max_tool_calls=int(os.environ.get("RECON_BUDGET_TOOLS", "12")),
        max_seconds=int(os.environ.get("RECON_BUDGET_SECONDS", "900")),
    )
    rows = []
    for sd in seeded:
        tape = Tape({"models": {r: model for r in ROLES}, "case": sd.case_id, "clock": T.isoformat()})
        models = {r: RecordingLlm(model=model, inner=OllamaLlm(model=model), agent=r, tape=tape) for r in ROLES}
        tape.header["workflow_version"] = workflow_version(models)
        cfg = AgentConfig(
            models=models,
            mode="local",
            pricing=pricing,
            mcp_url=MCP_URL,
            token_key=RUN_KEY,
            budget=budget,
            http_factory=mcp_read.http_factory,
        )
        w = Pipeline(
            app_sessions,
            clock,
            Executor(app_sessions, clock, conns.__getitem__),
            Investigator(app_sessions, clock, cfg, adk_sessions),
            worker_id="w",
            lease_seconds=1800,
            reconcile_backoff_seconds=0,
        )

        async def deliver(d, w=w):
            return (await w.advance(d.run_id, d.stage, d.event_id)).status

        disp = Dispatcher(app_sessions, deliver)

        async def drain(disp=disp):
            for _ in range(50):
                if await disp.pump_once() == 0:
                    return

        started = time.monotonic()
        await w.start_agent(sd.tenant_id, sd.case_id)
        await drain()

        async def latest(cid=sd.case_id):
            # a stale plan can get a second run, so take the newest of each
            async with app_sessions() as s:
                case = (await s.execute(select(Case).where(Case.id == cid))).scalar_one()
                plan = (
                    await s.execute(select(Plan).where(Plan.case_id == cid).order_by(Plan.seq.desc()).limit(1))
                ).scalar_one_or_none()
                runs = (await s.execute(select(Run).where(Run.case_id == cid).order_by(Run.seq))).scalars().all()
            return case, plan, runs

        case, plan, runs = await latest()
        # a supervisor signs off the exact plan they're shown, same as the rules arm
        if case.state == "AWAITING_APPROVAL" and plan is not None:
            await w.approve(sd.tenant_id, plan.id, plan.material_hash, "sam", "supervisor", "APPROVED")
            await drain()
            case, plan, runs = await latest()
        agent_runs = [r for r in runs if r.investigation not in ("SKIPPED",) and r.workflow_version != "approval"]
        run = agent_runs[-1]
        e = verifier_engines
        v = await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)
        tape.save(out_dir / "tapes" / f"{sd.family}-{sd.variant}-{sd.case_id}.jsonl")
        row = {
            "family": sd.family,
            "variant": sd.variant,
            "tenant": sd.tenant_id,
            "case_id": sd.case_id,
            "passed": v.passed,
            "failed": [c.name for c in v.checks if not c.ok],
            "money_safe": all(c.ok for c in v.checks if c.name in MONEY_CHECKS) or bool(sd.label["financial"]),
            "case_state": case.state,
            "agent_runs": len(agent_runs),
            "investigation": run.investigation,
            "failure": run.failure,
            "disposition": (plan.payload or {}).get("recommended_disposition") if plan else None,
            "decision": plan.decision if plan else None,
            "expected": sd.label["disposition"],
            "model_calls": sum(r.model_calls for r in agent_runs),
            "tool_calls": sum(r.tool_calls for r in agent_runs),
            "seconds": round(time.monotonic() - started),
        }
        rows.append(row)
        print(
            f"\n{sd.family:22} {sd.variant:16} {'PASS' if v.passed else 'FAIL'} "
            f"{row['disposition']} (want {row['expected']}) calls={run.model_calls} {row['seconds']}s {row['failed']}"
        )
        summary = {
            "model": model,
            "budget": budget.as_dict(),
            "workflow_version": tape.header["workflow_version"],
            "prompts": os.environ.get("RECON_PROMPTS_DIR") or "repo",
            "rows": rows,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    by_family: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_family[r["family"]]["n"] += 1
        by_family[r["family"]]["passed"] += r["passed"]
    print("\n" + "\n".join(f"{f:22} {c['passed']}/{c['n']}" for f, c in by_family.items()))
    print(f"total {sum(r['passed'] for r in rows)}/{len(rows)}  -> {out_dir}")
    # the claim that holds even for a weak model: wrong answers never move the wrong money
    assert all(r["money_safe"] for r in rows), [r for r in rows if not r["money_safe"]]
