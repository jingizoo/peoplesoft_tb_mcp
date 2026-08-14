"""The AP completeness playbook: three systems, one verdict.

The composition rules matter more than the steps: a connector that cannot
be reached must make the run INCOMPLETE (never a pass), fixture mode must
be disclosed in the findings themselves, and adding this playbook must
not change what close_readiness gathers or costs.
"""
from __future__ import annotations

import datetime as dt
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
from pstb.playbooks import PlaybookRunner  # noqa: E402


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
        self.assertEqual(tie["status"], "attention")
        self.assertNotIn("12,750.00", tie["headline"],
                         "the July Coupa miss must not enter a June close")
        self.assertNotIn("CINV-2088", str(tie["detail"]))
        self.assertEqual(tie["detail"]["as_of"], "2026-06-30")
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


if __name__ == "__main__":
    unittest.main()
