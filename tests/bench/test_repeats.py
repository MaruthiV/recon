import json

from recon_bench.charts import outcome_bars
from recon_bench.report import load, repeats, report


def fake_run(root, name, outcomes, arm="graph"):
    d = root / name
    (d / "cases").mkdir(parents=True)
    manifest = {"arm": arm, "model": "m", "split": "val", "split_sha256": "s", "workflow_version": "w"}
    (d / "manifest.json").write_text(json.dumps(manifest))
    for seed, (passed, wrong) in outcomes.items():
        (d / "cases" / seed).mkdir()
        row = {
            "seed": seed,
            "family": "f",
            "variant": "v",
            "passed": passed,
            "wrong_close": wrong,
            "observed": {"case_state": "CLOSED", "disposition": "REFUND"},
            "approved_by_supervisor": False,
            "money_safe": not wrong,
            "checks": [],
            "seconds": 1.0,
            "model_calls": 1,
            "cost_micros": 0,
        }
        (d / "cases" / seed / "case_result.json").write_text(json.dumps(row))
    return d


def test_repeats_count_cases_right_every_pass_and_the_ones_that_flip(tmp_path):
    a = fake_run(tmp_path, "a", {"x": (True, False), "y": (True, False), "z": (False, True)})
    b = fake_run(tmp_path, "b", {"x": (True, False), "y": (False, False), "z": (False, True)})
    [r] = repeats([a, b])
    assert (r["k"], r["per_pass"], r["every_pass"], r["at_least_once"]) == (2, [2, 1], 1, 2)
    assert (r["wrong_close_at_least_once"], r["flips"]) == (1, ["y"])
    # one pass on its own isn't a repeat
    assert repeats([a]) == []

    svg = tmp_path / "v.svg"
    outcome_bars([("graph", [load(a)[1], load(b)[1]])], svg, "t")
    assert "1/3" in svg.read_text() and "1 to 2 a pass" in svg.read_text()


def test_runs_over_different_cases_are_also_compared_on_the_ones_they_share(tmp_path):
    full = fake_run(tmp_path, "full", {"x": (True, False), "y": (False, True), "z": (True, False)}, arm="rules")
    part = fake_run(tmp_path, "part", {"x": (False, False), "y": (True, False)})
    md = report([full, part])
    shared = md.split("On the 2 cases every run has:")[1]
    assert "1/2 (50%" in shared and "wrong customer-facing closes | 1/2 | 0/2" in shared
    # same cases everywhere, nothing extra
    assert "every run has" not in report([full])
