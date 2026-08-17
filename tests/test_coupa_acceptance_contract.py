"""Static controller-acceptance contract for a Coupa-first deployment.

These checks intentionally read only documentation and deterministic eval
metadata.  Connector, routing and evidence behavior remain covered by their
own focused suites; this file prevents the published acceptance pack from
quietly promoting a receipt-event candidate into booked accounting evidence.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals" / "cases.json"
DOCS = (
    ROOT / "docs" / "QUESTIONS.md",
    ROOT / "docs" / "CPA_ACCEPTANCE.md",
    ROOT / "docs" / "SETUP.md",
)
CANDIDATE_TOOLS = {"get_coupa_rni", "get_po_grni_candidates"}


class CoupaAcceptanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw = json.loads(EVALS.read_text(encoding="utf-8"))
        cases = raw["cases"]
        cls.cases = {case["id"]: case for case in cases}
        cls.docs = {
            path.name: path.read_text(encoding="utf-8") for path in DOCS
        }

    def test_eval_ids_are_unique(self) -> None:
        cases = json.loads(EVALS.read_text(encoding="utf-8"))["cases"]
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_period_end_candidate_is_source_scoped_and_inconclusive(self):
        case = self.cases["coupa-rni-period-end-candidates"]
        self.assertEqual(
            case["question"],
            "Show Coupa PO lines with net receipt activity above eligible "
            "invoice coverage at the selected period end, by currency.",
        )
        expect = case["expect"]
        self.assertEqual(expect["any_tool"], ["get_coupa_rni"])
        self.assertTrue(
            {"get_po_grni_candidates", "get_match_exceptions"}.issubset(
                expect["not_tool"]
            )
        )
        self.assertEqual(expect["tool_args_contain"], {
            "business_unit": "US001", "as_of_date": "2026-06-30"
        })
        required_words = {word.lower() for word in expect["answer_contains"]}
        self.assertIn("booked status not evaluated", required_words)
        self.assertIn("incomplete", required_words)

    def test_current_candidate_stays_candidate_only(self) -> None:
        expect = self.cases["coupa-rni-current-event-candidates"]["expect"]
        self.assertEqual(expect["any_tool"], ["get_coupa_rni"])
        self.assertIn("get_po_grni_candidates", expect["not_tool"])
        self.assertIn("booked status not evaluated", [
            value.lower() for value in expect["answer_contains"]
        ])

    def test_broad_and_cross_system_controls_forbid_candidate_shortcuts(self):
        for case_id in (
            "ap-completeness-playbook",
            "ap-accrual-readiness",
        ):
            with self.subTest(case_id=case_id):
                expect = self.cases[case_id]["expect"]
                self.assertTrue(CANDIDATE_TOOLS.issubset(expect["not_tool"]))
                self.assertEqual(expect["any_tool"], ["run_playbook"])
                self.assertEqual(
                    expect["tool_args_contain"]["playbook"],
                    "ap_completeness",
                )
                self.assertIn("incomplete", [
                    value.lower() for value in expect["answer_contains"]
                ])

    def test_exact_invoice_to_voucher_fact_has_no_governed_provider(self):
        expect = self.cases[
            "coupa-invoices-to-ap-period-end-incomplete"]["expect"]
        self.assertTrue({
            "run_playbook", "coupa_to_ap_tie",
            "get_coupa_rni", "get_po_grni_candidates",
        }.issubset(expect["not_tool"]))
        self.assertNotIn("any_tool", expect)
        self.assertNotIn("tool_args_contain", expect)
        self.assertIn("not established", [
            value.lower() for value in expect["answer_contains"]
        ])

    def test_booked_question_requires_not_established_without_bridge(self):
        expect = self.cases["coupa-booked-grni-protection"]["expect"]
        self.assertTrue(CANDIDATE_TOOLS.issubset(expect["not_tool"]))
        self.assertNotIn("any_tool", expect)
        self.assertIn("not established", [
            value.lower() for value in expect["answer_contains"]
        ])

    def test_receipt_identity_requires_matching_allocations(self):
        case = self.cases["coupa-receipt-level-matching-protection"]
        self.assertEqual(
            case["question"],
            "For Coupa receipt R123, which invoice covers it?",
        )
        expect = case["expect"]
        self.assertTrue(CANDIDATE_TOOLS.issubset(expect["not_tool"]))
        self.assertIn("get_coupa_invoices", expect["not_tool"])
        words = [value.lower() for value in expect["answer_contains"]]
        self.assertIn("not established", words)
        self.assertIn("matching-allocation", words)

    def test_docs_publish_the_live_source_and_accounting_boundary(self):
        combined = "\n".join(self.docs.values()).lower()
        self.assertIn("coupa.po_receipt_authority: true", combined)
        self.assertIn("mode=live", combined)
        self.assertIn("mode: live", combined)
        self.assertIn("mode: fixtures", combined)
        self.assertIn("booked status not evaluated", combined)
        self.assertIn("not established", combined)
        self.assertIn("core.inventory.receiving.read", combined)
        self.assertIn("business_unit_path", combined)

    def test_docs_do_not_overclaim_the_current_coupa_to_ap_diagnostic(self):
        combined = "\n".join(self.docs.values()).lower()
        for limitation in ("complete pagination", "same-bu", "selected as-of"):
            self.assertIn(limitation, combined)
        self.assertIn("current diagnostic only", combined)


if __name__ == "__main__":
    unittest.main()
