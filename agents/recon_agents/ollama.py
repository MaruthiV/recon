from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

PREFIX = "ollama:"


def _envelope(resp: dict[str, Any] | None) -> Any:
    # mcp sends the result twice (text + structuredContent), a small model only needs it once
    resp = resp or {}
    return resp.get("structuredContent", resp)


def tools_of(req: LlmRequest) -> list[dict[str, Any]]:
    out = []
    for t in (req.config.tools or []) if req.config else []:
        for fd in getattr(t, "function_declarations", None) or []:
            if fd.parameters_json_schema is not None:
                params = fd.parameters_json_schema
            elif fd.parameters is not None:
                params = fd.parameters.model_dump(mode="json", exclude_none=True)
            else:
                params = {"type": "object", "properties": {}}
            out.append(
                {
                    "type": "function",
                    "function": {"name": fd.name, "description": fd.description or "", "parameters": params},
                }
            )
    return out


def messages_of(req: LlmRequest) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    si = req.config.system_instruction if req.config else None
    if si:
        text = si if isinstance(si, str) else " ".join(p.text or "" for p in getattr(si, "parts", None) or [])
        msgs.append({"role": "system", "content": text})
    for c in req.contents:
        texts, calls = [], []
        for p in c.parts or []:
            if p.thought:
                continue
            if p.function_call is not None:
                calls.append({"function": {"name": p.function_call.name, "arguments": p.function_call.args or {}}})
            elif p.function_response is not None:
                msgs.append(
                    {
                        "role": "tool",
                        "tool_name": p.function_response.name,
                        "content": json.dumps(_envelope(p.function_response.response), default=str),
                    }
                )
            elif p.text:
                texts.append(p.text)
        if texts or calls:
            m: dict[str, Any] = {"role": "assistant" if c.role == "model" else "user", "content": "\n".join(texts)}
            if calls:
                m["tool_calls"] = calls
            msgs.append(m)
    return msgs


class OllamaLlm(BaseLlm):
    """adk model backed by a local ollama server, model ids look like `ollama:llama3.2`"""

    base_url: str = os.environ.get("RECON_OLLAMA_URL", "http://127.0.0.1:11434")
    num_ctx: int = 16384
    # the longest real answer (payment facts) is ~500 tokens, a looping or whitespace-padding decode stops here
    max_output: int = 1024
    timeout_s: int = 600
    # thinking models (qwen3) reason before answering, slow on a laptop, so off unless asked for
    think: bool = os.environ.get("RECON_LOCAL_THINK") == "1"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        body = {
            "model": self.model.removeprefix(PREFIX),
            "messages": messages_of(llm_request),
            "tools": tools_of(llm_request),
            "stream": False,
            "think": self.think,
            # deterministic enough to compare runs, and room for the ~9k token prompts
            "options": {"temperature": 0, "seed": 7, "num_ctx": self.num_ctx, "num_predict": self.max_output},
        }
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s) as c:
            r = await c.post("/api/chat", json=body)
            r.raise_for_status()
            out = r.json()
            msg = out.get("message") or {}
            parts = [
                types.Part(function_call=types.FunctionCall(name=tc["function"]["name"], args=_args(tc["function"])))
                for tc in msg.get("tool_calls") or []
            ]
            prompt, completion = out.get("prompt_eval_count") or 0, out.get("eval_count") or 0
            answer_schema = next(
                (t["function"]["parameters"] for t in body["tools"] if t["function"]["name"] == "set_model_response"),
                None,
            )
            if not parts and answer_schema is not None:
                # it answered in prose, ask once more with the answer schema enforced by the decoder
                retry = {**body, "tools": [], "format": answer_schema}
                retry["messages"] = [*body["messages"], {"role": "user", "content": "Return the answer as JSON only."}]
                r2 = await c.post("/api/chat", json=retry)
                r2.raise_for_status()
                out2 = r2.json()
                prompt += out2.get("prompt_eval_count") or 0
                completion += out2.get("eval_count") or 0
                try:
                    answer = json.loads((out2.get("message") or {}).get("content") or "")
                    parts = [types.Part(function_call=types.FunctionCall(name="set_model_response", args=answer))]
                except ValueError:
                    pass
        if not parts:
            parts = [types.Part(text=msg.get("content") or "")]
        usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=prompt, candidates_token_count=completion)
        yield LlmResponse(
            content=types.Content(role="model", parts=parts), usage_metadata=usage, model_version=self.model
        )


def _args(fn: dict[str, Any]) -> dict[str, Any]:
    a = fn.get("arguments") or {}
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            a = {}
    return a if isinstance(a, dict) else {}
