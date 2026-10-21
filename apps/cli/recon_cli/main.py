import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

# before adk loads: skip the gce mtls probe, use the stateless mcp protocol
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")
os.environ.setdefault("ADK_ENABLE_MCP_MODERN_PROTOCOL", "1")

import httpx
import httpx2
from google.adk.models.base_llm import BaseLlm
from google.adk.sessions import DatabaseSessionService
from sqlalchemy.ext.asyncio import create_async_engine

from recon_agents.meter import Pricing
from recon_agents.models import live_model
from recon_agents.replay import Tape, replay_models
from recon_agents.roles import ROLES
from recon_domain.connector import SystemClock
from recon_fixtures.dup_charge import seed_duplicate_charge, seeded_ids
from recon_mcp.server import create_app as create_mcp_app
from recon_provider.http import HttpConnector, HttpRefundReader
from recon_retrieval.embed import embedder_from_env
from recon_retrieval.index import index_from_env
from recon_storage.db import DbNames, make_engine, new_id, sessions, url
from recon_storage.users import ensure_demo_users
from recon_worker.executor import Executor
from recon_worker.investigate import AgentConfig, Investigator, resolve_case

SIM_URL = os.environ.get("RECON_SIM_URL", "http://localhost:8100")
OPERATOR = os.environ.get("SIM_OPERATOR_TOKEN", "sim-operator-dev")
RUN_KEY = os.environ.get("RECON_RUN_TOKEN_KEY", "dev-run-token-key").encode()
MCP_URL = "http://localhost:8200/mcp"
NAMES = DbNames.for_env("dev")


def provider_keys(tenant_id: str) -> tuple[str, str]:
    # dev keys follow the seeder's convention unless RECON_PROVIDER_KEYS overrides them
    keys = json.loads(os.environ.get("RECON_PROVIDER_KEYS", "{}"))
    if tenant_id in keys:
        return keys[tenant_id]["secret"], keys[tenant_id]["read"]
    name = tenant_id.removeprefix("t_")
    return f"sk_test_{name}", f"rk_test_{name}"


async def seed(args: argparse.Namespace) -> None:
    app_engine = make_engine(url("recon_app", NAMES.recon))
    eval_engine = make_engine(url("recon_eval", NAMES.eval))
    try:
        async with httpx.AsyncClient(base_url=SIM_URL) as sim:
            sd = await seed_duplicate_charge(
                sessions(app_engine),
                sim,
                {"x-sim-operator": OPERATOR},
                eval_engine,
                datetime.now(UTC),
                tenant=args.tenant,
                amount_minor=args.amount,
                ids=seeded_ids(args.seed) if args.seed else new_id,
            )
        indexed = await index_from_env(sessions(app_engine))
        await ensure_demo_users(sessions(app_engine), datetime.now(UTC), [sd.tenant_id])
    finally:
        await app_engine.dispose()
        await eval_engine.dispose()
    print(json.dumps({"tenant_id": sd.tenant_id, "case_id": sd.case_id, "indexed_chunks": indexed}))


async def reindex() -> int:
    if embedder_from_env() is None:
        print("set RECON_EMBED_MODEL (e.g. nomic-embed-text) to index policy chunks")
        return 2
    engine = make_engine(url("recon_app", NAMES.recon))
    try:
        print(json.dumps({"indexed_chunks": await index_from_env(sessions(engine))}))
    finally:
        await engine.dispose()
    return 0


def live_allowed() -> bool:
    return os.environ.get("RECON_LIVE") == "1" and bool(os.environ.get("GEMINI_API_KEY"))


async def investigate(args: argparse.Namespace) -> int:
    models: Mapping[str, BaseLlm | str]
    if args.replay:
        tape = Tape.load(Path(args.replay))
        models, mode = replay_models(tape), "replay"
        pricing = Pricing.load(tape.header.get("pricing", args.pricing))
    elif live_allowed():
        models, mode, pricing = {r: live_model(args.model) for r in ROLES}, "live", Pricing.load(args.pricing)
    else:
        print("refusing: live model calls need RECON_LIVE=1, GEMINI_API_KEY and a spend go (or pass --replay)")
        return 2

    secret, read_key = provider_keys(args.tenant)
    clock = SystemClock()
    app_engine = make_engine(url("recon_app", NAMES.recon))
    adk_engine = create_async_engine(
        url("recon_app", NAMES.recon), connect_args={"server_settings": {"search_path": "adk"}}
    )
    db = sessions(app_engine)
    async with httpx.AsyncClient(base_url=SIM_URL) as sim:
        service = create_mcp_app(
            url("recon_mcp_read", NAMES.recon), RUN_KEY, lambda _t: HttpRefundReader(sim, read_key), clock, "localhost"
        )

        def in_process(headers=None, timeout=None, auth=None):
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=service),
                headers=headers,
                timeout=timeout,
                auth=auth,
                base_url="http://localhost:8200",
            )

        cfg = AgentConfig(
            models=models, mode=mode, pricing=pricing, mcp_url=MCP_URL, token_key=RUN_KEY, http_factory=in_process
        )
        investigator = Investigator(db, clock, cfg, DatabaseSessionService(db_engine=adk_engine))
        conn = HttpConnector(sim, secret)
        executor = Executor(db, clock, lambda _t: conn)
        try:
            async with service.lifespan():
                out = await resolve_case(investigator, executor, args.tenant, args.case)
        finally:
            await service.engine.dispose()
            await app_engine.dispose()
            await adk_engine.dispose()
    print(
        json.dumps(
            {
                "case_state": out.case_state.value,
                "resolution": out.resolution,
                "decision": out.verdict.decision.value if out.verdict else None,
                "command_id": out.command_id,
                "command_state": out.command_state.value if out.command_state else None,
                "problems": out.problems,
            },
            indent=2,
        )
    )
    return 0


