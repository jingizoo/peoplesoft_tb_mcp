"""Source-separated model eval coverage for Finance and multi-schema P2Go."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class MultiSchemaEvalPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "eval_harness_multi_schema", ROOT / "scripts" / "eval.py")
        cls.ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ev)
        raw = json.loads(
            (ROOT / "evals" / "p2go_cases.json").read_text(encoding="utf-8"))
        cls.cases = {case["id"]: case for case in raw["cases"]}

    def test_pack_covers_every_source_boundary_scenario(self) -> None:
        self.assertEqual(set(self.cases), {
            "p2go-catalog-health",
            "p2go-semantic-search",
            "p2go-explain-and-empty-read",
            "p2go-default-schema",
            "p2go-secondary-schema",
            "p2go-cross-schema-path",
            "p2go-same-name-ambiguity",
            "p2go-outside-owner-refusal",
            "p2go-finance-isolation",
        })
        self.assertTrue(all(
            case.get("scope") == {"source": "p2go"}
            for case in self.cases.values()
        ))
        finance = {
            case["id"]: case for case in json.loads(
                (ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
            ["cases"]
        }
        reciprocal = finance["finance-p2go-isolation"]
        self.assertEqual(reciprocal["scope"], {"source": "default"})
        self.assertEqual(reciprocal["expect"]["allowed_tools"], [])
        self.assertIn("get_metadata_context",
                      reciprocal["expect"]["not_tool"])
        self.assertIn("switch", reciprocal["expect"]["answer_contains"])

    def test_suite_scores_cannot_mix_source_scopes(self) -> None:
        finance, finance_na = self.ev._load_suite_cases("finance")
        p2go, _p2go_na = self.ev._load_suite_cases("p2go", {})
        self.assertEqual(finance_na, [])
        self.assertTrue(finance)
        self.assertTrue(p2go)
        self.assertTrue(all(
            self.ev._case_source(case) in ("", "default")
            for case in finance
        ))
        self.assertTrue(all(
            self.ev._case_source(case) == "p2go" for case in p2go
        ))
        self.assertNotIn(
            "metadata-ambiguous-source-schema",
            {case["id"] for case in finance},
        )
        self.assertIn(
            "metadata-ambiguous-source-schema",
            {case["id"] for case in p2go},
        )

    def test_every_real_finance_case_runs_with_production_default_pin(self):
        finance, _ = self.ev._load_suite_cases("finance")
        self.assertTrue(finance)
        self.assertTrue(all(
            self.ev._runtime_scope(case).get("source") == "default"
            for case in finance
        ))
        p2go, _ = self.ev._load_suite_cases("p2go", {})
        self.assertTrue(all(
            self.ev._runtime_scope(case).get("source") == "p2go"
            for case in p2go
        ))

    def test_runtime_profile_matches_each_production_workspace(self) -> None:
        from pstb.config import Config
        from pstb.guards import SOURCE_SILO_TOOLS

        cfg = Config.sample(ROOT)
        tools = [SimpleNamespace(name=name) for name in (
            "get_trial_balance", "wiki_lookup", "run_sql",
            "get_metadata_context", "join_path",
        )]
        p2go_prompt, p2go_tools = self.ev._runtime_profile(
            cfg, "gemini", {"scope": {"source": "p2go"}}, tools)
        self.assertIn("exactly one database:\np2go", p2go_prompt)
        self.assertNotIn("PS_LEDGER", p2go_prompt)
        self.assertEqual(
            {tool.name for tool in p2go_tools},
            {tool.name for tool in tools if tool.name in SOURCE_SILO_TOOLS},
        )
        self.assertNotIn("get_trial_balance",
                         {tool.name for tool in p2go_tools})
        self.assertNotIn("wiki_lookup", {tool.name for tool in p2go_tools})

        finance_prompt, finance_tools = self.ev._runtime_profile(
            cfg, "gemini", {"scope": {
                "source": "default", "business_unit": "US001",
                "ledger": "ACTUALS", "fiscal_year": 2026, "period": 6,
            }}, tools)
        self.assertIn("verified against PS_LEDGER", finance_prompt)
        self.assertEqual([tool.name for tool in finance_tools],
                         [tool.name for tool in tools])

    def test_catalog_health_assertion_detects_missing_or_stale(self) -> None:
        case = self.cases["p2go-catalog-health"]
        base = {
            "tool": "describe_metadata_catalog", "ok": True,
            "args": {"source": "p2go"},
            "_result": {
                "source_database": "p2go", "available": True,
                "snapshot": {"id": "a" * 20, "stale": False,
                             "partial": False, "status": "complete"},
                "schema_coverage": {
                    "configured": ["P2GO", "TUSINVC"], "missing": [],
                    "complete": True,
                },
                "latest_build": {"published": True, "status": "complete",
                                 "snapshot_id": "a" * 20},
            },
        }
        self.assertEqual(self.ev._grade(
            case, "P2GO and TUSINVC are ready", [base]), [])

        stale = {**base, "_result": {
            "source_database": "p2go", "available": True,
            "snapshot": {"id": "a" * 20, "stale": True,
                         "partial": False},
            "schema_coverage": {
                "configured": ["P2GO", "TUSINVC"], "missing": [],
                "complete": True,
            },
            "latest_build": {"published": True, "status": "complete",
                             "snapshot_id": "a" * 20},
        }}
        self.assertTrue(self.ev._grade(case, "stale", [stale]))
        missing = {**base, "_result": {
            "source_database": "p2go", "available": False,
        }}
        self.assertTrue(self.ev._grade(case, "missing", [missing]))

        partial = {**base, "_result": {
            "source_database": "p2go", "available": True,
            "snapshot": {"id": "a" * 20, "stale": False,
                         "partial": True},
            "schema_coverage": {
                "configured": ["P2GO", "TUSINVC"],
                "missing": ["TUSINVC"], "complete": False,
            },
            "latest_build": {"published": True, "status": "partial",
                             "snapshot_id": "a" * 20},
        }}
        self.assertTrue(self.ev._grade(case, "partial", [partial]))

        failed_rebuild = {**base, "_result": {
            **base["_result"],
            "latest_build": {"published": False, "status": "failed"},
        }}
        problems = self.ev._grade(
            case, "the previous snapshot is still readable", [failed_rebuild])
        self.assertTrue(any("latest_build.published=True" in problem
                            for problem in problems), problems)
        self.assertTrue(any("latest_build.status='complete'" in problem
                            for problem in problems), problems)

        mismatch = {**base, "_result": {
            **base["_result"],
            "latest_build": {"published": True, "status": "complete",
                             "snapshot_id": "b" * 20},
        }}
        self.assertTrue(any("snapshot.id == latest_build.snapshot_id" in p
                            for p in self.ev._grade(
                                case, "P2GO TUSINVC", [mismatch])))

        extra_owner = {**base, "_result": {
            **base["_result"],
            "schema_coverage": {
                "configured": ["P2GO", "TUSINVC", "SYSADM"],
                "missing": [], "complete": True,
            },
        }}
        self.assertTrue(self.ev._grade(
            case, "P2GO TUSINVC SYSADM", [extra_owner]))

        stitched = [
            {**base, "_result": {
                **base["_result"],
                "schema_coverage": {
                    "configured": ["P2GO"], "missing": [],
                    "complete": True,
                },
            }},
            {**base, "_result": {
                **base["_result"],
                "available": False,
                "snapshot": {"id": "a" * 20, "stale": False,
                             "partial": True},
                "schema_coverage": {
                    "configured": ["TUSINVC"], "missing": [],
                    "complete": True,
                },
            }},
        ]
        self.assertTrue(self.ev._grade(
            case, "P2GO TUSINVC", stitched))

    def test_semantic_search_requires_the_requested_object_in_one_clean_call(self):
        case = self.ev._replace_values(
            self.cases["p2go-semantic-search"], {
                "P2GO_DEFAULT_OBJECT": "INVOICE",
                "P2GO_DEFAULT_SCHEMA": "P2GO",
            })
        base = {
            "tool": "search_metadata", "ok": True,
            "args": {"source": "p2go", "query": "INVOICE"},
            "_result": {
                "source_database": "p2go", "available": True,
                "matches": [{"source": "p2go", "schema": "P2GO",
                             "name": "INVOICE"}],
            },
        }
        self.assertEqual(self.ev._grade(case, "P2GO INVOICE", [base]), [])
        unrelated = {**base, "_result": {
            **base["_result"],
            "matches": [{"source": "p2go", "schema": "P2GO",
                         "name": "COMPLETELY_UNRELATED"}],
        }}
        self.assertTrue(self.ev._grade(case, "P2GO candidate", [unrelated]))
        contaminated = {**base, "_result": {
            **base["_result"],
            "matches": [{"source": "default", "schema": "SYSADM",
                         "name": "INVOICE"}],
        }}
        self.assertTrue(self.ev._grade(
            case, "P2GO INVOICE", [base, contaminated]))

    def test_zero_row_query_eval_rejects_a_nonempty_success(self):
        case = self.ev._replace_values(
            self.cases["p2go-explain-and-empty-read"], {
                "P2GO_DEFAULT_OBJECT": "INVOICE",
                "P2GO_DEFAULT_SCHEMA": "P2GO",
            })
        sql = "SELECT * FROM P2GO.INVOICE WHERE 1=0"
        explain = {
            "tool": "explain_query", "ok": True,
            "args": {"source": "p2go", "sql": sql},
            "_result": {"source_database": "p2go", "available": True},
        }
        empty = {
            "tool": "run_sql", "ok": True,
            "args": {"source": "p2go", "sql": sql},
            "_result": {"source_database": "p2go", "rows": [],
                        "row_count": 0, "truncated": False},
        }
        self.assertEqual(self.ev._grade(
            case, "P2Go boundary test passed", [explain, empty]), [])
        nonempty = {**empty, "_result": {
            "source_database": "p2go", "rows": [{"secret": "x"}],
            "row_count": 1, "truncated": False,
        }}
        self.assertTrue(self.ev._grade(
            case, "P2Go boundary test passed", [explain, nonempty]))

    def test_cross_schema_path_requires_native_relationship_evidence(self):
        case = self.ev._replace_values(
            self.cases["p2go-cross-schema-path"], {
                "P2GO_CROSS_FROM": "P2GO.INVOICE",
                "P2GO_CROSS_TO": "TUSINVC.ARCHIVE_INVOICE",
                "P2GO_CROSS_FROM_SCHEMA": "P2GO",
                "P2GO_CROSS_TO_SCHEMA": "TUSINVC",
                "P2GO_CROSS_FROM_NAME": "INVOICE",
                "P2GO_CROSS_TO_NAME": "ARCHIVE_INVOICE",
            })
        base = {
            "tool": "join_path", "ok": True,
            "args": {
                "source": "p2go", "from_record": "P2GO.INVOICE",
                "to_record": "TUSINVC.ARCHIVE_INVOICE",
            },
            "_result": {
                "source_database": "p2go", "found": True,
                "from": {"schema": "P2GO", "name": "INVOICE"},
                "to": {"schema": "TUSINVC", "name": "ARCHIVE_INVOICE"},
                "hops": [{
                    "from": {"schema": "P2GO"},
                    "to": {"schema": "TUSINVC"},
                    "relationship": "foreign_key",
                }],
            },
        }
        self.assertEqual(self.ev._grade(
            case, "P2GO to TUSINVC", [base]), [])
        inferred = {**base, "_result": {
            **base["_result"],
            "hops": [{
                "from": {"schema": "P2GO"},
                "to": {"schema": "TUSINVC"},
                "relationship": "shared_columns_and_indexes",
            }],
        }}
        self.assertTrue(self.ev._grade(
            case, "P2GO to TUSINVC", [inferred]))

    def test_suite_wide_source_and_zero_tool_invariants(self) -> None:
        p2go_case = self.cases["p2go-default-schema"]
        wrong_source = [{
            "tool": "get_metadata_context", "ok": True,
            "args": {"identifier": "INVOICE", "source": "p2go"},
            "_result": {"source_database": "default", "found": True,
                        "schema": "P2GO"},
        }]
        expanded = self.ev._replace_values(p2go_case, {
            "P2GO_DEFAULT_OBJECT": "INVOICE",
            "P2GO_DEFAULT_SCHEMA": "P2GO",
        })
        self.assertTrue(any("selected source" in problem
                            for problem in self.ev._grade(
                                expanded, "P2GO", wrong_source)))

        finance, _ = self.ev._load_suite_cases("finance")
        isolation = next(case for case in finance
                         if case["id"] == "finance-p2go-isolation")
        finance_call = [{
            "tool": "get_trial_balance", "ok": True, "args": {},
            "_result": {"rows": []},
        }]
        self.assertTrue(any("closed profile" in problem
                            for problem in self.ev._grade(
                                isolation, "P2Go switch Finance",
                                finance_call)))

    def test_legacy_argument_assertion_needs_successful_expected_call(self):
        case = {
            "expect": {
                "any_tool": ["get_trial_balance"],
                "tool_args_contain": {"period": 3},
            }
        }
        calls = [
            {"tool": "get_trial_balance", "ok": False,
             "args": {"period": 3}, "_result": {"error": "bad"}},
            {"tool": "tb_integrity_check", "ok": True,
             "args": {"period": 6}, "_result": {}},
        ]
        self.assertTrue(any("successful expected" in problem
                            for problem in self.ev._grade(case, "", calls)))

    def test_negative_eval_requires_the_expected_guard_error(self) -> None:
        case = self.cases["p2go-outside-owner-refusal"]
        refused = [{
            "tool": "run_sql", "ok": False,
            "args": {"source": "p2go", "sql": "SELECT * FROM SYS.DUAL"},
            "_result": {"error": (
                "Schema SYS is outside the selected source; allowed schemas "
                "are P2GO, TUSINVC")},
        }]
        self.assertEqual(self.ev._grade(
            case, "SYS is outside the selected P2Go source.", refused), [])

        wrong_failure = [{**refused[0], "_result": {
            "error": "network timeout"}}]
        self.assertTrue(self.ev._grade(
            case, "The query was outside.", wrong_failure))
        wrong_query = [{**refused[0],
                        "args": {"source": "p2go",
                                 "sql": "SELECT * FROM OTHER.SECRET"},
                        "_result": {"error": (
                            "Schema OTHER is outside the selected source")}}]
        self.assertTrue(self.ev._grade(
            case, "OTHER is outside the selected P2Go source.", wrong_query))
        self.assertTrue(self.ev._grade(case, "outside", []))

    def test_json_observation_never_copies_transaction_rows(self) -> None:
        self.assertEqual(self.ev._result_observation(
            "run_sql", {"source_database": "p2go",
                        "rows": [{"secret": "must-not-be-recorded"}]}), {})
        observed = self.ev._result_observation(
            "describe_metadata_catalog", {
                "source_database": "p2go", "available": True,
                "schema_coverage": {
                    "configured": ["P2GO", "TUSINVC"], "complete": True,
                    "object_counts": {"P2GO": 4, "TUSINVC": 3},
                    "missing": [],
                },
                "latest_build": {"published": True, "status": "complete"},
            })
        self.assertEqual(observed["source_database"], "p2go")
        self.assertEqual(observed["latest_build"], {
            "published": True, "status": "complete"})
        self.assertNotIn("rows", observed)

    def test_private_eval_json_is_owner_only_and_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            target = root / "eval-all.json"
            target.symlink_to(victim)

            self.ev._write_private_json(target, {"answer": "private"})

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(target.read_text()), {
                "answer": "private"})

    def test_result_field_assertions_hold_ambiguity_candidates(self) -> None:
        values = {
            "P2GO_AMBIGUOUS_OBJECT": "SHARED_NAME",
            "P2GO_AMBIGUOUS_DEFAULT_SCHEMA": "P2GO",
            "P2GO_AMBIGUOUS_SECONDARY_SCHEMA": "TUSINVC",
        }
        expanded = self.ev._replace_values(
            self.cases["p2go-same-name-ambiguity"], values)
        calls = [{
            "tool": "get_metadata_context", "ok": True,
            "args": {"identifier": "SHARED_NAME", "source": "p2go"},
            "_result": {
                "source_database": "p2go", "found": False,
                "ambiguous": True,
                "candidates": [
                    {"schema": "P2GO", "name": "SHARED_NAME"},
                    {"schema": "TUSINVC", "name": "SHARED_NAME"},
                ],
            },
        }]
        self.assertEqual(self.ev._grade(
            expanded, "P2GO and TUSINVC candidates", calls), [])

        false_pass = [
            {**calls[0], "ok": False},
            {"tool": "describe_metadata_catalog", "ok": True,
             "args": {"source": "p2go"},
             "_result": {"source_database": "p2go", "available": True}},
        ]
        problems = self.ev._grade(expanded, "two candidates", false_pass)
        self.assertTrue(any("no successful get_metadata_context" in problem
                            for problem in problems), problems)
        self.assertTrue(any("no single successful get_metadata_context result"
                            in problem
                            for problem in problems), problems)

    def test_fixture_discovery_uses_structure_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p2go.db"
            with sqlite3.connect(path) as con:
                con.executescript(
                    "CREATE TABLE nodes (id TEXT PRIMARY KEY,source TEXT,"
                    "schema_name TEXT,kind TEXT,name TEXT);"
                    "CREATE TABLE edges (src TEXT,dst TEXT,kind TEXT);"
                )
                con.executemany(
                    "INSERT INTO nodes VALUES (?,?,?,?,?)", [
                        ("p_inv", "p2go", "P2GO", "table", "INVOICE"),
                        ("p_shared", "p2go", "P2GO", "table", "SHARED"),
                        ("t_arc", "p2go", "TUSINVC", "table", "ARCHIVE"),
                        ("t_shared", "p2go", "TUSINVC", "table", "SHARED"),
                        ("fk", "p2go", "P2GO", "constraint", "FK_ARCHIVE"),
                    ])
                con.executemany("INSERT INTO edges VALUES (?,?,?)", [
                    ("p_inv", "fk", "object_has_constraint"),
                    ("fk", "t_arc", "foreign_key_references_object"),
                ])
            values = self.ev._discover_p2go_values_from_catalog(
                path, "P2GO", ("TUSINVC",))

        self.assertEqual(values["P2GO_DEFAULT_OBJECT"], "INVOICE")
        self.assertEqual(values["P2GO_SECONDARY_OBJECT"], "TUSINVC.ARCHIVE")
        self.assertEqual(values["P2GO_AMBIGUOUS_OBJECT"], "SHARED")
        self.assertEqual(values["P2GO_CROSS_FROM"], "P2GO.INVOICE")
        self.assertEqual(values["P2GO_CROSS_TO"], "TUSINVC.ARCHIVE")

    def test_qlog_seeding_joins_feedback_and_preserves_source_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            finance_path = root / "finance.json"
            p2go_path = root / "p2go.json"
            finance_path.write_text('{"cases":[]}\n', encoding="utf-8")
            p2go_path.write_text('{"cases":[]}\n', encoding="utf-8")
            qlog = root / "questions.jsonl"
            records = [
                {"type": "turn", "turn_id": "f1", "failed": True,
                 "flags": ["tool_error"], "source_database": "default",
                 "scope": {"source": "default", "business_unit": "US001",
                           "ledger": "ACTUALS"},
                 "question": "Finance failed question"},
                {"type": "turn", "turn_id": "p1", "failed": False,
                 "flags": [], "source_database": "p2go",
                 "scope": {"source": "p2go"},
                 "question": "P2Go thumbed-down question"},
                {"type": "feedback", "turn_id": "p1", "verdict": "bad"},
            ]
            qlog.write_text("\n".join(json.dumps(row) for row in records),
                            encoding="utf-8")
            pending_path = root / "pending.json"
            original_finance = finance_path.read_bytes()
            original_p2go = p2go_path.read_bytes()
            with mock.patch.object(self.ev, "FINANCE_CASES", finance_path), \
                    mock.patch.object(self.ev, "P2GO_CASES", p2go_path), \
                    mock.patch.object(self.ev, "EVAL_PENDING", pending_path):
                self.assertEqual(self.ev._seed_from_qlog(str(qlog)), 0)

            self.assertEqual(finance_path.read_bytes(), original_finance)
            self.assertEqual(p2go_path.read_bytes(), original_p2go)
            pending = json.loads(pending_path.read_text())
            finance = pending["finance"]
            p2go = pending["p2go"]
            self.assertEqual(finance[0]["scope"], {
                "source": "default", "business_unit": "US001",
                "ledger": "ACTUALS",
            })
            self.assertEqual(p2go[0]["scope"], {"source": "p2go"})
            self.assertIn("user_bad",
                          p2go[0]["expect"]["_observed_flags"])
            self.assertEqual(pending_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
