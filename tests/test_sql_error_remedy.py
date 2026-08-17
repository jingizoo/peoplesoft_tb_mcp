"""A failed ad-hoc query must teach, not just fail.

The trend eval exposed the loop: the model writes SQL from its memory of
PeopleSoft, memory invents columns (H.INVOICE_PERIOD, a PS_ITEM join on
ITEM_ID), and the bare 'no such column' left it guessing schema again
until it ran out of rounds. The error now carries the REAL column lists
of every table the query referenced — catalog lookups only — so the
retry is a one-round fix. The eval matcher fix rides along: a structured
argument matches when the expected string names one of its keys.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402


class RemedyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config.sample(ROOT)
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_unknown_column_error_names_the_real_columns(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT H.INVOICE_PERIOD FROM PS_BI_HDR H "
                "WHERE H.BILL_STATUS = 'INV'")
        msg = str(ctx.exception)
        self.assertIn("PS_BI_HDR has columns:", msg)
        self.assertIn("INVOICE_DT", msg)
        self.assertIn("search_records", msg)

    def test_unknown_table_is_rejected_before_execution(self) -> None:
        # Tables are validated up front by run_sql's own name check, which
        # already suggests near-misses — the remedy layer is for columns.
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql("SELECT X FROM PS_NOT_A_RECORD")
        msg = str(ctx.exception)
        self.assertIn("PS_NOT_A_RECORD does not exist", msg)
        self.assertIn("list_tables", msg)

    def test_joined_tables_are_each_described(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT H.NOPE FROM PS_BI_HDR H "
                "JOIN PS_CUSTOMER C ON C.CUST_ID = H.BILL_TO_CUST_ID")
        msg = str(ctx.exception)
        self.assertIn("PS_BI_HDR has columns:", msg)
        self.assertIn("PS_CUSTOMER has columns:", msg)

    def test_non_schema_errors_pass_through_untouched(self) -> None:
        remedied = self.engine._sql_error_remedy(
            "SELECT 1 FROM PS_LEDGER", Exception("database is locked"))
        self.assertEqual(remedied, "database is locked")

    def test_good_sql_still_runs(self) -> None:
        out = self.engine.run_sql(
            "SELECT COUNT(*) AS n FROM PS_BI_HDR WHERE BILL_STATUS = 'INV'")
        self.assertEqual(len(out["rows"]), 1)


class EvalMatcherTests(unittest.TestCase):
    """The harness bug that kept a passing behavior red: a pivot spec WAS
    passed, but dict args could never match tool_args_contain."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "eval_harness", ROOT / "scripts" / "eval.py")
        cls.ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ev)

    def _grade(self, expect, calls):
        return self.ev._grade({"expect": expect}, "fine answer", calls)

    def test_dict_arg_matches_by_key(self) -> None:
        calls = [{"tool": "run_sql", "ok": True, "args": {
            "sql": "SELECT ...", "pivot": {"row_field": "customer",
                                           "column_field": "period",
                                           "value_field": "amt"}}}]
        problems = self._grade(
            {"any_tool": ["run_sql"], "tool_args_contain": {"pivot": "row_field"}},
            calls)
        self.assertEqual(problems, [])

    def test_scalar_args_still_require_equality(self) -> None:
        calls = [{"tool": "t", "ok": True, "args": {"business_unit": "US001"}}]
        problems = self._grade(
            {"tool_args_contain": {"business_unit": "ALL"}}, calls)
        self.assertTrue(problems, "US001 must not satisfy ALL")

    def test_ordered_tools_requires_successful_subsequence(self) -> None:
        calls = [
            {"tool": "search_metadata", "ok": True, "args": {}},
            {"tool": "wiki_search", "ok": True, "args": {}},
            {"tool": "get_metadata_context", "ok": True, "args": {}},
            {"tool": "run_sql", "ok": True, "args": {}},
        ]
        problems = self._grade({
            "all_tools": ["search_metadata", "get_metadata_context", "run_sql"],
            "ordered_tools": [
                "search_metadata", "get_metadata_context", "run_sql"],
        }, calls)
        self.assertEqual(problems, [])

    def test_ordered_tools_rejects_reversed_or_failed_context(self) -> None:
        reversed_calls = [
            {"tool": "get_metadata_context", "ok": True, "args": {}},
            {"tool": "search_metadata", "ok": True, "args": {}},
            {"tool": "run_sql", "ok": True, "args": {}},
        ]
        self.assertTrue(self._grade({"ordered_tools": [
            "search_metadata", "get_metadata_context", "run_sql"]},
            reversed_calls))
        failed = [
            {"tool": "search_metadata", "ok": True, "args": {}},
            {"tool": "get_metadata_context", "ok": False, "args": {}},
            {"tool": "run_sql", "ok": True, "args": {}},
        ]
        self.assertTrue(self._grade({"all_tools": [
            "search_metadata", "get_metadata_context", "run_sql"]}, failed))


if __name__ == "__main__":
    unittest.main()
