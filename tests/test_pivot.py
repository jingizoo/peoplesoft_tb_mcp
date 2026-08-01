"""Consolidating many rows into one cross-tab.

The reason this exists is not SQL. One GROUP BY always returned every period
at once. It is that a consolidated table is made almost entirely of DERIVED
figures — totals, changes, percentages — and the number guard rejects figures
no tool produced. So combining results was penalised: the arithmetic a
consolidation needs was exactly the arithmetic that got blocked.

Moving the arithmetic server-side resolves it, and the test that matters is
the last class here: the derived figures must now pass the very guard that
used to reject them, while an invented figure still does not.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.guards import ungrounded_figures  # noqa: E402
from pstb.pivot import MAX_COLUMNS, PivotError, pivot_rows  # noqa: E402

FLAT = [
    {"cust": "ACME", "period": 5, "amt": 38500.0},
    {"cust": "ACME", "period": 6, "amt": 238985.19},
    {"cust": "Beacon", "period": 6, "amt": 88400.0},
    {"cust": "Beacon", "period": 11, "amt": 42000.0},
]


class ShapeTests(unittest.TestCase):
    def test_rows_become_a_matrix_with_sorted_columns(self) -> None:
        p = pivot_rows(FLAT, "cust", "period", "amt")
        self.assertEqual(p["columns"], [5, 6, 11])
        self.assertEqual([r["row"] for r in p["rows"]], ["ACME", "Beacon"])
        self.assertEqual(p["rows"][0]["values"], [38500.0, 238985.19, 0.0])

    def test_columns_sort_numerically_not_as_text(self) -> None:
        # Periods sorted as text read "1, 10, 11, 12, 2" — which is not a
        # trend, it is a shuffle that looks like one.
        rows = [{"c": "x", "p": p, "v": 1.0} for p in (1, 2, 10, 11, 12, 3)]
        self.assertEqual(pivot_rows(rows, "c", "p", "v")["columns"],
                         [1, 2, 3, 10, 11, 12])

    def test_a_missing_cell_is_a_true_zero(self) -> None:
        # ACME has no period 11 row. That is zero activity, not missing data,
        # and a blank would make a reader doubt the query.
        p = pivot_rows(FLAT, "cust", "period", "amt")
        self.assertEqual(p["rows"][0]["values"][2], 0.0)

    def test_duplicate_cells_are_summed(self) -> None:
        rows = FLAT + [{"cust": "ACME", "period": 5, "amt": 1500.0}]
        p = pivot_rows(rows, "cust", "period", "amt")
        self.assertEqual(p["rows"][0]["values"][0], 40000.0)

    def test_an_empty_result_says_so_rather_than_reporting_zeros(self) -> None:
        p = pivot_rows([], "cust", "period", "amt")
        self.assertEqual(p["rows"], [])
        self.assertIn("nothing to consolidate", p["note"])

    def test_a_misnamed_field_names_what_the_query_did_return(self) -> None:
        with self.assertRaises(PivotError) as ctx:
            pivot_rows(FLAT, "customer", "period", "amt")
        self.assertIn("cust", str(ctx.exception))

    def test_too_many_columns_refuses_with_a_way_out(self) -> None:
        rows = [{"c": "x", "p": i, "v": 1.0} for i in range(MAX_COLUMNS + 5)]
        with self.assertRaises(PivotError) as ctx:
            pivot_rows(rows, "c", "p", "v")
        self.assertIn("down the side", str(ctx.exception))


class DerivedFigureTests(unittest.TestCase):
    def test_totals_are_computed_on_every_edge(self) -> None:
        p = pivot_rows(FLAT, "cust", "period", "amt")
        self.assertEqual(p["rows"][0]["total"], 277485.19)
        self.assertEqual(p["column_totals"], [38500.0, 327385.19, 42000.0])
        self.assertEqual(p["grand_total"], 407885.19)

    def test_change_states_which_columns_it_compared(self) -> None:
        p = pivot_rows(FLAT, "cust", "period", "amt")
        self.assertIn("last column (11) minus first column (5)",
                      p["change_basis"])

    def test_percent_change_from_a_zero_base_is_not_invented(self) -> None:
        # 0 -> 5000 is not "100% growth" and not "0%". It has no percentage,
        # and stating one would be a fabricated fact.
        rows = [{"c": "x", "p": 1, "v": 0.0}, {"c": "x", "p": 2, "v": 5000.0}]
        row = pivot_rows(rows, "c", "p", "v")["rows"][0]
        self.assertIsNone(row["change_pct"])
        self.assertEqual(row["change"], 5000.0)

    def test_a_row_empty_at_both_ends_is_not_called_flat(self) -> None:
        # Billed heavily mid-range, nothing at either end: change is zero and
        # that is arithmetically true and narratively false.
        rows = [{"c": "x", "p": 1, "v": 0.0}, {"c": "x", "p": 2, "v": 90000.0},
                {"c": "x", "p": 3, "v": 0.0}]
        row = pivot_rows(rows, "c", "p", "v")["rows"][0]
        self.assertEqual(row["change"], 0.0)
        self.assertIn("activity_span", row)
        self.assertEqual(row["activity_span"]["first_active_column"], 2)
        self.assertIn("rather than implying a flat trend",
                      row["activity_span"]["note"])

    def test_a_row_spanning_the_full_range_carries_no_span_caveat(self) -> None:
        rows = [{"c": "x", "p": 1, "v": 10.0}, {"c": "x", "p": 2, "v": 20.0}]
        self.assertNotIn("activity_span",
                         pivot_rows(rows, "c", "p", "v")["rows"][0])


class GroundingTests(unittest.TestCase):
    """The point of the whole exercise."""

    def setUp(self) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()

    def _pivoted(self) -> dict:
        return self.engine.run_sql(
            "SELECT C.NAME1 AS customer, "
            "CAST(SUBSTR(H.INVOICE_DT,6,2) AS INTEGER) AS month, "
            "SUM(H.INVOICE_AMOUNT) AS revenue "
            "FROM PS_BI_HDR H JOIN PS_CUSTOMER C ON C.CUST_ID = H.BILL_TO_CUST_ID "
            "WHERE H.BUSINESS_UNIT='US001' AND H.BILL_STATUS='INV' GROUP BY 1,2",
            max_rows=500,
            pivot={"row_field": "customer", "column_field": "month",
                   "value_field": "revenue"})

    def test_derived_figures_pass_the_guard_that_used_to_block_them(self) -> None:
        out = self._pivoted()
        payload = [json.dumps(out)]
        piv = out["pivot"]
        row = piv["rows"][0]
        for label, figure in [
            ("a cell", f"{row['values'][piv['columns'].index(6)]:,.2f}"),
            ("a row total", f"{row['total']:,.2f}"),
            ("a column total", f"{piv['column_totals'][0]:,.2f}"),
            ("the grand total", f"{piv['grand_total']:,.2f}"),
        ]:
            answer = f"The figure was {figure}."
            self.assertEqual(
                ungrounded_figures(answer, payload), [],
                f"{label} was rejected as ungrounded — the server computed it, "
                "so the consolidation is still unusable")

    def test_an_invented_figure_is_still_rejected(self) -> None:
        # Grounding the derived numbers must not have opened the door.
        payload = [json.dumps(self._pivoted())]
        self.assertEqual(
            ungrounded_figures("Revenue was 1,234,567.00 overall.", payload),
            ["1,234,567.00"])

    def test_a_subtly_altered_figure_is_still_rejected(self) -> None:
        out = self._pivoted()
        payload = [json.dumps(out)]
        wrong = out["pivot"]["grand_total"] + 0.72
        self.assertTrue(
            ungrounded_figures(f"Total was {wrong:,.2f}.", payload),
            "a transposed or nudged figure passed as grounded")

    def test_pivot_is_absent_when_not_asked_for(self) -> None:
        out = self.engine.run_sql("SELECT COUNT(*) AS n FROM PS_BI_HDR")
        self.assertNotIn("pivot", out)

    def test_a_bad_pivot_spec_is_reported_not_swallowed(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT COUNT(*) AS n FROM PS_BI_HDR",
                pivot={"row_field": "nope", "column_field": "n",
                       "value_field": "n"})
        self.assertIn("nope", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
