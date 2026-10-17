INVESTIGATOR = "payment_investigator"
POLICY = "policy_specialist"
COORDINATOR = "coordinator"
ROLES = (INVESTIGATOR, POLICY, COORDINATOR)

# what each agent may call, the mcp server enforces this from the run token
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    INVESTIGATOR: ("get_case", "get_order", "list_payments", "get_payment", "list_refunds", "get_refund"),
    POLICY: ("search_policy", "get_policy_section"),
    COORDINATOR: ("get_case", "compute_refund_quote", "get_policy_section"),
}
