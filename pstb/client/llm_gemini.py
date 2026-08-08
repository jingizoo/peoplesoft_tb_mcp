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


def adc_remedy(project: str, detail: str = "") -> str:
    """What to actually run, for THIS deployment.

    Google's own message — "Your default credentials were not found, see
    <generic doc page>" — is true and useless: it does not know this runs
    headless over SSH, where the plain login command tries to open a
    browser that is not there. Naming the flag is the difference between a
    two-minute fix and an afternoon.
    """
    return (
        "Gemini could not authenticate: no Google Application Default "
        f"Credentials on this host{f' ({detail})' if detail else ''}. "
        f"Project is {project or '(unset)'}. Pick one:\n"
        "  1. Sign in as yourself. Over SSH the browser cannot open, so "
        "the flag is required:\n"
        "       gcloud auth application-default login --no-launch-browser\n"
        "     It prints a URL — open it on your laptop, paste the code "
        "back. Credentials land in "
        "~/.config/gcloud/application_default_credentials.json.\n"
        "  2. Use a service account key: put its path in .env as "
        "GOOGLE_APPLICATION_CREDENTIALS=/path/key.json (chmod 600), then "
        "restart.\n"
        "  3. Keep working now on the local model — switch the provider to "
        "ollama; it needs no cloud credentials and every curated tool "
        "answers the same.\n"
        "Nothing here is stored or logged by this application; the SDK "
        "reads the credentials directly."
    )


def _require_adc(cfg: Config) -> None:
    """Fail at construction with a remedy, not mid-turn with a doc link."""
    try:
        from google.auth import default as _default
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:  # google-auth ships with google-genai; be tolerant
        return
    try:
        _default()
    except DefaultCredentialsError as e:
        raise RuntimeError(adc_remedy(cfg.llm.gemini_project, str(e)[:120]))


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
        # Resolve credentials HERE, not on the first question. google.auth's
        # own lookup is local — it reads a file or asks the metadata server —
        # so this costs nothing and moves the failure from the middle of a
        # user's turn to provider construction, where the surface can say
        # which provider could not start.
        _require_adc(cfg)
        self.client = genai.Client(
            vertexai=True,
            project=cfg.llm.gemini_project,
            location=cfg.llm.gemini_location,
        )
        self.model = cfg.llm.gemini_model
        self.project = cfg.llm.gemini_project
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
                # Credentials can also die MID-SESSION: a refresh token
                # revoked, a key rotated, a session that outlived its ADC.
                # Retrying that is pointless and the raw message is the
                # same doc link, so translate it here too.
                if type(e).__name__ in ("DefaultCredentialsError",
                                        "RefreshError") or (
                        "default credentials were not found" in msg.lower()):
                    raise RuntimeError(
                        adc_remedy(self.project, msg[:120])) from e
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
