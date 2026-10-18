from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from recon_domain.plan import ResolutionPlan
from recon_domain.rules import ask_for_order, rules_plan
from recon_storage.repos import NotFound, get_case, load_case_facts


async def plan_by_rules(s: AsyncSession, tenant_id: str, case_id: str, now: datetime) -> ResolutionPlan:
    try:
        return rules_plan(await load_case_facts(s, tenant_id, case_id, now))
    except NotFound:
        # a case with no order has no facts to reason over yet
        case = await get_case(s, tenant_id, case_id)
        return ask_for_order(case.id, case.version)
