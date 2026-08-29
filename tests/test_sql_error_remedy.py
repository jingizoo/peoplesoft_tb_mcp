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
from unittest.mock import patch

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

    def test_remote_database_links_are_refused_before_execution(self) -> None:
        for sql in (
            "SELECT * FROM PS_LEDGER@OTHER_DB",
            "SELECT * FROM OPENQUERY(REMOTE, 'SELECT 1')",
            "SELECT * FROM REMOTE_DB.SYSADM.PS_LEDGER",
            "SELECT * FROM SERVER.REMOTE_DB.SYSADM.PS_LEDGER",
            "SELECT * FROM FINANCE_DB..PS_LEDGER",
            "SELECT * FROM [FINANCE_DB]..[PS_LEDGER]",
            "SELECT * FROM FINANCE_SERVER...PS_LEDGER",
            "SELECT * FROM [FINANCE_SERVER]...[PS_LEDGER]",
            "SELECT * FROM FINANCE_DB . dbo . SECRET",
            "SELECT * FROM [FINANCE_DB]. /* boundary */ [dbo].[SECRET]",
            "SELECT * FROM [FIN]]ANCE].[dbo].[SECRET]",
            "SELECT * FROM PS_LEDGER L, FINANCE_DB.dbo.SECRET S",
            "SELECT * FROM PS_LEDGER L CROSS APPLY "
            "FINANCE_DB.dbo.GET_ROWS() S",
            "SELECT * FROM PS_LEDGER L OUTER APPLY "
            "[FIN]]ANCE].[dbo].[GET_ROWS]() S",
            "SELECT FINANCE_DB.dbo.GET_SECRET()",
            "SELECT L.ACCOUNT, FINANCE_DB.dbo.GET_BALANCE(L.ACCOUNT) "
            "FROM PS_LEDGER L",
        ):
            with self.subTest(sql=sql), self.assertRaises(EngineError) as caught:
                self.engine.run_sql(sql)
            self.assertIn("selected database source", str(caught.exception))

    def test_a_backticked_reference_cannot_slip_past_every_guard(self) -> None:
        """A backtick is not an identifier quote this system understands,
        and that is exactly the danger. `_SQL_IDENTIFIER` covers unquoted,
        "..." and [...] only, so a backticked name yields NO table
        reference -- and every control keyed on those references is then
        SKIPPED rather than failed: the schema allowlist, the existence
        check, and the operator's record veto. SQLite executes backticks
        happily, so this is a live read of an unlisted table, not a
        syntax error that fails safe."""
        for sql in (
            "SELECT * FROM `PS_LEDGER`",
            "SELECT * FROM `my-proj-1.dataset.table`",
            "SELECT * FROM PS_LEDGER L JOIN `SECRET` S ON S.ID = L.ACCOUNT",
            "SELECT * FROM `SYSADM`.`PS_LEDGER`",
        ):
            with self.subTest(sql=sql):
                # The assertion lives INSIDE the subTest so each case
                # proves THIS guard refused it. Left outside, a case
                # caught by the three-part-name rule would satisfy the
                # loop while the backtick rule did nothing.
                with self.assertRaises(EngineError) as caught:
                    self.engine.run_sql(sql)
                self.assertIn("Backtick", str(caught.exception))

    def test_the_bypass_this_closes_is_real(self) -> None:
        """Proof the refusal is the right fix rather than a belt: the
        extractor genuinely sees NOTHING in a backticked statement, so
        nothing downstream can be trusted to catch it."""
        scrubbed = self.engine._scrub_sql("SELECT * FROM `PS_LEDGER`")
        self.assertEqual(self.engine._table_refs(scrubbed), set())
        plain = self.engine._scrub_sql("SELECT * FROM PS_LEDGER")
        self.assertEqual(self.engine._table_refs(plain), {"PS_LEDGER"})

    def test_a_backtick_inside_a_value_is_not_a_refusal(self) -> None:
        """The refusal reads SCRUBBED sql, so a backtick in a literal is
        already blanked. A guard that fired on a correct question would
        be worse than the hole it closes."""
        out = self.engine.run_sql(
            "SELECT COUNT(*) AS n FROM PS_BI_HDR WHERE BILL_STATUS = 'a`b'")
        self.assertEqual(len(out["rows"]), 1)

    def test_remote_database_links_are_refused_before_explain(self) -> None:
        for sql in (
            "SELECT * FROM P2GO_ORDER@FINANCE_LINK",
            "SELECT * FROM OPENQUERY(REMOTE, 'SELECT 1')",
            "SELECT * FROM FINANCE_DB.SYSADM.PS_LEDGER",
            "SELECT * FROM SERVER.FINANCE_DB.SYSADM.PS_LEDGER",
            "SELECT * FROM FINANCE_DB..PS_LEDGER",
            "SELECT * FROM [FINANCE_DB]..[PS_LEDGER]",
            "SELECT * FROM FINANCE_SERVER...PS_LEDGER",
            "SELECT * FROM [FINANCE_SERVER]...[PS_LEDGER]",
            "SELECT * FROM FINANCE_DB . dbo . SECRET",
            "SELECT * FROM [FINANCE_DB]. /* boundary */ [dbo].[SECRET]",
            'SELECT * FROM "FIN""ANCE"."dbo"."SECRET"',
            "SELECT * FROM PS_LEDGER L, FINANCE_DB.dbo.SECRET S",
            "SELECT * FROM PS_LEDGER L CROSS APPLY "
            "FINANCE_DB.dbo.GET_ROWS() S",
            "SELECT * FROM PS_LEDGER L OUTER APPLY "
            "[FIN]]ANCE].[dbo].[GET_ROWS]() S",
            "SELECT FINANCE_DB.dbo.GET_SECRET()",
            "SELECT L.ACCOUNT, FINANCE_DB.dbo.GET_BALANCE(L.ACCOUNT) "
            "FROM PS_LEDGER L",
        ):
            with self.subTest(sql=sql), self.assertRaises(EngineError) as caught:
                self.engine.explain_query(sql)
            self.assertIn("selected database source", str(caught.exception))

    def test_same_database_owner_qualification_remains_supported(self) -> None:
        # Two-part OWNER.TABLE is intentionally distinct from a SQL Server
        # database.schema.table or an Oracle @DBLINK.
        refs = self.engine._table_refs("SELECT * FROM SYSADM.PS_LEDGER")
        self.assertEqual(refs, {"SYSADM.PS_LEDGER"})
        self.assertEqual(next(iter(refs)).count("."), 1)

    def test_oracle_local_package_call_is_not_a_remote_reference(self) -> None:
        sql = self.engine._scrub_sql(
            "SELECT SYS.DBMS_LOB.SUBSTR(CLOB_COL, 10) FROM LOCAL_TABLE")
        with patch.object(self.engine.db, "dialect", "oracle"):
            self.engine._require_local_database_refs(sql)

    def test_sqlserver_unicode_remote_identifiers_fail_closed(self) -> None:
        statements = (
            "SELECT * FROM Σ.Δ.Γ.Ω",
            "SELECT dbo.fn(Σ.Δ.Γ) FROM PS_LEDGER",
        )
        with patch.object(self.engine.db, "dialect", "sqlserver"):
            for sql in statements:
                with self.subTest(path="run", sql=sql), self.assertRaises(
                        EngineError) as run_error:
                    self.engine.run_sql(sql)
                self.assertIn(
                    "selected database source", str(run_error.exception))
                with self.subTest(path="explain", sql=sql), self.assertRaises(
                        EngineError) as explain_error:
                    self.engine.explain_query(sql)
                self.assertIn(
                    "selected database source", str(explain_error.exception))

        self.assertEqual(
            self.engine._table_refs("SELECT * FROM Σ.Δ"), {"Σ.Δ"})

    def test_select_into_is_refused_for_execution_and_explain(self) -> None:
        statements = (
            "SELECT * INTO P2GO_COPY FROM PS_LEDGER",
            "WITH X AS (SELECT * FROM PS_LEDGER) "
            "SELECT * INTO P2GO_COPY FROM X",
        )
        for sql in statements:
            with self.subTest(path="run", sql=sql), self.assertRaises(
                    EngineError) as run_error:
                self.engine.run_sql(sql)
            self.assertIn("INTO", str(run_error.exception))
            with self.subTest(path="explain", sql=sql), self.assertRaises(
                    EngineError) as explain_error:
                self.engine.explain_query(sql)
            self.assertIn("INTO", str(explain_error.exception))


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
        calls = [{"tool": "run_sql", "ok": True,
                  "_result": {"source_database": "default"}, "args": {
                      "sql": "SELECT ...",
                      "pivot": {"row_field": "customer",
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
            {"tool": "search_metadata", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
            {"tool": "wiki_search", "ok": True, "args": {}},
            {"tool": "get_metadata_context", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
            {"tool": "run_sql", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
        ]
        problems = self._grade({
            "all_tools": ["search_metadata", "get_metadata_context", "run_sql"],
            "ordered_tools": [
                "search_metadata", "get_metadata_context", "run_sql"],
        }, calls)
        self.assertEqual(problems, [])

    def test_ordered_tools_rejects_reversed_or_failed_context(self) -> None:
        reversed_calls = [
            {"tool": "get_metadata_context", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
            {"tool": "search_metadata", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
            {"tool": "run_sql", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
        ]
        self.assertTrue(self._grade({"ordered_tools": [
            "search_metadata", "get_metadata_context", "run_sql"]},
            reversed_calls))
        failed = [
            {"tool": "search_metadata", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
            {"tool": "get_metadata_context", "ok": False, "args": {}},
            {"tool": "run_sql", "ok": True, "args": {},
             "_result": {"source_database": "default"}},
        ]
        self.assertTrue(self._grade({"all_tools": [
            "search_metadata", "get_metadata_context", "run_sql"]}, failed))


if __name__ == "__main__":
    unittest.main()
