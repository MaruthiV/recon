import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from recon_domain.connector import Clock, ProviderRefund, ReadFailed, RefundReader, SystemClock
from recon_domain.plan import ReasonCode
from recon_domain.policy import AuthorityClass, PolicyVersion
from recon_domain.tokens import MCP_READ, RUN_TOKEN_HEADER, BadToken, verify
from recon_domain.validation import quote
from recon_storage.db import make_engine, new_id, sessions
from recon_storage.models import (
    Case,
    CaseMessage,
    Order,
    OrderLine,
    Payment,
    PolicyDocument,
    Return,
    Subscription,
)
from recon_storage.repos import NotFound, load_case_facts

from .views import (
    CaseView,
    LineView,
    MessageView,
    OrderView,
    PaymentList,
    PaymentView,
    PolicySearch,
    PolicySection,
    QuoteView,
    RefundList,
    RefundView,
    Result,
    ReturnView,
    Status,
    SubscriptionView,
    ToolError,
)

TOOLS = (
    "get_case",
    "get_order",
    "list_payments",
    "get_payment",
    "list_refunds",
    "get_refund",
    "search_policy",
    "get_policy_section",
    "compute_refund_quote",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
MAX_HITS = 5


@dataclass(frozen=True)
class Call:
    tenant_id: str
    case_id: str
    correlation_id: str
    observed_at: datetime

    def found(self, data: Any, source_ids: list[str], version: str | None = None) -> Result[Any]:
        return Result(
            status=Status.FOUND,
            source_ids=source_ids,
            source_version=version,
            observed_at=self.observed_at,
            correlation_id=self.correlation_id,
            data=data,
        )

    def none(self, why: str, source_ids: list[str] | None = None) -> Result[Any]:
        return Result(
            status=Status.NONE_FOUND,
            source_ids=source_ids or [],
            observed_at=self.observed_at,
            correlation_id=self.correlation_id,
            error=ToolError(code="not_found", message=why, retryable=False),
        )

    def failed(self, code: str, message: str, retryable: bool, source_ids: list[str] | None = None) -> Result[Any]:
        return Result(
            status=Status.QUERY_FAILED,
            source_ids=source_ids or [],
            observed_at=self.observed_at,
            correlation_id=self.correlation_id,
            error=ToolError(code=code, message=message, retryable=retryable),
        )


def payment_view(p: Payment) -> PaymentView:
    return PaymentView(
        payment_id=p.id,
        ref=f"payment:{p.id}:v{p.version}",
        version=p.version,
        order_id=p.order_id,
        provider_payment_id=p.provider_payment_id,
        amount_minor=p.amount_minor,
        currency=p.currency,
        status=p.status,
        captured_at=p.captured_at,
        refunded_minor=p.refunded_minor,
        reserved_minor=p.reserved_minor,
    )


def refund_view(r: ProviderRefund, payment_id: str) -> RefundView:
    return RefundView(
        refund_id=r.id,
        payment_id=payment_id,
        provider_payment_id=r.payment_intent,
        amount_minor=r.amount_minor,
        currency=r.currency,
        status=r.status,
        recon_command_id=r.metadata.get("recon_command_id"),
        failure_reason=r.failure_reason,
    )


class ReadService:
    """mcp-read: run token checked per request, tenant and case come only from the token"""

    def __init__(self, inner: Any, engine: AsyncEngine, key: bytes, clock: Clock) -> None:
        self.inner = inner
        self.engine = engine
        self.key = key
        self.clock = clock

    def lifespan(self):
        return self.inner.router.lifespan_context(self.inner)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        try:
            claims = verify(headers.get(RUN_TOKEN_HEADER, ""), self.key, MCP_READ, self.clock.now())
        except BadToken as e:
            body = json.dumps({"error": f"run token rejected: {e}"}).encode()
            await send(
                {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]}
            )
            return await send({"type": "http.response.body", "body": body})
        scope.setdefault("state", {})["claims"] = claims
        scope["state"]["correlation_id"] = headers.get("x-request-id") or new_id("req")
        await self.inner(scope, receive, send)


