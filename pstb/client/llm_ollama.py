"""Ollama provider — local models with tool calling (llama3.1, qwen3, etc.)."""
from __future__ import annotations

import json

from ..config import Config
from .llm_base import LLMProvider, LLMResponse, ToolCall, ToolResult, ToolSpec, clean_schema


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, cfg: Config, system_prompt: str, tools: list[ToolSpec]):
        try:
            import ollama
        except ImportError as e:
            raise RuntimeError(
                "ollama package not installed — run: pip install -e '.[llm]'"
            ) from e
        self._ollama = ollama
        self.client = ollama.Client(host=cfg.llm.ollama_host)
        self.model = cfg.llm.ollama_model
        self.temperature = cfg.llm.temperature
        self.system_prompt = system_prompt
        self.tools_payload = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": clean_schema(t.schema),
                },
            }
            for t in tools
        ]
        self.reset()

    def reset(self) -> None:
        self.messages: list = [{"role": "system", "content": self.system_prompt}]

    def _generate(self) -> LLMResponse:
        try:
            resp = self.client.chat(
                model=self.model,
                messages=self.messages,
                tools=self.tools_payload,
                options={"temperature": self.temperature},
            )
        except self._ollama.ResponseError as e:
            hint = ""
            if "does not support tools" in str(e).lower():
                hint = " — use a tool-calling model, e.g.: ollama pull llama3.1:8b"
            elif "not found" in str(e).lower():
                hint = f" — pull it first: ollama pull {self.model}"
            raise RuntimeError(f"Ollama error: {e}{hint}") from e
        msg = resp["message"]
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
        calls = []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function", tc) if isinstance(tc, dict) else tc.function
            fname = fn["name"] if isinstance(fn, dict) else fn.name
            fargs = fn["arguments"] if isinstance(fn, dict) else fn.arguments
            if isinstance(fargs, str):
                try:
                    fargs = json.loads(fargs)
                except json.JSONDecodeError:
                    fargs = {}
            calls.append(ToolCall(id=f"call_{len(self.messages)}_{i}", name=fname, args=dict(fargs or {})))
        # store assistant turn back in native form
        self.messages.append(
            {
                "role": "assistant",
                "content": content,
                **(
                    {"tool_calls": [
                        {"function": {"name": c.name, "arguments": c.args}} for c in calls
                    ]}
                    if calls
                    else {}
                ),
            }
        )
        return LLMResponse(text=content, tool_calls=calls)

    def send_user(self, text: str) -> LLMResponse:
        self.messages.append({"role": "user", "content": text})
        return self._generate()

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        for r in results:
            self.messages.append({"role": "tool", "name": r.name, "content": r.content})
        return self._generate()
