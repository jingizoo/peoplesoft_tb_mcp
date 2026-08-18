"""One source may intentionally contain several schemas, never the whole DB.

The P2Go connection uses APPSADM, which can see far more than the P2GO and
TUSINVC application owners.  Visibility is therefore not authorization: every
runtime/catalog path must bind the configured owner and reject any other owner
before the statement reaches Oracle.
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402


class _OracleSchemas(Database):
    OBJECTS = {
        ("P2GO", "INVOICE"),
        ("P2GO", "SHARED_NAME"),
        ("TUSINVC", "ARCHIVE_INVOICE"),
        ("TUSINVC", "SHARED_NAME"),
        # The privileged login can see this, but this source must not.
        ("OTHER", "SECRET"),
    }
    COLUMNS = {
        ("P2GO", "INVOICE"): ["INVOICE_ID", "AMOUNT"],
        ("P2GO", "SHARED_NAME"): ["P2GO_ID"],
        ("TUSINVC", "ARCHIVE_INVOICE"): ["ARCHIVE_ID", "AMOUNT"],
        ("TUSINVC", "SHARED_NAME"): ["TUSINVC_ID"],
        ("OTHER", "SECRET"): ["SECRET_VALUE"],
    }

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls: list[dict] = []
        self.plans: list[str] = []

    def explain_plan(self, sql, params=None):
        self.plans.append(sql)
        return {"available": False, "reason": "fixture"}

    def query(self, sql, params=None, max_rows=None):
        params = dict(params or {})
        self.calls.append({"sql": sql, "params": params})
        upper = " ".join(sql.upper().split())
        if "FROM ALL_OBJECTS" in upper:
            if "OBJECT_NAME LIKE :PAT" in upper:
                owners = [
                    value for key, value in params.items()
                    if key == "owner" or key.startswith("owner")
                ]
                needle = str(params.get("pat") or "%").strip("%").upper()
                rows = [
                    {"schema_name": owner, "table_name": name,
                     "object_type": "TABLE"}
                    for owner, name in sorted(self.OBJECTS)
                    if owner in owners and needle in name
                ]
                return rows[: max_rows or len(rows)], False
            target = (str(params.get("o") or "").upper(),
                      str(params.get("n") or "").upper())
            return ([{"x": 1}] if target in self.OBJECTS else []), False
        if "FROM ALL_TAB_COLUMNS" in upper:
            target = (str(params.get("owner") or "").upper(),
                      str(params.get("tname") or "").upper())
            rows = [
                {"column_name": name, "data_type": "VARCHAR2",
                 "data_length": 30, "nullable": "Y"}
                for name in self.COLUMNS.get(target, [])
            ]
            return rows, False
        if "FROM ALL_IND_COLUMNS" in upper:
            return [], False
        if "FROM ALL_TABLES" in upper:
            return [{"n": 10}], False
        # The guarded business SELECT itself.
        return [{"ok": 1}], False


class _PlanCursor:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def fetchall(self):
        return [(
            "TABLE ACCESS", "FULL", "TUSINVC", "ARCHIVE_INVOICE",
            250_000, 9_000,
        )]

    def close(self):
        pass


class _PlanConnection:
    def __init__(self):
        self.cursor_instance = _PlanCursor()

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        pass


class _OraclePlanDatabase(Database):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.connection = _PlanConnection()

    @contextlib.contextmanager
    def _session(self):
        yield self.connection, False


class MultiSchemaRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config.sample(ROOT)
        self.cfg.db.backend = "oracle"
        self.cfg.db.schema = "P2GO"
        self.cfg.db.schemas = ["P2GO", "TUSINVC"]
        self.db = _OracleSchemas(self.cfg)
        self.engine = TBEngine(self.db, self.cfg)

    def test_unqualified_query_resolves_to_default_schema(self) -> None:
        out = self.engine.run_sql("SELECT * FROM INVOICE")
        self.assertIn("FROM P2GO.INVOICE", out["sql_executed"])
        lookup = next(c for c in self.db.calls
                      if "FROM ALL_OBJECTS" in c["sql"].upper())
        self.assertEqual(lookup["params"], {"o": "P2GO", "n": "INVOICE"})

    def test_each_allowed_schema_is_catalog_checked_as_itself(self) -> None:
        out = self.engine.run_sql("SELECT * FROM TUSINVC.ARCHIVE_INVOICE")
        self.assertIn("FROM TUSINVC.ARCHIVE_INVOICE", out["sql_executed"])
        lookup = next(c for c in self.db.calls
                      if "FROM ALL_OBJECTS" in c["sql"].upper())
        self.assertEqual(
            lookup["params"],
            {"o": "TUSINVC", "n": "ARCHIVE_INVOICE"},
        )

    def test_disallowed_schema_is_rejected_before_any_database_call(self) -> None:
        with self.assertRaises(EngineError) as caught:
            self.engine.run_sql("SELECT * FROM OTHER.SECRET")
        self.assertIn("outside the selected source", str(caught.exception))
        self.assertIn("P2GO, TUSINVC", str(caught.exception))
        self.assertEqual(self.db.calls, [])
        self.assertEqual(self.db.plans, [])

    def test_explain_uses_the_identical_schema_boundary(self) -> None:
        with self.assertRaises(EngineError):
            self.engine.explain_query("SELECT * FROM OTHER.SECRET")
        self.assertEqual(self.db.calls, [])
        self.assertEqual(self.db.plans, [])

        out = self.engine.explain_query(
            "SELECT * FROM TUSINVC.ARCHIVE_INVOICE")
        self.assertEqual(out["sql"], "SELECT * FROM TUSINVC.ARCHIVE_INVOICE")
        self.assertEqual(self.db.plans, [
            "SELECT * FROM TUSINVC.ARCHIVE_INVOICE",
        ])
        self.assertEqual(out["tables"][0]["table"],
                         "TUSINVC.ARCHIVE_INVOICE")

    def test_explain_qualifies_unqualified_name_with_default(self) -> None:
        out = self.engine.explain_query("SELECT * FROM INVOICE")
        self.assertEqual(out["sql"], "SELECT * FROM P2GO.INVOICE")
        self.assertEqual(self.db.plans, ["SELECT * FROM P2GO.INVOICE"])

    def test_quoted_spaced_and_comment_spaced_allowed_owners_work(self) -> None:
        statements = (
            'SELECT * FROM "TUSINVC"."ARCHIVE_INVOICE"',
            "SELECT * FROM TUSINVC . ARCHIVE_INVOICE",
            "SELECT * FROM TUSINVC /* local owner */ . ARCHIVE_INVOICE",
        )
        for sql in statements:
            with self.subTest(path="run", sql=sql):
                self.db.calls.clear()
                self.db.plans.clear()
                out = self.engine.run_sql(sql)
                self.assertEqual(out["sql_executed"], sql)
                lookup = next(c for c in self.db.calls
                              if "FROM ALL_OBJECTS" in c["sql"].upper())
                self.assertEqual(
                    lookup["params"],
                    {"o": "TUSINVC", "n": "ARCHIVE_INVOICE"},
                )
            with self.subTest(path="explain", sql=sql):
                self.db.calls.clear()
                self.db.plans.clear()
                out = self.engine.explain_query(sql)
                self.assertEqual(out["sql"], sql)
                self.assertEqual(self.db.plans, [sql])

    def test_quoted_case_sensitive_schema_lookalike_is_not_allowed(self) -> None:
        for method in (self.engine.run_sql, self.engine.explain_query):
            with self.subTest(method=method.__name__), self.assertRaises(
                    EngineError) as caught:
                method('SELECT * FROM "tusinvc"."ARCHIVE_INVOICE"')
            self.assertIn("outside the selected source", str(caught.exception))
            self.assertEqual(self.db.calls, [])
            self.assertEqual(self.db.plans, [])

    def test_disallowed_owner_spellings_all_fail_before_database(self) -> None:
        statements = (
            'SELECT * FROM "SYSADM"."PS_LEDGER"',
            "SELECT * FROM SYSADM . PS_LEDGER",
            "SELECT * FROM SYSADM /* no */ . /* escape */ PS_LEDGER",
            "SELECT * FROM [SYSADM].[PS_LEDGER]",
        )
        for sql in statements:
            for method in (self.engine.run_sql, self.engine.explain_query):
                with self.subTest(sql=sql, method=method.__name__), \
                        self.assertRaises(EngineError):
                    method(sql)
                self.assertEqual(self.db.calls, [])
                self.assertEqual(self.db.plans, [])

    def test_every_table_factor_is_checked_in_comma_join(self) -> None:
        sql = "SELECT * FROM P2GO.INVOICE I, SYSADM.PS_LEDGER L"
        for method in (self.engine.run_sql, self.engine.explain_query):
            with self.subTest(method=method.__name__), self.assertRaises(
                    EngineError):
                method(sql)
            self.assertEqual(self.db.calls, [])
            self.assertEqual(self.db.plans, [])

    def test_allowed_cross_schema_comma_join_checks_both_owners(self) -> None:
        out = self.engine.run_sql(
            "SELECT * FROM P2GO.INVOICE I, "
            "TUSINVC.ARCHIVE_INVOICE A")
        lookups = [c["params"] for c in self.db.calls
                   if "FROM ALL_OBJECTS" in c["sql"].upper()]
        self.assertEqual(
            set((c["o"], c["n"]) for c in lookups),
            {("P2GO", "INVOICE"),
             ("TUSINVC", "ARCHIVE_INVOICE")},
        )
        self.assertIn("TUSINVC.ARCHIVE_INVOICE", out["sql_executed"])

    def test_unqualified_comma_join_qualifies_every_factor(self) -> None:
        out = self.engine.run_sql(
            "SELECT * FROM INVOICE I, SHARED_NAME S")
        self.assertIn("FROM P2GO.INVOICE I", out["sql_executed"])
        self.assertIn(", P2GO.SHARED_NAME S", out["sql_executed"])

    def test_disallowed_owner_inside_nested_subquery_is_checked(self) -> None:
        sql = (
            "SELECT * FROM P2GO.INVOICE I WHERE EXISTS ("
            "SELECT 1 FROM SYSADM.PS_LEDGER L)"
        )
        for method in (self.engine.run_sql, self.engine.explain_query):
            with self.subTest(method=method.__name__), self.assertRaises(
                    EngineError):
                method(sql)
            self.assertEqual(self.db.calls, [])
            self.assertEqual(self.db.plans, [])

    def test_disallowed_owner_in_parenthesized_join_is_checked(self) -> None:
        sql = (
            "SELECT * FROM (OTHER.SECRET S JOIN P2GO.INVOICE I "
            "ON S.ID=I.ID)"
        )
        for method in (self.engine.run_sql, self.engine.explain_query):
            with self.subTest(method=method.__name__), self.assertRaises(
                    EngineError):
                method(sql)
            self.assertEqual(self.db.calls, [])
            self.assertEqual(self.db.plans, [])

    def test_disallowed_owner_in_apply_is_checked(self) -> None:
        statements = (
            "SELECT * FROM P2GO.INVOICE I CROSS APPLY OTHER.SECRET S",
            "SELECT * FROM P2GO.INVOICE I OUTER APPLY OTHER.SECRET S",
        )
        for sql in statements:
            for method in (self.engine.run_sql, self.engine.explain_query):
                with self.subTest(sql=sql, method=method.__name__), \
                        self.assertRaises(EngineError):
                    method(sql)
                self.assertEqual(self.db.calls, [])
                self.assertEqual(self.db.plans, [])

    def test_allowed_parenthesized_cross_schema_join_uses_both_owners(self) -> None:
        sql = (
            "SELECT * FROM (TUSINVC.ARCHIVE_INVOICE A "
            "JOIN P2GO.INVOICE I ON A.AMOUNT=I.AMOUNT)"
        )
        out = self.engine.run_sql(sql)
        self.assertEqual(out["sql_executed"], sql)
        lookups = [c["params"] for c in self.db.calls
                   if "FROM ALL_OBJECTS" in c["sql"].upper()]
        self.assertEqual(
            {(c["o"], c["n"]) for c in lookups},
            {("P2GO", "INVOICE"),
             ("TUSINVC", "ARCHIVE_INVOICE")},
        )

        self.db.calls.clear()
        self.db.plans.clear()
        explained = self.engine.explain_query(sql)
        self.assertEqual(explained["sql"], sql)
        self.assertEqual(self.db.plans, [sql])

    def test_allowed_apply_targets_use_tusinvc_owner(self) -> None:
        statements = (
            "SELECT * FROM P2GO.INVOICE I CROSS APPLY "
            "TUSINVC.ARCHIVE_INVOICE A",
            "SELECT * FROM P2GO.INVOICE I OUTER APPLY "
            "TUSINVC.ARCHIVE_INVOICE A",
        )
        for sql in statements:
            with self.subTest(path="run", sql=sql):
                self.db.calls.clear()
                self.db.plans.clear()
                out = self.engine.run_sql(sql)
                self.assertEqual(out["sql_executed"], sql)
                lookups = [c["params"] for c in self.db.calls
                           if "FROM ALL_OBJECTS" in c["sql"].upper()]
                self.assertEqual(
                    {(c["o"], c["n"]) for c in lookups},
                    {("P2GO", "INVOICE"),
                     ("TUSINVC", "ARCHIVE_INVOICE")},
                )
            with self.subTest(path="explain", sql=sql):
                self.db.calls.clear()
                self.db.plans.clear()
                explained = self.engine.explain_query(sql)
                self.assertEqual(explained["sql"], sql)
                self.assertEqual(self.db.plans, [sql])

    def test_disallowed_parenthesized_join_after_comma_is_checked(self) -> None:
        statements = (
            "SELECT * FROM P2GO.INVOICE I, (OTHER.SECRET S JOIN "
            "TUSINVC.ARCHIVE_INVOICE A ON S.ID=A.ARCHIVE_ID)",
            "SELECT * FROM P2GO.INVOICE I, ((OTHER.SECRET S JOIN "
            "TUSINVC.ARCHIVE_INVOICE A ON S.ID=A.ARCHIVE_ID))",
        )
        for sql in statements:
            for method in (self.engine.run_sql, self.engine.explain_query):
                with self.subTest(sql=sql, method=method.__name__), \
                        self.assertRaises(EngineError):
                    method(sql)
                self.assertEqual(self.db.calls, [])
                self.assertEqual(self.db.plans, [])

    def test_allowed_parenthesized_join_after_comma_uses_both_schemas(self) -> None:
        statements = (
            "SELECT * FROM P2GO.INVOICE I, (TUSINVC.ARCHIVE_INVOICE A "
            "JOIN P2GO.SHARED_NAME S ON S.P2GO_ID=A.ARCHIVE_ID)",
            "SELECT * FROM P2GO.INVOICE I, ((TUSINVC.ARCHIVE_INVOICE A "
            "JOIN P2GO.SHARED_NAME S ON S.P2GO_ID=A.ARCHIVE_ID))",
        )
        for sql in statements:
            with self.subTest(path="run", sql=sql):
                self.db.calls.clear()
                self.db.plans.clear()
                out = self.engine.run_sql(sql)
                self.assertEqual(out["sql_executed"], sql)
                lookups = [c["params"] for c in self.db.calls
                           if "FROM ALL_OBJECTS" in c["sql"].upper()]
                self.assertEqual(
                    {(c["o"], c["n"]) for c in lookups},
                    {("P2GO", "INVOICE"), ("P2GO", "SHARED_NAME"),
                     ("TUSINVC", "ARCHIVE_INVOICE")},
                )
            with self.subTest(path="explain", sql=sql):
                self.db.calls.clear()
                self.db.plans.clear()
                explained = self.engine.explain_query(sql)
                self.assertEqual(explained["sql"], sql)
                self.assertEqual(self.db.plans, [sql])

    def test_tusinvc_plan_owner_drives_cost_gate_and_index_advice(self) -> None:
        sql = "SELECT * FROM TUSINVC.ARCHIVE_INVOICE"
        row_targets: list[str] = []
        index_targets: list[str] = []

        def rows(table):
            row_targets.append(table)
            return 250_000 if table == "TUSINVC.ARCHIVE_INVOICE" else 1

        def indexes(table):
            index_targets.append(table)
            if table == "TUSINVC.ARCHIVE_INVOICE":
                return [{"name": "IX_ARCHIVE_ID", "unique": False,
                         "columns": ["ARCHIVE_ID"]}]
            return []

        def plan(statement, params=None):
            self.db.plans.append(statement)
            return {"available": True, "steps": [{
                "operation": "TABLE ACCESS", "options": "FULL",
                "object_owner": "TUSINVC",
                "object": "ARCHIVE_INVOICE", "rows": 250_000,
                "cost": 9000,
            }]}

        self.engine._approx_rows = rows
        self.db.indexes = indexes
        self.db.explain_plan = plan

        with self.assertRaises(EngineError) as caught:
            self.engine.run_sql(sql)
        message = str(caught.exception)
        self.assertIn("TUSINVC.ARCHIVE_INVOICE", message)
        self.assertIn("IX_ARCHIVE_ID", message)
        self.assertTrue(row_targets)
        self.assertEqual(set(row_targets), {"TUSINVC.ARCHIVE_INVOICE"})
        self.assertEqual(set(index_targets), {"TUSINVC.ARCHIVE_INVOICE"})
        self.assertFalse(any(c["sql"] == sql for c in self.db.calls),
                         "the rejected full scan must never execute")

        row_targets.clear()
        index_targets.clear()
        self.db.plans.clear()
        explained = self.engine.explain_query(sql)
        advice = " ".join(explained["advice"])
        self.assertIn("TUSINVC.ARCHIVE_INVOICE", advice)
        self.assertIn("IX_ARCHIVE_ID", advice)
        self.assertEqual(set(row_targets), {"TUSINVC.ARCHIVE_INVOICE"})
        self.assertEqual(set(index_targets), {"TUSINVC.ARCHIVE_INVOICE"})

    def test_oracle_plan_reader_preserves_object_owner(self) -> None:
        db = _OraclePlanDatabase(self.cfg)
        plan = db.explain_plan(
            "SELECT * FROM TUSINVC.ARCHIVE_INVOICE")
        self.assertTrue(plan["available"])
        self.assertEqual(plan["steps"][0]["object_owner"], "TUSINVC")
        self.assertEqual(plan["steps"][0]["object"], "ARCHIVE_INVOICE")
        plan_read = db.connection.cursor_instance.statements[1]
        self.assertIn("OBJECT_OWNER", plan_read)

    def test_list_tables_binds_only_the_two_configured_owners(self) -> None:
        out = self.engine.list_tables("INVOICE")
        self.assertEqual(
            {(r["schema_name"], r["table_name"]) for r in out["tables"]},
            {("P2GO", "INVOICE"), ("TUSINVC", "ARCHIVE_INVOICE")},
        )
        call = self.db.calls[-1]
        self.assertIn("OWNER IN (:owner0, :owner1)", call["sql"])
        self.assertEqual(call["params"]["owner0"], "P2GO")
        self.assertEqual(call["params"]["owner1"], "TUSINVC")
        self.assertNotIn("OTHER", call["params"].values())

    def test_describe_and_index_catalog_use_qualified_owner(self) -> None:
        out = self.engine.describe_table("TUSINVC.ARCHIVE_INVOICE")
        self.assertEqual(
            [c["column_name"] for c in out["columns"]],
            ["ARCHIVE_ID", "AMOUNT"],
        )
        describe = next(c for c in self.db.calls
                        if "FROM ALL_TAB_COLUMNS" in c["sql"].upper())
        self.assertEqual(describe["params"]["owner"], "TUSINVC")
        indexes = next(c for c in self.db.calls
                       if "FROM ALL_IND_COLUMNS" in c["sql"].upper())
        self.assertEqual(indexes["params"]["o"], "TUSINVC")

    def test_quoted_describe_uses_the_same_catalog_owner(self) -> None:
        out = self.engine.describe_table(
            '"TUSINVC"."ARCHIVE_INVOICE"')
        self.assertEqual(out["columns"][0]["column_name"], "ARCHIVE_ID")
        describe = next(c for c in self.db.calls
                        if "FROM ALL_TAB_COLUMNS" in c["sql"].upper())
        self.assertEqual(describe["params"]["owner"], "TUSINVC")

    def test_column_cache_does_not_mix_same_named_cross_schema_tables(self) -> None:
        self.assertEqual(self.db.columns("P2GO.SHARED_NAME"), {"P2GO_ID"})
        self.assertEqual(self.db.columns("TUSINVC.SHARED_NAME"),
                         {"TUSINVC_ID"})

    def test_describe_rejects_an_outside_owner_without_querying(self) -> None:
        with self.assertRaises(EngineError) as caught:
            self.engine.describe_table("OTHER.SECRET")
        self.assertIn("outside this source", str(caught.exception))
        self.assertEqual(self.db.calls, [])


if __name__ == "__main__":
    unittest.main()
