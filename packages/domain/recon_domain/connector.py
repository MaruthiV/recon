from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .states import CommandState


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kw: float) -> None:
        self._now += timedelta(**kw)


@dataclass(frozen=True, slots=True)
class ProviderRefund:
    id: str
    payment_intent: str
    amount_minor: int
    currency: str
    status: str
    metadata: dict[str, str] = field(default_factory=dict)
    failure_reason: str | None = None


class SubmitKind(StrEnum):
    APPLIED = "APPLIED"  # provider returned a refund object
    REJECTED = "REJECTED"  # provider says it did nothing for this key
    MISMATCH = "MISMATCH"  # key reused with different params, our bug
    UNKNOWN = "UNKNOWN"  # 409, 429, 5xx, timeout, conn reset


@dataclass(frozen=True, slots=True)
class SubmitResult:
    kind: SubmitKind
    refund: ProviderRefund | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    replayed: bool = False
    request_id: str | None = None


class ReadFailed(Exception):
    pass


class RefundReader(Protocol):
    async def list_refunds(self, payment_intent: str) -> list[ProviderRefund]: ...

    async def get_refund(self, refund_id: str) -> ProviderRefund | None: ...


class PaymentConnector(RefundReader, Protocol):
    async def create_refund(
        self,
        *,
        payment_intent: str,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> SubmitResult: ...


def state_for_refund_status(status: str) -> CommandState:
    match status:
        case "succeeded":
            return CommandState.CONFIRMED_SUCCESS
        case "pending" | "requires_action":
            return CommandState.PENDING_PROVIDER
        case "failed" | "canceled":
            return CommandState.CONFIRMED_FAILURE
        case _:
            return CommandState.MANUAL_REVIEW
