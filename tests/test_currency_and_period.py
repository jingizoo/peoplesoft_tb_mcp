"""Two screens disagreeing about the same money, and a period that stopped.

Both were found by an outside review of a month-old snapshot, and both
were still live on main. They share a shape: the answer looked complete,
was internally consistent, and was wrong in a way only a second screen
revealed.

  1. CURRENCY. search_customers summed BAL_AMT across currencies, so
     C1006 read 56,500.00 while the aging beside it — which converts —
     read 56,760.87 USD. The billing workbench did the same to
     INVOICE_AMOUNT by status. Neither said which currency it meant, so
     neither reader could tell which number to believe.
  2. PERIOD. The scope chip's fiscal year and period reached the LEDGER
     tools and stopped. AR, Billing and AP do not filter on FISCAL_YEAR
     and ACCOUNTING_PERIOD — they take a date — so a user could select
     FY2025 P12, read a 2025 trial balance, and read today's receivables
     beside it with nothing on screen admitting they were different
     moments.

The rule both fixes follow is the one this module already had for aging:
convert server-side, fail closed when a rate is missing, and say which
currency and which rate produced the figure.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling, ARError  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402

BU = "US001"


def _ar(db_cls=Database):
    cfg = load_config(str(ROOT / "config.yaml"))
    return ARBilling(TBEngine(db_cls(cfg), cfg))


class SearchCurrencyTests(unittest.TestCase):
    """C1006 holds one EUR item and several USD ones."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ar = _ar()

    def test_search_agrees_with_the_aging_beside_it(self) -> None:
        # The exact discrepancy the review reproduced: 56,500.00 raw
        # against 56,760.87 converted.
        found = self.ar.search_customers(query="C1006", business_unit=BU)
        row = next(c for c in found["customers"] if c["cust_id"] == "C1006")
        aged = self.ar.aging(business_unit=BU, customer_id="C1006")
        self.assertAlmostEqual(row["open_balance"],
                               aged["customers"][0]["total"], places=2)
        self.assertEqual(found["display_currency"], aged["display_currency"])

    def test_it_says_which_currency_the_figure_is_in(self) -> None:
        found = self.ar.search_customers(query="C1006", business_unit=BU)
        self.assertEqual(found["display_currency"], "USD")
        self.assertIn("converted server-side", found["note"])

    def test_the_unconverted_parts_are_shown_for_a_mixed_customer(self):
        row = next(c for c in self.ar.search_customers(
            query="C1006", business_unit=BU)["customers"]
            if c["cust_id"] == "C1006")
        self.assertEqual(set(row["balances_by_currency"]), {"EUR", "USD"})
        self.assertEqual(row["balances_by_currency"]["EUR"], 3_000.00)

    def test_a_single_currency_customer_is_not_cluttered(self) -> None:
        # A breakdown of one is noise on every row of every search.
        row = next(c for c in self.ar.search_customers(
            query="C1001", business_unit=BU)["customers"]
            if c["cust_id"] == "C1001")
        self.assertNotIn("balances_by_currency", row)

    def test_the_rate_that_produced_the_figure_is_declared(self) -> None:
        found = self.ar.search_customers(query="C1006", business_unit=BU)
        self.assertTrue(any("EUR->USD" in f
                            for f in found.get("fx_applied") or []))

    def test_a_missing_rate_refuses_rather_than_dropping_the_euros(self):
        # Silently skipping the unconvertible half would be the same bug
        # wearing a smaller number.
        with patch.object(self.ar.e, "exchange_rate",
                          side_effect=EngineError("no CRRNT rate")):
            with self.assertRaises(ARError) as ctx:
                self.ar.search_customers(query="C1006", business_unit=BU)
        self.assertIn("never summed", str(ctx.exception))

    def test_a_customer_with_no_open_items_is_still_returned(self) -> None:
        # The LEFT JOIN miss must not vanish from a SEARCH.
        found = self.ar.search_customers(query="C1011", business_unit=BU)
        row = next(c for c in found["customers"] if c["cust_id"] == "C1011")
        self.assertEqual(row["open_balance"], 0.0)

    def test_a_site_with_no_currency_column_still_answers(self) -> None:
        class NoCurrency(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "BAL_CURRENCY"}
                        if table == "PS_ITEM" else cols)

        found = _ar(NoCurrency).search_customers(query="C1006",
                                                 business_unit=BU)
        row = next(c for c in found["customers"] if c["cust_id"] == "C1006")
        self.assertTrue(row["open_balance"])


class WorkbenchCurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ar = _ar()
        cls.out = cls.ar.billing_workbench(business_unit=BU)

    def test_finalized_agrees_with_the_currency_aware_ranking(self) -> None:
        inv = next(s for s in self.out["statuses"] if s["status"] == "INV")
        ranked = self.ar.top_billing_customers(business_unit=BU,
                                               display_currency="USD")
        self.assertAlmostEqual(
            inv["amount"], sum(c["billed"] for c in ranked["customers"]),
            places=2)

    def test_every_status_says_which_currency_it_is_in(self) -> None:
        self.assertEqual(self.out["display_currency"], "USD")
        for row in self.out["statuses"]:
            self.assertEqual(row["currency"], "USD")

    def test_the_mixed_status_shows_its_parts(self) -> None:
        inv = next(s for s in self.out["statuses"] if s["status"] == "INV")
        self.assertEqual(set(inv["amounts_by_currency"]), {"EUR", "USD"})

    def test_a_single_currency_status_is_not_cluttered(self) -> None:
        rdy = next((s for s in self.out["statuses"] if s["status"] == "RDY"),
                   None)
        if rdy is not None:
            self.assertNotIn("amounts_by_currency", rdy)

    def test_the_rate_is_declared(self) -> None:
        self.assertTrue(any("EUR->USD" in f
                            for f in self.out.get("fx_applied") or []))

    def test_a_missing_rate_refuses_rather_than_mis_summing(self) -> None:
        with patch.object(self.ar.e, "exchange_rate",
                          side_effect=EngineError("no CRRNT rate")):
            with self.assertRaises(ARError):
                self.ar.billing_workbench(business_unit=BU)

    def test_no_currency_column_is_disclosed_not_assumed_silently(self):
        class NoCurrency(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "BI_CURRENCY_CD"}
                        if table == "PS_BI_HDR" else cols)

        out = _ar(NoCurrency).billing_workbench(business_unit=BU)
        self.assertTrue(any("BI_CURRENCY_CD" in n
                            for n in out["record_notes"]))
        self.assertTrue(out["statuses"])


class PeriodScopeTests(unittest.TestCase):
    """The selected period has to reach the tools that take a date."""

    def test_the_aging_ages_against_the_selected_period_end(self) -> None:
        from pstb.gui import app as gui
        from pstb.guards import apply_request_scope
        scope = gui._validated_scope({"business_unit": BU,
                                      "ledger": "ACTUALS",
                                      "fiscal_year": 2026, "period": 3})
        self.assertEqual(scope["as_of_date"], "2026-03-31")
        args = apply_request_scope("get_ar_aging", {}, scope)
        aged = _ar().aging(**args)
        self.assertEqual(aged["as_of"], "2026-03-31")

    def test_the_backdated_answer_still_carries_its_own_warning(self) -> None:
        # PS_ITEM is current-state, so an as-of in the past is an
        # approximation. Applying the period must not quietly imply it is
        # a reconstruction.
        aged = _ar().aging(business_unit=BU, as_of_date="2026-03-31")
        self.assertTrue(aged.get("historical_approximation"))
        self.assertIn("current-state", aged["warning"])

    def test_a_ledger_tool_still_gets_the_period_not_the_date(self) -> None:
        from pstb.gui import app as gui
        from pstb.guards import apply_request_scope
        scope = gui._validated_scope({"business_unit": BU,
                                      "ledger": "ACTUALS",
                                      "fiscal_year": 2026, "period": 3})
        args = apply_request_scope("get_trial_balance", {}, scope)
        self.assertEqual(args["period"], 3)
        self.assertNotIn("as_of_date", args)

    def test_a_cleared_period_leaves_every_tool_on_its_default(self) -> None:
        from pstb.gui import app as gui
        # "" is what the page's "Any year" option actually submits.
        scope = gui._validated_scope({"business_unit": BU,
                                      "ledger": "ACTUALS",
                                      "fiscal_year": "", "period": ""})
        self.assertNotIn("as_of_date", scope)

    def test_a_malformed_date_in_a_scope_is_refused(self) -> None:
        from pstb.guards import normalize_request_scope
        with self.assertRaises(ValueError):
            normalize_request_scope({"business_unit": BU,
                                     "as_of_date": "31/03/2026"})


