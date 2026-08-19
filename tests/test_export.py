"""Full-data CSV export.

The point of this feature is that the file is NOT what the screen shows:
the card holds a display-capped preview and the export re-runs the tool
to get the whole population. The tests that matter are therefore about
honesty and safety rather than formatting — that a re-run really happens,
that hitting the ceiling is stated in the filename (which outlives every
toast), that a spreadsheet cannot be turned into a weapon by data from a
record someone else writes to, and that a model cannot lift the row limit
that keeps its own context small.
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import export  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402

DAY = dt.date(2026, 8, 6)


class TableFinderTests(unittest.TestCase):
    """Shape tolerance: a tool shipped tomorrow is exportable tomorrow."""

    def test_finds_the_preferred_collection(self) -> None:
        t = export.tabular({"business_unit": "US001",
                            "customers": [{"cust_id": "C1", "billed": 5.0}],
                            "notes": [{"n": 1}, {"n": 2}, {"n": 3}]})
        self.assertEqual(t["label"], "customers")
        self.assertEqual(t["columns"], ["cust_id", "billed"])

    def test_falls_back_to_the_largest_record_list(self) -> None:
        t = export.tabular({"widgets": [{"a": 1}, {"a": 2}],
                            "gizmos": [{"b": 1}]})
        self.assertEqual(t["label"], "widgets")

    def test_union_of_keys_keeps_first_seen_order(self) -> None:
        t = export.tabular({"rows": [{"a": 1}, {"b": 2}, {"a": 3, "c": 4}]})
        self.assertEqual(t["columns"], ["a", "b", "c"])

    def test_pivot_exports_the_grid_with_totals(self) -> None:
        t = export.tabular({"pivot": {
            "row_field": "customer", "columns": ["2026-01", "2026-02"],
            "rows": [{"row": "ACME", "values": [10.0, 20.0], "total": 30.0,
                      "change": 10.0, "change_pct": 100.0}],
            "column_totals": [10.0, 20.0], "grand_total": 30.0}})
        self.assertEqual(t["columns"],
                         ["customer", "2026-01", "2026-02", "total",
                          "change", "change_pct"])
        self.assertEqual(t["rows"][0]["2026-02"], 20.0)
        self.assertEqual(t["rows"][-1]["customer"], "TOTAL")

    def test_payloads_without_a_table_are_not_exportable(self) -> None:
        self.assertIsNone(export.tabular({"ok": True, "latency_ms": 12}))
        self.assertIsNone(export.tabular({"rows": []}))

    def test_nested_values_become_readable_cells(self) -> None:
        t = export.tabular({"rows": [{"id": 1, "tags": ["a", "b"],
                                      "meta": {"k": "v"}}]})
        csv_text = export.to_csv(t)
        self.assertIn("a; b", csv_text)
        self.assertIn('{""k"": ""v""}', csv_text)


class CsvSafetyTests(unittest.TestCase):
    def test_formula_injection_is_neutralized_in_text(self) -> None:
        # A supplier name is data from a record other people write to.
        t = export.tabular({"rows": [{"vendor": "=cmd|'/c calc'!A1",
                                      "note": "@SUM(1)", "amt": -250.5}]})
        out = export.to_csv(t)
        self.assertIn("'=cmd", out)
        self.assertIn("'@SUM(1)", out)
        self.assertIn("-250.5", out)
        self.assertNotIn("'-250.5", out,
                         "a negative amount must keep its sign, not become "
                         "text — numbers are never neutralized")

    def test_quoting_survives_commas_quotes_and_newlines(self) -> None:
        t = export.tabular({"rows": [
            {"descr": 'Smith, Inc "HQ"\nsecond line', "amt": 1.0}]})
        out = export.to_csv(t)
        self.assertIn('"Smith, Inc ""HQ""', out)
        self.assertTrue(out.rstrip().endswith('1.0'))

    def test_excel_reads_utf8_because_of_the_bom(self) -> None:
        out = export.to_csv(export.tabular({"rows": [{"n": "café"}]}))
        self.assertTrue(out.startswith("﻿"))
        self.assertIn("café", out)


class TruncationHonestyTests(unittest.TestCase):
    def test_the_filename_records_the_cut(self) -> None:
        payload = {"rows": [{"i": i} for i in range(10)]}
        out = export.export("run_sql", {}, {}, payload=payload, row_cap=4,
                            today=DAY)
        self.assertEqual(out["rows"], 4)
        self.assertTrue(out["truncated"])
        self.assertIn("TRUNCATED_at_4_rows", out["filename"])
        self.assertIn("10 rows matched", out["note"])

    def test_a_complete_export_says_so(self) -> None:
        out = export.export("run_sql", {}, {},
                            payload={"rows": [{"i": 1}]}, row_cap=100,
                            today=DAY)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["filename"], "run_sql_2026-08-06_1_rows.csv")

    def test_the_ceiling_cannot_be_raised_past_the_hard_limit(self) -> None:
        payload = {"rows": [{"i": i} for i in range(3)]}
        out = export.export("t", {}, {}, payload=payload,
                            row_cap=10_000_000, today=DAY)
        self.assertLessEqual(out["row_cap"], export.EXPORT_MAX_ROWS)

    def test_nothing_to_export_refuses_with_a_remedy(self) -> None:
        with self.assertRaises(export.ExportError) as ctx:
            export.export("coupa_health", {}, {}, payload={"ok": True})
        self.assertIn("not a row set", str(ctx.exception))


class RerunTests(unittest.TestCase):
    """The file must beat the screen — that is the whole feature."""

    def test_a_registered_tool_is_rerun_beyond_the_captured_rows(self) -> None:
        captured = {"rows": [{"i": 0}], "truncated": True}
        registry = {"run_sql": lambda args, cap: {
            "rows": [{"i": i} for i in range(cap)]}}
        out = export.export("run_sql", {"sql": "SELECT 1"}, registry,
                            payload=captured, row_cap=250, today=DAY)
        self.assertTrue(out["rerun"])
        self.assertEqual(out["rows"], 250,
                         "the export served the preview instead of re-running")

    def test_unregistered_tools_fall_back_and_say_so(self) -> None:
        out = export.export("some_new_tool", {}, {},
                            payload={"rows": [{"i": 1}]}, today=DAY)
        self.assertFalse(out["rerun"])
        self.assertIn("rows captured for this card", out["note"])

    def test_unsupported_arguments_are_dropped_not_fatal(self) -> None:
        def only_bu(business_unit: str = "", max_rows: int = 10):
            return {"rows": [{"bu": business_unit, "cap": max_rows}]}

        registry = {"t": lambda a, cap: export._call_filtered(
            only_bu, a, {"max_rows": cap})}
        out = export.export("t", {"business_unit": "US001", "ledger": "X",
                                  "fiscal_year": 2026}, registry, row_cap=7,
                            today=DAY)
        self.assertIn("US001", out["csv"])
        self.assertIn("7", out["csv"])

    def test_coupa_rni_export_lifts_only_the_display_cap(self) -> None:
        class CoupaRNI:
            def __init__(self) -> None:
                self.calls = []

            def received_not_invoiced(
                    self, business_unit: str = "", max_rows=None,
                    display_rows: int = 200) -> dict:
                self.calls.append({"business_unit": business_unit,
                                   "max_rows": max_rows,
                                   "display_rows": display_rows})
                rows = [{"order_line_id": i, "candidate_amount": 1.0}
                        for i in range(205)]
                return {"lines": rows[:display_rows]}

        coupa = CoupaRNI()
        registry = export.build_registry(coupa=coupa)
        out = export.export(
            "get_coupa_rni", {"business_unit": "US001"}, registry,
            payload={"lines": [{"order_line_id": 1}]}, row_cap=500,
            today=DAY)

        self.assertTrue(out["rerun"])
        self.assertEqual(out["rows"], 205)
        self.assertFalse(out["truncated"])
        self.assertEqual(coupa.calls, [{
            "business_unit": "US001", "max_rows": None,
            "display_rows": 500,
        }])
        self.assertIn("Full result re-run server-side", out["note"])

    def test_coupa_source_truncation_is_in_filename_at_exact_export_cap(self):
        class CoupaRNI:
            def received_not_invoiced(self, display_rows: int = 200) -> dict:
                return {
                    "population": {"candidate_count": 3,
                                   "display_truncated": True},
                    "lines": [{"order_line_id": i} for i in range(2)],
                }

        out = export.export(
            "get_coupa_rni", {}, export.build_registry(coupa=CoupaRNI()),
            row_cap=2, today=DAY)
        self.assertEqual(out["rows"], 2)
        self.assertTrue(out["truncated"])
        self.assertIn("TRUNCATED_at_2_rows", out["filename"])
        self.assertIn("2 of 3", out["note"])

    def test_coupa_export_flag_view_exports_receipts_not_candidates(self):
        class CoupaRNI:
            def received_not_invoiced(self, display_rows: int = 200) -> dict:
                return {
                    "population": {"candidate_count": 1,
                                   "display_truncated": False},
                    "lines": [{"order_line_id": "CANDIDATE-ONLY"}],
                    "export_evidence": {
                        "receipt_transaction_count": 2,
                        "display_truncated": False,
                        "receipt_transactions": [
                            {"receipt_transaction_id": "REC-1",
                             "exported": True},
                            {"receipt_transaction_id": "REC-2",
                             "exported": False},
                        ],
                    },
                }

        out = export.export(
            "get_coupa_rni", {"_export_view": "receipt_export_state"},
            export.build_registry(coupa=CoupaRNI()), row_cap=10, today=DAY)
        self.assertEqual(out["rows"], 2)
        self.assertIn("REC-1", out["csv"])
        self.assertNotIn("CANDIDATE-ONLY", out["csv"])
        self.assertIn("receipt_export_state", out["filename"])


class ModelCannotRaiseItsOwnLimitTests(unittest.TestCase):
    """run_sql caps a model at 500 rows so payloads stay readable. Export
    lifts that ceiling — but only from the endpoint, never from a tool the
    model can call."""

    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config.sample(ROOT)
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_the_mcp_tool_signature_has_no_ceiling_parameter(self) -> None:
        src = (ROOT / "pstb" / "server.py").read_text(encoding="utf-8")
        start = src.index("def run_sql(")
        body = src[start:start + 1400]
        self.assertNotIn("row_ceiling", body,
                         "the model-facing run_sql exposes the export "
                         "ceiling — a model could pull 50k rows into "
                         "context")

    def test_engine_still_caps_a_model_sized_request(self) -> None:
        sql = "SELECT ACCOUNT AS a FROM PS_LEDGER WHERE BUSINESS_UNIT = 'US001'"
        capped = self.engine.run_sql(sql, max_rows=5000)
        self.assertLessEqual(len(capped["rows"]), 500)

    def test_export_path_may_exceed_it(self) -> None:
        sql = "SELECT ACCOUNT AS a FROM PS_LEDGER WHERE BUSINESS_UNIT = 'US001'"
        wide = self.engine.run_sql(sql, max_rows=5000, row_ceiling=5000)
        capped = self.engine.run_sql(sql, max_rows=5000)
        self.assertGreaterEqual(len(wide["rows"]), len(capped["rows"]))


class EndpointTests(unittest.TestCase):
    def test_export_endpoint_streams_csv_with_honest_headers(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
            r = client.post("/api/export", json={
                "tool": "run_sql",
                "args": {"sql": "SELECT ACCOUNT AS a, POSTED_TOTAL_AMT AS amt "
                                "FROM PS_LEDGER WHERE BUSINESS_UNIT = 'US001'"},
            })
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/csv", r.headers["content-type"])
            self.assertIn("attachment; filename=",
                          r.headers["content-disposition"])
            self.assertIn("finance_run_sql_",
                          r.headers["content-disposition"])
            self.assertEqual(r.headers["X-Export-Source"], "finance")
            self.assertEqual(r.headers["X-Export-Rerun"], "1")
            self.assertGreater(int(r.headers["X-Export-Rows"]), 0)
            self.assertIn("a,amt", r.text)

    def test_a_result_only_card_exports_without_a_rerun(self) -> None:
        # The AP tie-out is not re-runnable from the registry (it spans two
        # systems), so its captured breaks are what gets exported — still a
        # real CSV, and the header says no re-run happened.
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
            r = client.post("/api/export", json={
                "tool": "coupa_to_ap_tie",
                "result": {"amount_breaks": [
                    {"invoice": "INV-9002", "difference": 300.0}]}})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["X-Export-Rerun"], "0")
            self.assertIn("INV-9002", r.text)

    def test_nothing_exportable_is_a_400_with_the_reason(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
            r = client.post("/api/export", json={
                "tool": "coupa_health", "result": {"ok": True}})
            self.assertEqual(r.status_code, 400)
            self.assertIn("not a row set", r.json()["detail"])

    def test_p2go_sql_export_reruns_only_on_the_bound_child_engine(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        class Registry:
            @staticmethod
            def resolve_command(command):
                return "p2go" if command == "p2go" else "default"

            @staticmethod
            def resolve_name(source):
                return str(source or "default")

            @staticmethod
            def describe():
                return [
                    {"source": "default", "command": "finance"},
                    {"source": "p2go", "command": "p2go"},
                ]

        class ChildEngine:
            def __init__(self):
                self.calls = []

            def run_sql(self, sql, max_rows=100, row_ceiling=0):
                self.calls.append(sql)
                return {"rows": [{"database": "p2go"}]}

        child = ChildEngine()
        with (patch.object(gapp.engine, "registry", Registry()),
              patch.object(gapp.engine, "for_source", return_value=child)
              as bound,
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            response = client.post("/api/source/p2go/export", json={
                "tool": "run_sql",
                "args": {"source": "p2go", "sql": "SELECT 1 AS x"},
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("p2go", response.text)
        self.assertIn("p2go_run_sql_",
                      response.headers["content-disposition"])
        self.assertEqual(response.headers["X-Export-Source"], "p2go")
        bound.assert_called_once_with("p2go")
        self.assertEqual(child.calls, ["SELECT 1 AS x"])

    def test_finance_export_never_resolves_a_secondary_engine(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        with (patch.object(gapp.engine, "for_source") as child,
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            response = client.post("/api/source/finance/export", json={
                "tool": "run_sql",
                "args": {"source": "default", "sql": "SELECT 1 AS x"},
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("finance_run_sql_",
                      response.headers["content-disposition"])
        self.assertEqual(response.headers["X-Export-Source"], "finance")
        child.assert_not_called()

    def test_export_route_rejects_conflicting_source_aliases(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        class Registry:
            @staticmethod
            def resolve_command(command):
                return "p2go"

            @staticmethod
            def resolve_name(source):
                return str(source or "default")

            @staticmethod
            def describe():
                return [
                    {"source": "default", "command": "finance"},
                    {"source": "p2go", "command": "p2go"},
                ]

        with (patch.object(gapp.engine, "registry", Registry()),
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            response = client.post("/api/source/p2go/export", json={
                "tool": "run_sql",
                "args": {"source": "p2go", "db": "default",
                         "sql": "SELECT 1"},
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("args.db", response.json()["detail"])

    def test_restricted_user_cannot_bypass_raw_sql_policy_via_export(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        access = SimpleNamespace(
            all_units=False, units={"US001"}, oprid="RESTRICTED",
            allows=lambda unit: unit == "US001")
        with (patch.object(gapp, "access_for_request", return_value=access),
              patch.object(gapp.engine, "for_source") as child,
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            response = client.post("/api/source/finance/export", json={
                "tool": "run_sql",
                "args": {"source": "default", "sql": "SELECT 1"},
            })
        self.assertEqual(response.status_code, 403)
        child.assert_not_called()

    def test_no_finance_units_cannot_enable_p2go_raw_sql_via_export(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        class Registry:
            @staticmethod
            def resolve_command(command):
                return "p2go" if command == "p2go" else "default"

            @staticmethod
            def resolve_name(source):
                return str(source or "default")

            @staticmethod
            def describe():
                return [
                    {"source": "default", "command": "finance"},
                    {"source": "p2go", "command": "p2go"},
                ]

        access = SimpleNamespace(
            all_units=False, units=frozenset(), oprid="P2GO_ONLY",
            allows=lambda _unit: False,
        )
        with (patch.object(gapp.engine, "registry", Registry()),
              patch.object(gapp, "access_for_request", return_value=access),
              patch.object(gapp.engine, "for_source") as child,
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            response = client.post("/api/source/p2go/export", json={
                "tool": "run_sql",
                "args": {"source": "p2go", "sql": "SELECT 1"},
            })
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("arbitrary SQL", response.json()["detail"])
        child.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class HeaderRowInjectionTests(unittest.TestCase):
    """A pivot's COLUMNS are database values, so the header needs the same
    neutralising the cells already get.

    to_csv passed `columns` straight to writerow while every data cell went
    through _cell. On a pivot the column labels come from the database —
    account descriptions, customer names, period labels — so poisoning
    PS_GL_ACCOUNT_TBL.DESCR put the payload on the one row Excel evaluates
    first, under a module docstring promising "no formula injection".
    """

    RISKY = ("=cmd|'/C calc.exe'!A0", "+1+1", "-2+3", "@SUM(A1:A9)")

    def test_a_poisoned_column_label_is_neutralised(self):
        from pstb.export import to_csv
        for payload in self.RISKY:
            with self.subTest(payload=payload):
                csv_text = to_csv({"columns": ["account", payload],
                                   "rows": [{"account": "1000", payload: 1}]})
                header = csv_text.lstrip("﻿").splitlines()[0]
                self.assertNotIn(f",{payload}", header,
                                 "the label reached Excel as a formula")
                self.assertIn("'" + payload, header)

    def test_ordinary_headers_are_untouched(self):
        from pstb.export import to_csv
        csv_text = to_csv({"columns": ["account", "Ending DR", "FY2026 P6"],
                           "rows": []})
        self.assertEqual(csv_text.lstrip("﻿").splitlines()[0],
                         "account,Ending DR,FY2026 P6")

    def test_the_row_cells_still_work(self):
        """Guard against fixing the header by breaking what already worked."""
        from pstb.export import to_csv
        csv_text = to_csv({"columns": ["vendor"],
                           "rows": [{"vendor": "=cmd|'/C calc'!A0"}]})
        self.assertIn("'=cmd", csv_text)
