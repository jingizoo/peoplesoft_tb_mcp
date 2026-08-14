"""Gemini routing discipline: forced tool rounds and greedy tool selection.

The decision logic is pure functions so it tests without the SDK or
credentials. What matters: mode ANY appears ONLY on the user turn of a
tool-needing question — anywhere else it would make a final prose answer
impossible (chained turns) or invent a spurious call (small talk).
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.chat import agent_turn  # noqa: E402
from pstb.client.llm_base import LLMResponse, ToolCall  # noqa: E402
from pstb.client.llm_gemini import (call_temperature, routing_tool_names,  # noqa: E402
                                    tool_mode)
from pstb.config import Config  # noqa: E402


class ToolModeTests(unittest.TestCase):
    def test_forced_only_on_the_user_turn_of_a_tool_question(self) -> None:
        self.assertEqual(tool_mode(True, True, True), "ANY")

    def test_chained_turns_can_always_answer(self) -> None:
        # send_tool_results must stay AUTO or the turn can never end.
        self.assertEqual(tool_mode(True, True, False), "AUTO")

    def test_small_talk_is_never_forced_into_a_call(self) -> None:
        self.assertEqual(tool_mode(True, False, True), "AUTO")

    def test_the_feature_can_be_switched_off(self) -> None:
        self.assertEqual(tool_mode(False, True, True), "AUTO")

    def test_routing_is_greedy_prose_is_not(self) -> None:
        self.assertEqual(call_temperature(0.2, 0.0, "ANY"), 0.0)
        self.assertEqual(call_temperature(0.2, 0.0, "AUTO"), 0.2)

    def test_config_defaults_exist(self) -> None:
        cfg = Config.sample(ROOT)
        self.assertTrue(cfg.llm.gemini_force_tool_round)
        self.assertEqual(cfg.llm.gemini_routing_temperature, 0.0)


class GeminiShortlistTests(unittest.TestCase):
    AVAILABLE = {
        "get_trial_balance", "tb_integrity_check", "run_playbook",
        "get_open_payables", "get_vendor_payments", "get_duplicate_payments",
        "get_coupa_rni", "search_records", "describe_record",
        "profile_record", "compare_records", "run_sql", "explain_query",
        "wiki_lookup", "wiki_search", "wiki_get_page", "get_asset_register",
        "get_ar_aging", "get_invoice_totals", "get_billing_workbench",
        "resolve_period", "list_financial_scopes", "detect_transaction_anomalies",
    }

    def test_ap_question_gets_ap_and_discovery_not_an_unrelated_module(self):
        got = set(routing_tool_names(
            "Any confirmed duplicate vendor payments?", self.AVAILABLE))
        self.assertIn("get_duplicate_payments", got)
        self.assertIn("search_records", got)
        self.assertNotIn("get_asset_register", got)

    def test_gl_close_question_keeps_the_composed_control(self):
        got = set(routing_tool_names("Are we ready to close GL?",
                                     self.AVAILABLE))
        self.assertIn("run_playbook", got)
        self.assertIn("tb_integrity_check", got)
        self.assertNotIn("get_coupa_rni", got)

    def test_custom_question_keeps_the_full_discovery_sequence(self):
        got = set(routing_tool_names(
            "Which custom record has the approval status field?",
            self.AVAILABLE))
        for name in ("search_records", "describe_record", "profile_record",
                     "compare_records", "run_sql"):
            self.assertIn(name, got)

    def test_small_talk_is_not_constrained(self):
        self.assertEqual(routing_tool_names("thanks", self.AVAILABLE), [])


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.routing_questions = []

    def set_routing_question(self, text):
        self.routing_questions.append(text)

    def send_user(self, text):
        return self.responses.pop(0)

    def send_tool_results(self, results):
        return self.responses.pop(0)

    def reset(self):
        pass


class FakeSession:
    def __init__(self, outputs):
        self.outputs = outputs

    async def call_tool(self, name, arguments):
        await asyncio.sleep(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(self.outputs[name]))],
            is_error=False)


class TurnSetsExpectationTests(unittest.IsolatedAsyncioTestCase):
    """The agent loop tells the provider whether this question should open
    with a tool call, from the same intent classification the evidence
    gates use — one judgement, two consumers."""

    async def _run(self, question, responses, outputs=None):
        provider = ScriptedProvider(responses)
        session = FakeSession(outputs or {})
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            await agent_turn(provider, session, question, surface="gui",
                            scope={"business_unit": "US001",
                                   "ledger": "ACTUALS"})
        return provider

    async def test_a_data_question_expects_a_tool_call(self) -> None:
        provider = await self._run(
            "show the AR aging",
            [LLMResponse(tool_calls=[ToolCall(id="a", name="get_ar_aging",
                                              args={})]),
             LLMResponse(text="Aging retrieved.")],
            {"get_ar_aging": {"scope_status": "ok",
                              "totals": {"total": 1.0}}})
        self.assertTrue(provider.expect_tool_call)
        self.assertEqual(provider.routing_questions, ["show the AR aging"])

    async def test_small_talk_does_not(self) -> None:
        provider = await self._run(
            "thanks, that was helpful",
            [LLMResponse(text="Glad it helped.")])
        self.assertFalse(provider.expect_tool_call)


if __name__ == "__main__":
    unittest.main()
