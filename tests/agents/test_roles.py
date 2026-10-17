from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY, ROLE_TOOLS, ROLES
from recon_mcp.server import TOOLS

# spec §7 allowed callers, agent-reachable read tools only
SPEC_CALLERS = {
    "get_case": {INVESTIGATOR, COORDINATOR},
    "get_order": {INVESTIGATOR},
    "list_payments": {INVESTIGATOR},
    "get_payment": {INVESTIGATOR},
    "list_refunds": {INVESTIGATOR},
    "get_refund": {INVESTIGATOR},
    "search_policy": {POLICY},
    "get_policy_section": {POLICY},
    "compute_refund_quote": {COORDINATOR},
}


def test_role_tools_match_spec_callers():
    assert set(SPEC_CALLERS) == set(TOOLS)
    for role in ROLES:
        assert set(ROLE_TOOLS[role]) == {t for t, who in SPEC_CALLERS.items() if role in who}, role
