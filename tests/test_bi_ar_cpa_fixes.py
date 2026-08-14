"""CPA regressions for governed Billing and currency-consistent AR."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling, ARError  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.relationships import Relationships  # noqa: E402

BU = "US001"
AS_OF = "2026-08-06"


def _stack(finalized=None):
    cfg = Config.sample(ROOT)
    if finalized is not None:
        cfg.semantics = {
            "billing_invoiced": {"values": list(finalized)}
        }
    db = Database(cfg)
    ar = ARBilling(TBEngine(db, cfg))
    return db, ar, Relationships(ar)


class GovernedBillingSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # RDY represents a legitimate site-specific finalized code. CAN is
        # deliberately included to prove the accounting hard stop wins over
        # a mistaken configuration value.
        cls.db, cls.ar, cls.rel = _stack(("INV", "RDY", "CAN"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_custom_finalized_status_drives_totals_and_ranking(self) -> None:
        totals = self.ar.invoice_totals(BU)
        rdy = next(r for r in totals["by_status"] if r["status"] == "RDY")
        can = next(r for r in totals["by_status"] if r["status"] == "CAN")
        self.assertEqual(rdy["class"], "finalized")
        self.assertEqual(can["class"], "excluded_terminal")
        self.assertTrue(any("were ignored" in note for note in
                            totals.get("record_notes", [])))
        self.assertAlmostEqual(
            totals["invoiced_total_by_currency"]["USD"],
            1088535.19, places=2)

        ranked = self.ar.top_billing_customers(
            BU, n=20, as_of_date=AS_OF, display_currency="USD")
        c1003 = next(c for c in ranked["customers"]
                     if c["cust_id"] == "C1003")
        self.assertEqual(c1003["invoices"], 3)
        self.assertEqual(c1003["billed"], 176100.0)
        self.assertEqual(ranked["population"]["source"],
                         "config.yaml semantics")

    def test_workbench_and_lifecycle_use_the_same_classes(self) -> None:
        workbench = self.ar.billing_workbench(BU, as_of_date=AS_OF)
        classes = {r["status"]: r["class"]
                   for r in workbench["statuses"]}
        self.assertEqual(classes["RDY"], "finalized")
        self.assertEqual(classes["CAN"], "terminal")
        self.assertFalse(
            {"RDY", "CAN"} & {r["status"]
                              for r in workbench["stuck_invoices"]})

        lifecycle = self.ar.invoice_lifecycle(BU, as_of_date=AS_OF)
        life_classes = {r["status"]: r["class"]
                        for r in lifecycle["billing_statuses"]}
        self.assertEqual(life_classes["RDY"], "finalized")
        self.assertEqual(life_classes["CAN"], "terminal")
        self.assertNotIn("bill_rdy",
                         {r["stage"] for r in lifecycle["stages"]})
        self.assertNotIn("bill_can",
                         {r["stage"] for r in lifecycle["stages"]})

    def test_customer_360_never_calls_cancelled_revenue_or_pipeline(self) -> None:
        custom = self.rel.customer_financial_360(
            "C1003", BU, as_of_date=AS_OF)
        rdy = next(r for r in custom["billing"]["by_status"]
                   if r["status"] == "RDY")
        self.assertTrue(rdy["finalized"])
        self.assertEqual(rdy["class"], "finalized")
        self.assertNotIn(
            "RDY", {r["status"]
                    for r in custom["billing"]["not_yet_finalized"]})

        cancelled = self.rel.customer_financial_360(
            "C1005", BU, as_of_date=AS_OF)
        can = next(r for r in cancelled["billing"]["by_status"]
                   if r["status"] == "CAN")
        self.assertFalse(can["finalized"])
        self.assertEqual(can["class"], "terminal")
        self.assertNotIn(
            "CAN", {r["status"]
                    for r in cancelled["billing"]["not_yet_finalized"]})

    def test_an_all_cancelled_finalized_override_is_refused(self) -> None:
        db, ar, _ = _stack(("CAN",))
        try:
            with self.assertRaisesRegex(ARError, "cancelled/terminal"):
                ar.invoice_totals(BU)
        finally:
            db.close()


class CustomerIntelligenceCurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.ar, _ = _stack()
        cls.intel = cls.ar.customer_intelligence(
            BU, n=20, as_of_date=AS_OF, display_currency="USD")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_open_ar_and_overdue_reconcile_to_currency_aware_aging(self) -> None:
        intel = {c["cust_id"]: c for c in self.intel["customers"]}
        for cust_id in ("C1004", "C1006"):
            with self.subTest(customer=cust_id):
                aging = self.ar.aging(
                    BU, customer_id=cust_id, as_of_date=AS_OF,
                    display_currency="USD")
                aged = aging["customers"][0]
                row = intel[cust_id]
                self.assertAlmostEqual(row["open_ar"], aged["total"],
                                       places=2)
                self.assertAlmostEqual(row["overdue_amt"],
                                       aging["overdue_total"],
                                       places=2)
                self.assertAlmostEqual(row["disputed_amt"],
                                       aged["disputed_amt"], places=2)
                self.assertEqual(row["open_ar_currency"], "USD")
                self.assertEqual(row["overdue_currency"], "USD")
                self.assertEqual(row["disputed_currency"], "USD")

    def test_fx_and_amount_provenance_are_explicit(self) -> None:
        c1006 = next(c for c in self.intel["customers"]
                     if c["cust_id"] == "C1006")
        self.assertEqual(c1006["open_ar"], 56760.87)
        self.assertEqual(c1006["open_ar_source_currencies"], ["EUR", "USD"])
        self.assertTrue(any("EUR->USD" in n
                            for n in self.intel["ar_fx_applied"]))
        provenance = " ".join(
            str(v) for v in self.intel["population"]["applied"][2].values())
        self.assertIn("PS_ITEM.BAL_AMT", provenance)
        self.assertIn("PS_RT_RATE_TBL", provenance)


class InvalidBusinessUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.ar, _ = _stack()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_billing_tools_return_no_data_not_zero_or_clean(self) -> None:
        calls = {
            "invoice_totals": lambda: self.ar.invoice_totals("NO_SUCH_BU"),
            "top_billing": lambda: self.ar.top_billing_customers(
                "NO_SUCH_BU", as_of_date=AS_OF, display_currency="USD"),
            "workbench": lambda: self.ar.billing_workbench(
                "NO_SUCH_BU", as_of_date=AS_OF),
            "lifecycle": lambda: self.ar.invoice_lifecycle(
                "NO_SUCH_BU", as_of_date=AS_OF),
        }
        for name, call in calls.items():
            with self.subTest(tool=name):
                out = call()
                self.assertEqual(out["scope_status"],
                                 "business_unit_not_found")
                self.assertIn("NO DATA", out["note"])
                self.assertNotIn("control_status", out)
                self.assertNotIn("total_billed", out)
                self.assertNotIn("invoice_count", out)
                self.assertNotIn("invoiced_total_by_currency", out)


if __name__ == "__main__":
    unittest.main()
