"""The population block: a total that says what it counted.

"Total invoice amount" means finalized bills — but the default must be
visible, its exclusions split into pipeline (could become revenue) vs
cancelled (never will), amounts suppressed when pre-finalization headers
carry zeros, and the tool must refuse rather than answer 0.00 when its
own default emptied the result. One grouped query produces the answer
AND its counterfactual — the disclosure is free.
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
from pstb.guards import payload_rates, rate_findings  # noqa: E402


def _stack(db_path=None):
    cfg = Config.sample(ROOT)
    if db_path:
        cfg.db.sqlite_path = str(db_path)
        cfg.db.use_views = False
    db = Database(cfg)
    return db, ARBilling(TBEngine(db, cfg))


class PopulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.ar = _stack()
        con = sqlite3.connect(ROOT / "sample_data" / "ps_sample.db")
        cls.expected = dict(con.execute(
            "SELECT BI_CURRENCY_CD, ROUND(SUM(INVOICE_AMOUNT),2) "
            "FROM PS_BI_HDR WHERE BILL_STATUS='INV' AND BUSINESS_UNIT='US001' "
            "GROUP BY BI_CURRENCY_CD").fetchall())
        con.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_totals_match_the_database_per_currency(self) -> None:
        out = self.ar.invoice_totals(business_unit="US001")
        self.assertEqual(out["invoiced_total_by_currency"], self.expected,
                         "the finalized total must be the database's own "
                         "grouped sum, currency by currency, never merged")

    def test_pipeline_and_cancelled_are_never_lumped(self) -> None:
        out = self.ar.invoice_totals(business_unit="US001")
        pop = out["population"]
        pipe = {x["status"] for x in pop["excluded_pipeline"]}
        term = {x["status"] for x in pop["excluded_terminal"]}
        self.assertIn("CAN", term)
        self.assertNotIn("CAN", pipe,
                         "a cancelled bill in the pipeline list books "
                         "upside that will never exist")
        self.assertTrue({"RDY", "HLD", "NEW"} <= pipe)

    def test_every_applied_default_is_disclosed(self) -> None:
        out = self.ar.invoice_totals(business_unit="US001", fiscal_year=2026)
        preds = " | ".join(a["predicate"]
                           for a in out["population"]["applied"])
        self.assertIn("BILL_STATUS = 'INV'", preds)
        self.assertIn("BUSINESS_UNIT = 'US001'", preds)
        self.assertIn("2026-01-01..2026-12-31", preds,
                      "the window moves the answer more than the status "
                      "filter — hiding it is the silent-default failure")

    def test_no_fiscal_year_means_disclosed_whole_history(self) -> None:
        out = self.ar.invoice_totals(business_unit="US001")
        preds = [a for a in out["population"]["applied"]
                 if a["predicate"] == "no date window"]
        self.assertTrue(preds, "an absent window must be disclosed, not "
                               "silently assumed")

    def test_pipeline_share_is_declared_for_the_rate_guard(self) -> None:
        out = self.ar.invoice_totals(business_unit="US001")
        payload = [json.dumps(out, default=str)]
        share = out["population"]["pipeline_share_pct"].get("USD")
        self.assertIsNotNone(share)
        self.assertEqual(
            rate_findings(f"the pipeline is {share}% of billings", payload),
            [], "a share the tool computed was flagged as unverified")
        self.assertTrue(payload_rates(payload))

    def test_the_grouped_aggregate_is_one_query_warm(self) -> None:
        self.ar.invoice_totals(business_unit="US001")  # warm caches
        calls = []
        orig = self.db.query
        self.db.query = lambda sql, p=None, **kw: (
            calls.append(sql), orig(sql, p, **kw))[1]
        try:
            self.ar.invoice_totals(business_unit="US001")
        finally:
            self.db.query = orig
        self.assertEqual(len(calls), 1,
                         "the disclosure must ride the same aggregate — "
                         "a second query is a hot-path regression")


class DegradedShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-invtot-"))
        self.dst = self.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", self.dst)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zero_amount_pipeline_suppresses_amounts(self) -> None:
        con = sqlite3.connect(self.dst)
        con.execute("UPDATE PS_BI_HDR SET INVOICE_AMOUNT = 0 "
                    "WHERE BILL_STATUS IN ('RDY','HLD','NEW')")
        con.commit(); con.close()
        db, ar = _stack(self.dst)
        try:
            out = ar.invoice_totals(business_unit="US001")
            pop = out["population"]
            self.assertEqual(pop["amount_basis"],
                             "unavailable_pre_finalization")
            self.assertEqual(pop["pipeline_share_pct"], {},
                             "a share computed over unreliable zeros is a "
                             "confident falsehood")
            self.assertTrue(any("trust the counts" in n
                                for n in out.get("record_notes", [])))
        finally:
            db.close()

    def test_default_emptying_the_result_refuses_with_the_remedy(self) -> None:
        con = sqlite3.connect(self.dst)
        con.execute("UPDATE PS_BI_HDR SET BILL_STATUS = 'RDY' "
                    "WHERE BILL_STATUS = 'INV'")
        con.commit(); con.close()
        db, ar = _stack(self.dst)
        try:
            out = ar.invoice_totals(business_unit="US001")
            self.assertEqual(out["scope_status"], "empty_after_default")
            self.assertIn("finalized-only default", out["detail"])
            self.assertIn("RDY", out["detail"],
                          "the remedy must name what DOES exist")
            self.assertNotIn("invoiced_total_by_currency", out,
                             "a zero produced by our own default must never "
                             "render as a plain total")
        finally:
            db.close()


class ExportAndRegistrationTests(unittest.TestCase):
    def test_by_status_is_exportable(self) -> None:
        from pstb import export
        db, ar = _stack()
        try:
            out = ar.invoice_totals(business_unit="US001")
            table = export.tabular(out)
            self.assertEqual(table["label"], "by_status")
        finally:
            db.close()

    def test_tool_is_a_full_citizen_of_the_gate(self) -> None:
        from pstb.guards import (FINANCIAL_EVIDENCE_TOOLS, _TOOL_SCOPE_ARGS,
                                 financial_tool_domains)
        self.assertIn("get_invoice_totals", FINANCIAL_EVIDENCE_TOOLS)
        self.assertTrue(financial_tool_domains("get_invoice_totals"))
        self.assertIn("fiscal_year", _TOOL_SCOPE_ARGS["get_invoice_totals"])


if __name__ == "__main__":
    unittest.main()
