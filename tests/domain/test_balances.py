from hypothesis import given
from hypothesis import strategies as st

from recon_domain.balances import ClaimBalance, PaymentBalance, remaining_minor


@given(
    st.integers(0, 10**7),
    st.lists(st.integers(1, 10**6), max_size=30),
    st.integers(0, 10**7),
)
def test_greedy_partial_refunds_never_exceed_captured_or_permitted(captured, asks, permitted):
    # apply asks one by one, each capped by remaining, like prepare does under lock
    reserved = refunded = 0
    claim_reserved = claim_consumed = 0
    for i, ask in enumerate(asks):
        left = remaining_minor(
            ClaimBalance(permitted, claim_reserved, claim_consumed),
            PaymentBalance(captured, refunded, reserved),
        )
        if ask <= left:
            if i % 2:
                refunded += ask
                claim_consumed += ask
            else:
                reserved += ask
                claim_reserved += ask
    assert reserved + refunded <= captured
    assert claim_reserved + claim_consumed <= permitted


def test_remaining_is_min_of_claim_and_payment():
    assert remaining_minor(ClaimBalance(4900, 0, 0), PaymentBalance(4900, 0, 0)) == 4900
    assert remaining_minor(ClaimBalance(4900, 1000, 0), PaymentBalance(4900, 0, 0)) == 3900
    assert remaining_minor(ClaimBalance(4900, 0, 0), PaymentBalance(4900, 4000, 0)) == 900
    assert remaining_minor(ClaimBalance(100, 0, 200), PaymentBalance(4900, 0, 0)) == 0
