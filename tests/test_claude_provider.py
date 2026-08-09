"""The Claude provider, without an API key or a network.

Everything asserted here is a property of the REQUEST this provider builds
and the TRANSCRIPT it keeps, because those are what differ from the other
two providers and what a wrong change breaks silently:

  - a dangling tool_use is closed before the next question, or Anthropic
    rejects that question with an error about the previous one;
  - assistant turns are replayed verbatim, because thinking blocks carry
    signatures the API validates;
  - the forced tool round lands on the user turn and nowhere else;
  - an optional feature the account or model will not take is dropped and
    the turn still answers.
"""
from __future__ import annotations

import copy
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.llm_base import PROVIDERS, ToolResult, ToolSpec  # noqa: E402
from pstb.client.llm_claude import (  # noqa: E402
    ClaudeProvider,
    api_key_remedy,
    tool_choice_for,
)
from pstb.config import Config  # noqa: E402


# ---------------------------------------------------------------- test double

class FakeAuthError(Exception):
    pass


class FakeBadRequest(Exception):
    pass


def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def reply(*blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason,
                           stop_details=stop_details)


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _Endpoint:
    def __init__(self, client, beta):
        self.client, self.beta = client, beta

    def stream(self, **kwargs):
        # Snapshot: the provider passes its live message list, which keeps
        # growing after the call. Asserting against it later would test the
        # end state, not what was sent.
        self.client.calls.append(dict(copy.deepcopy(kwargs), _beta=self.beta))
        nxt = self.client.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Stream(nxt)


class FakeClient:
    def __init__(self, script, auth=True, **_):
        self.script = list(script)
        self.calls: list = []
        self.auth_headers = {"x-api-key": "k"} if auth else {}
        self.messages = _Endpoint(self, beta=False)
        self.beta = SimpleNamespace(messages=_Endpoint(self, beta=True))


def fake_sdk(script, auth=True):
    mod = types.ModuleType("anthropic")
    mod.AuthenticationError = FakeAuthError
    mod.BadRequestError = FakeBadRequest
    mod.Anthropic = lambda **kw: FakeClient(script, auth=auth)
    return mod


TOOLS = [ToolSpec(name="get_trial_balance", description="TB",
                  schema={"type": "object",
                          "properties": {"business_unit": {"type": "string"}}}),
         ToolSpec(name="list_financial_scopes", description="scopes",
                  schema={})]


def build(script, auth=True, **overrides):
    cfg = Config.sample(ROOT)
    for key, value in overrides.items():
        setattr(cfg.llm, key, value)
    with patch.dict(sys.modules, {"anthropic": fake_sdk(script, auth)}):
        return ClaudeProvider(cfg, "SYSTEM PROMPT", TOOLS)


# ---------------------------------------------------------------- pure bits

class ToolChoiceTests(unittest.TestCase):
    """Same discipline as Gemini's tool_mode, by the only mechanism this
    API offers — Claude rejects temperature, so there is no greedy-decoding
    half of the pair."""

    def test_forced_only_on_the_user_turn_of_a_tool_question(self) -> None:
        self.assertEqual(tool_choice_for(True, True, True), {"type": "any"})

    def test_chained_turns_can_always_answer(self) -> None:
        self.assertEqual(tool_choice_for(True, True, False), {"type": "auto"})

    def test_small_talk_is_never_forced_into_a_call(self) -> None:
        self.assertEqual(tool_choice_for(True, False, True), {"type": "auto"})

    def test_the_feature_can_be_switched_off(self) -> None:
        self.assertEqual(tool_choice_for(False, True, True), {"type": "auto"})


class ConfigTests(unittest.TestCase):
    def test_defaults_exist(self) -> None:
        cfg = Config.sample(ROOT)
        self.assertEqual(cfg.llm.claude_model, "claude-opus-5")
        self.assertTrue(cfg.llm.claude_force_tool_round)
        self.assertTrue(cfg.llm.claude_fallbacks)
        self.assertGreaterEqual(cfg.llm.claude_max_tokens, 8000)

    def test_claude_is_a_reachable_provider_everywhere(self) -> None:
        # A provider build_provider knows about but no surface offers is
        # not shipped. The console reads its choices from this same tuple.
        from pstb import settings as st
        self.assertIn("claude", PROVIDERS)
        self.assertEqual(st.BY_KEY["llm.provider"].choices, PROVIDERS)

    def test_the_key_is_settable_without_an_editor(self) -> None:
        from pstb import settings as st
        self.assertIn("ANTHROPIC_API_KEY", st.SECRET_KEYS)
        self.assertIn("ANTHROPIC_API_KEY", st.SECRET_LABELS)


