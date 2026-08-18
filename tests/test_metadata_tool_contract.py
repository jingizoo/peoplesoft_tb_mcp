"""MCP/controller boundaries for the structural metadata catalog.

Catalog discovery helps choose a record; it is never evidence that an AR, AP
or GL assertion is true.  The classification tests here guard the same leak
that broad anomaly scans once caused: a structural tool must not satisfy the
financial evidence gate merely because its result is non-empty.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MetadataToolContractTests(unittest.TestCase):
    def test_metadata_tools_have_the_small_read_only_contract(self) -> None:
        from pstb import server

        search = getattr(server, "search_metadata", None)
        describe_catalog = getattr(server, "describe_metadata_catalog", None)
        context = getattr(server, "get_metadata_context", None)
        self.assertIsNotNone(search, "search_metadata is not registered")
        self.assertIsNotNone(
            describe_catalog, "describe_metadata_catalog is not registered")
        self.assertIsNotNone(context,
                             "get_metadata_context is not registered")
        self.assertEqual(
            set(inspect.signature(search).parameters),
            {"query", "source", "kinds", "limit"},
        )
        self.assertEqual(
            set(inspect.signature(describe_catalog).parameters), {"source"})
        self.assertEqual(
            set(inspect.signature(context).parameters),
            {"identifier", "source", "limit"},
        )

    def test_relationship_graph_is_registered_when_raw_sql_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                "db:\n"
                "  backend: sqlite\n"
                f"  sqlite_path: {ROOT / 'sample_data' / 'ps_sample.db'}\n"
                "tools:\n"
                "  allow_raw_sql: false\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["PSTB_CONFIG"] = str(config)
            probe = subprocess.run(
                [sys.executable, "-c", (
                    "from pstb import server; "
                    "assert server.cfg.tools.allow_raw_sql is False; "
                    "assert callable(server.join_path); "
                    "assert not hasattr(server, 'run_sql')"
                )],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def test_metadata_is_structural_and_never_financial_evidence(self) -> None:
        from pstb.guards import (FINANCIAL_EVIDENCE_TOOLS, STRUCTURAL_TOOLS,
                                 financial_tool_domains,
                                 financial_tool_is_relevant)

        for tool in ("describe_metadata_catalog", "search_metadata",
                     "get_metadata_context"):
            self.assertIn(tool, STRUCTURAL_TOOLS)
            self.assertNotIn(tool, FINANCIAL_EVIDENCE_TOOLS)
            self.assertEqual(financial_tool_domains(tool), set())
            self.assertFalse(financial_tool_is_relevant(
                tool, "How many billing-interface rows are rejected?"))

    def test_acceptance_pack_uses_metadata_first_custom_paths(self) -> None:
        cases = {
            c["id"]: c
            for c in json.loads((ROOT / "evals" / "cases.json").read_text(
                encoding="utf-8"))["cases"]
        }
        required = {
            "custom-record": "get_metadata_context",
            "custom-billing-record-no-prefix": "get_metadata_context",
            "metadata-field-label-discovery": "search_metadata",
            "metadata-ambiguous-source-schema": "get_metadata_context",
            "metadata-live-evidence-boundary": "run_sql",
        }
        for case_id, required_tool in required.items():
            self.assertIn(case_id, cases)
            expect = cases[case_id]["expect"]
            self.assertIn(required_tool, expect.get("any_tool") or [])
            self.assertIn("search_records", expect.get("not_tool") or [])

        live = cases["metadata-live-evidence-boundary"]
        self.assertEqual(
            live["expect"]["tool_args_contain"]["business_unit"], "US001")
        self.assertEqual(live["expect"]["ordered_tools"], [
            "search_metadata", "get_metadata_context", "run_sql"])
        self.assertEqual(live["expect"]["all_tools"], [
            "search_metadata", "get_metadata_context", "run_sql"])
        self.assertIn("metadata alone is not an answer",
                      live["question"].lower())
        isolated = cases["metadata-ambiguous-source-schema"]
        self.assertEqual(isolated["scope"], {"source": "p2go"})
        self.assertEqual(
            isolated["expect"]["tool_args_contain"]["source"], "p2go")
        self.assertNotIn("every configured database",
                         isolated["question"].lower())

    def test_metadata_tools_receive_no_business_unit_scope_argument(self) -> None:
        from pstb.guards import _TOOL_SCOPE_ARGS

        for tool in ("describe_metadata_catalog", "search_metadata",
                     "get_metadata_context"):
            self.assertEqual(
                _TOOL_SCOPE_ARGS.get(tool), {"source": "source"},
                "metadata discovery follows the selected database namespace "
                "but must never inherit PeopleSoft BU/ledger/time fields",
            )

    def test_gemini_custom_discovery_shortlist_includes_metadata_sequence(self):
        from pstb.client.llm_gemini import routing_tool_names

        available = {
            "search_metadata", "get_metadata_context",
            "describe_metadata_catalog", "search_records",
            "describe_record", "profile_record", "compare_records",
            "run_sql", "wiki_search",
        }
        got = set(routing_tool_names(
            "Which custom record has the approval status field?", available))
        self.assertIn("search_metadata", got)
        self.assertIn("get_metadata_context", got)
        self.assertIn("profile_record", got)

    def test_web_ui_has_dedicated_explainable_metadata_cards(self) -> None:
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text(
            encoding="utf-8")
        for renderer in (
                "renderMetadataCatalog", "renderMetadataSearch",
                "renderMetadataContext"):
            self.assertIn(f"function {renderer}", html)
        for tool, renderer in (
                ("describe_metadata_catalog", "renderMetadataCatalog"),
                ("search_metadata", "renderMetadataSearch"),
                ("get_metadata_context", "renderMetadataContext")):
            self.assertIn(
                f"if(name==='{tool}') return {renderer}(data);", html)
        self.assertIn("unresolved — do not guess", html)
        self.assertIn("Declared keys & relationships", html)
        self.assertIn("View lineage", html)
        self.assertIn("database-native dependency catalog, no stored SQL", html)
        self.assertIn("matched metadata:", html)
        self.assertIn("semantic advisory order", html)
        self.assertIn("deterministic fallback", html)
        self.assertIn("semantic weight", html)

    def test_semantic_reranker_is_advisory_inside_structural_search(self):
        from pstb import server

        class FakeReranker:
            enabled = True

            def rerank(self, query, matches):
                return {
                    "matches": list(reversed(matches)),
                    "applied": True, "status": "applied",
                    "boundary": "only reordered deterministic candidates",
                }

        class FakeCatalog:
            def search(self, **kwargs):
                return {"available": True, "matches": [
                    {"object_id": "one", "confidence": "confirmed"},
                    {"object_id": "two", "confidence": "candidate"},
                ]}

        old_catalog, old_reranker = server.metadata_catalog, server.metadata_reranker
        try:
            server.metadata_catalog = FakeCatalog()
            server.metadata_reranker = FakeReranker()
            got = server.search_metadata("business phrase")
        finally:
            server.metadata_catalog = old_catalog
            server.metadata_reranker = old_reranker
        self.assertEqual([row["object_id"] for row in got["matches"]],
                         ["two", "one"])
        self.assertEqual(got["matches"][0]["confidence"], "candidate")
        self.assertTrue(got["semantic_rerank"]["applied"])


class PublicQueryOwnerPortabilityTests(unittest.TestCase):
    def test_metadata_collector_uses_oracle_safe_public_owner_join(self):
        from pstb.metadata import _pt_public_query_rows

        class OracleCatalog:
            dialect = "oracle"
            prefix = "SYSADM."

            def __init__(self):
                self.sql = ""

            def query(self, sql, params=None, max_rows=None):
                self.sql = sql
                return [], False

        db = OracleCatalog()
        self.assertEqual(list(_pt_public_query_rows(
            db, page_size=100, cap=100)), [])
        compact = " ".join(db.sql.split())
        self.assertIn("TRIM(D.OPRID) IS NULL", compact)
        self.assertIn("TRIM(R.OPRID) IS NULL", compact)
        self.assertNotIn("COALESCE(D.OPRID,'')", compact)
        self.assertNotIn("COALESCE(TRIM(D.OPRID)", compact.upper())
        self.assertNotIn("COALESCE(TRIM(R.OPRID)", compact.upper())
        self.assertNotIn("D.OPRID=R.OPRID", compact.replace(" ", ""))

    def test_blank_public_owner_never_uses_empty_string_or_null_equals_null(self):
        from pstb.psquery import QueryCatalog

        class OracleCatalog:
            dialect = "oracle"
            prefix = "SYSADM."

            def __init__(self):
                self.sql: list[str] = []

            def columns(self, table):
                return {
                    "PSQRYDEFN": {"QRYNAME", "OPRID", "DESCR"},
                    "PSQRYRECORD": {"QRYNAME", "OPRID", "RECNAME"},
                    "PSQRYSTATS": {"QRYNAME", "OPRID", "EXECCOUNT"},
                }.get(table, set())

            def has_column(self, table, column):
                return column in self.columns(table)

            def query(self, sql, params=None, max_rows=None):
                self.sql.append(sql)
                return [], False

        db = OracleCatalog()
        QueryCatalog(SimpleNamespace(db=db)).search_queries(
            record="VOUCHER", include_private=False)
        sql = db.sql[-1]
        compact = " ".join(sql.split())
        self.assertIn("TRIM(Q.OPRID) IS NULL", compact)
        self.assertNotIn("COALESCE(S.OPRID,'')", compact)
        self.assertIn("R.OPRID = Q.OPRID OR", compact)
        self.assertIn("TRIM(R.OPRID) IS NULL", compact)
        self.assertIn("S.OPRID = Q.OPRID OR", compact)
        self.assertIn("TRIM(S.OPRID) IS NULL", compact)

    def test_sqlserver_metadata_collector_uses_valid_blank_owner_sql(self):
        from pstb.metadata import _pt_public_query_rows

        class SqlServerCatalog:
            dialect = "sqlserver"
            prefix = "dbo."

            def __init__(self):
                self.sql = ""

            def query(self, sql, params=None, max_rows=None):
                self.sql = sql
                return [], False

        db = SqlServerCatalog()
        list(_pt_public_query_rows(db, page_size=100, cap=100))
        compact = " ".join(db.sql.split())
        self.assertNotIn("LENGTH(", compact)
        squashed = compact.replace(" ", "")
        for alias in ("D", "R"):
            self.assertIn(f"TRIM({alias}.OPRID)ISNULL", squashed)
            self.assertTrue(
                f"LEN(TRIM({alias}.OPRID))=0" in squashed
                or f"TRIM({alias}.OPRID)=''" in squashed,
                compact,
            )

    def test_live_sqlserver_query_catalog_uses_valid_blank_owner_sql(self):
        from pstb.psquery import QueryCatalog

        class SqlServerCatalog:
            dialect = "sqlserver"
            prefix = "dbo."

            def __init__(self):
                self.sql: list[str] = []

            def columns(self, table):
                return {
                    "PSQRYDEFN": {"QRYNAME", "OPRID", "DESCR"},
                    "PSQRYRECORD": {"QRYNAME", "OPRID", "RECNAME"},
                    "PSQRYSTATS": {"QRYNAME", "OPRID", "EXECCOUNT"},
                }.get(table, set())

            def has_column(self, table, column):
                return column in self.columns(table)

            def query(self, sql, params=None, max_rows=None):
                self.sql.append(sql)
                return [], False

        db = SqlServerCatalog()
        QueryCatalog(SimpleNamespace(db=db)).search_queries(
            record="VOUCHER", include_private=False)
        compact = " ".join(db.sql[-1].split())
        self.assertNotIn("LENGTH(", compact)
        squashed = compact.replace(" ", "")
        for alias in ("Q", "R", "S"):
            self.assertIn(f"TRIM({alias}.OPRID)ISNULL", squashed)
            self.assertTrue(
                f"LEN(TRIM({alias}.OPRID))=0" in squashed
                or f"TRIM({alias}.OPRID)=''" in squashed,
                compact,
            )


if __name__ == "__main__":
    unittest.main()
