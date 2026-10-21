from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

BOOT = 2000


def load(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    rows = [json.loads(p.read_text()) for p in sorted((run_dir / "cases").glob("*/case_result.json"))]
    return manifest, rows


def ci(flags: list[bool], seed: int = 7) -> tuple[float, float]:
    # case-resampled bootstrap, one rep per case, so the case is the cluster
    if not flags:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    n = len(flags)
    means = sorted(sum(flags[rnd.randrange(n)] for _ in range(n)) / n for _ in range(BOOT))
    return means[int(0.025 * BOOT)], means[int(0.975 * BOOT) - 1]


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def summarize(run_dir: Path, seeds: set[str] | None = None) -> dict[str, Any]:
    manifest, rows = load(run_dir)
    if seeds is not None:
        rows = [r for r in rows if r["seed"] in seeds]
    n = len(rows)
    passed = [r["passed"] for r in rows]
    fams: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        fams[r["family"]].append(r["passed"])
    escalated = [r for r in rows if r["observed"]["case_state"] == "ESCALATED"]
    autonomous = [r for r in rows if r["passed"] and not r["approved_by_supervisor"]]
    lo, hi = ci(passed)
    return {
        "run": run_dir.name,
        "arm": manifest["arm"],
        "model": manifest["model"],
        "split": manifest["split"],
        "split_sha256": manifest["split_sha256"],
        "workflow_version": manifest["workflow_version"],
        "n": n,
        "passed": sum(passed),
        "task_success": sum(passed) / n if n else 0.0,
        "task_success_ci95": [lo, hi],
        # resolved right with no person in the loop: no supervisor approval, not escalated
        "autonomous_resolution": len(autonomous),
        "wrong_closes": sum(r["wrong_close"] for r in rows),
        "escalated": len(escalated),
        "money_safe": sum(r["money_safe"] for r in rows),
        "supervisor_approvals": sum(r["approved_by_supervisor"] for r in rows),
        "per_family": {f: {"n": len(v), "passed": sum(v)} for f, v in sorted(fams.items())},
        "failures": [
            {
                "seed": r["seed"],
                "family": r["family"],
                "variant": r["variant"],
                "observed": r["observed"]["case_state"],
                "disposition": r["observed"]["disposition"],
                "why": r.get("failure") or [c["name"] for c in r["checks"] if not c["ok"]],
            }
            for r in rows
            if not r["passed"]
        ],
        "seconds_p50": pct([r["seconds"] for r in rows], 0.5),
        "seconds_p95": pct([r["seconds"] for r in rows], 0.95),
        "model_calls_p50": pct([r["model_calls"] for r in rows], 0.5),
        "model_cost_micros": sum(r["cost_micros"] for r in rows),
    }


def markdown(summaries: list[dict[str, Any]]) -> str:
    head = ["", *[f"{s['arm']} {s['model'] or ''}".strip() for s in summaries]]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]

    def row(label: str, f) -> None:
        lines.append("| " + " | ".join([label, *[f(s) for s in summaries]]) + " |")

    row("split", lambda s: f"{s['split']} (n={s['n']})")
    row(
        "task success",
        lambda s: (
            f"{s['passed']}/{s['n']} ({s['task_success']:.0%}, 95% CI {s['task_success_ci95'][0]:.0%}–{s['task_success_ci95'][1]:.0%})"
        ),
    )
    row("autonomous resolution", lambda s: f"{s['autonomous_resolution']}/{s['n']}")
    row("wrong customer-facing closes", lambda s: f"{s['wrong_closes']}/{s['n']}")
    row("escalated to a person", lambda s: f"{s['escalated']}/{s['n']}")
    row("money-safe", lambda s: f"{s['money_safe']}/{s['n']}")
    fams = sorted({f for s in summaries for f in s["per_family"]})
    for f in fams:
        row(
            f"· {f}",
            lambda s, f=f: f"{s['per_family'].get(f, {}).get('passed', 0)}/{s['per_family'].get(f, {}).get('n', 0)}",
        )
    row("p50 / p95 seconds per case", lambda s: f"{s['seconds_p50']} / {s['seconds_p95']}")
    row("p50 model calls", lambda s: str(s["model_calls_p50"]))
    row("model cost (list price)", lambda s: f"${s['model_cost_micros'] / 1_000_000:.4f}")
    return "\n".join(lines) + "\n"


def across(runs: list[list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    # one case's rows from every pass, only cases every pass ran
    per: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rows in runs:
        for r in rows:
            per[r["seed"]].append(r)
    return {s: v for s, v in per.items() if len(v) == len(runs)}


def repeats(run_dirs: list[Path]) -> list[dict[str, Any]]:
    # the same arm on the same split more than once: right every pass (pass^k), right at least once, what flips
    groups: dict[tuple[str, str | None, str], list[list[dict[str, Any]]]] = defaultdict(list)
    for d in run_dirs:
        m, rows = load(d)
        groups[(m["arm"], m["model"], m["split_sha256"])].append(rows)
    out = []
    for (arm, model, _), runs in groups.items():
        if len(runs) < 2:
            continue
        per = across(runs)
        every = [all(r["passed"] for r in v) for v in per.values()]
        once = [any(r["passed"] for r in v) for v in per.values()]
        out.append(
            {
                "arm": arm,
                "model": model,
                "k": len(runs),
                "n": len(per),
                "per_pass": [sum(r["passed"] for r in rows) for rows in runs],
                "every_pass": sum(every),
                "every_pass_ci95": list(ci(every)),
                "at_least_once": sum(once),
                "wrong_close_at_least_once": sum(any(r["wrong_close"] for r in v) for v in per.values()),
                "flips": sorted(s for s, v in per.items() if len({r["passed"] for r in v}) > 1),
            }
        )
    return out


def repeats_markdown(reps: list[dict[str, Any]]) -> str:
    head = ["", *[" ".join(x for x in (r["arm"], r["model"], f"×{r['k']}") if x) for r in reps]]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    rows = [
        ("right per pass", lambda r: ", ".join(map(str, r["per_pass"]))),
        (
            "right every pass",
            lambda r: (
                f"{r['every_pass']}/{r['n']} (95% CI {r['every_pass_ci95'][0]:.0%}–{r['every_pass_ci95'][1]:.0%})"
            ),
        ),
        ("right at least once", lambda r: f"{r['at_least_once']}/{r['n']}"),
        ("closed wrongly at least once", lambda r: f"{r['wrong_close_at_least_once']}/{r['n']}"),
        ("cases that flip", lambda r: str(len(r["flips"]))),
    ]
    for label, f in rows:
        lines.append("| " + " | ".join([label, *[f(r) for r in reps]]) + " |")
    return "\n".join(lines) + "\n"


def report(run_dirs: list[Path], out: Path | None = None) -> str:
    summaries = [summarize(d) for d in run_dirs]
    md = markdown(summaries)
    reps = repeats(run_dirs)
    if reps:
        md += "\n" + repeats_markdown(reps)
    # runs over different cases also get compared on the ones they all share
    cases = [{r["seed"] for r in load(d)[1]} for d in run_dirs]
    shared = set.intersection(*cases)
    if shared and any(c != shared for c in cases):
        md += f"\nOn the {len(shared)} cases every run has:\n\n" + markdown([summarize(d, shared) for d in run_dirs])
    for d, s in zip(run_dirs, summaries, strict=True):
        (d / "report.json").write_text(json.dumps(s, indent=2) + "\n")
        (d / "summary.md").write_text(markdown([s]))
    if out is not None:
        out.write_text(md)
    return md