class RemedyTests(unittest.TestCase):
    def test_it_names_all_three_ways_out(self) -> None:
        text = api_key_remedy()
        self.assertIn("ANTHROPIC_API_KEY", text)
        self.assertIn("ant auth login", text)
        self.assertIn("ollama", text)

    def test_construction_fails_with_the_remedy_not_a_traceback(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            build([], auth=False)
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))


# ---------------------------------------------------------------- requests

class RequestShapeTests(unittest.TestCase):
    def test_the_user_turn_forces_a_call_and_the_tool_turn_does_not(self):
        p = build([reply(block("tool_use", id="t1", name="get_trial_balance",
                               input={"business_unit": "US001"})),
                   reply(block("text", text="Balanced."))])
        p.send_user("show the trial balance")
        p.send_tool_results([ToolResult(call_id="t1",
                                        name="get_trial_balance",
                                        content='{"ok": true}')])
        self.assertEqual(p.client.calls[0]["tool_choice"], {"type": "any"})
        self.assertEqual(p.client.calls[1]["tool_choice"], {"type": "auto"})

    def test_small_talk_is_not_forced(self) -> None:
        p = build([reply(block("text", text="Glad it helped."))])
        p.expect_tool_call = False
        p.send_user("thanks")
        self.assertEqual(p.client.calls[0]["tool_choice"], {"type": "auto"})

    def test_temperature_is_never_sent(self) -> None:
        # Opus 5 returns 400 for it, so llm.temperature is deliberately
        # ignored here rather than passed through and rejected.
        p = build([reply(block("text", text="hi"))])
        p.send_user("hi")
        self.assertNotIn("temperature", p.client.calls[0])
        self.assertNotIn("top_p", p.client.calls[0])

    def test_thinking_and_effort_are_explicit(self) -> None:
        p = build([reply(block("text", text="hi"))], claude_effort="xhigh")
        p.send_user("hi")
        sent = p.client.calls[0]
        self.assertEqual(sent["thinking"], {"type": "adaptive"})
        self.assertEqual(sent["output_config"], {"effort": "xhigh"})

    def test_a_tool_with_no_arguments_still_gets_an_object_schema(self):
        p = build([reply(block("text", text="hi"))])
        empty = [t for t in p.tools_payload
                 if t["name"] == "list_financial_scopes"][0]
        self.assertEqual(empty["input_schema"]["type"], "object")
        self.assertEqual(empty["input_schema"]["properties"], {})

    def test_the_fixed_prompt_is_cached(self) -> None:
        p = build([reply(block("text", text="hi"))])
        p.send_user("hi")
        system = p.client.calls[0]["system"]
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})

    def test_only_one_conversation_breakpoint_at_a_time(self) -> None:
        # Four is the per-request limit; a ten-round loop that added one
        # marker per round would exceed it partway through a single turn.
        p = build([reply(block("tool_use", id="t1", name="get_trial_balance",
                               input={})),
                   reply(block("tool_use", id="t2", name="get_trial_balance",
                               input={})),
                   reply(block("text", text="done"))])
        p.send_user("q")
        p.send_tool_results([ToolResult(call_id="t1",
                                        name="get_trial_balance",
                                        content="{}")])
        p.send_tool_results([ToolResult(call_id="t2",
                                        name="get_trial_balance",
                                        content="{}")])
        marked = sum(1 for m in p.messages for b in m["content"]
                     if isinstance(b, dict) and "cache_control" in b)
        self.assertEqual(marked, 1)


# ---------------------------------------------------------------- transcript