class TopBillingWindowTests(unittest.TestCase):
    """A ranking is only as good as the population it ranked.

    Three defects, all of which produce a confident wrong list rather than
    a visibly broken one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.ar = _ar()

    def test_twelve_months_is_a_year_not_three_hundred_and_sixty_days(self):
        from pstb.ar import _months_before
        import datetime as dt
        out = self.ar.top_billing_customers(business_unit=BU,
                                            as_of_date="2026-08-12",
                                            display_currency="USD")
        self.assertEqual(out["since"], "2025-08-12")
        # months*30 landed here, five days late, every year.
        self.assertNotEqual(out["since"], "2025-08-17")
        self.assertEqual(
            _months_before(dt.date(2026, 8, 12), 12), dt.date(2025, 8, 12))

    def test_month_end_clamps_instead_of_overflowing(self) -> None:
        # One month before 31 March is the end of February, not 3 March.
        from pstb.ar import _months_before
        import datetime as dt
        self.assertEqual(_months_before(dt.date(2026, 3, 31), 1),
                         dt.date(2026, 2, 28))
        self.assertEqual(_months_before(dt.date(2024, 3, 31), 1),
                         dt.date(2024, 2, 29))
        self.assertEqual(_months_before(dt.date(2024, 2, 29), 12),
                         dt.date(2023, 2, 28))

    def test_the_window_has_an_UPPER_bound_too(self) -> None:
        # This became live the moment the scope chip's period started
        # arriving here as as_of_date: a backdated ranking was still
        # counting invoices dated after it.
        early = self.ar.top_billing_customers(
            business_unit=BU, as_of_date="2026-03-31", months=120,
            display_currency="USD")
        late = self.ar.top_billing_customers(
            business_unit=BU, as_of_date="2026-08-12", months=120,
            display_currency="USD")
        self.assertLess(early["total_billed"], late["total_billed"])
        for row in early["customers"]:
            self.assertLessEqual(row["last_invoice_dt"] or "", "2026-03-31")

    def test_the_window_is_stated_in_the_note(self) -> None:
        out = self.ar.top_billing_customers(business_unit=BU,
                                            as_of_date="2026-08-12",
                                            display_currency="USD")
        self.assertIn("2025-08-12 to 2026-08-12 inclusive", out["note"])

    def test_a_complete_ranking_says_it_is_complete(self) -> None:
        out = self.ar.top_billing_customers(business_unit=BU,
                                            display_currency="USD")
        self.assertTrue(out["ranking_complete"])
        self.assertNotIn("NOT RELIABLE", out["note"])

    def test_a_cut_off_population_refuses_to_pose_as_a_ranking(self) -> None:
        # The worst of the three: a top-N over a truncated read can name
        # the wrong top customer, which is a wrong answer wearing a right
        # one. Forcing the cap is the only way to exercise it here.
        real = self.ar.db.query

        def capped(sql, params=None, **kw):
            if "PS_BI_HDR" in sql and "BILL_TO_CUST_ID" in sql:
                kw["max_rows"] = 2
            return real(sql, params, **kw)

        self.ar.db.query = capped
        try:
            out = self.ar.top_billing_customers(business_unit=BU,
                                                display_currency="USD")
        finally:
            self.ar.db.query = real
        self.assertFalse(out["ranking_complete"])
        self.assertIn("NOT RELIABLE", out["note"])
        self.assertIn("Narrow the window", out["note"])

    def test_still_buying_uses_calendar_months_as_well(self) -> None:
        out = self.ar.top_billing_customers(
            business_unit=BU, as_of_date="2026-08-12",
            active_within_months=3, display_currency="USD")
        self.assertEqual(out["active_since"], "2026-05-12")
        for row in out["customers"]:
            self.assertGreaterEqual(row["last_invoice_dt"], "2026-05-12")


if __name__ == "__main__":
    unittest.main()
