from __future__ import annotations

from enum import StrEnum
from fractions import Fraction


class Rounding(StrEnum):
    HALF_EVEN = "HALF_EVEN"
    DOWN = "DOWN"
    UP = "UP"


def round_fraction(value: Fraction, mode: Rounding) -> int:
    if value < 0:
        raise ValueError("refund amounts are never negative")
    whole, rest = divmod(value.numerator, value.denominator)
    if rest == 0:
        return whole
    match mode:
        case Rounding.DOWN:
            return whole
        case Rounding.UP:
            return whole + 1
        case Rounding.HALF_EVEN:
            twice = 2 * rest
            if twice < value.denominator:
                return whole
            if twice > value.denominator:
                return whole + 1
            return whole if whole % 2 == 0 else whole + 1


def prorate(amount_minor: int, unused_units: int, total_units: int, mode: Rounding) -> int:
    """refundable part of amount_minor for the unused share of a service period"""
    if total_units <= 0:
        raise ValueError("total_units must be positive")
    if not 0 <= unused_units <= total_units:
        raise ValueError("unused_units must be within [0, total_units]")
    if amount_minor < 0:
        raise ValueError("amount_minor must be non-negative")
    return round_fraction(Fraction(amount_minor * unused_units, total_units), mode)
