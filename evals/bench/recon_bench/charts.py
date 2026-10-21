from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .report import across, load

# hand-rolled svg so the readme charts come straight from result files, no plotting deps
GREEN, LIGHT, AMBER, RED, GREY = "#2f855a", "#9fd0ae", "#c27c0e", "#c53030", "#d7d7d2"
INK, MUTED = "#1f2328", "#6b6f76"
FONT = "font-family='-apple-system, Segoe UI, Helvetica, Arial, sans-serif'"


def _svg(w: int, h: int, body: list[str], title: str) -> str:
    # own background so it reads the same in github light and dark mode
    return "\n".join(
        [
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}' {FONT}>",
            f"<rect width='{w}' height='{h}' rx='12' fill='#fbfbf9' stroke='#e6e6e1'/>",
            f"<text x='24' y='36' font-size='17' font-weight='600' fill='{INK}'>{title}</text>",
            *body,
            "</svg>",
        ]
    )


def _legend(x: int, y: int, items: list[tuple[str, str]]) -> list[str]:
    out = []
    for label, color in items:
        out.append(f"<rect x='{x}' y='{y - 10}' width='12' height='12' rx='3' fill='{color}'/>")
        out.append(f"<text x='{x + 18}' y='{y}' font-size='13' fill='{MUTED}'>{label}</text>")
        x += 30 + 7 * len(label)
    return out


def fault_matrix(rows: list[dict[str, Any]], path: Path) -> None:
    # one dot per trial: does it end with exactly the right money outcome
    faults = list(dict.fromkeys(r["fault"] for r in rows))
    names = {
        "FP2": "crash after prepare",
        "FP3": "crash before the send",
        "FP4": "crash after the provider commits",
        "FP5": "provider commits late",
        "FP6": "same event delivered twice",
        "FP7": "stale worker comes back",
        "FP9": "rate limited (429)",
        "FP10": "balance taken before the locks",
        "lost_response": "response lost after commit",
        "reset_before_send": "connection reset before send",
    }
    left, top, dx, dy = 290, 78, 26, 30
    body = _legend(24, 62, [("recovered", GREEN), ("not recovered", RED)])
    body.append(f"<text x='{left}' y='{top - 4}' font-size='12' fill='{MUTED}'>with the fault</text>")
    body.append(f"<text x='{left + 11 * dx + 20}' y='{top - 4}' font-size='12' fill='{MUTED}'>no-fault control</text>")
    for i, f in enumerate(faults):
        y = top + 18 + i * dy
        body.append(f"<text x='24' y='{y + 5}' font-size='13.5' fill='{INK}'>{names.get(f, f)}</text>")
        for j, faulted in enumerate((True, False)):
            trial = [r for r in rows if r["fault"] == f and r["faulted"] is faulted]
            for k, r in enumerate(trial):
                cx = left + 8 + k * dx + j * (11 * dx + 20)
                body.append(f"<circle cx='{cx}' cy='{y}' r='8' fill='{GREEN if r['recovered'] else RED}'/>")
    h = top + 30 + len(faults) * dy
    ok = sum(r["recovered"] for r in rows if r["faulted"])
    n = sum(r["faulted"] for r in rows)
    path.write_text(_svg(left + 22 * dx + 70, h, body, f"Fault recovery: {ok}/{n} trials, plus {n} controls"))


def outcome_bars(arms: list[tuple[str, list[list[dict[str, Any]]]]], path: Path, title: str) -> None:
    # per arm, each case across every pass: right each time, right only sometimes, never right, closed wrongly
    left, top, bar_w, row_h = 190, 92, 560, 46
    k = max(len(runs) for _, runs in arms)
    legend = [("right every pass" if k > 1 else "right outcome", GREEN)]
    legend += [("right in some passes", LIGHT)] if k > 1 else []
    legend += [("missed, left open", AMBER), ("closed wrongly" + (" at least once" if k > 1 else ""), RED)]
    body = _legend(24, 64, legend)
    for i, (name, runs) in enumerate(arms):
        per = across(runs).values()
        n = len(per)
        wrong = sum(any(r["wrong_close"] for r in v) for v in per)
        every = sum(all(r["passed"] for r in v) for v in per)
        some = sum(
            any(r["passed"] for r in v) and not all(r["passed"] for r in v)
            for v in per
            if not any(r["wrong_close"] for r in v)
        )
        rest = n - every - some - wrong
        y = top + i * row_h
        body.append(f"<text x='24' y='{y + 19}' font-size='14' fill='{INK}'>{name}</text>")
        x = left
        for count, color in ((every, GREEN), (some, LIGHT), (rest, AMBER), (wrong, RED)):
            w = bar_w * count / n if n else 0
            if w:
                body.append(f"<rect x='{x:.1f}' y='{y}' width='{w:.1f}' height='26' fill='{color}'/>")
            x += w
        passes = sorted(sum(r["passed"] for r in rows) for rows in runs)
        note = f"<tspan font-weight='600'>{every}/{n}</tspan>"
        if passes[0] != passes[-1]:
            note += f"<tspan fill='{MUTED}'>  ({passes[0]} to {passes[-1]} a pass)</tspan>"
        body.append(f"<text x='{left + bar_w + 12}' y='{y + 19}' font-size='13.5' fill='{INK}'>{note}</text>")
    path.write_text(_svg(left + bar_w + 180, top + len(arms) * row_h + 10, body, title))


