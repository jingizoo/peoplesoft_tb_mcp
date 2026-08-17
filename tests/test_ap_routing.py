"""The AP tools must be full citizens of the evidence gate.

The failure this pins: "how much do we owe our vendors" was classified
into the AR domain (the word "owe" belonged to receivables), and
get_open_payables carried no domains at all — so the gate discarded the
answer the tool had just grounded and told the user it had no PeopleSoft
result. The tool worked; the turn around it threw the answer away. Every
module-pack and connector tool now registers with the gate, and "owe" is
direction-sensitive: owed TO US is AR, WE owe is AP.
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
from pstb.guards import (FINANCIAL_EVIDENCE_TOOLS,  # noqa: E402
                         _TOOL_DOMAINS, financial_tool_domains,
                         question_financial_domains,
                         wants_all_business_units)


class ControllerQuestionIntentTests(unittest.TestCase):
    """Everyday controller wording must open with evidence, not prose."""

    def test_common_ap_questions_force_a_data_turn(self) -> None:
        from pstb.guards import evidence_intent
        for question in (
                "Any duplicate vendor payments?",
                "What should we accrue?",
                "What is voucher VCH1001 status?",
                "Are we ready to close AP?",
                "Show GRNI at June close"):
            self.assertEqual(evidence_intent(question), "data", question)

    def test_paid_as_a_verb_still_requires_ap_evidence(self) -> None:
        from pstb.guards import evidence_intent
        question = "Which records show we paid anything twice?"
        self.assertEqual(evidence_intent(question), "data")
        self.assertIn("ap", question_financial_domains(question))

    def test_delivered_ap_reconciliation_report_names_require_evidence(self) -> None:
        from pstb.guards import evidence_intent
        for code in ("APY1400", "APY1405", "APY1410", "APY1420"):
            question = f"Is the {code} reconciliation clean?"
            self.assertEqual(evidence_intent(question), "data", question)
            self.assertIn("ap", question_financial_domains(question), question)
        for code in ("APY1410", "APY1420"):
            self.assertIn(
                "balance",
                question_financial_domains(f"Does {code} reconcile?"), code)

    def test_gl_close_question_forces_a_data_turn(self) -> None:
        from pstb.guards import evidence_intent
        self.assertEqual(evidence_intent("Are we ready to close GL?"),
                         "data")

    def test_custom_catalog_question_routes_to_discovery(self) -> None:
        from pstb.guards import evidence_intent
        for question in (
                "Which record has the approval status field?",
                "Find the physical table used for our Phoenix interface",
                "Which invoice record is live rather than staging history?"):
            self.assertEqual(evidence_intent(question), "technical", question)

    def test_catalog_words_cannot_turn_off_financial_evidence(self) -> None:
        from pstb.guards import evidence_intent
        for question in (
                "what records show the trial balance does not tie?",
                "which tables are out of balance?",
                "what records explain the variance in travel?",
                "which records reconcile?"):
            self.assertEqual(evidence_intent(question), "data", question)
        self.assertEqual(
            evidence_intent("which records show we are compliant?"),
            "mixed")


class OweDirectionTests(unittest.TestCase):
    def test_money_we_owe_is_payables(self) -> None:
        for q in ("how much do we owe our vendors right now?",
                  "what do we owe suppliers",
                  "show unpaid vouchers",
                  "open payables by vendor"):
            self.assertIn("ap", question_financial_domains(q),
                          f"{q!r} did not classify as payables")
            self.assertNotIn("ar", question_financial_domains(q),
                             f"{q!r} still demands receivables evidence — "
                             "no AP tool can ever satisfy that")

    def test_money_owed_to_us_is_receivables(self) -> None:
        for q in ("who owes us money?", "how much is owed to us"):
            self.assertIn("ar", question_financial_domains(q))
            self.assertNotIn("ap", question_financial_domains(q))

    def test_every_question_domain_is_coverable_by_some_tool(self) -> None:
        # A domain no tool can ground is a refusal machine: any question
        # matching it can never pass the gate no matter what runs.  The one
        # deliberate exception is broad/booked GRNI: no current curated tool
        # proves it, so it must remain gated until governed accounting-line
        # evidence is added (guarded run_sql can still ground an approved
        # site-specific source dynamically).
        from pstb.guards import _QUESTION_DOMAINS
        intentionally_dynamic = {"grni"}
        coverable = set()
        for domains in _TOOL_DOMAINS.values():
            coverable |= domains
        for name in _QUESTION_DOMAINS:
            if name in intentionally_dynamic:
                continue
            self.assertIn(name, coverable,
                          f"domain {name!r} exists in questions but no tool "
                          "can ever ground it")
        self.assertNotIn("grni", _TOOL_DOMAINS["get_po_grni_candidates"])


class RegistrationTests(unittest.TestCase):
    PACK_TOOLS = ("get_open_payables", "get_vendor_payments",
                  "get_asset_register", "get_project_costs",
                  "get_coupa_invoices", "get_coupa_stuck_approvals",
                  "get_coupa_rni", "get_coupa_supplier_spend",
                  "coupa_to_ap_tie")

    def test_pack_tools_count_as_financial_evidence(self) -> None:
        for tool in self.PACK_TOOLS:
            self.assertIn(tool, FINANCIAL_EVIDENCE_TOOLS,
                          f"{tool} succeeds but counts for nothing — the "
                          "gate will refuse answers it grounded")
            self.assertTrue(financial_tool_domains(tool),
                            f"{tool} has no domains, so it can never cover "
                            "a question's requirement")

    def test_open_payables_covers_the_overdue_vendor_question(self) -> None:
        needed = question_financial_domains("overdue vendor invoices")
        self.assertTrue(
            needed.issubset(financial_tool_domains("get_open_payables")),
            f"needs {needed}, tool grounds "
            f"{financial_tool_domains('get_open_payables')}")


class ReconciliationIntentTests(unittest.TestCase):
    def test_a_tie_out_question_with_approved_is_data(self) -> None:
        from pstb.guards import evidence_intent
        for q in ("Did everything approved in Coupa land in AP?",
                  "does the subledger tie out to the control account",
                  "were the invoices matched against vouchers"):
            self.assertEqual(evidence_intent(q), "data",
                             f"{q!r} demanded policy evidence — a grounded "
                             "tie-out answer would be replaced by a wiki "
                             "refusal")

    def test_an_explicit_policy_word_still_wins(self) -> None:
        from pstb.guards import evidence_intent
        self.assertNotEqual(
            evidence_intent("what is our reconciliation policy?"), "data")


class CrossBuPhrasingTests(unittest.TestCase):
    def test_across_counts_without_the_word_all(self) -> None:
        for q in ("compare revenue across business units",
                  "top customers across bus",
                  "spend across units this year",
                  "cross-bu view of payables"):
            self.assertTrue(wants_all_business_units(q), q)

    def test_single_unit_questions_stay_scoped(self) -> None:
        for q in ("show the aging for US001",
                  "trial balance please",
                  "what is in the unit's ledger"):
            self.assertFalse(wants_all_business_units(q), q)


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

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


class HowMuchDoWeOweEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The user's verbatim complaint, as a permanent regression."""

    async def test_the_payables_answer_survives_the_gate(self) -> None:
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(id="a", name="get_open_payables",
                                             args={})]),
            LLMResponse(text="You owe 115,200.00 across 4 open vouchers; "
                             "27,650.00 of it is overdue."),
        ])
        session = FakeSession({"get_open_payables": {
            "business_unit": "US001", "open_total": 115200.0,
            "overdue_total": 27650.0, "voucher_count": 4,
            "by_vendor": [{"vendor": "Cobalt IT Services",
                           "open_amount": 91650.0}]}})
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider, session,
                "how much do we owe our vendors right now?",
                surface="gui",
                scope={"business_unit": "US001", "ledger": "ACTUALS"})
        self.assertIn("115,200.00", answer,
                      "the gate discarded an answer get_open_payables "
                      "grounded — the exact bug this file exists to pin")
        self.assertNotIn("could not obtain", answer)


if __name__ == "__main__":
    unittest.main()
