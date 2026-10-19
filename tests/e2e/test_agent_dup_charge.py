import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from recon_agents.meter import Budget, Pricing
from recon_agents.replay import RecordingLlm, Tape, replay_models
from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY, ROLES
from recon_agents.workflow import workflow_version
from recon_domain.states import CaseState as S
from recon_domain.states import CommandState as C
from recon_fixtures.dup_charge import seed_duplicate_charge, seeded_ids
from recon_provider.http import HttpConnector
from recon_storage.models import AuditEvent, Command, ModelUsage, Plan, Run
from recon_verifier.checks import verify_case
from recon_worker.executor import Executor
from recon_worker.investigate import AgentConfig, Investigator, resolve_case
from tests.agents.fakes import (
    FAKE_PRICING,
    confident,
    coordinator,
    empty_answer,
    fake_models,
    looping,
    no_plan,
)
from tests.conftest import MCP_URL, RUN_KEY, T, truncate_all

SEED = "dup-charge-1"
TAPES = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
RECORDED = sorted(TAPES.glob("dup_charge_*.jsonl"))


@pytest.fixture
async def aw(dbs, app_sessions, flaky_sim, op, verifier_engines, mcp_read, clock, adk_sessions):
    async def seed():
        sd = await seed_duplicate_charge(app_sessions, flaky_sim, op, verifier_engines["eval"], T, ids=seeded_ids(SEED))
        mcp_read.read_keys[sd.tenant_id] = sd.read_key
        return sd

    def investigator(models, mode="scripted", budget=None, pricing=FAKE_PRICING):
        cfg = AgentConfig(
            models=models,
            mode=mode,
            pricing=pricing,
            mcp_url=MCP_URL,
            token_key=RUN_KEY,
            budget=budget or Budget(),
            http_factory=mcp_read.http_factory,
        )
        return Investigator(app_sessions, clock, cfg, adk_sessions)

    async def resolve(sd, models, **kw):
        conn = HttpConnector(flaky_sim, sd.api_key)
        ex = Executor(app_sessions, clock, lambda _t: conn)
        return await resolve_case(investigator(models, **kw), ex, sd.tenant_id, sd.case_id)

    async def refunds():
        return (await flaky_sim.get("/sim/state", headers=op)).json()["refunds"]

    async def verify(sd):
        e = verifier_engines
        return await verify_case(e["recon"], e["sim"], e["eval"], sd.case_id)

    async def rows(model, *where):
        async with app_sessions() as s:
            return list((await s.execute(select(model).where(*where))).scalars())

    async def reset():
        await truncate_all(dbs)

    return SimpleNamespace(
        seed=seed,
        resolve=resolve,
        refunds=refunds,
        verify=verify,
        rows=rows,
        reset=reset,
        sim=flaky_sim,
        op=op,
        mcp=mcp_read,
    )


async def test_duplicate_charge_agent_path_scripted(aw):
    sd = await aw.seed()
    models = fake_models()
    out = await aw.resolve(sd, models)

    assert (out.case_state, out.resolution, out.command_state) == (S.CLOSED, "REFUNDED", C.CONFIRMED_SUCCESS)
    [refund] = await aw.refunds()
    assert (refund["payment_intent"], refund["amount"]) == (sd.duplicate_provider_id, 4900)
    verdict = await aw.verify(sd)
    assert verdict.passed, verdict.failed()

    [run] = await aw.rows(Run)
    assert run.investigation == "SUCCEEDED" and run.workflow_version == workflow_version(models)
    assert run.models["mode"] == "scripted"
    assert run.model_calls == sum(m.calls for m in models.values())  # type: ignore[attr-defined]
    # get_case, list_payments, list_refunds x2, search_policy, compute_refund_quote
    assert run.tool_calls == 6
    assert run.evidence is not None
    assert run.evidence[INVESTIGATOR]["duplicate_payment_ids"] == [sd.duplicate_payment_id]

    [plan] = await aw.rows(Plan)
    assert (plan.source, plan.run_id, plan.decision) == ("agent", run.id, "AUTHORIZE")

    usage = await aw.rows(ModelUsage)
    assert len(usage) == run.model_calls
    assert sum(u.est_cost_micros for u in usage) == run.est_cost_micros > 0
    assert {u.pricing_snapshot for u in usage} == {"scripted:test"}

    kinds = [a.kind for a in await aw.rows(AuditEvent)]
    assert kinds.index("investigation_started") < kinds.index("plan_proposed") < kinds.index("plan_validated")


