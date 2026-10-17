import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# money paths: no float literals, no float() anywhere
MONEY_PACKAGES = [
    "packages/domain/recon_domain",
    "packages/storage/recon_storage",
    "apps/provider_sim/provider_sim",
    "apps/worker/recon_worker",
    "packages/provider/recon_provider",
    "apps/mcp/recon_mcp",
    "agents/recon_agents",
    "evals/verifier/recon_verifier",
]
# timeouts and clock math are seconds, not money
ALLOWED = {"apps/worker/recon_worker/ledger.py"}


def offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            out.append(f"{path.name}:{node.lineno} float literal {node.value}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
            out.append(f"{path.name}:{node.lineno} float() call")
    return out


@pytest.mark.parametrize("pkg", MONEY_PACKAGES)
def test_no_floats_in_money_paths(pkg):
    found = []
    for f in sorted((ROOT / pkg).rglob("*.py")):
        if str(f.relative_to(ROOT)) not in ALLOWED and "migrations" not in f.parts:
            found += offenders(f)
    assert not found, found
