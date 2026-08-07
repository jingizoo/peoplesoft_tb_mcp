"""Gemini on Vertex AI via the google-genai SDK.

Note: Google's older `vertexai.generative_models` module was deprecated and
removed (June 2026). The supported way to call Gemini on Vertex AI is the
google-genai SDK with vertexai=True — auth comes from Application Default
Credentials (`gcloud auth application-default login`).
"""
from __future__ import annotations

import json
import time

from ..config import Config
from .llm_base import LLMProvider, LLMResponse, ToolCall, ToolResult, ToolSpec, clean_schema


def tool_mode(force_flag: bool, expect_tools: bool, is_user_turn: bool) -> str:
    """Which function-calling mode this call should use.

    ANY forces the model to emit a well-formed call from the declared tool
    list — the managed equivalent of constrained decoding. It is correct
    ONLY on the user turn of a question the agent classified as needing
    tools: forcing it on later turns would make a final prose answer
    impossible, and forcing it on small talk would invent a spurious call.
    """
    return "ANY" if (force_flag and expect_tools and is_user_turn) else "AUTO"


def call_temperature(base: float, routing: float, mode: str) -> float:
    """Greedy decoding when the model is choosing a tool; the configured
    temperature when it is writing prose or deciding whether to chain."""
    return routing if mode == "ANY" else base


class GeminiVertexProvider(LLMProvider):
    name = "gemini"

    def __init__(self, cfg: Config, system_prompt: str, tools: list[ToolSpec]):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise RuntimeError(
                "google-genai package not installed — run: pip install -e '.[llm]'"
            ) from e
        if not cfg.llm.gemini_project:
            raise RuntimeError(
                "Set GOOGLE_CLOUD_PROJECT in .env (and run: "
                "gcloud auth application-default login)"
            )
        self.types = types
        self.client = genai.Client(
            vertexai=True,
            project=cfg.llm.gemini_project,
            location=cfg.llm.gemini_location,
        )
        self.model = cfg.llm.gemini_model
        declarations = []
        for t in tools:
            schema = clean_schema(t.schema)
            try:
                declarations.append(
                    types.FunctionDeclaration(
                        name=t.name,
                        description=t.description,
                        parameters_json_schema=schema,
                    )
                )
            except (TypeError, ValueError):
                declarations.append(
                    types.FunctionDeclaration(
                        name=t.name, description=t.description, parameters=schema
                    )
                )
        kwargs = dict(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=declarations)],
            temperature=cfg.llm.temperature,
        )
        tb = getattr(cfg.llm, "gemini_thinking_budget", -1)
        if tb is not None and tb >= 0:
            try:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=tb)
            except (TypeError, AttributeError):
                pass  # older google-genai without ThinkingConfig
        self._base_kwargs = kwargs
        self._force_tool_round = bool(
            getattr(cfg.llm, "gemini_force_tool_round", True))
        self._routing_temperature = float(
            getattr(cfg.llm, "gemini_routing_temperature", 0.0))
        # The agent loop sets this per turn from its intent classification;
        # default True so a bare provider still routes firmly.
        self.expect_tool_call = True
        self.gen_config = types.GenerateContentConfig(**kwargs)
        self.reset()

    def _config_for(self, mode: str):
        """Per-call config: base settings plus function-calling mode and the
        temperature that matches what this call is doing."""
        if mode == "AUTO" and self._base_kwargs["temperature"] == \
                call_temperature(self._base_kwargs["temperature"],
                                 self._routing_temperature, mode):
            return self.gen_config
        kwargs = dict(self._base_kwargs)
        kwargs["temperature"] = call_temperature(
            kwargs["temperature"], self._routing_temperature, mode)
        try:
            kwargs["tool_config"] = self.types.ToolConfig(
                function_calling_config=self.types.FunctionCallingConfig(
                    mode=mode))
        except (TypeError, AttributeError):
            pass  # older google-genai without ToolConfig — mode stays AUTO
        return self.types.GenerateContentConfig(**kwargs)

    def reset(self) -> None:
        self.contents: list = []

    def _generate(self, config=None) -> LLMResponse:
        # Vertex rate limits (429) and transient 5xx are normal under load;
        # retry briefly instead of dropping the user's turn.
        resp = None
        for attempt in range(4):
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=self.contents,
                    config=config or self.gen_config,
                )
                break
            except Exception as e:
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                msg = str(e)
                transient = code in (429, 500, 503) or any(
                    k in msg for k in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE")
                )
                if attempt == 3 or not transient:
                    raise
                time.sleep(1.5 * (2 ** attempt))
        if not resp.candidates:
            return LLMResponse(text="(Gemini returned no candidates — possibly blocked.)")
        cand = resp.candidates[0]
        if cand.content is None:
            return LLMResponse(text=f"(Gemini stopped: {cand.finish_reason})")
        # Append the candidate content verbatim: Gemini 2.5 attaches thought
        # signatures to function-call parts, and replaying them unmodified is
        # what keeps multi-step tool chains coherent.
        self.contents.append(cand.content)
        text_parts, calls = [], []
        for i, part in enumerate(cand.content.parts or []):
            if getattr(part, "function_call", None):
                fc = part.function_call
                calls.append(
                    ToolCall(
                        id=getattr(fc, "id", None) or f"call_{len(self.contents)}_{i}",
                        name=fc.name,
                        args=dict(fc.args or {}),
                    )
                )
            elif getattr(part, "text", None):
                text_parts.append(part.text)
        return LLMResponse(text="".join(text_parts), tool_calls=calls)

    def send_user(self, text: str) -> LLMResponse:
        t = self.types
        self.contents.append(t.Content(role="user", parts=[t.Part.from_text(text=text)]))
        mode = tool_mode(self._force_tool_round,
                         getattr(self, "expect_tool_call", True), True)
        return self._generate(self._config_for(mode))

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        t = self.types
        parts = []
        for r in results:
            try:
                payload = json.loads(r.content)
                if not isinstance(payload, dict):
                    payload = {"result": payload}
            except (json.JSONDecodeError, TypeError):
                payload = {"result": r.content}
            parts.append(t.Part.from_function_response(name=r.name, response=payload))
        self.contents.append(t.Content(role="user", parts=parts))
        return self._generate()
