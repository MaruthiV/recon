import json

from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY
from recon_bench.report import report
from recon_bench.run import Arm, run
from recon_bench.world import world
from tests.agents.fakes import FAKE_MODEL, FAKE_PRICING, ScriptedLlm, coordinator, investigator, policy

ARTIFACTS = {
    "initial_fixture.json",
    "final_simulator_state.json",
    "case_result.json",
    "events.jsonl",
    "commands.json",
    "model_usage.json",
}


SCRIPTS = {INVESTIGATOR: investigator, POLICY: policy, COORDINATOR: coordinator()}


def scripted(role):
    return ScriptedLlm(model=FAKE_MODEL, script=SCRIPTS[role])


async def test_rules_arm_writes_every_artifact_and_a_report(tmp_path):
    async with world("benchtest") as w:
        out = await run(w, "dev", Arm("rules"), tmp_path, families={"duplicate_charge"}, limit=3)
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["split_sha256"] and len(manifest["cases"]) == 3
    for case in (out / "cases").iterdir():
        assert ARTIFACTS <= {p.name for p in case.iterdir()}
    md = report([out])
    s = json.loads((out / "report.json").read_text())
    assert (s["n"], s["passed"], s["money_safe"], s["wrong_closes"]) == (3, 3, 3, 0)
    assert "task success" in md and (out / "summary.md").exists()


async def test_an_agent_run_records_traces_and_replays_them_without_sending_anything(tmp_path):
    arm = Arm("graph", FAKE_MODEL, factory=scripted, price=FAKE_PRICING)
    async with world("benchtest") as w:
        out = await run(w, "dev", arm, tmp_path, families={"duplicate_charge"}, limit=1)
        [case] = list((out / "cases").iterdir())
        assert "trace.json" in {p.name for p in case.iterdir()}
        assert json.loads((case / "case_result.json").read_text())["passed"]

        again = await run(
            w, "dev", Arm("graph", replay=out, price=FAKE_PRICING), tmp_path, families={"duplicate_charge"}, limit=1
        )
        posts = (await w.sim.get("/sim/state", headers=w.op)).json()["requests_log"]
    [replayed] = list((again / "cases").iterdir())
    r = json.loads((replayed / "case_result.json").read_text())
    assert r["reproduced"] and r["observed"]["decision"] == "AUTHORIZE"
    # replay is for debugging: the plan and its verdict come back, the provider never hears about it
    assert not [p for p in posts if p["method"] == "POST" and p["path"] == "/v1/refunds"]
