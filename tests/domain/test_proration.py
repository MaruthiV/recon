from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from recon_domain.proration import Rounding, prorate, round_fraction

amounts = st.integers(min_value=0, max_value=10**9)
modes = st.sampled_from(list(Rounding))


@st.composite
def periods(draw):
    total = draw(st.integers(min_value=1, max_value=400))
    return draw(st.integers(min_value=0, max_value=total)), total


@given(amounts, periods(), modes)
def test_prorate_stays_within_amount(amount, period, mode):
    unused, total = period
    assert 0 <= prorate(amount, unused, total, mode) <= amount


@given(amounts, periods(), st.integers(min_value=0, max_value=400), modes)
def test_prorate_monotone_in_unused_units(amount, period, extra, mode):
    unused, total = period
    more = min(total, unused + extra)
    assert prorate(amount, unused, total, mode) <= prorate(amount, more, total, mode)


@given(amounts, periods())
def test_rounding_modes_bracket_exact_value(amount, period):
    unused, total = period
    exact = Fraction(amount * unused, total)
    down, up, even = (prorate(amount, unused, total, m) for m in (Rounding.DOWN, Rounding.UP, Rounding.HALF_EVEN))
    assert down <= exact <= up
    assert up - down <= 1
    assert down <= even <= up


@pytest.mark.parametrize(
    ("value", "expected"),
    [(Fraction(5, 2), 2), (Fraction(7, 2), 4), (Fraction(9, 4), 2), (Fraction(11, 4), 3), (Fraction(4), 4)],
)
def test_half_even_ties(value, expected):
    assert round_fraction(value, Rounding.HALF_EVEN) == expected


def test_full_and_zero_periods():
    assert prorate(4900, 30, 30, Rounding.DOWN) == 4900
    assert prorate(4900, 0, 30, Rounding.UP) == 0
    with pytest.raises(ValueError):
        prorate(4900, 31, 30, Rounding.DOWN)
