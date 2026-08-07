"""The next-wave tools: lifecycle, DSO, cash outlook, vendor intelligence,
post-close watch. Lean but pointed: each test pins the one property that
makes the tool honest rather than merely present.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.modules import ModulePacks  # noqa: E402
from pstb.playbooks import PlaybookRunner  # noqa: E402

AS_OF = "2026-08-06"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config.sample(ROOT)
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)
        cls.ar = ARBilling(cls.engine)
        cls.modules = ModulePacks(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()


class LifecycleTests(Base):
    def test_stages_cover_interface_to_ar(self) -> None:
        out = self.ar.invoice_lifecycle(business_unit="US001",
                                        as_of_date=AS_OF)
        names = {s["stage"] for s in out["stages"]}
        self.assertTrue({"interface_error", "finalized_not_in_ar",
                         "open_in_ar"} <= names)
        orphan = next(s for s in out["stages"]
                      if s["stage"] == "finalized_not_in_ar")
        self.assertEqual(orphan["amount"], 27500.0,
                         "the seeded orphan invoice must surface")

    def test_bottleneck_is_named_with_its_amount(self) -> None:
        out = self.ar.invoice_lifecycle(business_unit="US001",
                                        as_of_date=AS_OF)
        self.assertIsNotNone(out["bottleneck"])
        self.assertGreater(out["bottleneck"]["amount"], 0)

    def test_missing_creation_timestamp_is_disclosed_not_faked(self) -> None:
        out = self.ar.invoice_lifecycle(business_unit="US001",
                                        as_of_date=AS_OF)
        self.assertNotIn("cycle_time", out,
                         "the sample has no creation timestamps — a cycle "
                         "time here would be fabricated")
        self.assertTrue(any("creation timestamp" in n
                            for n in out.get("record_notes", [])))


class DsoTests(Base):
    def test_series_is_computed_with_the_formula_disclosed(self) -> None:
        out = self.ar.dso_trend(business_unit="US001")
        self.assertIn("formula", out)
        self.assertGreaterEqual(len(out["rows"]), 6)
        for r in out["rows"]:
            if r["dso_days"] is not None:
                self.assertGreater(r["dso_days"], 0)
                self.assertLess(r["dso_days"], 365)

    def test_zero_revenue_period_shows_no_dso(self) -> None:
        out = self.ar.dso_trend(business_unit="US001")
        for r in out["rows"]:
            if r["revenue"] <= 0:
                self.assertIsNone(r["dso_days"],
                                  "a DSO over zero revenue is a "
                                  "misleading number, not a metric")


class CashOutlookTests(Base):
    def test_overdue_is_its_own_bucket(self) -> None:
        out = self.ar.cash_outlook(business_unit="US001", as_of_date=AS_OF)
        buckets = {r["week"] for r in out["rows"]}
        self.assertIn("overdue", buckets)

    def test_net_is_in_minus_out_per_row(self) -> None:
        out = self.ar.cash_outlook(business_unit="US001", as_of_date=AS_OF)
        for r in out["rows"]:
            self.assertAlmostEqual(
                r["net"], round(r["expected_in"] - r["expected_out"], 2),
                places=2)

    def test_currencies_never_merge(self) -> None:
        out = self.ar.cash_outlook(business_unit="US001", as_of_date=AS_OF)
        pairs = [(r["week"], r["currency"]) for r in out["rows"]]
        self.assertEqual(len(pairs), len(set(pairs)),
                         "one row per (week, currency) — merging "
                         "currencies fabricates a number")

    def test_totals_are_precomputed_for_the_summary_sentence(self) -> None:
        # Found by the eval: the model summed buckets in prose and the
        # guard withheld the answer. The payload must carry the totals.
        out = self.ar.cash_outlook(business_unit="US001", as_of_date=AS_OF)
        t = out["totals_by_currency"]["USD"]
        window = [r for r in out["rows"]
                  if r["currency"] == "USD" and r["week"] != "overdue"]
        self.assertAlmostEqual(
            t["expected_in"], round(sum(r["expected_in"] for r in window), 2),
            places=2)
        self.assertAlmostEqual(
            t["net"], round(t["expected_in"] - t["expected_out"], 2),
            places=2)

    def test_it_says_it_is_not_a_forecast(self) -> None:
        out = self.ar.cash_outlook(business_unit="US001", as_of_date=AS_OF)
        self.assertIn("NOT a payment-behavior forecast", out["note"])


class VendorIntelligenceTests(Base):
    def test_all_paying_vendors_rank_with_shares(self) -> None:
        out = self.modules.vendor_intelligence(business_unit="US001",
                                               as_of_date=AS_OF)
        self.assertGreaterEqual(len(out["vendors"]), 3)
        self.assertAlmostEqual(
            sum(v["share_pct"] for v in out["vendors"]), 100.0, places=0)

    def test_timing_is_amount_weighted_days_vs_due(self) -> None:
        out = self.modules.vendor_intelligence(business_unit="US001",
                                               as_of_date=AS_OF)
        for v in out["vendors"]:
            self.assertIn("avg_days_vs_due", v)

    def test_observations_carry_figures(self) -> None:
        out = self.modules.vendor_intelligence(business_unit="US001",
                                               as_of_date=AS_OF)
        for o in out["observations"]:
            self.assertTrue(any(isinstance(v, (int, float))
                                for k, v in o.items() if k != "text"),
                            "an observation without its figure is advice, "
                            "not arithmetic")


class PostCloseWatchTests(Base):
    def test_watch_pins_to_the_latest_posted_period(self) -> None:
        runner = PlaybookRunner(self.engine, self.ar)
        out = runner.run("post_close_watch")
        self.assertEqual((out["fiscal_year"], out["period"]), (2026, 6),
                         "the just-closed period is the one under watch")

    def test_late_posts_step_runs_with_the_period_end(self) -> None:
        runner = PlaybookRunner(self.engine, self.ar)
        out = runner.run("post_close_watch")
        late = next(s for s in out["steps"] if s["step"] == "late_posts")
        self.assertNotEqual(late["status"], "skipped", late["headline"])
        self.assertIn("2026-06-30", late["headline"])

    def test_it_gathers_only_its_own_inputs(self) -> None:
        runner = PlaybookRunner(self.engine, self.ar)
        out = runner.run("post_close_watch")
        self.assertEqual(set(out["input_timings_ms"]),
                         {"integrity", "late_posts", "latest_posted"})


if __name__ == "__main__":
    unittest.main()


class DailyBriefTests(Base):
    def test_the_staged_exceptions_surface_and_quiet_checks_stay_quiet(self):
        runner = PlaybookRunner(self.engine, self.ar)
        out = runner.run("daily_brief")
        by = {s2["step"]: s2["status"] for s2 in out["steps"]}
        self.assertEqual(by["billing_exceptions"], "attention")
        self.assertEqual(by["orphans"], "attention")
        self.assertEqual(by["ap_pipeline"], "attention")
        self.assertEqual(by["duplicates"], "ok",
                         "the staged duplicates are outside the 3-month "
                         "brief window — a quiet check must stay quiet")
        self.assertEqual(by["cash_squeeze"], "ok")
        self.assertEqual(by["late_posts"], "ok")

    def test_it_gathers_only_its_own_inputs(self):
        runner = PlaybookRunner(self.engine, self.ar)
        out = runner.run("daily_brief")
        self.assertEqual(set(out["input_timings_ms"]),
                         {"workbench", "duplicates", "payables",
                          "late_posts", "cash", "latest_posted"})

    def test_a_dead_input_makes_the_brief_incomplete(self):
        class DeadAr(type(self.ar)):
            pass

        runner = PlaybookRunner(self.engine, self.ar)
        original = runner.ar.cash_outlook
        runner.ar.cash_outlook = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("cash source down"))
        try:
            out = runner.run("daily_brief")
        finally:
            runner.ar.cash_outlook = original
        self.assertEqual(out["verdict"], "incomplete",
                         "a brief that could not check cash must never "
                         "read as 'nothing needs attention'")


class BudgetVarianceTests(Base):
    """Budget comparison has its own vocabulary — favourability depends on
    the account type, not the sign of the variance."""

    def _out(self, **kw):
        return self.engine.budget_variance(
            business_unit="US001", fiscal_year=2026, period=6, **kw)

    def test_under_spending_is_favourable_under_earning_is_not(self):
        out = self._out(top=50)
        by = {r["account"]: r for r in out["rows"]}
        travel = by["6400"]
        self.assertGreater(travel["variance"], 0)
        self.assertFalse(travel["favourable"],
                         "spending MORE than budget cannot be favourable")
        cogs = by["5000"]
        self.assertLess(cogs["variance"], 0)
        self.assertTrue(cogs["favourable"])
        rev = by["4000"]
        self.assertLess(rev["variance"], 0)
        self.assertTrue(rev["favourable"],
                        "revenue below budget is MORE negative and means "
                        "more revenue — favourable")

    def test_balance_sheet_excluded_by_default_and_disclosed(self):
        out = self._out()
        self.assertTrue(all(r["account_type"] in ("R", "E")
                            for r in out["rows"]))
        pop = out["population"]
        self.assertGreater(pop["excluded_balance_sheet_accounts"], 0)
        self.assertIn("ACCOUNT_TYPE", pop["applied"][0]["predicate"])
        wide = self._out(include_balance_sheet=True, top=100)
        self.assertTrue(any(r["account_type"] == "A" for r in wide["rows"]))

    def test_totals_never_merge_revenue_and_expense(self):
        out = self._out(top=100)
        self.assertEqual(set(out["totals"]), {"revenue", "expense"})
        for side in out["totals"].values():
            self.assertAlmostEqual(
                side["variance"], round(side["actual"] - side["budget"], 2),
                places=2)

    def test_opening_balances_are_not_in_a_ytd_comparison(self):
        # Period 0 is the balance-sheet opening balance; budgets have none,
        # so folding it in would report it all as variance.
        self.assertIn("period 0 opening balances excluded",
                      self._out()["basis"])

    def test_a_missing_budget_ledger_refuses_with_the_remedy(self):
        from pstb.engine import EngineError
        with self.assertRaises(EngineError) as ctx:
            self._out(budget_ledger="NOSUCH")
        msg = str(ctx.exception)
        self.assertIn("No ledger named", msg)
        self.assertIn("Commitment Control", msg,
                      "sites that budget in KK need to be told, not left "
                      "guessing why the tool found nothing")

    def test_the_budget_ledger_cannot_be_the_actuals_ledger(self):
        from pstb.engine import EngineError
        with self.assertRaises(EngineError):
            self._out(budget_ledger="ACTUALS")