class TranscriptTests(unittest.TestCase):
    def test_the_assistant_turn_is_replayed_verbatim(self) -> None:
        # Thinking blocks carry signatures the API validates; anything that
        # rebuilds the turn from parsed text loses them and the next call
        # is rejected.
        thinking = block("thinking", thinking="...", signature="sig")
        call = block("tool_use", id="t1", name="get_trial_balance", input={})
        p = build([reply(thinking, call), reply(block("text", text="done"))])
        p.send_user("q")
        self.assertIs(p.messages[-1]["content"][0], thinking)
        p.send_tool_results([ToolResult(call_id="t1",
                                        name="get_trial_balance",
                                        content="{}")])
        replayed = p.client.calls[1]["messages"][1]["content"]
        self.assertEqual([b.type for b in replayed], ["thinking", "tool_use"])

    def test_tool_results_carry_the_id_the_model_issued(self) -> None:
        p = build([reply(block("tool_use", id="toolu_abc",
                               name="get_trial_balance", input={})),
                   reply(block("text", text="done"))])
        resp = p.send_user("q")
        self.assertEqual(resp.tool_calls[0].id, "toolu_abc")
        p.send_tool_results([ToolResult(call_id="toolu_abc",
                                        name="get_trial_balance",
                                        content='{"rows": []}')])
        sent = p.client.calls[1]["messages"][-1]["content"][0]
        self.assertEqual(sent["tool_use_id"], "toolu_abc")
        self.assertEqual(sent["type"], "tool_result")

    def test_a_dangling_call_is_closed_before_the_next_question(self) -> None:
        """The agent loop stops at MAX_TOOL_ROUNDS even mid-chain. Anthropic
        refuses a user message that follows an unanswered tool_use — and in
        the GUI that message is the user's NEXT question, so the failure
        would land on the wrong one."""
        p = build([reply(block("tool_use", id="t9", name="get_trial_balance",
                               input={})),
                   reply(block("text", text="ok"))])
        p.send_user("first question")
        p.send_user("second question")
        closing = p.client.calls[1]["messages"][-2]["content"]
        self.assertEqual(closing[0]["type"], "tool_result")
        self.assertEqual(closing[0]["tool_use_id"], "t9")
        self.assertTrue(closing[0]["is_error"])

    def test_reset_clears_the_conversation_only(self) -> None:
        p = build([reply(block("text", text="a")),
                   reply(block("text", text="b"))])
        p.send_user("q")
        p.reset()
        p.send_user("q2")
        self.assertEqual(len(p.client.calls[1]["messages"]), 1)
        self.assertEqual(p.client.calls[1]["system"][0]["text"],
                         "SYSTEM PROMPT")


# ---------------------------------------------------------------- failures

class DegradeTests(unittest.TestCase):
    def test_a_rejected_optional_feature_is_dropped_not_fatal(self) -> None:
        p = build([FakeBadRequest("fallbacks: unsupported parameter"),
                   reply(block("text", text="answered anyway"))])
        resp = p.send_user("q")
        self.assertEqual(resp.text, "answered anyway")
        self.assertFalse(p._fallbacks)
        self.assertTrue(p.client.calls[0]["_beta"])
        self.assertFalse(p.client.calls[1]["_beta"])

    def test_forcing_is_given_up_before_thinking(self) -> None:
        # If the two are ever reported as incompatible, thinking shapes
        # every answer and the forced call only shapes the first turn —
        # and chat.py's evidence gates already backstop a skipped tool.
        p = build([FakeBadRequest(
                       "thinking is not compatible with tool_choice any"),
                   reply(block("text", text="ok"))], claude_fallbacks=False)
        p.send_user("q")
        self.assertTrue(p._thinking)
        self.assertFalse(p._force_tool_round)
        self.assertEqual(p.client.calls[1]["tool_choice"], {"type": "auto"})

    def test_an_unrelated_bad_request_is_not_swallowed(self) -> None:
        p = build([FakeBadRequest("messages.0: unexpected role 'banana'")],
                  claude_fallbacks=False)
        with self.assertRaises(FakeBadRequest):
            p.send_user("q")

    def test_mid_session_auth_failure_gets_the_remedy(self) -> None:
        p = build([FakeAuthError("invalid x-api-key")])
        with self.assertRaises(RuntimeError) as caught:
            p.send_user("q")
        self.assertIn("ant auth login", str(caught.exception))

    def test_a_refusal_is_reported_not_crashed_on(self) -> None:
        p = build([reply(stop_reason="refusal",
                         stop_details=SimpleNamespace(category="cyber"))])
        resp = p.send_user("q")
        self.assertIn("declined", resp.text)
        self.assertIn("cyber", resp.text)
        self.assertEqual(resp.tool_calls, [])

    def test_hitting_the_output_cap_says_so(self) -> None:
        p = build([reply(stop_reason="max_tokens")])
        resp = p.send_user("q")
        self.assertIn("claude_max_tokens", resp.text)


if __name__ == "__main__":
    unittest.main()