def family_grid(arms: list[tuple[str, list[list[dict[str, Any]]]]], path: Path, title: str) -> None:
    # per family, per arm: cases right every pass, as a shaded cell with the count in it
    fams = sorted({r["family"] for _, runs in arms for rows in runs for r in rows})
    left, top, cw, ch = 210, 84, 150, 34
    body = []
    for j, (name, _) in enumerate(arms):
        body.append(
            f"<text x='{left + j * cw + cw / 2}' y='{top - 10}' font-size='13' text-anchor='middle' fill='{MUTED}'>{name}</text>"
        )
    for i, fam in enumerate(fams):
        y = top + i * ch
        body.append(f"<text x='24' y='{y + 22}' font-size='13.5' fill='{INK}'>{fam.replace('_', ' ')}</text>")
        for j, (_, runs) in enumerate(arms):
            mine = [v for v in across(runs).values() if v[0]["family"] == fam]
            ok, n = sum(all(r["passed"] for r in v) for v in mine), len(mine)
            share = ok / n if n else 0
            color = GREEN if share >= 0.9 else AMBER if share >= 0.5 else RED
            x = left + j * cw
            body.append(
                f"<rect x='{x + 6}' y='{y + 4}' width='{cw - 12}' height='{ch - 8}' rx='6' fill='{color}' fill-opacity='{0.35 + 0.5 * share:.2f}'/>"
            )
            body.append(
                f"<text x='{x + cw / 2}' y='{y + 22}' font-size='13.5' text-anchor='middle' fill='{INK}'>{ok}/{n}</text>"
            )
    path.write_text(_svg(left + len(arms) * cw + 24, top + len(fams) * ch + 16, body, title))


def duplicate_bars(rows: list[dict[str, Any]], path: Path, model: str | None = None) -> None:
    # b3: how often the naive agent's retries turn into a second refund, per provider behaviour
    labels = {
        "none": "provider behaves",
        "fail_500": "500 before doing anything",
        "throttle": "rate limited (429)",
        "commit_then_500": "refund done, then a 500",
    }
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["fault"]].append(r)
    left, top, bar_w, row_h = 230, 92, 420, 46
    body = _legend(24, 64, [("one refund", GREEN), ("refunded twice", RED), ("no refund", GREY)])
    for i, f in enumerate(labels):
        rs = by.get(f, [])
        n = len(rs)
        dup = sum(r["duplicate"] for r in rs)
        none = sum(r["refunds"] == 0 for r in rs)
        one = n - dup - none
        y = top + i * row_h
        body.append(f"<text x='24' y='{y + 19}' font-size='14' fill='{INK}'>{labels[f]}</text>")
        x = left
        for count, color in ((one, GREEN), (dup, RED), (none, GREY)):
            w = bar_w * count / n if n else 0
            if w:
                body.append(f"<rect x='{x:.1f}' y='{y}' width='{w:.1f}' height='26' fill='{color}'/>")
            x += w
        note = f"{one + dup}/{n} refunded, {dup} twice"
        body.append(f"<text x='{left + bar_w + 12}' y='{y + 19}' font-size='13.5' fill='{INK}'>{note}</text>")
    title = f"Naive agent with a direct refund tool ({model})" if model else "Naive agent with a direct refund tool"
    path.write_text(_svg(left + bar_w + 190, top + len(labels) * row_h + 10, body, title))


def from_results(
    root: Path, out: Path, val_runs: dict[str, list[Path]] | None = None, b3: Path | None = None
) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    made = []
    e3 = root / "faults" / "e3.jsonl"
    if e3.exists():
        rows = [json.loads(line) for line in e3.read_text().splitlines() if line.strip()]
        fault_matrix(rows, out / "fault-recovery.svg")
        made.append(out / "fault-recovery.svg")
    if val_runs:
        arms = [(name, [load(d)[1] for d in dirs]) for name, dirs in val_runs.items()]
        n = len(across(arms[0][1]))
        k = max(len(runs) for _, runs in arms)
        passes = {2: "two", 3: "three"}.get(k, str(k))
        title = f"Validation split, {n} cases" + (f", {passes} passes each" if k > 1 else "")
        outcome_bars(arms, out / "validation.svg", title)
        family_grid(
            arms, out / "families.svg", "Right every pass, by case family" if k > 1 else "Right outcome by case family"
        )
        made += [out / "validation.svg", out / "families.svg"]
    if b3 is not None:
        meta = json.loads((b3 / "manifest.json").read_text()) if (b3 / "manifest.json").exists() else {}
        model = (meta.get("model") or "").removeprefix("ollama:") or None
        duplicate_bars(json.loads((b3 / "results.json").read_text()), out / "naive-agent.svg", model)
        made.append(out / "naive-agent.svg")
    return made
