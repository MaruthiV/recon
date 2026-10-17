import pytest
from hypothesis import given
from hypothesis import strategies as st

from recon_domain.money import Currency, CurrencyMismatch, Money, min_money

usd = st.integers(min_value=-(10**12), max_value=10**12).map(lambda n: Money(n, Currency.USD))


def test_currency_mismatch_rejected():
    class Fake(str):
        pass

    a = Money(4900, Currency.USD)
    other = object.__new__(Money)
    object.__setattr__(other, "amount_minor", 100)
    object.__setattr__(other, "currency", Fake("eur"))
    with pytest.raises(CurrencyMismatch):
        _ = a + other
    with pytest.raises(CurrencyMismatch):
        _ = a <= other


def test_unknown_currency_rejected():
    with pytest.raises(ValueError):
        Currency("eur")
    with pytest.raises(TypeError):
        Money(100, "usd")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [49.0, True, "4900", None])
def test_amount_must_be_int(bad):
    with pytest.raises(TypeError):
        Money(bad, Currency.USD)  # type: ignore[arg-type]


@given(usd, usd)
def test_add_sub_roundtrip(a, b):
    assert (a + b) - b == a
    assert min_money(a, b) <= a and min_money(a, b) <= b
