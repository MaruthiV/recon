import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from recon_bench.naive import run_naive
from recon_bench.world import world
from recon_fixtures.manifests import specs
from tests.agents.fakes import FAKE_MODEL, ScriptedLlm, call

# a refund that's at most half the charge: the provider's own cap can't stop a second one, only a ledger can
RETURN = next(s for s in specs("dev") if s.variant == "two_lines")


def retries_once(req: LlmRequest) -> list[types.Part]:
    # a plain, reasonable-sounding policy: if the refund call errors, try it once more
    seen = [
        (p.function_response.name, p.function_response.response or {})
        for c in req.contents
        for p in c.parts or []
        if p.function_response
    ]
    names = [n for n, _ in seen]
    case = next((r for n, r in seen if n == "get_case"), None)
    if case is None:
        return [call("get_case")]
    pay, ret = case["payments"][0]["payment_id"], case["returns"][0]["return_id"]
    q = next((r for n, r in seen if n == "compute_refund_quote"), None)
    if q is None:
        return [call("compute_refund_quote", payment_id=pay, reason_code="RETURNED_ITEMS", subject_id=ret)]
    creates = [r for n, r in seen if n == "create_refund"]
    if not creates or ("error" in creates[-1] and len(creates) < 2):
        return [call("create_refund", payment_id=pay, amount_minor=q["amount_minor"])]
    assert names
    return [types.Part(text="Your refund is on its way.")]


@pytest.mark.parametrize(
    ("fault", "refunds", "over"),
    [("none", 1, False), ("fail_500", 1, False), ("commit_then_500", 2, True)],
)
async def test_a_naive_agent_that_retries_on_error_refunds_twice_when_the_provider_already_did_it(fault, refunds, over):
    async with world("benchtest") as w:
        r = await run_naive(w, RETURN, lambda: ScriptedLlm(model=FAKE_MODEL, script=retries_once), fault)
    assert (r["refunds"], r["over_refund"]) == (refunds, over), r
    assert r["refunded"] == r["owed"] * refunds
