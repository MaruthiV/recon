from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from recon_domain.connector import Clock
from recon_domain.states import CommandState as C
from recon_storage.models import Command, WebhookInbox

from .ledger import Ledger

TOLERANCE_SECONDS = 300  # stripe libraries' default


def verify_signature(header: str, body: bytes, secret: str, now: datetime, tolerance: int = TOLERANCE_SECONDS) -> bool:
    pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
    t = next((v for k, v in pairs if k.strip() == "t"), None)
    # only v1 counts, anything else could be a downgrade
    sigs = [v for k, v in pairs if k.strip() == "v1"]
    if t is None or not t.isdigit() or not sigs:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        return False
    return abs(int(now.timestamp()) - int(t)) <= tolerance


class Webhooks:
    """inbox first, process later: a webhook only ever triggers an authoritative re-read"""

    def __init__(
        self,
        sessions: async_sessionmaker,
        clock: Clock,
        ledger: Ledger,
        secret_for: Callable[[str], str | None],
        provider: str = "sim",
    ) -> None:
        self.sessions = sessions
        self.clock = clock
        self.ledger = ledger
        self.secret_for = secret_for
        self.provider = provider

    async def receive(self, tenant_id: str, body: bytes, signature: str) -> int:
        secret = self.secret_for(tenant_id)
        if secret is None:
            return 404
        if not verify_signature(signature, body, secret, self.clock.now()):
            return 400
        try:
            evt = json.loads(body)
            event_id, type_ = evt["id"], evt["type"]
        except (ValueError, KeyError, TypeError):
            return 400
        async with self.sessions() as s, s.begin():
            await s.execute(
                pg_insert(WebhookInbox)
                .values(
                    tenant_id=tenant_id,
                    provider=self.provider,
                    event_id=event_id,
                    type=type_,
                    payload_hash=hashlib.sha256(body).hexdigest(),
                    payload=evt,
                    received_at=self.clock.now(),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "provider", "event_id"])
            )
        return 200  # a duplicate is still a 2xx, the provider just stops retrying

    async def process_pending(self, limit: int = 50) -> int:
        async with self.sessions() as s:
            rows = list(
                (
                    await s.execute(
                        select(WebhookInbox)
                        .where(WebhookInbox.processed_at.is_(None))
                        .order_by(WebhookInbox.id)
                        .limit(limit)
                    )
                ).scalars()
            )
        for row in rows:
            outcome = await self._process(row.tenant_id, row.payload)
            async with self.sessions() as s, s.begin():
                await s.execute(
                    update(WebhookInbox)
                    .where(WebhookInbox.id == row.id, WebhookInbox.processed_at.is_(None))
                    .values(processed_at=self.clock.now(), outcome=outcome)
                )
        return len(rows)

    async def _process(self, tenant_id: str, evt: dict) -> str:
        obj = (evt.get("data") or {}).get("object") or {}
        if obj.get("object") != "refund":
            return "ignored"
        cid = (obj.get("metadata") or {}).get("recon_command_id")
        async with self.sessions() as s:
            q = select(Command).where(Command.tenant_id == tenant_id)
            q = q.where(Command.id == cid) if cid else q.where(Command.provider_refund_id == obj.get("id"))
            cmd = (await s.execute(q)).scalar_one_or_none()
        if cmd is None:
            return "no_command"
        before = C(cmd.state)
        if before is C.SUBMITTING:
            return "deferred"  # the submitter is on the wire, its own result or a reconcile settles this
        if before not in (C.UNKNOWN, C.PENDING_PROVIDER):
            return f"already {before.value}"
        # the payload's status is ignored on purpose, events arrive late, twice and out of order
        after = await self.ledger.reconcile(cmd.id)
        if after is not before:
            await self._nudge(cmd.id)
        return f"refreshed {before.value} -> {after.value}"

    async def _nudge(self, command_id: str) -> None:
        # whoever waits on this command shouldn't sit out its backoff
        async with self.sessions() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE outbox_events SET available_at = now() WHERE dispatched_at IS NULL"
                    " AND run_id IN (SELECT id FROM runs WHERE command_id = :c)"
                ),
                {"c": command_id},
            )
