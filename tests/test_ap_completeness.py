"""The AP completeness playbook: three systems, one verdict.

The composition rules matter more than the steps: a connector that cannot
be reached must make the run INCOMPLETE (never a pass), fixture mode must
be disclosed in the findings themselves, and adding this playbook must
not change what close_readiness gathers or costs.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.connectors import ConnectorError  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import (  # noqa: E402
    financial_result_domains, financial_tool_is_relevant,
    question_financial_domains, tool_result_status)
from pstb.playbooks import (  # noqa: E402
    PlaybookRunner, _step_accrual_candidates, _step_procurement_tie)


class _FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 14)


class ApCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config.sample(ROOT)
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)
        cls.runner = PlaybookRunner(cls.engine, ARBilling(cls.engine))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def setUp(self) -> None:
        # The fixture calendar/data is intentionally frozen in FY2026.  Pin
        # "today" so historical/current-period assertions do not decay as
        # the wall clock advances.
        date_patch = patch("pstb.playbooks.dt.date", _FixedDate)
        date_patch.start()
        self.addCleanup(date_patch.stop)

    def test_period_end_excludes_later_data_and_marks_snapshots_incomplete(
            self) -> None:
        out = self.runner.run("ap_completeness", fiscal_year=2026, period=6)
        self.assertEqual(out["as_of"], "2026-06-30")
        self.assertEqual(out["data_as_of"], "2026-06-30")
        self.assertEqual(out["verdict"], "incomplete")
        by_id = {s["step"]: s for s in out["steps"]}
        tie = by_id["procurement_tie"]
        self.assertEqual(tie["status"], "skipped")
        self.assertIn("current diagnostic", tie["headline"])
        self.assertNotIn("12,750.00", tie["headline"],
                         "the July Coupa miss must not enter a June close")
        self.assertNotIn("CINV-2088", str(tie["detail"]))
        self.assertEqual(tie["detail"]["requested_as_of"], "2026-06-30")
        self.assertEqual(by_id["accruals"]["status"], "skipped")
        self.assertIn("historical", by_id["accruals"]["headline"])
        self.assertEqual(by_id["voucher_pipeline"]["status"], "skipped")
        self.assertIn("current open/close status",
                      by_id["voucher_pipeline"]["headline"])

    def test_fixture_mode_is_disclosed_in_the_finding(self) -> None:
        out = self.runner.run("ap_completeness", fiscal_year=2026, period=6)
        tie = next(s for s in out["steps"] if s["step"] == "procurement_tie")
        self.assertIn("SAMPLE", tie["headline"],
                      "a demo verdict must not read as live procurement")

    def test_it_gathers_only_its_own_inputs(self) -> None:
        out = self.runner.run("ap_completeness")
        self.assertEqual(set(out["input_timings_ms"]),
                         {"ap_tie", "rni", "payables"},
                         "an AP playbook billing ledger-wide close inputs "
                         "is the cost regression the context split exists "
                         "to prevent")

    def test_close_readiness_inputs_are_unchanged(self) -> None:
        out = self.runner.run("close_readiness")
        self.assertEqual(set(out["input_timings_ms"]),
                         {"integrity", "aging", "workbench",
                          "latest_posted"})

    def test_a_dead_connector_makes_the_run_incomplete(self) -> None:
        class DeadCoupa:
            mode = "live"

            def received_not_invoiced(self):
                raise ConnectorError("coupa is unreachable at https://x")

            def ap_tie(self, db, **kw):
                raise ConnectorError("coupa is unreachable at https://x")

        runner = PlaybookRunner(self.engine, ARBilling(self.engine),
                                coupa=DeadCoupa())
        out = runner.run("ap_completeness")
        self.assertEqual(out["verdict"], "incomplete",
                         "an unreachable connector must never read as "
                         "'AP is complete'")
        skipped = {s["step"] for s in out["steps"]
                   if s["status"] == "skipped"}
        self.assertEqual(skipped,
                         {"procurement_tie", "accruals", "voucher_pipeline"})
        self.assertIn("NOT a pass", out["summary"])

    def test_unscoped_coupa_tie_is_never_called_or_returned(self) -> None:
        class ScopedCoupa:
            mode = "live"
            tie_called = False

            def ap_tie(self, db, **kwargs):
                self.tie_called = True
                return {"evaluated": True,
                        "missing_in_ap": [{"invoice": "FOREIGN-SECRET"}]}

            def received_not_invoiced(self, **kwargs):
                return {"evaluated": False,
                        "reason": "historical current status unavailable"}

        coupa = ScopedCoupa()
        runner = PlaybookRunner(self.engine, ARBilling(self.engine),
                                coupa=coupa)
        out = runner.run("ap_completeness", business_unit="US001",
                         fiscal_year=2026, period=6)
        self.assertFalse(coupa.tie_called)
        self.assertNotIn("FOREIGN-SECRET", json.dumps(out))
        tie = next(row for row in out["steps"]
                   if row["step"] == "procurement_tie")
        self.assertEqual(tie["status"], "skipped")

    def test_an_unfinished_period_uses_current_data_only_as_disclosed_context(
            self) -> None:
        out = self.runner.run("ap_completeness",
                              fiscal_year=2026, period=8)
        self.assertFalse(out["period_has_ended"])
        self.assertEqual(out["as_of"], "2026-08-31")
        self.assertEqual(out["data_as_of"], "2026-08-14")
        self.assertEqual(out["verdict"], "incomplete")
        self.assertTrue(all(s["status"] == "skipped" for s in out["steps"]))
        self.assertIn("current-state-only", out["note"])

    def test_listed_for_discovery(self) -> None:
        names = [p["playbook"]
                 for p in self.runner.list_playbooks()["playbooks"]]
        self.assertIn("ap_completeness", names)

    def test_actual_incomplete_playbook_grounds_only_the_broad_control(self):
        for question in (
            "Is AP complete for month-end?",
            "Are we complete in AP for month-end?",
            "Is month-end AP complete?",
            "Is accounts payable complete for month-end?",
            "Are all AP obligations captured for close?",
            "Are we ready to close AP?",
            "Is AP ready for close?",
            "AP month-end completeness",
            "Did everything that should hit AP actually hit AP?",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"ap_completeness"}, question)
            self.assertTrue(financial_tool_is_relevant(
                "run_playbook", question), question)
            self.assertFalse(financial_tool_is_relevant(
                "get_open_payables", question), question)
        out = self.runner.run("ap_completeness", fiscal_year=2026, period=6)
        self.assertEqual(out["verdict"], "incomplete")
        self.assertEqual(
            financial_result_domains("run_playbook", json.dumps(out)),
            {"ap_completeness"})

    def test_malformed_playbook_cannot_ground_ap_completeness(self):
        out = self.runner.run("ap_completeness", fiscal_year=2026, period=6)
        out["steps"] = out["steps"][:-1]
        self.assertEqual(
            financial_result_domains("run_playbook", json.dumps(out)), set())

    def test_forged_ap_pass_is_rejected_until_missing_controls_exist(self):
        forged = {
            "playbook": "ap_completeness", "business_unit": "WRONG",
            "fiscal_year": 2026, "period": 6, "as_of": "garbage",
            "verdict": "passed", "skipped_count": 0,
            "attention_count": 0,
            "steps": [{"step": step, "status": "ok", "headline": "ok"}
                      for step in ("procurement_tie", "accruals",
                                   "voucher_pipeline")],
        }
        raw = json.dumps(forged)
        self.assertFalse(tool_result_status("run_playbook", raw)[0])
        self.assertEqual(financial_result_domains("run_playbook", raw), set())


class ApCompletenessStepTruthTests(unittest.TestCase):
    @staticmethod
    def _rni(*, count=0, positive=0, conclusion="no_po_linked_candidates",
             exceptions=None, display=0):
        return {
            "status": "evaluated", "evaluated": True,
            "conclusion": conclusion,
            "population": {
                "complete": True, "candidate_count": count,
                "positive_candidate_count": positive,
                "displayed_candidate_count": display,
            },
            "rni_totals_by_currency": ({"USD": float(count)} if count else {}),
            "lines": [{"rni_candidate_amount": 1.0}] * display,
            "exceptions": {
                "counts": exceptions or {
                    "invoice_present_not_eligible": 0,
                    "over_invoiced": 0,
                    "net_credit_invoice_activity": 0,
                    "excluded_receiving_types": 0,
                },
            },
            "booked_status": "not_evaluated",
        }

    def test_candidate_step_uses_full_count_and_never_claims_booking(self):
        status, headline, _ = _step_accrual_candidates({
            "rni": self._rni(count=205, positive=205, display=200),
            "coupa_mode": "live",
        })
        self.assertEqual(status, "skipped")
        self.assertIn("205", headline)
        self.assertIn("not evaluated", headline)
        self.assertNotIn("accrue ", headline.lower())

    def test_zero_candidates_with_exceptions_is_not_clean(self):
        for key in ("over_invoiced", "invoice_present_not_eligible"):
            counts = {
                "invoice_present_not_eligible": 0, "over_invoiced": 0,
                "net_credit_invoice_activity": 0,
                "excluded_receiving_types": 0,
            }
            counts[key] = 1
            status, headline, _ = _step_accrual_candidates({
                "rni": self._rni(
                    conclusion="exceptions_present_no_positive_candidates",
                    exceptions=counts),
                "coupa_mode": "live",
            })
            self.assertEqual(status, "skipped")
            self.assertIn("exception", headline)
            self.assertNotIn("nothing received", headline)

    def test_zero_sequential_collection_is_not_an_ap_pass(self):
        status, headline, _ = _step_accrual_candidates({
            "rni": self._rni(), "coupa_mode": "live"})
        self.assertEqual(status, "skipped")
        self.assertIn("not an atomic", headline)

    def test_current_unscoped_tie_can_never_pass_the_playbook(self):
        status, headline, _ = _step_procurement_tie({
            "ap_tie": {"evaluated": True, "matched": 10,
                       "missing_in_ap": [], "amount_breaks": []},
            "coupa_mode": "live",
        })
        self.assertEqual(status, "skipped")
        self.assertIn("pagination", headline)


if __name__ == "__main__":
    unittest.main()
