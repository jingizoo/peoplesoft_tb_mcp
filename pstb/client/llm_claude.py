"""Claude on the Anthropic API, through the official `anthropic` SDK.

Four things differ from the other two providers and are worth knowing
before editing this file.

TEMPERATURE IS GONE. Claude Opus 5 rejects temperature, top_p and top_k
with a 400, so llm.temperature — which Ollama and Gemini both honour — is
deliberately not sent. Depth is steered with output_config.effort, and
tool selection is made deterministic by FORCING a call rather than by
decoding greedily. The routing discipline is the same as Gemini's; only
the mechanism differs.

THE TRANSCRIPT IS STRICT. Every tool_use block the model emits must be
answered by a tool_result carrying the same id, in the very next user
message. Gemini and Ollama tolerate a dangling call; Anthropic returns a
400. The agent loop can leave one dangling when it hits MAX_TOOL_ROUNDS,
so send_user() closes any that are open before adding the user's text —
otherwise the NEXT question in a reused GUI session would fail with an
error about the previous one.

ASSISTANT TURNS ARE REPLAYED VERBATIM. The whole turn goes back as it
came, thinking blocks included: the API rejects modified ones, and on
Opus 5 thinking is on by default.

THE FIXED PROMPT IS CACHED. The system prompt plus every tool schema is
~19k characters that never change and are re-sent on every turn of every
round; a cache breakpoint on the system block bills them at a tenth of
the input rate after the first call. A second breakpoint rides the newest
message, so a ten-round tool loop re-reads the conversation instead of
re-paying for it. Both are cost/latency only — remove them and the
answers are identical.
"""
from __future__ import annotations

import sys

from ..config import Config
from .llm_base import LLMProvider, LLMResponse, ToolCall, ToolResult, ToolSpec

# The scalar "default" form of server-side fallbacks: on a policy refusal
# the API re-runs the request on Anthropic's recommended substitute rather
# than handing back an empty answer. Routed by refusal category, so it
# needs no model list to maintain.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Rejected as a tool result when the agent loop ran out of rounds with a
# call still open. See _close_open_tool_calls.
UNANSWERED = ("This call was not executed: the turn ended before it ran.")


def api_key_remedy(detail: str = "") -> str:
    """What to actually do, for THIS deployment.

    The SDK's own message names three constructor arguments, which is
    true and useless to someone who has never opened this file. An unset
    ANTHROPIC_API_KEY is also not the only way to have credentials — a
    signed-in CLI profile counts — so the check that raises this looks at
    what the client resolved, not at the environment variable.
    """
    return (
        "Claude could not authenticate: the Anthropic SDK resolved no "
        f"credentials on this host{f' ({detail})' if detail else ''}. "
        "Pick one:\n"
        "  1. Put a key in .env as ANTHROPIC_API_KEY=... and restart. The "
        "configuration console writes it for you without an editor or an "
        "admin: open /console, unlock, and set 'Anthropic API key'.\n"
        "  2. Sign in as yourself if the Anthropic CLI is installed. Over "
        "SSH the browser cannot open, so the flag is required:\n"
        "       ant auth login --no-launch-browser\n"
        "     It stores a profile under ~/.config/anthropic/ that the SDK "
        "reads on its own — no environment variable needed.\n"
        "  3. Keep working now on the local model — switch the provider to "
        "ollama; it needs no cloud credentials and every curated tool "
        "answers the same.\n"
        "Nothing here is stored or logged by this application; the SDK "
        "reads the credential directly."
    )


def tool_choice_for(force_flag: bool, expect_tools: bool,
                    is_user_turn: bool) -> dict:
    """Which tool_choice this call should carry.

    "any" forces the model to emit a well-formed call from the declared
    list — Anthropic's equivalent of Gemini's function-calling mode ANY,
    and correct in exactly the same one place: the user turn of a question
    the agent classified as needing tools. On a later turn it would make a
    final prose answer impossible; on small talk it would invent a call.
    """
    if force_flag and expect_tools and is_user_turn:
        return {"type": "any"}
    return {"type": "auto"}


