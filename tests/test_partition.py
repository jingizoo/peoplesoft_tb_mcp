"""Partitioned execution: break a slow aggregate into indexed slices.

The top-20 class of query dies on the real instance because one GROUP BY
over millions of rows exceeds the query timeout — while the same query for
ONE business unit is an index range scan that returns in seconds. The model
even proposes the per-unit strategy when the direct query fails; it lacked
a mechanical way to run N slices and merge them. Now the query is written
for one slice with a :partition bind, the engine runs every slice
concurrently, and the merge uses the correct combiner per aggregate with
ORDER BY / FETCH applied once at the end.

The test that matters most is merge correctness: partitioned results must
equal the direct query's results exactly, because a merge bug produces
plausible wrong numbers, not errors.
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

from pstb.config import Config, load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.partition import (PartitionError, merge_partials,  # noqa: E402
                            parse_mergeable)

SLICE_SQL = """SELECT H.BILL_TO_CUST_ID AS cust, COUNT(*) AS invoices,
       SUM(H.INVOICE_AMOUNT) AS billed, MAX(H.INVOICE_DT) AS last_dt
  FROM PS_BI_HDR H
 WHERE H.BILL_STATUS = 'INV' AND H.BUSINESS_UNIT = :partition
 GROUP BY H.BILL_TO_CUST_ID
 ORDER BY billed DESC FETCH FIRST 5 ROWS ONLY"""


class ParseTests(unittest.TestCase):
    def test_a_grouped_topn_parses(self) -> None:
        parsed = parse_mergeable(SLICE_SQL)
        self.assertEqual(parsed["keys"], ["cust"])
        self.assertEqual(parsed["aggs"],
                         {"invoices": "sum", "billed": "sum",
                          "last_dt": "max"})
        self.assertEqual(parsed["order"], ("billed", True, 5))
        self.assertNotIn("FETCH", parsed["chunk_sql"].upper())

    def test_avg_refuses_with_the_rewrite(self) -> None:
        with self.assertRaises(PartitionError) as ctx:
            parse_mergeable("SELECT C AS c, AVG(X) AS a FROM T "
                            "WHERE B = :partition GROUP BY C")
        self.assertIn("SUM", str(ctx.exception))

    def test_count_distinct_refuses(self) -> None:
        with self.assertRaises(PartitionError):
            parse_mergeable("SELECT C AS c, COUNT(DISTINCT X) AS n FROM T "
                            "WHERE B = :partition GROUP BY C")

    def test_no_group_by_refuses(self) -> None:
        with self.assertRaises(PartitionError):
            parse_mergeable("SELECT SUM(X) AS s FROM T WHERE B = :partition")

    def test_order_by_non_alias_refuses(self) -> None:
        with self.assertRaises(PartitionError):
            parse_mergeable("SELECT C AS c, SUM(X) AS s FROM T "
                            "WHERE B = :partition GROUP BY C "
                            "ORDER BY OTHER_COL DESC")


class MergeTests(unittest.TestCase):
    def test_combiners_are_correct_per_aggregate(self) -> None:
        parsed = {"keys": ["cust"], "order": None,
                  "aggs": {"n": "sum", "total": "sum", "last": "max",
                           "first": "min"}}
        merged = merge_partials(parsed, [
            {"cust": "A", "n": 2, "total": 100.0, "last": "2026-05-01",
             "first": "2026-01-01"},
            {"cust": "A", "n": 3, "total": 250.0, "last": "2026-07-15",
             "first": "2026-03-01"},
            {"cust": "B", "n": 1, "total": 40.0, "last": "2026-02-02",
             "first": "2026-02-02"},
        ])
        by = {r["cust"]: r for r in merged}
        self.assertEqual(by["A"]["n"], 5)
        self.assertEqual(by["A"]["total"], 350.0)
        self.assertEqual(by["A"]["last"], "2026-07-15")
        self.assertEqual(by["A"]["first"], "2026-01-01")
        self.assertEqual(by["B"]["n"], 1)

    def test_order_and_fetch_apply_after_the_merge(self) -> None:
        # A top-1 of each slice is not the top-1 of the whole: B leads in
        # both slices separately summed, A wins overall.
        parsed = {"keys": ["cust"], "order": ("total", True, 1),
                  "aggs": {"total": "sum"}}
        merged = merge_partials(parsed, [
            {"cust": "A", "total": 60.0}, {"cust": "B", "total": 70.0},
            {"cust": "A", "total": 65.0}, {"cust": "B", "total": 30.0},
        ])
        self.assertEqual([(r["cust"], r["total"]) for r in merged],
                         [("A", 125.0)])


class EndToEndPartitionTests(unittest.TestCase):
    """Two business units in the data; partitioned must equal direct."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-part-"))
        dst = self.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        con = sqlite3.connect(dst)
        con.execute("INSERT INTO PS_BUS_UNIT_TBL_GL VALUES ('EU001','EUR')")
        con.execute("INSERT INTO PS_BUS_UNIT_LED VALUES ('EU001','ACTUALS')")
        cols = [r[1] for r in con.execute("PRAGMA table_info(PS_BI_HDR)")]
        # The SAME customer bills in both units — the merge must sum across
        # slices, and a slice-local top-N would get the ranking wrong.
        for bu, inv, cust, amt, dt_ in [
            ("EU001", "EU-1", "C1001", 400000.0, "2026-07-01"),
            ("EU001", "EU-2", "SHARED", 90000.0, "2026-06-01"),
            ("US001", "US-9", "SHARED", 95000.0, "2026-05-01"),
        ]:
            row = {"BUSINESS_UNIT": bu, "INVOICE": inv,
                   "BILL_TO_CUST_ID": cust, "BILL_STATUS": "INV",
                   "INVOICE_AMOUNT": amt, "INVOICE_DT": dt_,
                   "BI_CURRENCY_CD": "USD"}
            con.execute(
                f"INSERT INTO PS_BI_HDR VALUES ({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])
        con.commit()
        con.close()
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partitioned_equals_direct(self) -> None:
        part = self.engine.run_sql(
            SLICE_SQL, max_rows=50, partition={"values": "business_units"})
        self.assertEqual(part["partitioned"]["partitions"], 2)
        direct = self.engine.run_sql(
            SLICE_SQL.replace("AND H.BUSINESS_UNIT = :partition", "")
                     .replace("FETCH FIRST 5 ROWS ONLY", "LIMIT 5"),
            max_rows=50)
        self.assertEqual(
            [(r["cust"], round(r["billed"], 2)) for r in part["rows"]],
            [(r["cust"], round(r["billed"], 2)) for r in direct["rows"]],
            "the merged partials differ from the direct query — plausible "
            "wrong numbers, the worst failure this mechanism can have")

    def test_a_cross_slice_customer_is_summed_not_ranked_per_slice(self) -> None:
        part = self.engine.run_sql(
            SLICE_SQL, max_rows=50, partition={"values": "business_units"})
        shared = next(r for r in part["rows"] if r["cust"] == "SHARED")
        self.assertEqual(round(shared["billed"], 2), 185000.0)
        self.assertEqual(shared["invoices"], 2)
        self.assertEqual(shared["last_dt"][:10], "2026-06-01")

    def test_missing_partition_bind_refuses(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT BILL_TO_CUST_ID AS c, SUM(INVOICE_AMOUNT) AS s "
                "FROM PS_BI_HDR GROUP BY BILL_TO_CUST_ID",
                partition={"values": ["US001"]})
        self.assertIn(":partition", str(ctx.exception))

    def test_explicit_values_and_bind_name_work(self) -> None:
        out = self.engine.run_sql(
            SLICE_SQL.replace(":partition", ":bu"), max_rows=50,
            partition={"bind": "bu", "values": ["EU001"]})
        self.assertEqual(out["partitioned"]["partitions"], 1)
        self.assertIn("C1001", [r["cust"] for r in out["rows"]])


if __name__ == "__main__":
    unittest.main()
