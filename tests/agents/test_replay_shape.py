from google.adk.models.llm_request import LlmRequest
from google.genai import types

from recon_agents.replay import same, shape


def req(description="Read one payment.", result=None):
    decl = types.FunctionDeclaration(
        name="get_payment",
        description=description,
        parameters=types.Schema(
            type=types.Type.OBJECT, properties={"payment_id": types.Schema(type=types.Type.STRING)}
        ),
    )
    env = {"status": "found", "data": result or {"payment_id": "pay_1", "amount_minor": 4900}}
    return LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part(text="hi")]),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="get_payment", response={"structuredContent": env}
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[decl])]),
    )


def test_new_values_replay_but_a_changed_tool_does_not():
    recorded = shape(req())
    assert same(shape(req(result={"payment_id": "pay_9", "amount_minor": 1})), recorded)  # values don't matter
    assert not same(shape(req(description="Read one payment, with refunds.")), recorded)  # what the model is told
    grown = {"payment_id": "pay_1", "amount_minor": 4900, "evidence_refs": ["payment:pay_1:v1"]}
    assert not same(shape(req(result=grown)), recorded)  # what the tool hands back


def test_a_tape_from_before_these_checks_is_held_to_what_it_recorded():
    old = {k: v for k, v in shape(req()).items() if k not in ("tool_declarations", "response_structure")}
    assert same(shape(req(description="changed")), old)
