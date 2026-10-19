from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from recon_fixtures import families
from recon_fixtures.families import FAMILIES, TENANTS, Spec, limit_sections, make_specs, terms_sections

# per family, generator seed. held_out is frozen before any tuning and isn't run until m6
SPLITS = {"dev": (20, 1), "val": (10, 2), "held_out": (50, 3)}
FROZEN = Path(__file__).resolve().parents[2] / "manifests" / "frozen.json"


def specs(split: str) -> list[Spec]:
    per, seed = SPLITS[split]
    return make_specs(split, per_family=per, seed=seed)


def corpus() -> dict[str, Any]:
    return {
        key: {
            "approve_limit": t.approve_limit,
            "limits": limit_sections(t.approve_limit),
            "terms": [{**asdict(term), "sections": terms_sections(term)} for term in t.terms],
        }
        for key, t in TENANTS.items()
    }


def manifest(split: str) -> dict[str, Any]:
    # the generator source is in the hash too, so a label rule change can't slip past a frozen split
    return {
        "split": split,
        "families": FAMILIES,
        "generator_sha256": hashlib.sha256(Path(families.__file__).read_bytes()).hexdigest(),
        "corpus": corpus(),
        "cases": [asdict(s) for s in specs(split)],
    }


def digest(split: str) -> str:
    blob = json.dumps(manifest(split), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def frozen() -> dict[str, str]:
    return json.loads(FROZEN.read_text())


if __name__ == "__main__":
    # python -m recon_fixtures.manifests: prints hashes only, never held-out contents
    print(json.dumps({split: digest(split) for split in SPLITS}, indent=2))