@pytest.mark.parametrize(
    ("budget", "why"),
    [
        (Budget(max_model_calls=4), "model calls"),
        (Budget(max_tool_calls=3), "tool calls"),
        (Budget(max_cost_micros=1), "est spend"),
    ],
)
async def test_budget_exhaustion_does_not_fabricate_plan(aw, budget, why):
    sd = await aw.seed()
    out = await aw.resolve(sd, fake_models(**{INVESTIGATOR: looping}), budget=budget)

    assert out.case_state is S.ESCALATED and out.command_id is None
    [run] = await aw.rows(Run)
    assert run.investigation == "BUDGET_EXHAUSTED" and why in (run.failure or "")
    assert await aw.rows(Plan) == []
    assert await aw.rows(Command) == []
    assert await aw.refunds() == []
    [failed] = await aw.rows(AuditEvent, AuditEvent.kind == "investigation_failed")
    assert failed.data["status"] == "BUDGET_EXHAUSTED"


@pytest.mark.parametrize(("script", "why"), [(no_plan, "validation error"), (empty_answer, "without a plan")])
async def test_workflow_without_plan_fails_closed(aw, script, why):
    sd = await aw.seed()
    out = await aw.resolve(sd, fake_models(**{COORDINATOR: script}))

    assert out.case_state is S.ESCALATED
    [run] = await aw.rows(Run)
    assert run.investigation == "FAILED" and why in (run.failure or ""), run.failure
    assert await aw.rows(Plan) == []
    assert await aw.refunds() == []


async def test_plan_without_any_evidence_reads_is_not_accepted(aw):
    sd = await aw.seed()
    facts = {
        "case_id": sd.case_id,
        "case_version": 1,
        "customer_id": "?",
        "order_id": None,
        "order_lookup": "no_order",
        "customer_request": "?",
        "payments": [],
        "duplicate_payment_ids": [],
    }
    guessed = {
        "case_id": sd.case_id,
        "case_version": 1,
        "recommended_disposition": "REFUND",
        "payment_id": sd.duplicate_payment_id,
        "amount_minor": 4900,
        "currency": "usd",
        "policy_version_ids": [sd.terms_policy_id],
        "evidence_refs": [f"payment:{sd.original_payment_id}:v1", f"payment:{sd.duplicate_payment_id}:v1"],
        "reason_code": "DUPLICATE_CAPTURE",
        "explanation": "guessed",
    }
    models = fake_models(
        **{
            INVESTIGATOR: confident(facts),
            POLICY: confident({"citations": []}),
            COORDINATOR: confident(guessed),
        }
    )
    out = await aw.resolve(sd, models)

    assert out.case_state is S.ESCALATED
    [run] = await aw.rows(Run)
    assert (run.investigation, run.tool_calls) == ("FAILED", 0)
    assert "without reading any evidence" in (run.failure or "")
    assert await aw.rows(Plan) == []
    assert await aw.refunds() == []


async def test_agent_plan_still_has_to_pass_the_validator(aw):
    sd = await aw.seed()
    out = await aw.resolve(sd, fake_models(**{COORDINATOR: coordinator(amount_minor=9900)}))

    assert out.case_state is S.ESCALATED and out.command_id is None
    [plan] = await aw.rows(Plan)
    assert (plan.source, plan.decision) == ("agent", "REJECT")
    assert await aw.refunds() == []


