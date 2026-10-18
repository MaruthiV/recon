import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from recon_agents.meter import Price, Pricing
from recon_agents.roles import COORDINATOR, INVESTIGATOR, POLICY

FAKE_MODEL = "scripted-fake"
FAKE_PRICING = Pricing("test", {FAKE_MODEL: Price(input=1_000_000, output=2_000_000)})


def first_text(req: LlmRequest) -> str:
    return next(p.text for c in req.contents for p in (c.parts or []) if p.text)


def results(req: LlmRequest) -> list[tuple[str, dict[str, Any]]]:
    # (tool, envelope) in the order the model saw them
    out = []
    for c in req.contents:
        for p in c.parts or []:
            fr = p.function_response
            # a tool error carries no envelope, act like it never answered and ask again
            env = (fr.response or {}).get("structuredContent") if fr is not None else None
            if fr is not None and fr.name != "set_model_response" and env:
                out.append((fr.name or "", env))
    return out


def call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def answer(req: LlmRequest, payload: dict[str, Any]) -> types.Part:
    if "set_model_response" in (req.tools_dict or {}):
        return call("set_model_response", **payload)
    return types.Part(text=json.dumps(payload))


def investigator(req: LlmRequest) -> list[types.Part]:
    brief = json.loads(first_text(req))["case"]
    seen = results(req)
    by_tool = {name: env for name, env in seen}
    if "list_payments" not in by_tool:
        return [call("get_case"), call("list_payments", order_id=brief["order_id"])]
    payments = by_tool["list_payments"]["data"]["payments"]
    refund_envs = [env for name, env in seen if name == "list_refunds"]
    if not refund_envs:
        return [call("list_refunds", payment_id=p["payment_id"]) for p in payments]
    case = by_tool["get_case"]["data"]
    observed = []
    for p, env in zip(payments, refund_envs, strict=True):
        refunds = (env.get("data") or {}).get("refunds", [])
        observed.append(
            {
                "payment_id": p["payment_id"],
                "ref": p["ref"],
                "provider_payment_id": p["provider_payment_id"],
                "amount_minor": p["amount_minor"],
                "currency": p["currency"],
                "status": p["status"],
                "captured_at": p["captured_at"],
                "refunds_lookup": env["status"],
                "refunds": [
                    {k: r[k] for k in ("refund_id", "amount_minor", "currency", "status", "recon_command_id")}
                    for r in refunds
                ],
            }
        )
    dups = [
        later["payment_id"]
        for i, later in enumerate(observed)
        if any(
            e["amount_minor"] == later["amount_minor"]
            and e["currency"] == later["currency"]
            and e["status"] == later["status"] == "succeeded"
            for e in observed[:i]
        )
    ]
    return [
        answer(
            req,
            {
                "case_id": case["case_id"],
                "case_version": case["case_version"],
                "customer_id": case["customer_id"],
                "order_id": case["order_id"],
                "customer_request": case["messages"][0]["body"],
                "payments": observed,
                "duplicate_payment_ids": dups,
                "open_questions": [],
            },
        )
    ]


def policy(req: LlmRequest) -> list[types.Part]:
    seen = dict(results(req))
    if "search_policy" not in seen:
        return [call("search_policy", query="charged twice duplicate charge refund")]
    hits = (seen["search_policy"].get("data") or {}).get("hits", [])
    citations = [
        {k: h[k] for k in ("ref", "policy_id", "version", "authority_class")} | {"quote": h["text"][:80]} for h in hits
    ]
    return [answer(req, {"citations": citations, "exclusions": [], "contradictions": []})]


def plan_payload(brief: dict[str, Any], quote: dict[str, Any] | None, **override: Any) -> dict[str, Any]:
    facts, evidence, case = brief["payment_facts"], brief["policy_evidence"], brief["case"]
    base = {
        "case_id": case["case_id"],
        "case_version": case["case_version"],
        "payment_id": None,
        "claim_id": None,
        "amount_minor": None,
        "currency": None,
        "policy_version_ids": [],
        "evidence_refs": [p["ref"] for p in facts["payments"]],
        "missing_evidence": [],
    }
    if any(p["refunds_lookup"] == "query_failed" for p in facts["payments"]):
        return (
            base
            | {
                "recommended_disposition": "ESCALATE",
                "reason_code": "MISSING_EVIDENCE",
                "explanation": "A refund lookup failed, so we can't tell whether a refund already exists.",
                "missing_evidence": ["provider refunds"],
            }
            | override
        )
    assert quote is not None
    terms = quote["terms_policy_id"]
    return (
        base
        | {
            "recommended_disposition": "REFUND",
            "reason_code": "DUPLICATE_CAPTURE",
            "payment_id": quote["payment_id"],
            "amount_minor": quote["amount_minor"],
            "currency": quote["currency"],
            "policy_version_ids": [terms],
            "evidence_refs": base["evidence_refs"]
            + [c["ref"] for c in evidence["citations"] if c["policy_id"] == terms],
            "explanation": f"Payment {quote['payment_id']} duplicated an earlier capture; refunding {quote['amount_minor']}.",
        }
        | override
    )


def coordinator(**override: Any) -> Callable[[LlmRequest], list[types.Part]]:
    def script(req: LlmRequest) -> list[types.Part]:
        brief = json.loads(first_text(req))
        seen = dict(results(req))
        facts = brief["payment_facts"]
        failed = any(p["refunds_lookup"] == "query_failed" for p in facts["payments"])
        if not failed and "compute_refund_quote" not in seen:
            return [
                call(
                    "compute_refund_quote",
                    payment_id=facts["duplicate_payment_ids"][0],
                    reason_code="DUPLICATE_CAPTURE",
                )
            ]
        quote = (seen.get("compute_refund_quote") or {}).get("data")
        return [answer(req, plan_payload(brief, quote, **override))]

    return script


def looping(req: LlmRequest) -> list[types.Part]:
    # never finishes, keeps re-reading
    return [call("get_case")]


def no_plan(req: LlmRequest) -> list[types.Part]:
    return [types.Part(text="I think this should be refunded.")]


def confident(payload: dict[str, Any]) -> Callable[[LlmRequest], list[types.Part]]:
    # answers straight away without reading anything
    return lambda req: [answer(req, payload)]


def empty_answer(req: LlmRequest) -> list[types.Part]:
    # the node finishes cleanly with no output, same shape as the adk 2.11 resume bug
    return [types.Part(text="")]


class ScriptedLlm(BaseLlm):
    script: Callable[[LlmRequest], list[types.Part]]
    calls: int = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.calls += 1
        parts = self.script(llm_request)
        prompt = sum(len(p.text or "") for c in llm_request.contents for p in (c.parts or [])) // 4
        usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=prompt + 100, candidates_token_count=40)
        yield LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage)


def fake_models(**scripts: Callable[[LlmRequest], list[types.Part]]) -> dict[str, BaseLlm]:
    chosen = {INVESTIGATOR: investigator, POLICY: policy, COORDINATOR: coordinator()} | scripts
    return {role: ScriptedLlm(model=FAKE_MODEL, script=s) for role, s in chosen.items()}
