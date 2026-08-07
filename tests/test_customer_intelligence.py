"""Customer intelligence: context and computed observations, honestly.

What must hold: enrichments reconcile to the records (product mix sums
tie to bill lines), observations are arithmetic with their figures in
the payload (guard-verifiable, never generated advice), a site missing
an enrichment record loses the COLUMN and is told so — never the tool —
and the whole thing stays within a fixed query budget warm.
"""
from __future__ import annotations

import json
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
from pstb.guards import rate_caveat, rate_findings  # noqa: E402

AS_OF = "2026-08-06"


def _stack(db_path=None):
    cfg = Config.sample(ROOT)
    if db_path:
        cfg.db.sqlite_path = str(db_path)
        cfg.db.use_views = False
    db = Database(cfg)
    return db, ARBilling(TBEngine(db, cfg))


class EnrichmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.ar = _stack()
        cls.out = cls.ar.customer_intelligence(
            business_unit="US001", n=5, as_of_date=AS_OF)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_geography_attaches_to_every_ranked_customer(self) -> None:
        for c in self.out["customers"]:
            self.assertIsNotNone(c["location"],
                                 f"{c['cust_id']} has no location though "
                                 "PS_CUST_ADDRESS is seeded for all")
            self.assertTrue(c["location"]["city"])

    def test_product_mix_reconciles_to_bill_lines(self) -> None:
        con = sqlite3.connect(ROOT / "sample_data" / "ps_sample.db")
        cid = self.out["customers"][0]["cust_id"]
        expected = dict(con.execute(
            "SELECT L.IDENTIFIER, ROUND(SUM(L.NET_EXTENDED_AMT),2) "
            "FROM PS_BI_LINE L JOIN PS_BI_HDR H "
            "ON H.BUSINESS_UNIT=L.BUSINESS_UNIT AND H.INVOICE=L.INVOICE "
            "WHERE H.BILL_STATUS='INV' AND H.BILL_TO_CUST_ID=? "
            "GROUP BY L.IDENTIFIER", (cid,)).fetchall())
        con.close()
        for prod in self.out["customers"][0]["top_products"]:
            self.assertAlmostEqual(prod["amount"],
                                   expected[prod["identifier"]], places=2,
                                   msg="product mix must be the database's "
                                       "own sum, not an approximation")

    def test_payment_behavior_is_computed_not_guessed(self) -> None:
        by_id = {c["cust_id"]: c for c in self.out["customers"]}
        acme = by_id["C1001"]
        self.assertGreater(acme["overdue_amt"], 0)
        self.assertGreater(acme["avg_days_late"], 0)
        self.assertLessEqual(acme["overdue_amt"], acme["open_ar"])

    def test_observations_carry_their_figures(self) -> None:
        kinds = {o["kind"] for o in self.out["observations"]}
        self.assertIn("concentration", kinds)
        self.assertIn("late_payment", kinds)
        payload = [json.dumps(self.out, default=str)]
        for o in self.out["observations"]:
            self.assertEqual(
                rate_caveat(rate_findings(o["text"], payload)), "",
                f"observation states an ungroundable rate: {o['text']!r}")

    def test_concentration_share_matches_the_ranking(self) -> None:
        conc = next(o for o in self.out["observations"]
                    if o["kind"] == "concentration")
        self.assertEqual(conc["share_pct"],
                         self.out["customers"][0]["share_pct"])

    def test_population_uses_the_concept_register(self) -> None:
        applied = self.out["population"]["applied"]
        self.assertEqual(applied[0]["predicate"], "BILL_STATUS = 'INV'")
        self.assertIn("billing_invoiced", applied[0]["source"])

    def test_query_budget_warm(self) -> None:
        self.ar.customer_intelligence(business_unit="US001", n=5,
                                      as_of_date=AS_OF)  # warm
        calls = []
        orig = self.db.query
        self.db.query = lambda sql, p=None, **kw: (
            calls.append(sql), orig(sql, p, **kw))[1]
        try:
            self.ar.customer_intelligence(business_unit="US001", n=5,
                                          as_of_date=AS_OF)
        finally:
            self.db.query = orig
        self.assertLessEqual(
            len(calls), 4,
            f"customer intelligence grew to {len(calls)} warm queries — "
            "ranking, addresses, lines, items is the budget")


class ShapeToleranceTests(unittest.TestCase):
    def test_missing_enrichment_records_degrade_named(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pstb-ci-"))
        try:
            dst = tmp / "s.db"
            shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
            con = sqlite3.connect(dst)
            con.execute("DROP TABLE PS_CUST_ADDRESS")
            con.execute("DROP TABLE PS_BI_LINE")
            con.commit(); con.close()
            db, ar = _stack(dst)
            try:
                out = ar.customer_intelligence(business_unit="US001", n=5,
                                               as_of_date=AS_OF)
                self.assertTrue(out["customers"],
                                "core ranking must survive missing "
                                "enrichment records")
                self.assertIsNone(out["customers"][0]["location"])
                self.assertEqual(out["customers"][0]["top_products"], [])
                notes = " ".join(out.get("record_notes", []))
                self.assertIn("PS_CUST_ADDRESS", notes)
                self.assertIn("PS_BI_LINE", notes)
            finally:
                db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RegistrationTests(unittest.TestCase):
    def test_full_citizen_of_the_gate_and_export(self) -> None:
        from pstb import export
        from pstb.guards import (FINANCIAL_EVIDENCE_TOOLS, _TOOL_SCOPE_ARGS,
                                 financial_tool_domains)
        self.assertIn("get_customer_intelligence", FINANCIAL_EVIDENCE_TOOLS)
        self.assertTrue(
            financial_tool_domains("get_customer_intelligence"))
        self.assertIn("get_customer_intelligence", _TOOL_SCOPE_ARGS)
        db, ar = _stack()
        try:
            out = ar.customer_intelligence(business_unit="US001", n=3,
                                           as_of_date=AS_OF)
            self.assertEqual(export.tabular(out)["label"], "customers")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