async def resume(args: argparse.Namespace) -> int:
    # re-enqueue one run's current stage and drive it in-process, the sweeper does the same for all runs
    from sqlalchemy import select, text

    from recon_storage.models import Run
    from recon_worker.dispatch import Dispatcher
    from recon_worker.pipeline import Pipeline

    secret, _ = provider_keys(args.tenant)
    engine = make_engine(url("recon_app", NAMES.recon))
    db = sessions(engine)
    clock = SystemClock()
    try:
        async with httpx.AsyncClient(base_url=SIM_URL) as sim:
            conn = HttpConnector(sim, secret)
            pipe = Pipeline(db, clock, Executor(db, clock, lambda _t: conn), worker_id="cli-resume")
            async with db() as s, s.begin():
                run = (await s.execute(select(Run).where(Run.id == args.run))).scalar_one()
                await s.execute(
                    text(
                        "INSERT INTO outbox_events (tenant_id, run_id, stage, event_id, available_at)"
                        " VALUES (:t, :r, :st, :e, now()) ON CONFLICT (event_id) DO NOTHING"
                    ),
                    {
                        "t": run.tenant_id,
                        "r": run.id,
                        "st": run.stage,
                        "e": f"{run.id}/{run.stage}/{run.generation}/resume",
                    },
                )

            async def here(d):
                return (await pipe.advance(d.run_id, d.stage, d.event_id)).status

            disp = Dispatcher(db, here)
            for _ in range(20):
                if await disp.pump_once() == 0:
                    break
            async with db() as s:
                run = (await s.execute(select(Run).where(Run.id == args.run))).scalar_one()
        print(json.dumps({"run_id": run.id, "stage": run.stage, "lease_owner": run.lease_owner}))
    finally:
        await engine.dispose()
    return 0


async def evaluate(args: argparse.Namespace) -> int:
    import logging

    from recon_agents.meter import Budget
    from recon_bench.report import report
    from recon_bench.run import Arm, run
    from recon_bench.world import world

    if args.arm != "rules" and not (args.model or args.replay):
        print("agent arms need --model (e.g. ollama:qwen3:4b-instruct) or --replay <run dir>")
        return 2
    live = args.model and not args.model.startswith("ollama:") and not args.replay
    if live and not live_allowed():
        print("refusing: gemini calls need RECON_LIVE=1 and GEMINI_API_KEY")
        return 2
    # held_out runs once per config in m6, before that only the 5-case smoke
    if args.split == "held_out" and os.environ.get("RECON_HELD_OUT") != "1" and not (args.limit and args.limit <= 5):
        print("refusing: held_out needs --limit 5 or less until m6 (RECON_HELD_OUT=1)")
        return 2
    arm = Arm(
        args.arm,
        args.model,
        Path(args.replay) if args.replay else None,
        Budget(max_model_calls=args.calls, max_tool_calls=args.calls + 2, max_seconds=900),
        fault=args.fault,
    )
    # one line per case is the progress, not every http request
    for name in ("httpx", "httpx2", "mcp", "google_adk", "google"):
        logging.getLogger(name).setLevel(logging.WARNING)
    split = lambda v: set(filter(None, (v or "").split(","))) or None  # noqa: E731
    async with world() as w:
        out = await run(w, args.split, arm, Path(args.out), split(args.families), split(args.only), args.limit)
    if not args.replay:
        print(report([out]))
    print(out)
    return 0


async def naive(args: argparse.Namespace) -> int:
    from recon_bench.naive import run_b3
    from recon_bench.run import Arm
    from recon_bench.world import world

    if not args.model.startswith("ollama:") and not live_allowed():
        print("refusing: gemini calls need RECON_LIVE=1 and GEMINI_API_KEY")
        return 2
    arm = Arm("single", args.model)
    out = Path(args.out) / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    async with world("bench_b3") as w:
        summary = await run_b3(w, lambda: arm.inner("naive_agent"), out, args.instances, args.model)
    print(json.dumps(summary, indent=2))
    print(out)
    return 0


