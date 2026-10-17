from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .proration import Rounding


class AuthorityClass(StrEnum):
    REFUND_TERMS = "refund_terms"
    OPERATIONAL_LIMITS = "operational_limits"
    FAQ = "faq"


class AmbiguousPolicy(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    id: str
    authority_class: AuthorityClass
    version: int
    effective_from: datetime
    effective_to: datetime | None

    def applies_at(self, at: datetime) -> bool:
        return self.effective_from <= at and (self.effective_to is None or at < self.effective_to)


@dataclass(frozen=True, slots=True)
class RefundTerms:
    policy: PolicyVersion
    refund_window_days: int
    duplicate_refund_allowed: bool
    rounding: Rounding


@dataclass(frozen=True, slots=True)
class OperationalLimits:
    policy: PolicyVersion
    auto_approve_limit_minor: int


def applicable[T: (RefundTerms, OperationalLimits)](rules: tuple[T, ...], at: datetime) -> T | None:
    # refund terms use purchase time, operational limits use decision time (callers pick `at`)
    hits = [r for r in rules if r.policy.applies_at(at)]
    if len(hits) > 1:
        raise AmbiguousPolicy(f"{len(hits)} versions apply at {at.isoformat()}")
    return hits[0] if hits else None