async def test_failed_refund_lookup_escalates_without_moving_money(aw):
    sd = await aw.seed()
    await aw.sim.post("/sim/faults", json={"account": sd.account, "kind": "throttle_reads", "count": 2}, headers=aw.op)
    out = await aw.resolve(sd, fake_models())

    assert out.case_state is S.ESCALATED
    [run] = await aw.rows(Run)
    lookups = {p["refunds_lookup"] for p in run.evidence[INVESTIGATOR]["payments"]}
    assert lookups == {"query_failed"}
    [plan] = await aw.rows(Plan)
    assert plan.payload["recommended_disposition"] == "ESCALATE" and plan.decision == "NO_MONEY"
    assert await aw.refunds() == []


async def test_recorded_tape_replays_without_the_model(aw, tmp_path):
    tape = Tape({"models": {}, "seed": SEED, "clock": T.isoformat()})
    fakes = fake_models()
    recording = {
        role: RecordingLlm(model=fakes[role].model, inner=fakes[role], agent=role, tape=tape) for role in ROLES
    }
    tape.header["models"] = {role: m.model for role, m in recording.items()}
    tape.header["workflow_version"] = workflow_version(recording)
    sd = await aw.seed()
    first = await aw.resolve(sd, recording)
    tape.save(tmp_path / "tape.jsonl")

    await aw.reset()
    sd = await aw.seed()
    calls_before = sum(m.calls for m in fakes.values())  # type: ignore[attr-defined]
    replayed = replay_models(Tape.load(tmp_path / "tape.jsonl"))
    again = await aw.resolve(sd, replayed, mode="replay")

    assert sum(m.calls for m in fakes.values()) == calls_before  # type: ignore[attr-defined]
    assert (again.case_state, again.resolution) == (first.case_state, first.resolution) == (S.CLOSED, "REFUNDED")
    [run] = await aw.rows(Run)
    assert run.workflow_version == tape.header["workflow_version"]
    assert {u.pricing_snapshot for u in await aw.rows(ModelUsage)} == {"replay:test"}
    assert (await aw.verify(sd)).passed


@pytest.mark.parametrize("path", RECORDED or [None], ids=lambda p: p.stem if p else "none")
async def test_duplicate_charge_agent_path_recorded(aw, path):
    if path is None:
        pytest.skip("no recorded gemini tape yet, it comes from a live run")
    tape = Tape.load(path)
    models = replay_models(tape)
    # a tape is a recording of these exact prompts and schemas, a changed prompt means re-record it
    assert workflow_version(models) == tape.header["workflow_version"], f"{path.name} is stale, re-record it"
    sd = await aw.seed()
    out = await aw.resolve(sd, models, mode="replay", pricing=Pricing.load(tape.header["pricing"]))
    assert (out.case_state, out.resolution) == (S.CLOSED, "REFUNDED")
    assert len(await aw.refunds()) == 1
    assert (await aw.verify(sd)).passed


@pytest.mark.live
async def test_duplicate_charge_agent_path(aw):
    if os.environ.get("RECON_LIVE") != "1" or not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("live: needs RECON_LIVE=1, GEMINI_API_KEY and a spend go")
    from recon_agents.models import live_model

    model = os.environ.get("RECON_MODEL", "gemini-3.8-flash")
    pricing = Pricing.load("2026-10-17")
    tape = Tape(
        {"models": {r: model for r in ROLES}, "seed": SEED, "clock": T.isoformat(), "pricing": pricing.snapshot}
    )
    models = {r: RecordingLlm(model=model, inner=live_model(model), agent=r, tape=tape) for r in ROLES}
    tape.header["workflow_version"] = workflow_version(models)
    sd = await aw.seed()
    # free tier: per-minute 429 waits and slow thinking aren't processing time, so allow more wall clock
    budget = Budget(max_seconds=int(os.environ.get("RECON_BUDGET_SECONDS", "300")))
    out = await aw.resolve(sd, models, mode="live", pricing=pricing, budget=budget)

    [run] = await aw.rows(Run)
    print(f"\nlive run: {run.investigation} calls={run.model_calls} tools={run.tool_calls} est={run.est_cost_micros}µ$")
    for u in await aw.rows(ModelUsage):
        print(
            f"  {u.agent:22} in={u.prompt_tokens} cached={u.cached_tokens} out={u.output_tokens} "
            f"thoughts={u.thoughts_tokens} est={u.est_cost_micros}µ$ {u.latency_ms}ms"
        )
    print(f"  plan: {[p.payload for p in await aw.rows(Plan)]}")
    # always keep the raw tape, only a passing run becomes the ci fixture
    tape.save(Path(os.environ.get("RECON_TAPE_OUT", "results/tapes")) / f"{run.id}.jsonl")
    assert (out.case_state, out.resolution) == (S.CLOSED, "REFUNDED"), (run.failure, out.problems)
    assert len(await aw.refunds()) == 1
    assert (await aw.verify(sd)).passed
    if os.environ.get("RECON_RECORD") == "1":
        tape.save(TAPES / f"dup_charge_{model}.jsonl")