def _interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recon")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="seed the demo duplicate-charge case into the dev dbs and sim")
    s.add_argument("--profile", choices=["demo"], default="demo")
    s.add_argument("--tenant", default="acme")
    s.add_argument("--seed", help="deterministic ids, needed to replay a recorded tape")
    s.add_argument("--amount", type=int, default=4900, help="charge in minor units, over 10000 needs an approval")
    i = sub.add_parser("investigate", help="run INVESTIGATE then the deterministic money path for one case")
    i.add_argument("--case", required=True)
    i.add_argument("--tenant", default="t_acme")
    i.add_argument("--model", default="gemini-3.8-flash")
    i.add_argument("--pricing", default="2026-10-17")
    i.add_argument("--replay", help="recorded tape, no model calls")
    r = sub.add_parser("resume", help="re-enqueue a run's current stage and drive it")
    r.add_argument("--run", required=True)
    r.add_argument("--tenant", default="t_acme")
    v = sub.add_parser("dev", help="local stack: sim, mcp-read, mcp-exec, worker (auto-restart), api, web")
    v.add_argument("--agent", help="live:<model> (needs RECON_LIVE=1) or replay:<tape>")
    v.add_argument("--no-web", action="store_true")
    v.add_argument("--families", action="store_true", help="also seed one case per workflow variant (t_demo_* tenants)")
    sub.add_parser("index", help="embed policy chunks for hybrid search (needs RECON_EMBED_MODEL)")
    ev = sub.add_parser("eval", help="run one arm over a frozen split, writing spec §20 artifacts per case")
    ev.add_argument("--split", choices=["dev", "val", "held_out"], default="dev")
    ev.add_argument("--arm", choices=["rules", "graph", "single"], default="rules")
    ev.add_argument("--model", help="ollama:<tag> (free, local) or a gemini id (needs RECON_LIVE=1)")
    ev.add_argument("--replay", help="a previous run dir: recorded traces, validation only, nothing sent")
    ev.add_argument("--families", help="comma-separated families")
    ev.add_argument("--only", help="comma-separated case seeds, to re-run single cases from a manifest")
    ev.add_argument("--limit", type=int)
    ev.add_argument("--calls", type=int, default=10, help="model call budget per case")
    ev.add_argument("--fault", choices=["FP2", "FP3", "FP4", "FP6"], help="inject this fault once per case")
    ev.add_argument("--out", default="results/eval")
    b3 = sub.add_parser(
        "b3", help="naive direct-tool agent under provider faults (no ledger), counts duplicate refunds"
    )
    b3.add_argument("--model", required=True, help="ollama:<tag>, a gemini id needs RECON_LIVE=1")
    b3.add_argument("--instances", type=int, default=10)
    b3.add_argument("--out", default="results/b3")
    ch = sub.add_parser("charts", help="readme charts (svg) from result files")
    ch.add_argument(
        "--val",
        nargs="*",
        default=[],
        help="name=run_dir pairs, comma separate repeats, e.g. rules=results/eval/a,results/eval/b",
    )
    ch.add_argument("--b3", help="a results/b3/<stamp> dir")
    ch.add_argument("--held-out", nargs="*", default=[], help="name=run_dir pairs, compared on the cases they share")
    ch.add_argument("--out", default="assets")
    rp = sub.add_parser("report", help="aggregate eval runs, side by side")
    rp.add_argument("runs", nargs="+")
    rp.add_argument("--out")
    d = sub.add_parser("demo", help="spec §25 demo, no model calls")
    d.add_argument("scenario", choices=["dup-charge"])
    d.add_argument("--fault", choices=["FP4"], default="FP4")
    args = p.parse_args(argv)
    if args.cmd == "seed":
        asyncio.run(seed(args))
        return 0
    if args.cmd == "index":
        return asyncio.run(reindex())
    if args.cmd == "eval":
        return asyncio.run(evaluate(args))
    if args.cmd == "charts":
        from recon_bench.charts import from_results

        runs = dict(pair.split("=", 1) for pair in args.val)
        made = from_results(
            Path("results"),
            Path(args.out),
            {k: [Path(d) for d in v.split(",")] for k, v in runs.items()} or None,
            Path(args.b3) if args.b3 else None,
            {k: Path(v) for k, v in (pair.split("=", 1) for pair in args.held_out)} or None,
        )
        print("\n".join(str(m) for m in made))
        return 0
    if args.cmd == "b3":
        return asyncio.run(naive(args))
    if args.cmd == "report":
        from recon_bench.report import report

        print(report([Path(r) for r in args.runs], Path(args.out) if args.out else None))
        return 0
    if args.cmd == "resume":
        return asyncio.run(resume(args))
    if args.cmd == "dev":
        from .dev import dev

        # a plain kill (playwright, ci, a terminal closing) cleans up the children like ctrl-c does
        signal.signal(signal.SIGTERM, _interrupt)
        try:
            return asyncio.run(dev(NAMES, args.agent, not args.no_web, args.families))
        except KeyboardInterrupt:
            return 0
    if args.cmd == "demo":
        from .demo import dup_charge_fp4

        return asyncio.run(dup_charge_fp4(NAMES))
    return asyncio.run(investigate(args))


if __name__ == "__main__":
    sys.exit(main())
