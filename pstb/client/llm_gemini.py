"""Gemini on Vertex AI via the google-genai SDK.

Note: Google's older `vertexai.generative_models` module was deprecated and
removed (June 2026). The supported way to call Gemini on Vertex AI is the
google-genai SDK with vertexai=True — auth comes from Application Default
Credentials (`gcloud auth application-default login`).
"""
from __future__ import annotations

import json

from ..config import Config
from .llm_base import LLMProvider, LLMResponse, ToolCall, ToolResult, ToolSpec, clean_schema


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
        self.gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=declarations)],
            temperature=cfg.llm.temperature,
        )
        self.reset()

    def reset(self) -> None:
        self.contents: list = []

    def _generate(self) -> LLMResponse:
        resp = self.client.models.generate_content(
            model=self.model, contents=self.contents, config=self.gen_config
        )
        if not resp.candidates:
            return LLMResponse(text="(Gemini returned no candidates — possibly blocked.)")
        cand = resp.candidates[0]
        if cand.content is None:
            return LLMResponse(text=f"(Gemini stopped: {cand.finish_reason})")
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
        return self._generate()

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
