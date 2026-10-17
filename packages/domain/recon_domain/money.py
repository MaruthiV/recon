from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Currency(StrEnum):
    USD = "usd"


class CurrencyMismatch(ValueError):
    pass


@dataclass(frozen=True, slots=True, order=False)
class Money:
    amount_minor: int
    currency: Currency

    def __post_init__(self) -> None:
        # bool is an int subclass, don't let True sneak in as 1 cent
        if type(self.amount_minor) is not int:
            raise TypeError(f"amount_minor must be int, got {type(self.amount_minor).__name__}")
        if not isinstance(self.currency, Currency):
            raise TypeError(f"currency must be Currency, got {self.currency!r}")

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(0, currency)

    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"can't combine Money with {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor >= other.amount_minor

    def is_positive(self) -> bool:
        return self.amount_minor > 0

    def __str__(self) -> str:
        return f"{self.amount_minor} {self.currency.value}"


def min_money(a: Money, b: Money) -> Money:
    return a if a <= b else b