@pytest.mark.parametrize(("drops", "closes"), [(2, True), (1000, False)])
async def test_fp11_mcp_read_drops_are_retried_or_escalated_never_guessed(aw, drops, closes):
    sd = await aw.seed()
    aw.mcp.drop_next = drops
    out = await aw.resolve(sd, fake_models())

    refunds = await aw.refunds()
    if closes:
        # a couple of dropped reads: the agent asks again inside its budget and finishes
        assert (out.case_state, out.resolution) == (S.CLOSED, "REFUNDED")
        assert len(refunds) == 1
    else:
        # the read service stays down: no evidence, no plan, no money
        assert out.case_state is S.ESCALATED
        assert await aw.rows(Plan) == [] and refunds == []


@pytest.mark.local
async def test_duplicate_charge_agent_path_local(aw):
    if os.environ.get("RECON_LOCAL") != "1":
        pytest.skip("local: needs RECON_LOCAL=1 and an ollama server")
    from recon_agents.ollama import OllamaLlm

    model = "ollama:" + os.environ.get("RECON_LOCAL_MODEL", "llama3.2")
    pricing = Pricing.load("local")
    tape = Tape(
        {"models": {r: model for r in ROLES}, "seed": SEED, "clock": T.isoformat(), "pricing": pricing.snapshot}
    )
    models = {r: RecordingLlm(model=model, inner=OllamaLlm(model=model), agent=r, tape=tape) for r in ROLES}
    tape.header["workflow_version"] = workflow_version(models)
    sd = await aw.seed()
    budget = Budget(max_seconds=int(os.environ.get("RECON_BUDGET_SECONDS", "900")))
    out = await aw.resolve(sd, models, mode="local", pricing=pricing, budget=budget)

    [run] = await aw.rows(Run)
    print(f"\nlocal run: {run.investigation} calls={run.model_calls} tools={run.tool_calls} failure={run.failure}")
    for u in await aw.rows(ModelUsage):
        print(f"  {u.agent:22} in={u.prompt_tokens} out={u.output_tokens} {u.latency_ms}ms")
    print(f"  plan: {[p.payload for p in await aw.rows(Plan)]}")
    tape.save(Path(os.environ.get("RECON_TAPE_OUT", "results/tapes")) / f"{run.id}.jsonl")
    assert (out.case_state, out.resolution) == (S.CLOSED, "REFUNDED"), (run.failure, out.problems)
    assert len(await aw.refunds()) == 1
    assert (await aw.verify(sd)).passed


async def test_the_single_agent_baseline_runs_the_same_money_path(aw):
    from recon_agents.roles import SINGLE
    from tests.agents.fakes import FAKE_MODEL, ScriptedLlm, single

    sd = await aw.seed()
    models = {SINGLE: ScriptedLlm(model=FAKE_MODEL, script=single)}
    out = await aw.resolve(sd, models)
    assert (out.case_state, out.resolution) == (S.CLOSED, "REFUNDED")
    assert len(await aw.refunds()) == 1 and (await aw.verify(sd)).passed
    [run] = await aw.rows(Run)
    # one agent, its own workflow version, the plan still goes through the validator and ledger
    assert set(run.models) == {"mode", SINGLE} and run.workflow_version == workflow_version(models)
    assert run.workflow_version != workflow_version(fake_models())
    [plan] = await aw.rows(Plan)
    assert plan.decision == "AUTHORIZE"
