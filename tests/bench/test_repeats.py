import json

from recon_bench.charts import outcome_bars
from recon_bench.report import load, repeats


def fake_run(root, name, outcomes):
    d = root / name
    (d / "cases").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"arm": "graph", "model": "m", "split_sha256": "s"}))
    for seed, (passed, wrong) in outcomes.items():
        (d / "cases" / seed).mkdir()
        row = {"seed": seed, "family": "f", "passed": passed, "wrong_close": wrong}
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
