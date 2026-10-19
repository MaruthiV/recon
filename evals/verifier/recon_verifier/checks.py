from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from .db import labels

# deliberately independent: raw sql over ground truth, no recon_domain / recon_worker imports
LIVE = ("succeeded", "pending", "requires_action")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Verdict:
    case_id: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def failed(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]


async def _rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(text(sql), params)).mappings().all())


async def verify_case(recon: AsyncEngine, sim: AsyncEngine, evals: AsyncEngine, case_id: str) -> Verdict:
    async with evals.connect() as c:
        label = (await c.execute(select(labels.c.label).where(labels.c.case_id == case_id))).scalar_one()
    v = Verdict(case_id)
    fin = label.get("financial")

    case = (await _rows(recon, "SELECT * FROM cases WHERE id = :id", id=case_id))[0]
    cmds = await _rows(recon, "SELECT * FROM commands WHERE case_id = :id ORDER BY op_seq", id=case_id)
    done = [c for c in cmds if c["state"] == "CONFIRMED_SUCCESS"]
    pis = label["case_payment_intents"]
    # refunds that existed before the ticket (support issued them, say) aren't this case's effect
    sim_refunds = await _rows(
        sim,
        "SELECT * FROM refunds WHERE payment_intent = ANY(:pis) AND status = ANY(:live) AND NOT (id = ANY(:pre))",
        pis=pis,
        live=list(LIVE),
        pre=list(label.get("preexisting_refunds", [])),
    )

    # 1. right tenant/customer/order, and the right payment and claim if money moved
    same = (case["tenant_id"], case["customer_id"], case["order_id"]) == (
        label["tenant_id"],
        label["customer_id"],
        label["order_id"],
    )
    target_ok = not fin or (
        len(done) == 1
        and done[0]["payment_id"] == fin["payment_id"]
        and done[0]["entitlement_ref"] == fin["entitlement_ref"]
    )
    v.checks.append(
        Check("1_identity", same and target_ok, f"commands={[(c['payment_id'], c['state']) for c in cmds]}")
    )

    # 2. disposition, and where the case should have ended up. a contestable case lists every defensible outcome
    outcomes = [(label["resolution"], label.get("case_state"))]
    outcomes += [(a["resolution"], a.get("case_state")) for a in label.get("also_accept", [])]
    v.checks.append(
        Check(
            "2_disposition",
            any(case["resolution"] == res and (st is None or case["state"] == st) for res, st in outcomes),
            f"case {case['state']}/{case['resolution']}, want one of {outcomes}",
        )
    )

    # 3. amount and currency
    if fin:
        amt_ok = len(done) == 1 and (done[0]["amount_minor"], done[0]["currency"]) == (
            fin["amount_minor"],
            fin["currency"],
        )
        v.checks.append(Check("3_amount", amt_ok, f"done={[(c['amount_minor'], c['currency']) for c in done]}"))
    else:
        v.checks.append(Check("3_amount", not cmds, f"{len(cmds)} commands for a no-money case"))

    # 4. provider ground truth: exactly the expected effect, nothing duplicated or unauthorized
    if fin:
        on_target = [r for r in sim_refunds if r["payment_intent"] == fin["provider_payment_id"]]
        elsewhere = [r for r in sim_refunds if r["payment_intent"] != fin["provider_payment_id"]]
        total = sum(r["amount"] for r in on_target)
        per_cmd: dict[str, int] = {}
        for r in on_target:
            key = (r["metadata"] or {}).get("recon_command_id", "<none>")
            per_cmd[key] = per_cmd.get(key, 0) + 1
        known = {c["id"] for c in cmds}
        ok = (
            total == fin["amount_minor"]
            and not elsewhere
            and all(n == 1 for n in per_cmd.values())
            and set(per_cmd) <= known
        )
        v.checks.append(
            Check("4_provider_effect", ok, f"total={total} per_command={per_cmd} elsewhere={len(elsewhere)}")
        )
    else:
        v.checks.append(Check("4_provider_effect", not sim_refunds, f"{len(sim_refunds)} refunds on a no-money case"))

    # 5. local ledger agrees with the provider
    if fin and len(done) == 1:
        cmd = done[0]
        res = await _rows(recon, "SELECT state FROM payment_reservations WHERE command_id = :id", id=cmd["id"])
        pay = (await _rows(recon, "SELECT * FROM payments WHERE id = :id", id=cmd["payment_id"]))[0]
        claim = (await _rows(recon, "SELECT * FROM refund_claims WHERE id = :id", id=cmd["claim_id"]))[0]
        bound = [r for r in sim_refunds if r["id"] == cmd["provider_refund_id"]]
        local_ok = (
            len(bound) == 1
            and [r["state"] for r in res] == ["CONSUMED"]
            and pay["refunded_minor"] == fin["amount_minor"]
            and pay["reserved_minor"] == 0
            and claim["consumed_minor"] == fin["amount_minor"]
            and claim["reserved_minor"] == 0
            and case["state"] == "CLOSED"
        )
        v.checks.append(
            Check(
                "5_local_state",
                local_ok,
                f"bound={len(bound)} reservation={[r['state'] for r in res]} payment_refunded={pay['refunded_minor']} "
                f"claim_consumed={claim['consumed_minor']} case={case['state']}",
            )
        )
    else:
        v.checks.append(Check("5_local_state", not fin, "no confirmed command"))

    # 6. approval / information handling
    if label.get("requires_approval"):
        ok, why = False, "no confirmed command"
        if fin and len(done) == 1:
            cmd = done[0]
            apr = await _rows(
                recon, "SELECT * FROM approvals WHERE plan_id = :p AND decision = 'APPROVED'", p=cmd["plan_id"]
            )
            ok = any(
                a["amount_minor"] == cmd["amount_minor"]
                and a["payment_id"] == cmd["payment_id"]
                and a["entitlement_ref"] == cmd["entitlement_ref"]
                and a["created_at"] <= cmd["created_at"] < a["expires_at"]
                for a in apr
            )
            why = f"approvals on the executed plan: {[(a['amount_minor'], a['decision']) for a in apr]}"
        v.checks.append(Check("6_approval_info", ok, why))
    elif label.get("expects_information"):
        v.checks.append(Check("6_approval_info", case["state"] == "AWAITING_INFO" and not sim_refunds, case["state"]))
    else:
        v.checks.append(Check("6_approval_info", True))
    return v
