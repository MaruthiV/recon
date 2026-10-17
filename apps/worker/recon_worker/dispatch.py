from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


@dataclass(frozen=True)
class Delivery:
    id: int
    run_id: str
    stage: str
    event_id: str
    attempts: int


Deliver = Callable[[Delivery], Awaitable[int]]


class Dispatcher:
    """outbox -> worker, at least once, like cloud tasks: a row is done only after a 2xx"""

    def __init__(
        self,
        sessions: async_sessionmaker,
        deliver: Deliver,
        visibility_seconds: int = 30,
        batch: int = 10,
        duplicate: bool = False,
        max_attempts: int = 25,
    ) -> None:
        self.sessions = sessions
        self.deliver = deliver
        self.visibility = visibility_seconds
        self.batch = batch
        self.duplicate = duplicate
        self.max_attempts = max_attempts

    async def _claim(self) -> list[Delivery]:
        # short txn, rows come back hidden for `visibility` so a crashed dispatcher's work reappears
        async with self.sessions() as s, s.begin():
            rows = (
                await s.execute(
                    text(
                        "UPDATE outbox_events SET available_at = now() + make_interval(secs => :v),"
                        " attempts = attempts + 1 WHERE id IN ("
                        " SELECT id FROM outbox_events WHERE dispatched_at IS NULL AND dead_at IS NULL"
                        " AND available_at <= now()"
                        " ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED)"
                        " RETURNING id, run_id, stage, event_id, attempts"
                    ),
                    {"v": self.visibility, "n": self.batch},
                )
            ).all()
        return sorted((Delivery(*r) for r in rows), key=lambda d: d.id)

    async def _send(self, d: Delivery) -> tuple[int, str | None]:
        try:
            if self.duplicate:
                # fp6: the same event lands twice at once
                codes = await asyncio.gather(self.deliver(d), self.deliver(d))
                return min(codes), None
            return await self.deliver(d), None
        except Exception as e:
            return 0, repr(e)[:300]

    async def pump_once(self) -> int:
        due = await self._claim()
        for d in due:
            code, err = await self._send(d)
            async with self.sessions() as s, s.begin():
                if 200 <= code < 300:
                    await s.execute(text("UPDATE outbox_events SET dispatched_at = now() WHERE id = :id"), {"id": d.id})
                elif d.attempts >= self.max_attempts:
                    await s.execute(
                        text("UPDATE outbox_events SET dead_at = now(), last_error = :e WHERE id = :id"),
                        {"e": f"dead after {d.attempts} attempts: {err or f'http {code}'}", "id": d.id},
                    )
                else:
                    # back off and let it come round again, same event id
                    delay = min(60, 2 ** min(d.attempts, 6))
                    await s.execute(
                        text(
                            "UPDATE outbox_events SET available_at = now() + make_interval(secs => :d),"
                            " last_error = :e WHERE id = :id"
                        ),
                        {"d": delay if code != 409 else 1, "e": err or f"http {code}", "id": d.id},
                    )
        return len(due)


class Sweeper:
    """repair loop: a live run with no pending event and no live lease gets its current stage re-enqueued"""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def sweep_once(self) -> int:
        async with self.sessions() as s, s.begin():
            res = await s.execute(
                text(
                    "INSERT INTO outbox_events (tenant_id, run_id, stage, event_id, available_at)"
                    " SELECT r.tenant_id, r.id, r.stage, r.id || '/' || r.stage || '/' || r.generation || '/sweep', now()"
                    " FROM runs r WHERE r.stage NOT IN ('DONE', 'DIRECT')"
                    " AND (r.lease_expires_at IS NULL OR r.lease_expires_at < now())"
                    " AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.run_id = r.id AND o.dispatched_at IS NULL)"
                    " ON CONFLICT (event_id) DO NOTHING"
                )
            )
        return res.rowcount or 0  # type: ignore[attr-defined]
