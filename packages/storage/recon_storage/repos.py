from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from recon_domain.money import Currency, Money
from recon_domain.policy import AuthorityClass, OperationalLimits, PolicyVersion, RefundTerms
from recon_domain.proration import Rounding
from recon_domain.states import CaseState, check_case
from recon_domain.validation import CaseFacts, ClaimFact, PaymentFact

from .db import new_id
from .models import AuditEvent, Case, Order, Payment, PolicyDocument, PolicyRule, RefundClaim


class NotFound(LookupError):
    pass


def payment_fact(p: Payment) -> PaymentFact:
    return PaymentFact(
        id=p.id,
        order_id=p.order_id,
        customer_id=p.customer_id,
        provider_payment_id=p.provider_payment_id,
        amount=Money(p.amount_minor, Currency(p.currency)),
        status=p.status,
        captured_at=p.captured_at,
        version=p.version,
        refunded_minor=p.refunded_minor,
        reserved_minor=p.reserved_minor,
    )


async def get_case(s: AsyncSession, tenant_id: str, case_id: str) -> Case:
    case = (await s.execute(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))).scalar_one_or_none()
    if case is None:
        raise NotFound(case_id)
    return case


async def load_case_facts(s: AsyncSession, tenant_id: str, case_id: str, now: datetime) -> CaseFacts:
    case = await get_case(s, tenant_id, case_id)
    if case.order_id is None:
        raise NotFound(f"{case_id} has no order")
    order = (await s.execute(select(Order).where(Order.tenant_id == tenant_id, Order.id == case.order_id))).scalar_one()
    pays = (
        (
            await s.execute(
                select(Payment)
                .where(Payment.tenant_id == tenant_id, Payment.order_id == order.id)
                .order_by(Payment.captured_at, Payment.id)
            )
        )
        .scalars()
        .all()
    )
    claims = (
        (
            await s.execute(
                select(RefundClaim).where(
                    RefundClaim.tenant_id == tenant_id, RefundClaim.payment_id.in_([p.id for p in pays])
                )
            )
        )
        .scalars()
        .all()
    )
    rows = (
        await s.execute(
            select(PolicyDocument, PolicyRule)
            .join(PolicyRule, PolicyRule.policy_document_id == PolicyDocument.id)
            .where(PolicyDocument.tenant_id == tenant_id)
        )
    ).all()

    terms: list[RefundTerms] = []
    limits: list[OperationalLimits] = []
    for doc, rule in rows:
        pv = PolicyVersion(
            doc.id, AuthorityClass(doc.authority_class), doc.version, doc.effective_from, doc.effective_to
        )
        if pv.authority_class is AuthorityClass.REFUND_TERMS:
            terms.append(
                RefundTerms(
                    pv,
                    rule.refund_window_days or 0,
                    bool(rule.duplicate_refund_allowed),
                    Rounding(rule.rounding_mode or "HALF_EVEN"),
                )
            )
        elif pv.authority_class is AuthorityClass.OPERATIONAL_LIMITS:
            limits.append(OperationalLimits(pv, rule.auto_approve_limit_minor or 0))

    return CaseFacts(
        tenant_id=tenant_id,
        case_id=case.id,
        case_version=case.version,
        customer_id=case.customer_id,
        order_id=order.id,
        order_placed_at=order.placed_at,
        order_currency=Currency(order.currency),
        payments=tuple(payment_fact(p) for p in pays),
        claims={
            c.entitlement_ref: ClaimFact(c.id, c.entitlement_ref, c.permitted_minor, c.reserved_minor, c.consumed_minor)
            for c in claims
        },
        refund_terms=tuple(terms),
        limits=tuple(limits),
        now=now,
    )


async def move_case(
    s: AsyncSession,
    tenant_id: str,
    case_id: str,
    src: CaseState,
    dst: CaseState,
    now: datetime,
    resolution: str | None = None,
) -> None:
    check_case(src, dst)
    values: dict[str, Any] = {"state": dst.value, "updated_at": now}
    if resolution is not None:
        values["resolution"] = resolution
    res = await s.execute(
        update(Case).where(Case.tenant_id == tenant_id, Case.id == case_id, Case.state == src.value).values(**values)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        raise NotFound(f"case {case_id} not in {src}")


def audit(
    s: AsyncSession,
    tenant_id: str,
    kind: str,
    now: datetime,
    *,
    case_id: str | None = None,
    command_id: str | None = None,
    actor: str = "system",
    **data: Any,
) -> None:
    s.add(
        AuditEvent(
            tenant_id=tenant_id,
            case_id=case_id,
            command_id=command_id,
            actor=actor,
            kind=kind,
            data=data,
            created_at=now,
        )
    )


__all__ = ["NotFound", "audit", "get_case", "load_case_facts", "move_case", "new_id", "payment_fact"]
