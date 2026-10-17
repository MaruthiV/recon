import argparse
import asyncio
import json
import os
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
from recon_agents.replay import Tape, replay_models
from recon_agents.roles import ROLES
from recon_domain.connector import SystemClock
from recon_fixtures.dup_charge import seed_duplicate_charge, seeded_ids
from recon_mcp.server import create_app as create_mcp_app
from recon_provider.http import HttpConnector, HttpRefundReader
from recon_storage.db import DbNames, make_engine, new_id, sessions, url
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
                ids=seeded_ids(args.seed) if args.seed else new_id,
            )
    finally:
        await app_engine.dispose()
        await eval_engine.dispose()
    print(json.dumps({"tenant_id": sd.tenant_id, "case_id": sd.case_id}))


def live_allowed() -> bool:
    return os.environ.get("RECON_LIVE") == "1" and bool(os.environ.get("GEMINI_API_KEY"))


async def investigate(args: argparse.Namespace) -> int:
    models: Mapping[str, BaseLlm | str]
    if args.replay:
        tape = Tape.load(Path(args.replay))
        models, mode = replay_models(tape), "replay"
        pricing = Pricing.load(tape.header.get("pricing", args.pricing))
    elif live_allowed():
        models, mode, pricing = {r: str(args.model) for r in ROLES}, "live", Pricing.load(args.pricing)
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recon")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="seed the demo duplicate-charge case into the dev dbs and sim")
    s.add_argument("--profile", choices=["demo"], default="demo")
    s.add_argument("--tenant", default="acme")
    s.add_argument("--seed", help="deterministic ids, needed to replay a recorded tape")
    i = sub.add_parser("investigate", help="run INVESTIGATE then the deterministic money path for one case")
    i.add_argument("--case", required=True)
    i.add_argument("--tenant", default="t_acme")
    i.add_argument("--model", default="gemini-3.8-flash")
    i.add_argument("--pricing", default="2026-10-17")
    i.add_argument("--replay", help="recorded tape, no model calls")
    r = sub.add_parser("resume", help="re-enqueue a run's current stage and drive it")
    r.add_argument("--run", required=True)
    r.add_argument("--tenant", default="t_acme")
    d = sub.add_parser("demo", help="spec §25 demo, no model calls")
    d.add_argument("scenario", choices=["dup-charge"])
    d.add_argument("--fault", choices=["FP4"], default="FP4")
    args = p.parse_args(argv)
    if args.cmd == "seed":
        asyncio.run(seed(args))
        return 0
    if args.cmd == "resume":
        return asyncio.run(resume(args))
    if args.cmd == "demo":
        from .demo import dup_charge_fp4

        return asyncio.run(dup_charge_fp4(NAMES))
    return asyncio.run(investigate(args))


if __name__ == "__main__":
    sys.exit(main())
