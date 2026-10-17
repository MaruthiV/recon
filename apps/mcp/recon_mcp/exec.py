import json
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from sqlalchemy import select

from recon_domain.connector import Clock, FakeClock, PaymentConnector, ProviderRefund, ReadFailed, SystemClock
from recon_domain.states import CommandState as C
from recon_domain.tokens import BadToken, verify
from recon_provider.http import HttpConnector
from recon_storage.db import make_engine, sessions, url
from recon_storage.models import Command

MCP_EXEC = "mcp-exec"
EXEC_TOOLS = ("execute_authorized_refund", "reconcile_refund")


class RefundOut(BaseModel):
    refund_id: str
    payment_intent: str
    amount_minor: int
    currency: str
    status: str
    metadata: dict[str, str]
    failure_reason: str | None = None


class ExecOut(BaseModel):
    ok: bool
    refused: str | None = None
    kind: str | None = None  # APPLIED | REJECTED | MISMATCH | UNKNOWN
    refund: RefundOut | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    replayed: bool = False
    request_id: str | None = None


class ReconcileOut(BaseModel):
    ok: bool
    refused: str | None = None
    read_failed: bool = False
    refunds: list[RefundOut] = []


def refund_out(r: ProviderRefund) -> RefundOut:
    return RefundOut(
        refund_id=r.id,
        payment_intent=r.payment_intent,
        amount_minor=r.amount_minor,
        currency=r.currency,
        status=r.status,
        metadata=r.metadata,
        failure_reason=r.failure_reason,
    )


class ExecService:
    def __init__(self, inner: Any, engine: Any) -> None:
        self.inner = inner
        self.engine = engine

    def lifespan(self):
        return self.inner.router.lifespan_context(self.inner)

    async def __call__(self, scope, receive, send) -> None:
        await self.inner(scope, receive, send)


def create_exec_app(
    dsn: str,
    key: bytes,
    connector_for: Callable[[str], PaymentConnector],
    clock: Clock | None = None,
    host: str = "127.0.0.1",
) -> ExecService:
    """mcp-exec: the only holder of the provider secret key, acts on persisted commands only"""
    clock = clock or SystemClock()
    engine = make_engine(dsn)
    db = sessions(engine)
    server = MCPServer("recon-exec")
    write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

    async def authorize(command_id: str, capability: str, for_submit: bool) -> tuple[Command | None, str | None]:
        try:
            claims = verify(capability, key, MCP_EXEC, clock.now())
        except BadToken as e:
            return None, f"capability rejected: {e}"
        if claims.get("command_id") != command_id:
            return None, "capability is for a different command"
        async with db() as s:
            cmd = (await s.execute(select(Command).where(Command.id == command_id))).scalar_one_or_none()
        # same answer for missing and other-tenant, nothing to probe
        if cmd is None or cmd.tenant_id != claims.get("tenant"):
            return None, "no such command"
        if claims.get("payload_hash") != cmd.payload_hash:
            return None, "payload hash doesn't match the persisted command"
        if for_submit:
            if cmd.state != C.SUBMITTING.value:
                return None, f"command is {cmd.state}, not SUBMITTING"
            if claims.get("attempt") != cmd.attempts:
                return None, "capability is for an older attempt"
        return cmd, None

    @server.tool(
        description="Apply one persisted, authorized refund command at the provider. Takes no amounts.",
        annotations=write,
    )
    async def execute_authorized_refund(command_id: str, authorization_capability: str) -> ExecOut:
        cmd, refused = await authorize(command_id, authorization_capability, for_submit=True)
        if cmd is None:
            return ExecOut(ok=False, refused=refused)
        # every value comes from the persisted command, nothing from the caller
        r = await connector_for(cmd.tenant_id).create_refund(
            payment_intent=cmd.provider_payment_id,
            amount_minor=cmd.amount_minor,
            currency=cmd.currency,
            idempotency_key=cmd.idempotency_key,
            metadata={"recon_command_id": cmd.id, "recon_tenant_id": cmd.tenant_id},
        )
        return ExecOut(
            ok=True,
            kind=r.kind.value,
            refund=refund_out(r.refund) if r.refund else None,
            http_status=r.http_status,
            error_type=r.error_type,
            error_code=r.error_code,
            replayed=r.replayed,
            request_id=r.request_id,
        )

    @server.tool(
        description="Read the provider's refunds for one command (by its metadata), or one refund by id.",
        annotations=read,
    )
    async def reconcile_refund(
        command_id: str, authorization_capability: str, refund_id: str | None = None
    ) -> ReconcileOut:
        cmd, refused = await authorize(command_id, authorization_capability, for_submit=False)
        if cmd is None:
            return ReconcileOut(ok=False, refused=refused)
        conn = connector_for(cmd.tenant_id)
        try:
            if refund_id is not None:
                one = await conn.get_refund(refund_id)
                found = [one] if one is not None and one.payment_intent == cmd.provider_payment_id else []
            else:
                found = await conn.list_refunds(cmd.provider_payment_id)
        except ReadFailed:
            return ReconcileOut(ok=True, read_failed=True)
        return ReconcileOut(ok=True, refunds=[refund_out(r) for r in found])

    inner = server.streamable_http_app(stateless_http=True, json_response=True, host=host)
    return ExecService(inner, engine)


def create_exec_from_env() -> ExecService:
    keys = json.loads(os.environ.get("RECON_PROVIDER_KEYS", "{}"))
    provider = httpx.AsyncClient(base_url=os.environ.get("RECON_SIM_URL", "http://localhost:8100"))
    conns = {t: HttpConnector(provider, k["secret"]) for t, k in keys.items()}
    fixed = os.environ.get("RECON_NOW")
    return create_exec_app(
        os.environ.get("RECON_EXEC_DATABASE_URL") or url("recon_app", "recon"),
        os.environ["RECON_EXEC_KEY"].encode(),
        conns.__getitem__,
        FakeClock(datetime.fromisoformat(fixed)) if fixed else SystemClock(),
        os.environ.get("RECON_EXEC_HOST", "127.0.0.1"),
    )
