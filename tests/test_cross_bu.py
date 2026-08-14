"""Compound questions that cross the selected scope.

"Top 20 customers across all BUs which are still buying" is one question to
an accountant and used to be impossible here, for two reasons that had
nothing to do with SQL speed. The business unit is a HARD scope field, so a
curated tool asked for another unit — or "ALL" — raised ScopeConflict; and
"still buying" existed as no parameter, so the model's only route was a
per-unit crawl over separate rounds that hit the round cap before it hit an
answer.

Now the user's own words unlock the pin ("across all business units" IS the
user changing scope, in words, for one turn), and the whole chain — cross-BU
grouping, per-unit currency normalization, recency filter, ranking — runs
server-side in one call.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import (ScopeConflict, apply_request_scope,  # noqa: E402
                         wants_all_business_units)


class CrossBuPhraseTests(unittest.TestCase):
    def test_phrasings_that_cross_units_unlock(self) -> None:
        for q in (
            "top 20 customers across all business units still buying",
            "top customers across all bus",
            "who do we bill the most company-wide",
            "revenue for all business units this year",
            "every business unit's top customers",
        ):
            self.assertTrue(wants_all_business_units(q), q)

    def test_ordinary_questions_do_not(self) -> None:
        for q in (
            "top 20 billing customers",
            "show me the trial balance",
            "what business units exist",       # catalog ask, not a crossing
            "all accounts with balances",
        ):
            self.assertFalse(wants_all_business_units(q), q)


class ScopeOverrideTests(unittest.TestCase):
    SCOPE = {"business_unit": "US001", "ledger": "ACTUALS"}

    def test_all_is_a_widening_and_is_allowed_without_the_phrasing(self):
        """Reversed deliberately, on a field report.

        "ALL" is not a different company — it is a superset containing the
        selected one, on a tool built to rank across units and label each
        row. The scope lock exists to stop an answer about US200 being
        presented as US001; widening to every unit, with the unit visible
        per row, is not that failure.

        It was a live false positive: the tool's own docstring tells the
        model that business_unit="ALL" ranks across every unit, so on a
        larger top-N the model took the advice and the guard refused its
        own instruction.
        """
        out = apply_request_scope("get_top_billing_customers",
                                  {"business_unit": "ALL", "n": 10},
                                  self.SCOPE)
        self.assertEqual(out["business_unit"], "ALL")
        self.assertEqual(out["n"], 10)
        self.assertEqual(sorted(out), ["business_unit", "n"],
                         "no extra key may be injected — out becomes the "
                         "MCP arguments and an unknown one is rejected")

    def test_a_lateral_move_to_another_unit_still_refuses(self) -> None:
        # This IS the failure the lock exists for: US200 presented as US001.
        with self.assertRaises(ScopeConflict):
            apply_request_scope("get_top_billing_customers",
                                {"business_unit": "US200"}, self.SCOPE)

    def test_all_is_refused_on_a_tool_that_cannot_honour_it(self) -> None:
        # get_ar_aging would take "ALL" for a literal unit name and return
        # nothing, which reads as "no receivables" rather than an error.
        with self.assertRaises(ScopeConflict):
            apply_request_scope("get_ar_aging",
                                {"business_unit": "ALL"}, self.SCOPE)

    def test_with_the_phrasing_the_models_value_wins(self) -> None:
        out = apply_request_scope("get_top_billing_customers",
                                  {"business_unit": "ALL"}, self.SCOPE,
                                  allow_bu_override=True)
        self.assertEqual(out["business_unit"], "ALL")

    def test_an_omitted_bu_still_defaults_to_the_chip(self) -> None:
        # The override loosens conflicts; it must not remove the default.
        out = apply_request_scope("get_top_billing_customers",
                                  {}, self.SCOPE, allow_bu_override=True)
        self.assertEqual(out["business_unit"], "US001")

    def test_hard_scope_on_other_fields_is_untouched(self) -> None:
        with self.assertRaises(ScopeConflict):
            apply_request_scope("get_trial_balance",
                                {"ledger": "BUDGET"}, self.SCOPE,
                                allow_bu_override=True)


class CrossBuRankingTests(unittest.TestCase):
    """Two business units, different currencies, one stale big spender."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-xbu-"))
        dst = self.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        con = sqlite3.connect(dst)
        con.execute("INSERT INTO PS_BUS_UNIT_TBL_GL VALUES ('EU001','EUR')")
        con.execute(
            "INSERT INTO PS_BUS_UNIT_TBL_FS VALUES "
            "('EU001','European Ops','DEU')")
        cols = [r[1] for r in con.execute("PRAGMA table_info(PS_BI_HDR)")]

        def inv(bu, invid, cust, amt, dt_, cur):
            row = {"BUSINESS_UNIT": bu, "INVOICE": invid,
                   "BILL_TO_CUST_ID": cust, "BILL_STATUS": "INV",
                   "INVOICE_AMOUNT": amt, "INVOICE_DT": dt_,
                   "BI_CURRENCY_CD": cur}
            con.execute(
                f"INSERT INTO PS_BI_HDR VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])

        inv("EU001", "EU-1", "ACME-EU", 500000, "2026-07-15", "EUR")
        inv("EU001", "EU-2", "ACME-EU", 400000, "2026-06-10", "EUR")
        # Large lifetime spend, but silent for almost a year.
        inv("EU001", "EU-3", "OLDCO-EU", 900000, "2025-09-01", "EUR")
        con.commit()
        con.close()
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.ar = ARBilling(TBEngine(self.db, cfg))

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _all(self, **kw):
        return self.ar.top_billing_customers(
            business_unit="ALL", months=18, display_currency="USD", **kw)

    def test_one_call_ranks_across_units_in_one_currency(self) -> None:
        out = self._all(n=20)
        ids = [c["cust_id"] for c in out["customers"]]
        names = [c.get("name") for c in out["customers"]]
        self.assertIn("ACME-EU", ids)
        self.assertIn("ACME Industrial", names)
        self.assertEqual({c["currency"] for c in out["customers"]}, {"USD"})
        self.assertIn("ALL business units", out["scope"])

    def test_each_row_names_the_units_it_billed_under(self) -> None:
        out = self._all(n=20)
        acme_eu = next(c for c in out["customers"]
                       if c["cust_id"] == "ACME-EU")
        self.assertEqual(acme_eu["business_units"], ["EU001"])

    def test_still_buying_excludes_the_stale_big_spender(self) -> None:
        active = self._all(n=20, active_within_months=3)
        ids = [c["cust_id"] for c in active["customers"]]
        self.assertNotIn(
            "OLDCO-EU", ids,
            "a customer silent since last September ranked as 'still buying'")
        self.assertIn("ACME-EU", ids)
        self.assertIn("last_invoice_dt", active["customers"][0])
        self.assertIn("still buying", active["activity_note"])

    def test_without_the_filter_the_stale_customer_ranks(self) -> None:
        # The recency filter must be OPT-IN: plain "top customers" includes
        # everyone in the window.
        out = self._all(n=20)
        self.assertIn("OLDCO-EU", [c["cust_id"] for c in out["customers"]])

    def test_single_bu_behaviour_is_unchanged(self) -> None:
        out = self.ar.top_billing_customers(business_unit="US001", n=5,
                                            display_currency="USD")
        self.assertEqual(out["business_unit"], "US001")
        self.assertNotIn("scope", out)
        for c in out["customers"]:
            self.assertNotIn("business_units", c)

    def test_single_bu_mixed_currency_refusal_is_unchanged(self) -> None:
        # The sample's US001 bills in USD and EUR; without display_currency
        # the tool has always refused to sum across currencies and returned
        # per-currency rows. The cross-BU work must not have touched that.
        out = self.ar.top_billing_customers(business_unit="US001", n=5)
        self.assertIn("by_currency", out)
        self.assertNotIn("customers", out)


if __name__ == "__main__":
    unittest.main()


class WideningIsDisclosedTests(unittest.TestCase):
    """Widening past the selected unit is allowed, so it must be VISIBLE.

    The scope chip still reads one business unit while the answer covers
    every one. Without per-row attribution the reader cannot tell, and a
    cross-unit ranking silently reads as a single-company one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from pstb.ar import ARBilling
        from pstb.config import Config
        from pstb.db import Database
        from pstb.engine import TBEngine
        cfg = Config.sample(ROOT)
        cls.ar = ARBilling(TBEngine(Database(cfg), cfg))

    def _rows(self, out):
        return out.get("customers") or out.get("by_currency") or []

    def test_every_row_names_the_unit_that_billed_it(self) -> None:
        out = self.ar.top_billing_customers(business_unit="ALL", n=5)
        rows = self._rows(out)
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(r.get("business_unit") or r.get("business_units"),
                            f"cross-unit row carries no unit: {r}")

    def test_the_note_says_the_answer_widened(self) -> None:
        out = self.ar.top_billing_customers(business_unit="ALL", n=5)
        self.assertIn("ALL business units", out["note"])

    def test_a_single_unit_ranking_says_nothing_of_the_kind(self) -> None:
        out = self.ar.top_billing_customers(business_unit="US001", n=5)
        self.assertNotIn("ALL business units", out["note"])
        for r in self._rows(out):
            self.assertNotIn("business_unit", r,
                             "a scoped ranking must not imply it crossed "
                             "units")


if __name__ == "__main__":
    unittest.main()