def create_app(
    dsn: str,
    key: bytes,
    reader_for: Callable[[str], RefundReader],
    clock: Clock | None = None,
    host: str = "127.0.0.1",
) -> ReadService:
    clock = clock or SystemClock()
    engine = make_engine(dsn)
    db = sessions(engine)
    server = MCPServer("recon-read")

    def begin(ctx: Context, tool: str) -> tuple[Call, Result[Any] | None]:
        req = ctx.request_context.request
        state = getattr(req, "state", None)
        claims = getattr(state, "claims", None) or {}
        call = Call(
            claims.get("tenant", ""),
            claims.get("case_id", ""),
            getattr(state, "correlation_id", None) or new_id("req"),
            clock.now(),
        )
        if not claims or tool not in claims.get("tools", ()):
            return call, call.failed("not_allowed", f"{tool} isn't allowed for this run", False)
        return call, None

    async def scoped_case(s: AsyncSession, call: Call) -> Case | None:
        q = select(Case).where(Case.tenant_id == call.tenant_id, Case.id == call.case_id)
        return (await s.execute(q)).scalar_one_or_none()

    async def scoped_payment(s: AsyncSession, call: Call, case: Case, payment_id: str) -> Payment | None:
        q = select(Payment).where(
            Payment.tenant_id == call.tenant_id, Payment.id == payment_id, Payment.customer_id == case.customer_id
        )
        return (await s.execute(q)).scalar_one_or_none()

    async def applicable_docs(s: AsyncSession, call: Call, case: Case) -> list[PolicyDocument]:
        placed = None
        if case.order_id is not None:
            placed = (
                await s.execute(
                    select(Order.placed_at).where(Order.tenant_id == call.tenant_id, Order.id == case.order_id)
                )
            ).scalar_one_or_none()
        docs = (await s.execute(select(PolicyDocument).where(PolicyDocument.tenant_id == call.tenant_id))).scalars()
        out = []
        for d in docs:
            pv = PolicyVersion(d.id, AuthorityClass(d.authority_class), d.version, d.effective_from, d.effective_to)
            # refund terms by purchase time, everything else by decision time
            at = placed if pv.authority_class is AuthorityClass.REFUND_TERMS else call.observed_at
            if at is not None and pv.applies_at(at):
                out.append(d)
        return out

    @server.tool(description="Read the current case and its messages.", annotations=READ_ONLY)
    async def get_case(ctx: Context) -> Result[CaseView]:
        call, denied = begin(ctx, "get_case")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            if case is None:
                return call.none("case not found")
            msgs = (
                await s.execute(
                    select(CaseMessage)
                    .where(CaseMessage.tenant_id == call.tenant_id, CaseMessage.case_id == case.id)
                    .order_by(CaseMessage.created_at, CaseMessage.id)
                )
            ).scalars()
            view = CaseView(
                case_id=case.id,
                case_version=case.version,
                state=case.state,
                subject=case.subject,
                customer_id=case.customer_id,
                order_id=case.order_id,
                created_at=case.created_at,
                messages=[MessageView(author=m.author, body=m.body, created_at=m.created_at) for m in msgs],
            )
        return call.found(view, [f"case:{case.id}"], f"v{case.version}")

    @server.tool(description="Read one order belonging to this case's customer.", annotations=READ_ONLY)
    async def get_order(order_id: str, ctx: Context) -> Result[OrderView]:
        call, denied = begin(ctx, "get_order")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            order = None
            if case is not None:
                q = select(Order).where(
                    Order.tenant_id == call.tenant_id, Order.id == order_id, Order.customer_id == case.customer_id
                )
                order = (await s.execute(q)).scalar_one_or_none()
            lines, rets, sub = [], [], None
            if order is not None:
                lines = list(
                    (
                        await s.execute(
                            select(OrderLine).where(
                                OrderLine.tenant_id == call.tenant_id, OrderLine.order_id == order.id
                            )
                        )
                    ).scalars()
                )
                rets = list(
                    (
                        await s.execute(
                            select(Return)
                            .where(Return.tenant_id == call.tenant_id, Return.order_id == order.id)
                            .order_by(Return.requested_at)
                        )
                    ).scalars()
                )
                sub = (
                    await s.execute(
                        select(Subscription).where(
                            Subscription.tenant_id == call.tenant_id, Subscription.order_id == order.id
                        )
                    )
                ).scalar_one_or_none()
        if order is None:
            return call.none(f"no order {order_id} for this customer")
        view = OrderView(
            order_id=order.id,
            version=order.version,
            customer_id=order.customer_id,
            total_minor=order.total_minor,
            currency=order.currency,
            placed_at=order.placed_at,
            lines=[
                LineView(
                    line_id=li.id,
                    sku=li.sku,
                    description=li.description,
                    kind=li.kind,
                    quantity=li.quantity,
                    unit_minor=li.unit_minor,
                )
                for li in lines
            ],
            returns=[
                ReturnView(
                    return_id=r.id,
                    ref=f"return:{r.id}:v{r.version}",
                    line_id=r.line_id,
                    quantity=r.quantity,
                    requested_at=r.requested_at,
                    received_at=r.received_at,
                )
                for r in rets
            ],
            subscription=sub
            and SubscriptionView(
                subscription_id=sub.id,
                ref=f"subscription:{sub.id}:v{sub.version}",
                plan_name=sub.plan_name,
                period_start=sub.period_start,
                period_end=sub.period_end,
                price_minor=sub.price_minor,
                cancelled_at=sub.cancelled_at,
            ),
        )
        return call.found(view, [f"order:{order.id}"], f"v{order.version}")

    @server.tool(description="List captured payments on one of this customer's orders.", annotations=READ_ONLY)
    async def list_payments(order_id: str, ctx: Context) -> Result[PaymentList]:
        call, denied = begin(ctx, "list_payments")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            if case is None:
                return call.none("case not found")
            pays = (
                await s.execute(
                    select(Payment)
                    .where(
                        Payment.tenant_id == call.tenant_id,
                        Payment.order_id == order_id,
                        Payment.customer_id == case.customer_id,
                    )
                    .order_by(Payment.captured_at, Payment.id)
                )
            ).scalars()
            views = [payment_view(p) for p in pays]
        if not views:
            return call.none(f"no payments on order {order_id} for this customer")
        return call.found(PaymentList(order_id=order_id, payments=views), [v.ref for v in views])

    @server.tool(description="Read one payment: amount, currency, status, version.", annotations=READ_ONLY)
    async def get_payment(payment_id: str, ctx: Context) -> Result[PaymentView]:
        call, denied = begin(ctx, "get_payment")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            p = await scoped_payment(s, call, case, payment_id) if case else None
        if p is None:
            return call.none(f"no payment {payment_id} for this customer")
        view = payment_view(p)
        return call.found(view, [view.ref], f"v{p.version}")

    @server.tool(
        description=(
            "List provider refunds for one payment. status=none_found means the provider has none; "
            "query_failed means the provider could not be read, which is not the same as no refunds."
        ),
        annotations=READ_ONLY,
    )
    async def list_refunds(payment_id: str, ctx: Context) -> Result[RefundList]:
        call, denied = begin(ctx, "list_refunds")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            p = await scoped_payment(s, call, case, payment_id) if case else None
        if p is None:
            return call.none(f"no payment {payment_id} for this customer")
        try:
            refunds = await reader_for(call.tenant_id).list_refunds(p.provider_payment_id)
        except ReadFailed as e:
            return call.failed("provider_unavailable", f"provider read failed: {e}", True, [f"payment:{p.id}"])
        if not refunds:
            return call.none(f"provider has no refunds for {payment_id}", [f"payment:{p.id}"])
        views = [refund_view(r, p.id) for r in refunds]
        return call.found(RefundList(payment_id=p.id, refunds=views), [f"refund:{v.refund_id}" for v in views])

    @server.tool(description="Read one provider refund by id.", annotations=READ_ONLY)
    async def get_refund(refund_id: str, ctx: Context) -> Result[RefundView]:
        call, denied = begin(ctx, "get_refund")
        if denied:
            return denied
        try:
            r = await reader_for(call.tenant_id).get_refund(refund_id)
        except ReadFailed as e:
            return call.failed("provider_unavailable", f"provider read failed: {e}", True)
        if r is None:
            return call.none(f"no refund {refund_id}")
        async with db() as s:
            case = await scoped_case(s, call)
            p = None
            if case is not None:
                q = select(Payment).where(
                    Payment.tenant_id == call.tenant_id,
                    Payment.provider_payment_id == r.payment_intent,
                    Payment.customer_id == case.customer_id,
                )
                p = (await s.execute(q)).scalar_one_or_none()
        if p is None:
            # same answer as a missing refund, don't leak other customers' refunds
            return call.none(f"no refund {refund_id}")
        return call.found(refund_view(r, p.id), [f"refund:{r.id}"])

    @server.tool(
        description="Search the tenant policy sections that apply to this case. Returns exact refs to cite.",
        annotations=READ_ONLY,
    )
    async def search_policy(query: str, ctx: Context) -> Result[PolicySearch]:
        call, denied = begin(ctx, "search_policy")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            if case is None:
                return call.none("case not found")
            docs = {d.id: d for d in await applicable_docs(s, call, case)}
            rows = (
                await s.execute(
                    text(
                        # any-term match, ranked, so long natural queries still hit
                        "WITH x AS (SELECT replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery AS q) "
                        "SELECT policy_document_id, section_id, heading, text FROM policy_chunks, x "
                        "WHERE tenant_id = :t AND policy_document_id = ANY(:docs) AND tsv @@ x.q "
                        "ORDER BY ts_rank(tsv, x.q) DESC, section_id LIMIT :k"
                    ),
                    {"q": query, "t": call.tenant_id, "docs": list(docs), "k": MAX_HITS},
                )
            ).all()
        hits = [
            PolicySection(
                ref=f"policy:{r.policy_document_id}:{r.section_id}",
                policy_id=r.policy_document_id,
                version=docs[r.policy_document_id].version,
                authority_class=docs[r.policy_document_id].authority_class,
                section_id=r.section_id,
                heading=r.heading,
                text=r.text,
                applies_to_case=True,
            )
            for r in rows
        ]
        data = PolicySearch(query=query, applicable_policy_ids=sorted(docs), hits=hits)
        if not hits:
            return Result(
                status=Status.NONE_FOUND,
                observed_at=call.observed_at,
                correlation_id=call.correlation_id,
                data=data,
                error=ToolError(code="not_found", message="no matching policy sections", retryable=False),
            )
        return call.found(data, [h.ref for h in hits])

    @server.tool(
        description="Fetch the exact text of one policy section by policy id and section id.", annotations=READ_ONLY
    )
    async def get_policy_section(policy_id: str, section_id: str, ctx: Context) -> Result[PolicySection]:
        call, denied = begin(ctx, "get_policy_section")
        if denied:
            return denied
        async with db() as s:
            case = await scoped_case(s, call)
            if case is None:
                return call.none("case not found")
            row = (
                await s.execute(
                    text(
                        "SELECT c.heading, c.text, d.version, d.authority_class FROM policy_chunks c "
                        "JOIN policy_documents d ON d.tenant_id = c.tenant_id AND d.id = c.policy_document_id "
                        "WHERE c.tenant_id = :t AND c.policy_document_id = :p AND c.section_id = :sec"
                    ),
                    {"t": call.tenant_id, "p": policy_id, "sec": section_id},
                )
            ).first()
            applicable = {d.id for d in await applicable_docs(s, call, case)}
        if row is None:
            return call.none(f"no section {section_id} in policy {policy_id}")
        ref = f"policy:{policy_id}:{section_id}"
        view = PolicySection(
            ref=ref,
            policy_id=policy_id,
            version=row.version,
            authority_class=row.authority_class,
            section_id=section_id,
            heading=row.heading,
            text=row.text,
            applies_to_case=policy_id in applicable,
        )
        return call.found(view, [ref], f"v{row.version}")

    @server.tool(
        description=(
            "Deterministic refund arithmetic for one payment under the applicable policy. "
            "For returns or cancellations pass subject_id (the return_id or subscription_id). "
            "Returns the most that may be refunded now, or why nothing can be."
        ),
        annotations=READ_ONLY,
    )
    async def compute_refund_quote(
        payment_id: str, reason_code: ReasonCode, ctx: Context, subject_id: str | None = None
    ) -> Result[QuoteView]:
        call, denied = begin(ctx, "compute_refund_quote")
        if denied:
            return denied
        async with db() as s:
            try:
                facts = await load_case_facts(s, call.tenant_id, call.case_id, call.observed_at)
            except NotFound:
                return call.none("case not found")
        q = quote(facts, payment_id, reason_code, subject_id)
        view = QuoteView(
            payment_id=payment_id,
            eligible=q.eligible,
            reasons=list(q.reasons),
            amount_minor=q.amount.amount_minor if q.amount else None,
            currency=q.amount.currency.value if q.amount else None,
            entitlement_ref=q.entitlement_ref,
            claim_id=q.claim_id,
            terms_policy_id=q.terms_policy_id,
        )
        return call.found(view, [f"payment:{payment_id}"])

    inner = server.streamable_http_app(stateless_http=True, json_response=True, host=host)
    return ReadService(inner, engine, key, clock)


def create_read_from_env() -> ReadService:
    import json
    import os
    from datetime import datetime

    import httpx

    from recon_domain.connector import FakeClock
    from recon_provider.http import HttpRefundReader
    from recon_storage.db import url

    keys = json.loads(os.environ.get("RECON_PROVIDER_KEYS", "{}"))
    provider = httpx.AsyncClient(base_url=os.environ.get("RECON_SIM_URL", "http://localhost:8100"))
    readers = {t: HttpRefundReader(provider, k["read"]) for t, k in keys.items() if k.get("read")}
    fixed = os.environ.get("RECON_NOW")
    return create_app(
        os.environ.get("RECON_MCP_DATABASE_URL") or url("recon_mcp_read", "recon"),
        os.environ.get("RECON_RUN_TOKEN_KEY", "dev-run-token-key").encode(),
        readers.__getitem__,
        FakeClock(datetime.fromisoformat(fixed)) if fixed else SystemClock(),
        os.environ.get("RECON_MCP_HOST", "127.0.0.1"),
    )
