"""explain_balance_change: the bridge has to tie, and it has to refuse the
comparisons that tie while being wrong.

The dangerous failure here is not an error — it is a decomposition that
sums perfectly, names a direction, and is backwards. Three cases in the
bundled sample produce exactly that if the rules below are removed, so
these tests use real data rather than fixtures:

  * FY2025 period 998 holds a balanced 25,000.00 audit accrual. Drop the
    post-adjustment rule and accounts 2100 and 6900 appear as phantom
    drivers of a liability that never moved.
  * Expense accounts are year-to-date. Compare FY2026 P6 against FY2025
    P12 and the sign inverts: -860,604.80 "decrease" where the
    like-for-like answer is +116,695.20 "increase".
  * Period 999 is where Close Ledger writes the year-end entries. Anchor
    on it and the close itself is reported as the change.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import BALANCE_EPS, EngineError, TBEngine  # noqa: E402

BU = "US001"


def _engine() -> TBEngine:
    cfg = load_config(str(ROOT / "config.yaml"))
    return TBEngine(Database(cfg), cfg)


class TieTests(unittest.TestCase):
    """The claim the tool makes about itself."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = _engine()

    def _bridge(self, **kw):
        return self.engine.explain_balance_change(business_unit=BU, **kw)

    def test_members_sum_to_the_total_change(self) -> None:
        for kw in ({"account": "1000-1999", "fiscal_year": 2026, "period": 6,
                    "vs_fiscal_year": 2025, "vs_period": 12},
                   {"account": "6000-6999", "by": "DEPTID",
                    "fiscal_year": 2026, "period": 6, "vs_period": 5},
                   {"account": "2000-2999", "fiscal_year": 2026, "period": 6,
                    "vs_period": 5}):
            out = self._bridge(**kw)
            rec = out["reconciliation"]
            self.assertLess(abs(rec["residual"]), BALANCE_EPS, kw)
            self.assertAlmostEqual(rec["members_sum"], rec["total_change"], 2,
                                   msg=str(kw))

    def test_the_residual_can_actually_move(self) -> None:
        # A tie computed by re-summing the same pivot under a different key
        # is algebraically identical for any input, so it certifies nothing.
        # total_change is walked from the raw rows instead; corrupt a member
        # and the residual must notice.
        import pstb.engine as eng
        real = eng.TBEngine._pivot
        out = self._bridge(account="1000-1999", fiscal_year=2026, period=6,
                           vs_period=5)
        self.assertLess(abs(out["reconciliation"]["residual"]), BALANCE_EPS)

        def lossy(self, rows, key_fields, period, include_adj):
            got = real(self, rows, key_fields, period, include_adj)
            if len(key_fields) > 1 or len(got) < 2:
                return got
            got.pop(sorted(got)[0])  # drop one member from the bridge side
            return got

        eng.TBEngine._pivot = lossy
        try:
            with self.assertRaises(EngineError) as ctx:
                self._bridge(account="1000-1999", fiscal_year=2026,
                             period=6, vs_period=5)
            self.assertIn("does not tie", str(ctx.exception))
        finally:
            eng.TBEngine._pivot = real

    def test_it_agrees_with_compare_trial_balance(self) -> None:
        scope = {"business_unit": BU, "account": "1000-1999",
                 "fiscal_year": 2026, "period": 6,
                 "vs_fiscal_year": 2025, "vs_period": 12}
        bridge = self.engine.explain_balance_change(**scope)
        compare = self.engine.compare_trial_balance(**scope)
        self.assertAlmostEqual(bridge["total_change"],
                               compare["total_change"], 2,
                               msg="the two tools must not disagree about "
                                   "the same movement")

    def test_no_negative_zero_reaches_the_reader(self) -> None:
        rec = self._bridge(account="1000-1999", fiscal_year=2026, period=6,
                           vs_fiscal_year=2025, vs_period=12)["reconciliation"]
        for field in ("residual", "rounding_drift"):
            self.assertEqual(repr(rec[field]), "0.0",
                             f"{field} printed as -0.0, which reads as a "
                             "problem in a block asserting there is none")


