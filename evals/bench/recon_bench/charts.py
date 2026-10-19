from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .report import load

# hand-rolled svg so the readme charts come straight from result files, no plotting deps
GREEN, AMBER, RED, GREY, INK, MUTED = "#2f855a", "#c27c0e", "#c53030", "#d7d7d2", "#1f2328", "#6b6f76"
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


def outcome_bars(arms: list[tuple[str, list[dict[str, Any]]]], path: Path, title: str) -> None:
    # per arm: right answer, sent to a person, wrong answer to the customer
    left, top, bar_w, row_h = 190, 92, 560, 46
    body = _legend(24, 64, [("right outcome", GREEN), ("missed, left open", AMBER), ("wrong answer, closed", RED)])
    for i, (name, rows) in enumerate(arms):
        n = len(rows)
        ok = sum(r["passed"] for r in rows)
        wrong = sum(r["wrong_close"] for r in rows)
        rest = n - ok - wrong
        y = top + i * row_h
        body.append(f"<text x='24' y='{y + 19}' font-size='14' fill='{INK}'>{name}</text>")
        x = left
        for count, color in ((ok, GREEN), (rest, AMBER), (wrong, RED)):
            w = bar_w * count / n if n else 0
            if w:
                body.append(f"<rect x='{x:.1f}' y='{y}' width='{w:.1f}' height='26' fill='{color}'/>")
            x += w
        body.append(f"<text x='{left + bar_w + 12}' y='{y + 19}' font-size='13.5' fill='{INK}'>{ok}/{n}</text>")
    path.write_text(_svg(left + bar_w + 80, top + len(arms) * row_h + 10, body, title))


def family_grid(arms: list[tuple[str, list[dict[str, Any]]]], path: Path, title: str) -> None:
    # per family, per arm: share right, as a shaded cell with the count in it
    fams = sorted({r["family"] for _, rows in arms for r in rows})
    left, top, cw, ch = 210, 84, 150, 34
    body = []
    for j, (name, _) in enumerate(arms):
        body.append(
            f"<text x='{left + j * cw + cw / 2}' y='{top - 10}' font-size='13' text-anchor='middle' fill='{MUTED}'>{name}</text>"
        )
    for i, fam in enumerate(fams):
        y = top + i * ch
        body.append(f"<text x='24' y='{y + 22}' font-size='13.5' fill='{INK}'>{fam.replace('_', ' ')}</text>")
        for j, (_, rows) in enumerate(arms):
            mine = [r for r in rows if r["family"] == fam]
            ok, n = sum(r["passed"] for r in mine), len(mine)
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


def duplicate_bars(rows: list[dict[str, Any]], path: Path) -> None:
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
        body.append(f"<text x='{left + bar_w + 12}' y='{y + 19}' font-size='13.5' fill='{INK}'>{dup}/{n} twice</text>")
    path.write_text(
        _svg(left + bar_w + 110, top + len(labels) * row_h + 10, body, "Naive agent with a direct refund tool")
    )


def from_results(root: Path, out: Path, val_runs: dict[str, Path] | None = None, b3: Path | None = None) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    made = []
    e3 = root / "faults" / "e3.jsonl"
    if e3.exists():
        rows = [json.loads(line) for line in e3.read_text().splitlines() if line.strip()]
        fault_matrix(rows, out / "fault-recovery.svg")
        made.append(out / "fault-recovery.svg")
    if val_runs:
        arms = [(name, load(d)[1]) for name, d in val_runs.items()]
        n = len(arms[0][1])
        outcome_bars(arms, out / "validation.svg", f"Validation split, {n} cases")
        family_grid(arms, out / "families.svg", "Right outcome by case family")
        made += [out / "validation.svg", out / "families.svg"]
    if b3 is not None:
        duplicate_bars(json.loads((b3 / "results.json").read_text()), out / "naive-agent.svg")
        made.append(out / "naive-agent.svg")
    return made
