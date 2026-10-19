import json

import httpx
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from recon_agents import ollama
from recon_agents.ollama import OllamaLlm


def request(with_answer_tool: bool) -> LlmRequest:
    decls = [types.FunctionDeclaration(name="get_case", parameters=types.Schema(type=types.Type.OBJECT))]
    if with_answer_tool:
        schema = types.Schema(type=types.Type.OBJECT, properties={"ok": types.Schema(type=types.Type.BOOLEAN)})
        decls.append(types.FunctionDeclaration(name="set_model_response", parameters=schema))
    req = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
        config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=decls)]),
    )
    return req


def fake_server(monkeypatch, replies):
    seen = []

    def handle(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content))
        return httpx.Response(200, json=replies[len(seen) - 1])

    real = httpx.AsyncClient
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    return seen


async def answers(llm, req):
    return [r async for r in llm.generate_content_async(req)]


async def test_every_call_caps_output_and_tool_calls_come_back(monkeypatch):
    seen = fake_server(
        monkeypatch,
        [{"message": {"tool_calls": [{"function": {"name": "get_case", "arguments": {}}}]}, "eval_count": 5}],
    )
    [resp] = await answers(OllamaLlm(model="ollama:qwen3:4b-instruct"), request(False))
    assert seen[0]["options"]["num_predict"] == 1024 and seen[0]["model"] == "qwen3:4b-instruct"
    assert resp.content and resp.content.parts and resp.content.parts[0].function_call
    assert resp.content.parts[0].function_call.name == "get_case"


async def test_a_prose_answer_gets_one_schema_constrained_retry(monkeypatch):
    seen = fake_server(
        monkeypatch,
        [{"message": {"content": "looks fine to me"}}, {"message": {"content": '{"ok": true}'}}],
    )
    [resp] = await answers(OllamaLlm(model="ollama:qwen3:4b-instruct"), request(True))
    assert len(seen) == 2 and seen[1]["format"]["properties"]["ok"] and seen[1]["tools"] == []
    assert seen[1]["options"]["num_predict"] == 1024
    fc = resp.content.parts[0].function_call  # type: ignore[union-attr,index]
    assert fc is not None and (fc.name, fc.args) == ("set_model_response", {"ok": True})
