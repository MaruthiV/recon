from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

POINTS = {
    "FP2": "after PREPARE commits, before the stage advance",
    "FP3": "after SUBMITTING commits, before the provider call",
    "FP4": "after the provider commits, before the response is saved",
    "FP7": "after the provider call, before the stale worker writes back",
}


class SimulatedCrash(BaseException):
    # BaseException so no `except Exception` in the stage code can swallow it
    pass


def exit_process(point: str) -> None:
    os._exit(137)


def raise_crash(point: str) -> None:
    raise SimulatedCrash(point)


def enabled_from_env() -> bool:
    return os.environ.get("RECON_FAULTS") == "1" and os.environ.get("RECON_ENV", "dev") != "public"


class Faults:
    """named crash points, armed by rows in fault_points, dead code unless RECON_FAULTS=1"""

    def __init__(
        self,
        sessions: async_sessionmaker | None = None,
        enabled: bool = False,
        on_hit: Callable[[str], None] = exit_process,
        before_hit: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.enabled = enabled and sessions is not None
        self.on_hit = on_hit
        self.before_hit = before_hit

    async def hit(self, point: str, scope_id: str) -> None:
        if not self.enabled or self.sessions is None:
            return
        async with self.sessions() as s, s.begin():
            row = (
                await s.execute(
                    text(
                        "UPDATE fault_points SET remaining = remaining - 1 WHERE id = ("
                        " SELECT id FROM fault_points WHERE point = :p AND remaining > 0"
                        " AND (scope_id IS NULL OR scope_id = :s) ORDER BY id LIMIT 1 FOR UPDATE)"
                        " RETURNING id"
                    ),
                    {"p": point, "s": scope_id},
                )
            ).first()
        if row is None:
            return
        if self.before_hit is not None:
            await self.before_hit(point)
        self.on_hit(point)


NO_FAULTS = Faults()
