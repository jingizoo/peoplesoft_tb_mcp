"""Cross-cutting contracts for journal status and GRNI review candidates.

The calculation tests live beside their engines.  This suite pins the parts
that tend to drift when a new tool is wired into the agent: caller scope,
financial-evidence eligibility, Gemini routing, provenance, prompt wording,
and a controller-facing renderer that does not turn incomplete into zero.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.llm_gemini import routing_tool_names  # noqa: E402
from pstb.client.prompt import system_prompt  # noqa: E402
from pstb import queries as q  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import JRNL_STATUS  # noqa: E402
from pstb.guards import (  # noqa: E402
    FINANCIAL_EVIDENCE_TOOLS,
    _TOOL_DOMAINS,
    _TOOL_SCOPE_ARGS,
    evidence_intent,
    financial_result_domains,
    financial_tool_is_relevant,
    question_financial_domains,
    source_of_tool,
    tool_result_status,
)


def _journal_row(code: str, *, journal_id: str = "J123",
                 unpost_seq: int = 0) -> dict:
    actionable = code in {"I", "E", "N", "T", "U", "V"}
    return {
        "journal_key": {
            "business_unit": "US001", "journal_id": journal_id,
            "journal_date": "2026-06-20", "unpost_seq": unpost_seq,
        },
        "header_status_code": code,
        "header_status_label": {
            "P": "Posted", "V": "Valid and ready to post",
            "N": "Needs edit",
        }.get(code, "Delivered journal status"),
        "requires_close_action": actionable,
    }


class RegistrationAndRoutingTests(unittest.TestCase):
    def test_tools_are_scoped_financial_evidence_with_exact_domains(self):
        for tool in ("get_journal_status", "get_po_grni_candidates"):
            self.assertIn(tool, FINANCIAL_EVIDENCE_TOOLS)
            self.assertEqual(source_of_tool(tool),
                             "peoplesoft_gl" if tool == "get_journal_status"
                             else "peoplesoft_ap")

        self.assertEqual(
            _TOOL_SCOPE_ARGS["get_journal_status"],
            {"business_unit": "business_unit", "ledger": "ledger",
             "fiscal_year": "fiscal_year", "period": "period",
             "as_of_date": "as_of_date"},
        )
        self.assertEqual(
            _TOOL_SCOPE_ARGS["get_po_grni_candidates"],
            {"business_unit": "business_unit", "as_of_date": "as_of_date"},
        )
        self.assertEqual(
            _TOOL_DOMAINS["get_journal_status"],
            {"journal", "journal_netting", "journal_posted_by",
             "journal_historical_status"},
        )
        self.assertEqual(
            _TOOL_DOMAINS["get_po_grni_candidates"],
            {"po_grni_candidates", "grni"},
        )
        self.assertNotIn(
            "balance", _TOOL_DOMAINS["get_po_grni_candidates"],
            "candidate schedule math must not ground a booked GL balance",
        )
        self.assertNotIn("ap", _TOOL_DOMAINS["get_po_grni_candidates"])

    def test_narrow_tools_cannot_satisfy_unrelated_financial_questions(self):
        self.assertFalse(financial_tool_is_relevant(
            "get_journal_status", "Does the trial balance tie?"))
        self.assertFalse(financial_tool_is_relevant(
            "get_po_grni_candidates", "Which vendors do we owe?"))
        # Bookedness and breadth are the two claims a candidate list must
        # never authorize. Each has its own domain and its own refusal text;
        # neither is grantable by get_po_grni_candidates.
        for question in (
            "What GRNI is booked today?",
            "What is the booked receipt-accrual liability?",
        ):
            self.assertIn("grni_booked", question_financial_domains(question),
                          question)
            self.assertFalse(financial_tool_is_relevant(
                "get_po_grni_candidates", question), question)
        for question in (
            "What is our total GRNI?",
            "Show me all received not invoiced",
            "What is the complete RNI position?",
        ):
            self.assertIn("grni_complete",
                          question_financial_domains(question), question)
            self.assertFalse(financial_tool_is_relevant(
                "get_po_grni_candidates", question), question)
        # The plain question is answerable — it was not before, and that was
        # the regression this pair of assertions now pins.
        for question in ("Do we have any GRNI?",
                         "Show me received not invoiced for US001"):
            self.assertIn("grni", question_financial_domains(question),
                          question)
            self.assertTrue(financial_tool_is_relevant(
                "get_po_grni_candidates", question), question)
        candidate = (
            "Which PO-linked received-not-invoiced items should we accrue "
            "today?")
        self.assertEqual(question_financial_domains(candidate),
                         {"po_grni_candidates"})
        self.assertTrue(financial_tool_is_relevant(
            "get_po_grni_candidates", candidate))

    def test_controller_wording_forces_the_right_financial_domains(self):
        questions = {
            "What is journal J0001's exact status?": {"journal"},
            "Which journals still need action before close?": {"journal"},
            "Which receipts are GRNI candidates at June close?": {
                "po_grni_candidates"},
            "Does journal J123 net to zero?": {
                "journal", "journal_netting"},
            "Was journal J123 posted by June 30?": {
                "journal", "journal_posted_by"},
            "What was J123's status at June 30?": {
                "journal", "journal_historical_status"},
            "Was journal J123 valid at June 30?": {
                "journal", "journal_historical_status"},
            "Was journal J123 in error at June 30?": {
                "journal", "journal_historical_status"},
            "Was journal J123 unposted at June 30?": {
                "journal", "journal_historical_status"},
        }
        for question, expected in questions.items():
            self.assertEqual(evidence_intent(question), "data", question)
            self.assertTrue(
                expected.issubset(question_financial_domains(question)),
                question,
            )

    def test_gemini_shortlist_keeps_the_specific_tools(self):
        available = {
            "get_journal_status", "get_po_grni_candidates",
            "tb_integrity_check", "get_match_exceptions", "run_playbook",
            "resolve_period", "list_financial_scopes", "search_metadata",
            "get_metadata_context", "run_sql", "wiki_lookup",
        }
        journal = set(routing_tool_names(
            "What is journal J0001's exact status?", available))
        self.assertIn("get_journal_status", journal)
        grni = set(routing_tool_names(
            "Which received-not-invoiced items should we review for June close?",
            available))
        self.assertIn("get_po_grni_candidates", grni)

    def test_result_domains_follow_journal_evidence_legs(self):
        status_only = {
            "status": "evaluated", "evaluated": True,
            "status_evaluated": True, "status_control_passed": True,
            "netting_evaluated": False, "netting_complete": False,
            "cutoff": {"historical_status_reconstructed": False},
            "evidence_completeness": {
                "complete": True, "status_complete": True,
                "population_complete": True, "statuses_classified": True,
                "netting_complete": False,
                "posting_date_claim_available": False,
            },
            "population": {"returned_journals": 1,
                           "population_complete": True},
            "journals": [{**_journal_row("P"), "posted_date": None}],
            "truncated": False,
        }
        grounded = financial_result_domains(
            "get_journal_status", json.dumps(status_only))
        self.assertEqual(grounded, {"journal"})
        for question in (
            "Does journal J123 net to zero?",
            "Was journal J123 posted by June 30?",
            "What was J123's status at June 30?",
            "Was journal J123 valid at June 30?",
            "Was journal J123 in error at June 30?",
            "Was journal J123 unposted at June 30?",
        ):
            self.assertFalse(
                question_financial_domains(question).issubset(grounded),
                question,
            )

        flags_only = {
            **status_only,
            "netting_evaluated": True,
            "netting_complete": True,
            "netting_passed": True,
            "evidence_completeness": {
                **status_only["evidence_completeness"],
                "netting_complete": True,
            },
        }
        self.assertNotIn("journal_netting", financial_result_domains(
            "get_journal_status", json.dumps(flags_only)))

        complete_netting = {
            **status_only,
            "netting_evaluated": True,
            "netting_complete": True,
            "netting_passed": False,
            "evidence_completeness": {
                **status_only["evidence_completeness"],
                "netting_complete": True,
            },
            "journals": [{
                **_journal_row("P"),
                "ledger_scope_confirmed": True,
                "currency_basis_complete": True,
                "line_count": 2,
                "currency": "USD",
                "debit_total": 100.0,
                "credit_total": 99.0,
                "signed_net": 1.0,
                "netting": False,
                "currency_totals": [{
                    "currency": "USD", "line_count": 2,
                    "debit_total": 100.0, "credit_total": 99.0,
                    "signed_net": 1.0, "netting": False,
                    "null_amount_count": 0,
                }],
            }],
        }
        self.assertIn("journal_netting", financial_result_domains(
            "get_journal_status", json.dumps(complete_netting)))

    def test_strict_controls_require_structured_mapping_payloads(self):
        for tool in ("get_journal_status", "get_po_grni_candidates",
                     "reconcile_ap_to_gl"):
            for content in ("plain text result", "[]"):
                ok, _ = tool_result_status(tool, content)
                self.assertFalse(ok, (tool, content))
        self.assertEqual(financial_result_domains(
            "get_journal_status", "plain text result"), set())
        self.assertEqual(financial_result_domains(
            "get_journal_status", "[]"), set())


class EvidenceWhitelistTests(unittest.TestCase):
    @staticmethod
    def _status(tool: str, payload: dict) -> tuple[bool, str]:
        return tool_result_status(tool, json.dumps(payload))

    def test_complete_classified_journal_population_is_evidence(self):
        ok, why = self._status("get_journal_status", {
            "status": "evaluated", "evaluated": True,
            "status_evaluated": True,
            "status_control_passed": False,
            "control_passed": False, "truncated": False,
            "evidence_completeness": {"complete": True,
                                      "status_complete": True,
                                      "population_complete": True,
                                      "statuses_classified": True},
            "population": {"returned_journals": 2,
                           "population_complete": True},
            "journals": [_journal_row("V", journal_id="JV"),
                         _journal_row("P", journal_id="JP")],
        })
        self.assertTrue(ok, why)

    def test_journal_missing_classification_or_cap_is_not_evidence(self):
        base = {
            "status": "evaluated", "evaluated": True,
            "status_evaluated": True,
            "status_control_passed": True,
            "control_passed": True, "truncated": False,
            "evidence_completeness": {"complete": True,
                                      "status_complete": True,
                                      "population_complete": True,
                                      "statuses_classified": True},
            "population": {"returned_journals": 1,
                           "population_complete": True},
            "journals": [_journal_row("P")],
        }
        for mutation in (
            {"truncated": True},
            {"evidence_completeness": {"complete": False,
                                       "population_complete": False}},
            {"journals": [{"header_status_code": "?"}]},
            {"journals": [_journal_row("V")]},
            {"status_evaluated": False},
            {"status_control_passed": None},
            {"status": "incomplete", "evaluated": False},
        ):
            payload = {**base, **mutation}
            ok, _ = self._status("get_journal_status", payload)
            self.assertFalse(ok, mutation)

    def test_complete_grni_candidate_population_is_evidence_for_ap_only(self):
        ok, why = self._status("get_po_grni_candidates", {
            "status": "evaluated", "evaluated": True,
            "coverage": {
                "classification": "po_linked_document_review_only",
                "all_grni_complete": False,
                "point_in_time_complete": True,
            },
            "candidate_basis": {"classification": "review_candidate_only"},
            "conclusion": "po_linked_candidates_present",
            "population": {"complete": True, "candidate_count": 1,
                           "truncated": False},
            "totals_by_currency": {"USD": 1250.0},
            "booked_status": "not_evaluated",
            "lines": [{"currency": "USD", "candidate_amount": 1250.0}],
        })
        self.assertTrue(ok, why)

    def test_partial_or_nonfinite_grni_population_is_not_evidence(self):
        base = {
            "status": "evaluated", "evaluated": True,
            "coverage": {
                "classification": "po_linked_document_review_only",
                "all_grni_complete": False,
                "point_in_time_complete": True,
            },
            "candidate_basis": {"classification": "review_candidate_only"},
            "conclusion": "po_linked_candidates_present",
            "population": {"complete": True, "candidate_count": 1,
                           "truncated": False},
            "totals_by_currency": {"USD": 1250.0},
            "booked_status": "not_evaluated",
            "lines": [{"currency": "USD", "candidate_amount": 1250.0}],
        }
        variants = [
            {**base, "status": "incomplete", "evaluated": False},
            {**base, "population": {"complete": False,
                                    "candidate_count": 1,
                                    "truncated": False}},
            {**base, "population": {"complete": True,
                                    "candidate_count": 1,
                                    "truncated": True}},
            {**base, "coverage": {
                "classification": "all_grni",
                "all_grni_complete": True,
                "point_in_time_complete": True,
            }},
            {**base, "coverage": {
                "classification": "po_linked_document_review_only",
                "all_grni_complete": False,
                "point_in_time_complete": False,
            }},
            {**base, "totals_by_currency": {"USD": math.nan}},
            {**base, "totals_by_currency": {"USD": math.inf}},
            {**base, "totals_by_currency": {"USD": "1250.00"}},
            {**base, "totals_by_currency": {"USD": "n/a"}},
            {**base, "totals_by_currency": {"USD": True}},
            {**base, "totals_by_currency": {"USD": None}},
            {**base, "lines": [{"currency": "USD",
                                  "candidate_amount": "n/a"}]},
            {**base, "lines": [{"currency": "USD",
                                  "candidate_amount": None}]},
            {**base, "candidate_basis": {
                "classification": "booked_liability"}},
            {**base, "booked_status": "posted"},
        ]
        for payload in variants:
            ok, _ = self._status("get_po_grni_candidates", payload)
            self.assertFalse(ok, payload)


class SemanticsAndPresentationTests(unittest.TestCase):
    def test_delivered_journal_codes_keep_their_exact_meaning(self):
        expected = {
            "D": "Deleted", "I": "Posting incomplete", "M": "model",
            "E": "Edit errors", "N": "not edited", "P": "Posted",
            "T": "entry incomplete", "U": "Unposted", "V": "Valid",
            "Z": "cannot unpost",
        }
        self.assertEqual(set(JRNL_STATUS), set(expected))
        for code, text in expected.items():
            self.assertIn(text.lower(), JRNL_STATUS[code].lower(), code)

    def test_close_probe_excludes_informational_codes_but_keeps_null(self):
        db = Database(Config.sample(ROOT))
        try:
            sql = q.unposted_journals(db)
        finally:
            db.close()
        self.assertIn("'P', 'D', 'M', 'Z'", sql)
        self.assertIn("JRNL_HDR_STATUS IS NULL", sql)

    def test_prompt_preserves_truth_boundaries(self):
        prompt = system_prompt(Config.sample(ROOT), provider="gemini")
        for text in ("get_journal_status", "get_po_grni_candidates",
                     "PS_JRNL_HEADER", "current state:",
                     "review-candidate population",
                     "not proof that PO_RECVACCR"):
            self.assertIn(text, prompt)

    def test_gui_has_dedicated_nonzero_safe_renderers(self):
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        for text in (
            "if(name==='get_journal_status') return renderJournalStatus(data);",
            "function renderJournalStatus",
            "Exact journal status matrix",
            "PS_JRNL_HEADER is current state",
            "status_control_passed",
            "Netting unavailable",
            "cannot support a trial-balance",
            "status clear",
            "netting exception",
            "if(name==='get_po_grni_candidates') return renderGRNICandidates(data);",
            "function renderGRNICandidates",
            "GRNI review candidates",
            "PO-linked candidate",
            "Outside scope",
            "Not booked-accrual evidence",
            "No zero or clean conclusion is shown",
        ):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
