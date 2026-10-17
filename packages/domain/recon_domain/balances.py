from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClaimBalance:
    permitted_minor: int
    reserved_minor: int
    consumed_minor: int


@dataclass(frozen=True, slots=True)
class PaymentBalance:
    captured_minor: int
    refunded_minor: int
    reserved_minor: int


def remaining_minor(claim: ClaimBalance, payment: PaymentBalance) -> int:
    by_claim = claim.permitted_minor - claim.reserved_minor - claim.consumed_minor
    by_payment = payment.captured_minor - payment.refunded_minor - payment.reserved_minor
    return max(0, min(by_claim, by_payment))