class PostAdjustmentBasisTests(unittest.TestCase):
    """FY2025 P998 carries a balanced 25,000.00 accrual: 6900 debit, 2100
    credit. It is the phantom-driver trap, in real bundled data."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = _engine()

    def test_an_adjusted_liability_is_not_reported_as_a_mover(self) -> None:
        out = self.engine.explain_balance_change(
            account="2000-2999", business_unit=BU, fiscal_year=2026,
            period=6, vs_fiscal_year=2025, vs_period=12)
        by_member = {d["member"]: d["contribution"] for d in out["drivers"]}
        self.assertEqual(by_member.get("2100"), 0.0,
                         "account 2100 did not move — its 25,000.00 is the "
                         "P998 accrual, already inside FY2026's opening")
        self.assertAlmostEqual(out["total_change"], -130403.15, 2)

    def test_the_basis_is_disclosed_not_assumed(self) -> None:
        crossing = self.engine.explain_balance_change(
            account="2000-2999", business_unit=BU, fiscal_year=2026,
            period=6, vs_fiscal_year=2025, vs_period=12)
        self.assertIn("POST-adjustment", crossing["reconciliation"]["basis"])
        within = self.engine.explain_balance_change(
            account="2000-2999", business_unit=BU, fiscal_year=2026,
            period=6, vs_period=5)
        self.assertIn("adjustments excluded",
                      within["reconciliation"]["basis"])


class RefusalTests(unittest.TestCase):
    """Each of these ties perfectly while being wrong. Refusing beats
    answering."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = _engine()

    def _refused(self, **kw) -> str:
        with self.assertRaises(EngineError) as ctx:
            self.engine.explain_balance_change(business_unit=BU, **kw)
        return str(ctx.exception)

    def test_income_statement_across_unequal_windows(self) -> None:
        msg = self._refused(account="6000-6999", fiscal_year=2026, period=6,
                            vs_fiscal_year=2025, vs_period=12)
        self.assertIn("YEAR-TO-DATE", msg)
        self.assertIn("vs_period=6", msg, "the remedy must name the fix")

    def test_but_a_like_for_like_year_over_year_is_allowed(self) -> None:
        out = self.engine.explain_balance_change(
            account="6000-6999", business_unit=BU, fiscal_year=2026,
            period=6, vs_fiscal_year=2025, vs_period=6)
        self.assertAlmostEqual(out["total_change"], 116695.20, 2)
        self.assertEqual(out["direction"], "increase")

    def test_and_a_same_year_expense_comparison_is_allowed(self) -> None:
        # YTD P6 minus YTD P5 is one month of spend — a real quantity.
        out = self.engine.explain_balance_change(
            account="6000-6999", business_unit=BU, fiscal_year=2026,
            period=6, vs_period=5)
        self.assertGreater(out["total_change"], 0)

    def test_the_close_period_is_not_a_point_in_time(self) -> None:
        self.assertIn("CLOSING", self._refused(
            account="1000-1999", fiscal_year=2026, period=6, vs_period=999))

    def test_an_adjustment_period_is_not_an_anchor(self) -> None:
        self.assertIn("adjustment bucket", self._refused(
            account="1000-1999", fiscal_year=2025, period=998, vs_period=12))

    def test_a_blank_account_is_refused_toward_compare(self) -> None:
        msg = self._refused(account="", fiscal_year=2026, period=6)
        self.assertIn("compare_trial_balance", msg,
                      "the whole trial balance nets to zero — send them to "
                      "the tool that finds the subject")

    def test_mixed_account_types_cannot_share_a_total(self) -> None:
        self.assertIn("signed", self._refused(
            account="1000-9999", fiscal_year=2026, period=6, vs_period=5))

    def test_only_one_dimension_per_call(self) -> None:
        self.assertIn("ONE dimension", self._refused(
            account="6000-6999", by="DEPTID,PRODUCT", fiscal_year=2026,
            period=6, vs_period=5))

    def test_an_unknown_dimension_names_the_allowed_set(self) -> None:
        self.assertIn("VENDOR_ID", self._refused(
            account="6000-6999", by="VENDOR_ID", fiscal_year=2026,
            period=6, vs_period=5))


class DisclosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = _engine()

    def test_a_defaulted_dimension_says_so(self) -> None:
        out = self.engine.explain_balance_change(
            account="1000-1999", business_unit=BU, fiscal_year=2026,
            period=6, vs_period=5)
        self.assertEqual(out["by"], "ACCOUNT")
        self.assertIn("defaulted", out["by_source"])
        single = self.engine.explain_balance_change(
            account="1100", business_unit=BU, fiscal_year=2026, period=6,
            vs_period=5)
        self.assertEqual(single["by"], "DEPTID",
                         "splitting one account BY account is a no-op")

    def test_an_explicit_dimension_is_never_overridden(self) -> None:
        out = self.engine.explain_balance_change(
            account="6000-6999", by="deptid", business_unit=BU,
            fiscal_year=2026, period=6, vs_period=5)
        self.assertEqual(out["by"], "DEPTID")
        self.assertEqual(out["by_source"], "explicit")

    def test_a_missing_comparison_period_is_flagged_not_narrated(self) -> None:
        # Ties perfectly, and means nothing: every balance looks brand new.
        out = self.engine.explain_balance_change(
            account="1000-1999", business_unit=BU, fiscal_year=2026,
            period=6, vs_fiscal_year=2019, vs_period=12)
        self.assertEqual(out["scope_status"], "comparison_period_empty")
        self.assertIn("missing baseline", out["note"])

    def test_both_sides_empty_is_no_data_not_a_zero_change(self) -> None:
        out = self.engine.explain_balance_change(
            account="7777", business_unit=BU, fiscal_year=2026, period=6,
            vs_period=5)
        self.assertTrue(out["no_data"])
        self.assertIn("NO DATA", out["detail"])

    def test_it_declares_its_percentages(self) -> None:
        # Undeclared percentages draw a rate caveat on a healthy answer.
        out = self.engine.explain_balance_change(
            account="6000-6999", by="DEPTID", business_unit=BU,
            fiscal_year=2026, period=6, vs_period=5)
        self.assertIsNotNone(out["total_change_pct"])
        for d in out["drivers"]:
            self.assertIn("share_of_gross_pct", d)
            self.assertIn("change_pct", d)

    def test_it_never_offers_price_volume_mix(self) -> None:
        out = self.engine.explain_balance_change(
            account="6000-6999", by="DEPTID", business_unit=BU,
            fiscal_year=2026, period=6, vs_period=5)
        self.assertIn("no quantity column", out["note"],
                      "an inferred average price from a GL balance is "
                      "fabricated, and the payload has to say so")

    def test_entries_and_exits_are_counted_not_matched(self) -> None:
        out = self.engine.explain_balance_change(
            account="1000-1999", business_unit=BU, fiscal_year=2026,
            period=6, vs_fiscal_year=2025, vs_period=12)
        self.assertIn("entered", out)
        self.assertIn("exited", out)
        self.assertIn("one exit and one entry", out["note"],
                      "a fuzzy rename match would silently delete a real "
                      "driver, so the limitation is stated instead")


class SortStabilityTests(unittest.TestCase):
    def test_tied_members_order_deterministically(self) -> None:
        # Set iteration order varies with PYTHONHASHSEED, so a sort on
        # magnitude alone picks a different top driver run to run.
        engine = _engine()
        runs = {tuple(d["member"] for d in engine.explain_balance_change(
            account="1000-1999", business_unit=BU, fiscal_year=2026,
            period=6, vs_period=5)["drivers"]) for _ in range(5)}
        self.assertEqual(len(runs), 1)


if __name__ == "__main__":
    unittest.main()
