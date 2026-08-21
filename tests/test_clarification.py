"""A turn may end in a question — under rules that keep it from being abused.

Two governed queues of ambiguity existed and both dead-ended: a guard that
said "multiple matches, ask which" left the model with no way to actually
ask, and a user facing a refusal had to retype their whole question with
the missing detail. ask_user ends the turn with ONE question and concrete
options; the GUI renders them as buttons.

The dangerous half is the exemption it needs. A clarification asserts
nothing, so it may pass the evidence gate — and that is exactly the hole a
confident model would smuggle an unevidenced figure through: "your balance
is 4,212,340.55 — want the detail?" is an answer wearing a question mark.
Every test in the abuse half exists because the exemption does.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from pstb.client.chat import agent_turn
from pstb.client.llm_base import ToolCall
from pstb.guards import (
    CLARIFICATION_TOOL,
    SOURCE_SILO_CHAT_TOOLS,
    SOURCE_SILO_TOOLS,
    clarification_violation,
)


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.user_messages = []
        self.tool_batches = []

    def _next(self):
        if not self.responses:
            raise AssertionError("scripted provider ran out of responses")
        return self.responses.pop(0)

    def send_user(self, text):
        self.user_messages.append(text)
        return self._next()

    def send_tool_results(self, results):
        self.tool_batches.append(results)
        return self._next()

    def reset(self):
        pass


class FakeSession:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        value = self.outputs[name]
        if isinstance(value, list):
            value = value.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(value))],
            is_error=False,
        )


def resp(text="", calls=()):
    return SimpleNamespace(text=text, tool_calls=list(calls))


def call(call_id, name, **args):
    return ToolCall(id=call_id, name=name, args=args)


def ask_payload(question, options):
    return {"clarification": {"question": question, "options": list(options)},
            "turn_ends": True}


def run(provider, session, text, **kw):
    return asyncio.run(agent_turn(provider, session, text, **kw))


class ClarificationGuardTests(unittest.TestCase):
    """The pure rule: which questions are admissible."""

    def test_an_amount_with_no_payload_behind_it_is_refused(self):
        why = clarification_violation("Did you mean 4,212,340.55?", [])
        self.assertIn("4,212,340.55", why)

    def test_years_periods_and_codes_are_the_canonical_legit_question(self):
        for text in ("FY2025 or FY2026?", "Period 6 or period 7?",
                     "US001 or CA001?", "Account 6000-6999 or 7000-7999?"):
            with self.subTest(text=text):
                self.assertEqual(clarification_violation(text, []), "")

    def test_a_figure_a_tool_produced_may_be_asked_about(self):
        payloads = [("run_sql", json.dumps({"rows": [{"amt": 1234.56}]}))]
        self.assertEqual(
            clarification_violation("Is 1,234.56 the total you meant?",
                                    payloads), "")

    def test_stricter_than_the_number_guard_when_nothing_ran(self):
        """ungrounded_figures stays quiet with no payloads (the domain gate
        owns that case for answers). A question would slip through that
        silence, so the clarification rule must not inherit it."""
        from pstb.guards import ungrounded_figures
        text = "Shall I book 9,999.99?"
        self.assertEqual(ungrounded_figures(text, []), [])
        self.assertNotEqual(clarification_violation(text, []), "")


class SiloMembershipTests(unittest.TestCase):
    def test_chat_may_ask_everywhere_including_a_secondary_workspace(self):
        self.assertIn(CLARIFICATION_TOOL, SOURCE_SILO_CHAT_TOOLS)

    def test_the_export_allowlist_did_not_gain_it(self):
        """SOURCE_SILO_TOOLS is also the export route's positive allowlist,
        and an export must never end in a question."""
        self.assertNotIn(CLARIFICATION_TOOL, SOURCE_SILO_TOOLS)


class ServerToolTests(unittest.TestCase):
    def _tool(self):
        import pstb.server as srv
        fn = getattr(srv.ask_user, "fn", srv.ask_user)
        return fn

    def test_a_valid_question_returns_the_structured_form(self):
        out = self._tool()("Which business unit?", "US001, CA001, UK001")
        self.assertEqual(out["clarification"]["options"],
                         ["US001", "CA001", "UK001"])
        self.assertTrue(out["turn_ends"])

    def test_one_option_is_not_a_choice(self):
        with self.assertRaises(ValueError):
            self._tool()("Which one?", "US001")

    def test_seven_options_is_a_listing_not_a_question(self):
        with self.assertRaises(ValueError):
            self._tool()("Which one?", ",".join(f"O{i}" for i in range(7)))

    def test_an_essay_is_not_a_question(self):
        with self.assertRaises(ValueError):
            self._tool()("x" * 301, "a, b")

    def test_an_option_is_a_label_not_an_explanation(self):
        with self.assertRaises(ValueError):
            self._tool()("Which?", "a, " + "b" * 81)


class TurnEndsInAQuestionTests(unittest.TestCase):
    """The agent loop half, driven through the real agent_turn."""

    def test_the_question_ends_the_turn_and_passes_the_gate(self):
        """A financial question with zero evidence normally dies at the
        evidence gate. When the turn ends in an admissible ask_user, the
        question IS the output — and the model is never called again."""
        provider = ScriptedProvider([
            resp(calls=[call("1", CLARIFICATION_TOOL,
                             question="Which business unit did you mean?",
                             options="US001, CA001")]),
        ])
        session = FakeSession({CLARIFICATION_TOOL: ask_payload(
            "Which business unit did you mean?", ["US001", "CA001"])})
        answer = run(provider, session, "what is the AP balance")
        self.assertIn("Which business unit did you mean?", answer)
        self.assertIn("1. US001", answer)
        self.assertIn("2. CA001", answer)
        self.assertNotIn("cannot", answer.lower())
        self.assertEqual(provider.tool_batches, [],
                         "the user speaks next, not the model")

    def test_a_smuggled_amount_is_withheld_with_the_reason(self):
        provider = ScriptedProvider([
            resp(calls=[call("1", CLARIFICATION_TOOL,
                             question="Is the balance 4,212,340.55?",
                             options="yes, no")]),
        ])
        session = FakeSession({CLARIFICATION_TOOL: ask_payload(
            "Is the balance 4,212,340.55?", ["yes", "no"])})
        answer = run(provider, session, "what is the AP balance")
        self.assertIn("withheld", answer)
        self.assertIn("4,212,340.55", answer)
        self.assertNotIn("1. yes", answer,
                         "a withheld question must not render its options")

    def test_the_questions_own_echo_cannot_ground_its_figure(self):
        """The ask_user result contains the question text. If that payload
        joined the grounding set, any smuggled figure would be grounded by
        its own echo and the guard above would never fire."""
        provider = ScriptedProvider([
            resp(calls=[call("1", CLARIFICATION_TOOL,
                             question="Confirm 9,876,543.21?",
                             options="yes, no")]),
        ])
        session = FakeSession({CLARIFICATION_TOOL: ask_payload(
            "Confirm 9,876,543.21?", ["yes", "no"])})
        answer = run(provider, session, "check the payment")
        self.assertIn("withheld", answer)

    def test_a_year_question_needs_no_evidence_at_all(self):
        provider = ScriptedProvider([
            resp(calls=[call("1", CLARIFICATION_TOOL,
                             question="FY2025 or FY2026?",
                             options="FY2025, FY2026")]),
        ])
        session = FakeSession({CLARIFICATION_TOOL: ask_payload(
            "FY2025 or FY2026?", ["FY2025", "FY2026"])})
        answer = run(provider, session, "show me the trial balance trend")
        self.assertIn("FY2025 or FY2026?", answer)
        self.assertIn("2. FY2026", answer)


class PromptRoutingTests(unittest.TestCase):
    """A tool is not a capability: nothing fires it unless the prompt
    routes to it and neighbouring payload guidance points at it."""

    @classmethod
    def setUpClass(cls):
        from pstb.client import prompt as prompt_module
        cls.text = Path(prompt_module.__file__).read_text()

    def test_the_doctrine_section_exists_with_its_rules(self):
        self.assertIn("ask_user", self.text)
        self.assertIn("NEVER invent an option", self.text)
        self.assertIn("NO\n   financial figures", self.text.replace("\r", ""))

    def test_the_ambiguous_customer_payload_points_at_it(self):
        start = self.text.index("ambiguous_customer")
        window = self.text[start:start + 300]
        self.assertIn("ask_user", window)


class BrowserWiringTests(unittest.TestCase):
    """The shipped page, checked statically like the approval panel is."""

    @classmethod
    def setUpClass(cls):
        import pstb.gui.app as gui
        cls.page = (Path(gui.__file__).parent / "static"
                    / "index.html").read_text()

    def test_answers_render_pipe_tables_as_tables(self):
        for needle in ("function pipeTable(", "isPipeRule(",
                       "ans-tablewrap", 'class="num"'):
            self.assertIn(needle, self.page)

    def test_amountish_cells_reuse_the_house_money_format(self):
        start = self.page.index("function tableCell(")
        block = self.page[start:start + 600]
        self.assertIn("money(", block)

    def test_the_options_render_as_buttons_that_send_the_choice(self):
        start = self.page.index("c.tool==='ask_user'")
        block = self.page[start:start + 900]
        self.assertIn("send(String(option),true)", block)

    def test_the_buttons_come_from_the_server_accepted_form(self):
        """Built from the ask_user call's result — never re-parsed from
        the model's prose, which may differ from what the server accepted."""
        start = self.page.index("c.tool==='ask_user'")
        block = self.page[start:start + 900]
        self.assertIn("result.clarification", block)

    def test_the_grounding_stores_exclude_the_questions_echo(self):
        import pstb.gui.app as gui
        source = Path(gui.__file__).read_text()
        start = source.index("def _observe_and_record")
        block = source[start:start + 900]
        self.assertIn("CLARIFICATION_TOOL", block)

    def test_gui_binds_the_clarification_tool_at_runtime(self):
        """A textual reference is not enough: the chat callback resolves
        this name only after a tool returns, so import-time tests missed the
        production NameError."""
        import pstb.gui.app as gui
        self.assertEqual(gui.CLARIFICATION_TOOL, CLARIFICATION_TOOL)


if __name__ == "__main__":
    unittest.main()