def _bget(block, key, default=None):
    """Read a field from a content block that may be an SDK object (what
    the model returned) or a plain dict (what we built)."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, cfg: Config, system_prompt: str, tools: list[ToolSpec]):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "anthropic package not installed — run: pip install -e '.[llm]'"
            ) from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=4)
        # Resolve credentials HERE, not on the first question — the lookup
        # is local (an environment variable or a file on disk), so it costs
        # nothing and moves the failure to provider construction, where the
        # surface can say which provider could not start. auth_headers is
        # empty only when nothing resolved: a key, a token, a CLI profile
        # and workload identity all populate it.
        if not self.client.auth_headers:
            raise RuntimeError(api_key_remedy())
        self.model = cfg.llm.claude_model
        self.max_tokens = int(getattr(cfg.llm, "claude_max_tokens", 32000)
                              or 32000)
        # Thinking and response text share max_tokens, and on Opus 5
        # thinking is on whether or not it is asked for. Say so explicitly
        # so the setting is visible in this file rather than implied by a
        # model default that differs across the family.
        self._thinking = True
        self._effort = str(getattr(cfg.llm, "claude_effort", "high")
                           or "").strip().lower()
        self._force_tool_round = bool(
            getattr(cfg.llm, "claude_force_tool_round", True))
        self._fallbacks = bool(getattr(cfg.llm, "claude_fallbacks", True))
        # The system prompt and the tool list are byte-identical on every
        # call; one breakpoint here covers both, because tools render
        # before system.
        self.system = [{"type": "text", "text": system_prompt,
                        "cache_control": {"type": "ephemeral"}}]
        self.tools_payload = [
            {
                "name": t.name,
                "description": t.description,
                # Deliberately NOT clean_schema(): that collapses anyOf and
                # drops $defs to satisfy Gemini's validator, which would
                # break a $ref here. Anthropic takes ordinary JSON Schema,
                # and chat.py has already stripped the keys nothing reads.
                "input_schema": _object_schema(t.schema),
            }
            for t in tools
        ]
        # The agent loop sets this per turn from its intent classification;
        # default True so a bare provider still routes firmly.
        self.expect_tool_call = True
        self.reset()

    def reset(self) -> None:
        self.messages: list = []
        self._cache_block: dict | None = None

    # ---- transcript -----------------------------------------------------

    def _mark_cache(self, block: dict) -> None:
        """Move the conversation breakpoint onto the newest block.

        Moved rather than added: four breakpoints are the per-request
        limit, and a ten-round tool loop would blow through that in a
        single turn. The old marker is removed from the stored message, so
        every request carries exactly two — system, and here.
        """
        if self._cache_block is not None:
            self._cache_block.pop("cache_control", None)
        block["cache_control"] = {"type": "ephemeral"}
        self._cache_block = block

    def _open_tool_calls(self) -> list:
        """Ids of tool_use blocks in the last assistant turn that nothing
        has answered yet."""
        if not self.messages or self.messages[-1].get("role") != "assistant":
            return []
        return [_bget(b, "id") for b in self.messages[-1].get("content") or []
                if _bget(b, "type") == "tool_use" and _bget(b, "id")]

    def _close_open_tool_calls(self) -> None:
        """Answer any dangling call before a plain user message follows it.

        The agent loop stops after MAX_TOOL_ROUNDS even when the model has
        just asked for more work, which leaves a tool_use with no result.
        Anthropic refuses the next request outright, and in the GUI that
        next request is the user's NEXT question — so the failure would
        surface one question late, attached to the wrong one.
        """
        open_ids = self._open_tool_calls()
        if not open_ids:
            return
        self.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid,
                         "content": UNANSWERED, "is_error": True}
                        for tid in open_ids],
        })

    # ---- the call --------------------------------------------------------

    def _request_kwargs(self, tool_choice: dict) -> dict:
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            messages=self.messages,
            tools=self.tools_payload,
            tool_choice=tool_choice,
        )
        if self._thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}
        return kwargs

    def _drop_unsupported(self, message: str, kwargs: dict) -> str:
        """Turn a 400 about an optional feature into a degraded retry.

        Every knob below is a nicety: fallbacks rescue a refusal, effort
        tunes depth, thinking improves the answer, forcing a call tightens
        routing. None of them is the product. A model or an account that
        will not take one must still be able to answer the question, so
        the feature is dropped for the rest of the process, once, out
        loud — rather than every turn failing with a parameter error the
        operator cannot act on.
        """
        low = message.lower()
        if self._fallbacks and "fallback" in low:
            self._fallbacks = False
            return "server-side fallbacks"
        # Checked before thinking: if the two are reported as
        # incompatible, keep thinking (it shapes every answer) and give up
        # the forced call (it shapes the first turn, and the evidence
        # gates in chat.py already backstop a model that skips the tool).
        if "tool_choice" in low and kwargs.get("tool_choice", {}).get(
                "type") == "any":
            self._force_tool_round = False
            kwargs["tool_choice"] = {"type": "auto"}
            return "forced tool rounds"
        if self._effort and ("output_config" in low or "effort" in low):
            self._effort = ""
            kwargs.pop("output_config", None)
            return "the effort setting"
        if self._thinking and "thinking" in low:
            self._thinking = False
            kwargs.pop("thinking", None)
            return "adaptive thinking"
        return ""

    def _call(self, kwargs: dict):
        """One API call, with the optional features shed on rejection.

        Retries for rate limits and 5xx are the SDK's job (max_retries=4
        on the client); this loop only handles "the request itself was
        refused", which retrying unchanged would never fix.
        """
        errors = (self._anthropic.BadRequestError, TypeError)
        for _ in range(4):
            try:
                if self._fallbacks:
                    with self.client.beta.messages.stream(
                            betas=[FALLBACK_BETA], fallbacks="default",
                            **kwargs) as stream:
                        return stream.get_final_message()
                with self.client.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except self._anthropic.AuthenticationError as e:
                # Credentials can also die mid-session — a key revoked, a
                # profile expired. Retrying is pointless and the raw
                # message is the same three constructor arguments, so
                # translate it here too.
                raise RuntimeError(api_key_remedy(str(e)[:160])) from e
            except errors as e:
                dropped = self._drop_unsupported(str(e), kwargs)
                if not dropped:
                    raise
                print(f"claude: {self.model} rejected {dropped}; continuing "
                      f"without it.", file=sys.stderr)
        raise RuntimeError(
            f"Claude rejected the request even after dropping every optional "
            f"feature. Model is {self.model!r} — check that name first.")

    def _generate(self, tool_choice: dict) -> LLMResponse:
        message = self._call(self._request_kwargs(tool_choice))
        blocks = list(message.content or [])
        # Replay the turn exactly as it arrived: thinking blocks carry
        # signatures the API validates, and an edited one is rejected.
        if blocks:
            self.messages.append({"role": "assistant", "content": blocks})

        if message.stop_reason == "refusal":
            category = getattr(getattr(message, "stop_details", None),
                               "category", None)
            return LLMResponse(
                text=f"(Claude declined this request"
                     f"{f': {category}' if category else ''}. Nothing was "
                     f"answered from memory.)")

        text_parts, calls = [], []
        for block in blocks:
            kind = _bget(block, "type")
            if kind == "text":
                text_parts.append(_bget(block, "text") or "")
            elif kind == "tool_use":
                calls.append(ToolCall(id=_bget(block, "id"),
                                      name=_bget(block, "name"),
                                      args=dict(_bget(block, "input") or {})))
        text = "".join(text_parts)
        if message.stop_reason == "max_tokens" and not calls and not text:
            text = (f"(Claude hit the {self.max_tokens:,}-token output cap "
                    f"before writing anything — raise llm.claude_max_tokens.)")
        return LLMResponse(text=text, tool_calls=calls)

    # ---- LLMProvider -----------------------------------------------------

    def send_user(self, text: str) -> LLMResponse:
        self._close_open_tool_calls()
        block = {"type": "text", "text": text}
        self.messages.append({"role": "user", "content": [block]})
        self._mark_cache(block)
        return self._generate(
            tool_choice_for(self._force_tool_round,
                            getattr(self, "expect_tool_call", True), True))

    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse:
        blocks = [{"type": "tool_result", "tool_use_id": r.call_id,
                   "content": r.content} for r in results]
        self.messages.append({"role": "user", "content": blocks})
        if blocks:
            self._mark_cache(blocks[-1])
        return self._generate(tool_choice_for(self._force_tool_round,
                                              True, False))


def _object_schema(schema: dict) -> dict:
    """Anthropic requires input_schema to be an object schema.

    A tool that takes no arguments arrives here as {} from FastMCP, which
    is a valid JSON Schema (it matches anything) and an invalid tool
    definition. Filling it in is cheaper than a 400 that names one tool
    out of seventy.
    """
    out = dict(schema or {})
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out
