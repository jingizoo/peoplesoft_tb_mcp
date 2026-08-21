"""Joins proved against the catalog, instead of invented.

The rules under test are all about what makes a join EDGE real:

  1. A shared column name is a candidate only if it identifies something.
     PS_BI_HDR and PS_VOUCHER both carry INVOICE_DT; joining on a date is
     a cartesian product wearing an ON clause, and it is the first thing
     this produced before the rule existed.
  2. Two records sharing only columns every record has (SETID, EFFDT) are
     not related. Before that rule the path finder happily ROUTED through
     PS_GL_ACCOUNT_TBL -> PS_DEPT_TBL on exactly that pair.
  3. Ranking is by index support, not hop count — because the difference
     between a range scan and a table scan is hours, and it is invisible
     in a schema diagram.
  4. The interesting answer is usually "one constant away": PeopleSoft
     leads its indexes with SETID and BUSINESS_UNIT, which the scope has
     already fixed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.graph import RecordGraph, is_joinable_column  # noqa: E402


class ColumnKindTests(unittest.TestCase):
    def test_values_are_not_join_keys(self) -> None:
        for column in ("INVOICE_DT", "DUE_DT", "GROSS_AMT", "PAID_AMT",
                       "MONETARY_AMOUNT", "DESCR254_MIXED", "NAME1",
                       "POST_STATUS", "EXCHANGE_RATE"):
            self.assertFalse(is_joinable_column(column), column)

    def test_identifiers_are(self) -> None:
        for column in ("CUST_ID", "VENDOR_ID", "VOUCHER_ID", "BUSINESS_UNIT",
                       "SETID", "ACCOUNT", "DEPTID", "INVOICE", "ITEM"):
            self.assertTrue(is_joinable_column(column), column)


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(str(ROOT / "config.yaml"))
        cls.db = Database(cls.cfg)
        cls.engine = TBEngine(cls.db, cls.cfg)
        seed = [rec for recs in TBEngine.RECORD_MAP.values()
                for rec, *_ in recs]
        cls.graph = RecordGraph(cls.db, seed)

    def test_a_real_key_join_is_found_and_cheap(self) -> None:
        path = self.graph.path("PS_BI_HDR", "PS_BI_LINE")
        self.assertIsNotNone(path)
        self.assertEqual(path.records, ["PS_BI_HDR", "PS_BI_LINE"])
        self.assertIn("INVOICE", path.hops[0].on)
        self.assertTrue(path.hops[0].indexed)
        self.assertEqual(path.confidence(), "high")

    def test_a_date_is_never_the_join(self) -> None:
        hop = self.graph.hop("PS_BI_HDR", "PS_VOUCHER")
        if hop is not None:
            self.assertNotIn("INVOICE_DT", hop.on,
                             "joining bills to vouchers on a date is a "
                             "cartesian product with an ON clause")

    def test_two_weak_columns_do_not_make_a_relationship(self) -> None:
        # Both are setup tables carrying SETID and EFFDT. Every account
        # against every department is not a join.
        self.assertIsNone(self.graph.hop("PS_GL_ACCOUNT_TBL", "PS_DEPT_TBL"))

    def test_the_constant_that_would_fix_a_scan_is_named(self) -> None:
        # The useful half: PS_CUSTOMER indexes (SETID, CUST_ID), so a join
        # supplying only CUST_ID cannot range-scan it — but SETID is a
        # value the selected scope already fixed.
        hop = self.graph.hop("PS_ITEM", "PS_CUSTOMER")
        self.assertIsNotNone(hop)
        self.assertEqual(hop.on, ("CUST_ID",))
        self.assertTrue(hop.indexable)
        self.assertIn("SETID", hop.supply())
        self.assertIn("pin", hop.describe())

    def test_an_unreachable_pair_says_so_instead_of_inventing_a_bridge(self):
        out = self.engine.join_path("PS_LEDGER", "PS_VENDOR")
        self.assertFalse(out["found"])
        self.assertIn("No join path", out["note"])
        self.assertIn("reachable_from_source", out)

    def test_the_skeleton_is_a_FROM_clause_not_a_whole_query(self) -> None:
        # What to SELECT and how to filter is the question's business; a
        # half-guessed SELECT list is how a plausible wrong answer starts.
        sql = self.graph.path("PS_BI_HDR", "PS_BI_LINE").to_sql()
        self.assertTrue(sql.startswith("FROM PS_BI_HDR"))
        self.assertIn("JOIN PS_BI_LINE", sql)
        self.assertNotIn("SELECT", sql.upper())

    def test_index_support_outranks_hop_count(self) -> None:
        # A hop neither side indexes costs more than two that both do, so
        # the walk is chosen by what the database can DO, not by shape.
        indexed = self.graph.hop("PS_BI_HDR", "PS_BI_LINE")
        scanning = self.graph.hop("PS_VOUCHER", "PS_VENDOR")
        self.assertIsNotNone(scanning)
        self.assertLess(indexed.cost() * 2, scanning.cost())

    def test_the_universe_is_bounded_not_crawled(self) -> None:
        # A live instance has tens of thousands of records; building every
        # pair would be its own outage.
        self.assertLess(len(self.graph.universe()), 200)

    def test_an_excluded_record_is_never_used_as_an_intermediate(self) -> None:
        baseline = self.graph.path("PS_LEDGER", "PS_JRNL_HEADER")
        self.assertEqual(
            baseline.records, ["PS_LEDGER", "PS_JRNL_LN", "PS_JRNL_HEADER"])
        self.assertIsNone(self.graph.path(
            "PS_LEDGER", "PS_JRNL_HEADER", exclude=["PS_JRNL_LN"]))


class ToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        cls.engine = TBEngine(Database(cfg), cfg)

    def test_the_payload_carries_its_own_caveat(self) -> None:
        # Shared column names are evidence, not a declared foreign key —
        # and a path presented as fact is a path nobody checks.
        out = self.engine.join_path("PS_ITEM", "PS_CUSTOMER")
        self.assertTrue(out["found"])
        self.assertIn("not a declared foreign key", out["caveat"])
        self.assertIn("explain_query", out["caveat"])

    def test_it_refuses_an_unreadable_record_by_name(self) -> None:
        from pstb.engine import EngineError
        with self.assertRaises(EngineError) as ctx:
            self.engine.join_path("PS_ITEM", "PS_NOT_A_RECORD")
        self.assertIn("PS_NOT_A_RECORD", str(ctx.exception))
        self.assertIn("search_records", str(ctx.exception))

    def test_both_endpoints_are_required(self) -> None:
        from pstb.engine import EngineError
        with self.assertRaises(EngineError):
            self.engine.join_path("PS_ITEM", "")


if __name__ == "__main__":
    unittest.main()
