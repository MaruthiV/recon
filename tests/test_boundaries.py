import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKGS = {
    "recon_domain": "packages/domain/recon_domain",
    "recon_storage": "packages/storage/recon_storage",
    "provider_sim": "apps/provider_sim/provider_sim",
    "recon_worker": "apps/worker/recon_worker",
    "recon_mcp": "apps/mcp/recon_mcp",
    "recon_agents": "agents/recon_agents",
    "recon_verifier": "evals/verifier/recon_verifier",
    "recon_fixtures": "evals/fixtures/recon_fixtures",
}
# who may not import whom
FORBIDDEN = {
    "provider_sim": {
        "recon_domain",
        "recon_storage",
        "recon_worker",
        "recon_mcp",
        "recon_agents",
        "recon_verifier",
        "recon_fixtures",
    },
    "recon_verifier": {
        "recon_domain",
        "recon_storage",
        "recon_worker",
        "recon_mcp",
        "recon_agents",
        "provider_sim",
        "recon_fixtures",
    },
    "recon_domain": {
        "recon_storage",
        "recon_worker",
        "recon_mcp",
        "recon_agents",
        "provider_sim",
        "recon_verifier",
        "sqlalchemy",
        "httpx",
        "fastapi",
        "google",
        "mcp",
    },
    "recon_storage": {
        "recon_worker",
        "recon_mcp",
        "recon_agents",
        "provider_sim",
        "recon_verifier",
        "httpx",
        "fastapi",
    },
    "recon_worker": {"provider_sim", "recon_mcp", "recon_verifier", "recon_fixtures"},
    # mcp-read can't reach the worker (ledger, write key) or the agents
    "recon_mcp": {"recon_worker", "recon_agents", "provider_sim", "recon_verifier", "recon_fixtures", "google"},
    # agents only see tools, never the db or the money path
    "recon_agents": {
        "recon_storage",
        "recon_worker",
        "recon_mcp",
        "provider_sim",
        "recon_verifier",
        "recon_fixtures",
        "sqlalchemy",
        "httpx",
    },
}


def imports(pkg_dir: Path) -> set[str]:
    out = set()
    for f in pkg_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                out |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


@pytest.mark.parametrize("pkg", sorted(FORBIDDEN))
def test_import_boundaries(pkg):
    bad = imports(ROOT / PKGS[pkg]) & FORBIDDEN[pkg]
    assert not bad, f"{pkg} imports {bad}"
