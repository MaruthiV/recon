INVESTIGATOR = "payment_investigator"
POLICY = "policy_specialist"
COORDINATOR = "coordinator"
ROLES = (INVESTIGATOR, POLICY, COORDINATOR)
# b2 baseline: one agent that does all three jobs
SINGLE = "single_agent"

# what each agent may call (spec §7 callers), the mcp server enforces this from the run token
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    INVESTIGATOR: ("get_case", "get_order", "list_payments", "get_payment", "list_refunds", "get_refund"),
    POLICY: ("search_policy", "get_policy_section"),
    COORDINATOR: ("get_case", "compute_refund_quote"),
}
ROLE_TOOLS[SINGLE] = tuple(dict.fromkeys(t for r in ROLES for t in ROLE_TOOLS[r]))


def roles_of(models) -> tuple[str, ...]:
    # the models mapping says which shape this is
    return (SINGLE,) if SINGLE in models else ROLES


def planner(roles: tuple[str, ...]) -> str:
    return roles[-1]
