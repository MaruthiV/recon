from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from recon_domain.connector import Clock, CommandRef, ProviderRefund, ReadFailed, SubmitKind, SubmitResult
from recon_domain.tokens import mint

MCP_EXEC = "mcp-exec"


class ExecRefused(Exception):
    pass


def _refund(d: dict[str, Any]) -> ProviderRefund:
    return ProviderRefund(
        id=d["refund_id"],
        payment_intent=d["payment_intent"],
        amount_minor=d["amount_minor"],
        currency=d["currency"],
        status=d["status"],
        metadata=dict(d.get("metadata") or {}),
        failure_reason=d.get("failure_reason"),
    )


class McpExecGateway:
    """worker -> mcp-exec, holds a capability signing key but never the provider key"""

    def __init__(
        self,
        url: str,
        key: bytes,
        clock: Clock,
        http_factory: Callable[[], httpx2.AsyncClient] | None = None,
        ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        self.url = url
        self.key = key
        self.clock = clock
        self.http_factory = http_factory or (lambda: httpx2.AsyncClient(timeout=30))
        self.ttl = ttl

    def _capability(self, cmd: CommandRef) -> str:
        now = self.clock.now()
        return mint(
            {
                "aud": MCP_EXEC,
                "tenant": cmd.tenant_id,
                "command_id": cmd.id,
                "payload_hash": cmd.payload_hash,
                "attempt": cmd.attempt,
                "iat": int(now.timestamp()),
                "exp": int((now + self.ttl).timestamp()),
            },
            self.key,
        )

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self.http_factory() as http, Client(streamable_http_client(self.url, http_client=http)) as c:
            r = await c.call_tool(tool, args)
        if r.is_error or r.structured_content is None:
            raise RuntimeError(f"mcp-exec {tool} errored: {r.content}")
        return r.structured_content

    async def submit(self, cmd: CommandRef) -> SubmitResult:
        try:
            out = await self._call(
                "execute_authorized_refund",
                {"command_id": cmd.id, "authorization_capability": self._capability(cmd)},
            )
        except Exception:
            # couldn't reach mcp-exec or it blew up mid-call: may or may not have reached the provider
            return SubmitResult(SubmitKind.UNKNOWN)
        if not out["ok"]:
            return SubmitResult(SubmitKind.REFUSED, error_type="exec_refused", error_code=out.get("refused"))
        return SubmitResult(
            SubmitKind(out["kind"]),
            _refund(out["refund"]) if out.get("refund") else None,
            out.get("http_status"),
            out.get("error_type"),
            out.get("error_code"),
            bool(out.get("replayed")),
            out.get("request_id"),
        )

    async def _reconcile(self, cmd: CommandRef, refund_id: str | None) -> list[ProviderRefund]:
        args: dict[str, Any] = {"command_id": cmd.id, "authorization_capability": self._capability(cmd)}
        if refund_id is not None:
            args["refund_id"] = refund_id
        try:
            out = await self._call("reconcile_refund", args)
        except Exception as e:
            raise ReadFailed(f"mcp-exec unreachable: {e!r}") from e
        if not out["ok"]:
            raise ExecRefused(out.get("refused"))
        if out.get("read_failed"):
            raise ReadFailed("provider read failed behind mcp-exec")
        return [_refund(r) for r in out.get("refunds", [])]

    async def refunds_for(self, cmd: CommandRef) -> list[ProviderRefund]:
        return await self._reconcile(cmd, None)

    async def refund(self, cmd: CommandRef, refund_id: str) -> ProviderRefund | None:
        found = await self._reconcile(cmd, refund_id)
        return found[0] if found else None
