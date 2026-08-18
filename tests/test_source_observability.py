"""Secret-free, source-separated observability for Finance and P2Go."""
from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path

from pstb import qlog_report
from pstb.client.chat import agent_turn
from pstb.client.llm_base import LLMResponse
from pstb.qlog import QuestionLog, observe_tool_call


class SourceObservabilityTests(unittest.TestCase):
    def test_diagnostics_renders_finance_scope_and_schema_boundary(self):
        html = (Path(__file__).resolve().parents[1] / "pstb" / "gui" /
                "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const schemaBoundary=", html)
        self.assertIn("const financeScopes=", html)
        self.assertIn(
            "[schemaBoundary,financeScopes].filter(Boolean).join(' · ')",
            html,
        )

    def test_tool_envelope_keeps_structure_and_drops_payload_content(self):
        output = json.dumps({
            "source_database": "p2go",
            "rows": [{"password": "SUPERSECRET", "amount": 998877.66}],
            "sql_executed": (
                "SELECT * FROM TUSINVC.ARCHIVE_INVOICE "
                "WHERE TOKEN='DO_NOT_LOG'"),
            "binds": {"token": "BIND_SECRET"},
            "target_owners": ["P2GO", "TUSINVC"],
            "truncated": False,
            "snapshot": {
                "id": "a" * 64,
                "source_fingerprint": "b" * 64,
                "schema_version": 2,
                "built_at": "2026-08-18T10:00:00Z",
                "status": "complete",
                "stale": False,
                "partial": False,
                "schema_coverage": {
                    "default": "P2GO",
                    "configured": ["P2GO", "TUSINVC", "UNCONFIGURED"],
                    "object_counts": {
                        "P2GO": 120, "TUSINVC": 30, "UNCONFIGURED": 999,
                    },
                    "missing": [],
                    "complete": True,
                },
                "latest_build": {
                    "build_run_id": "c" * 32,
                    "attempted_at": "2026-08-18T11:00:00Z",
                    "published": False,
                    "status": "failed",
                    "snapshot_id": "d" * 20,
                    "previous_snapshot_id": "a" * 64,
                    "failure_category": "metadata_unavailable",
                    "schema_coverage": {
                        "default": "P2GO",
                        "configured": ["P2GO", "TUSINVC"],
                        "object_counts": {"P2GO": 0, "TUSINVC": 0},
                        "missing": ["P2GO", "TUSINVC"],
                        "complete": False,
                    },
                    "error": "LATEST_BUILD_SECRET",
                },
            },
        })
        observed = observe_tool_call(
            tool="run_sql", output=output, ms=47, ok=True,
            expected_source="p2go",
            allowed_schemas=["P2GO", "TUSINVC"],
        )

        self.assertEqual(observed["result_source"], "p2go")
        self.assertTrue(observed["result_source_verified"])
        self.assertEqual(observed["target_owners"], ["P2GO", "TUSINVC"])
        self.assertEqual(observed["ms"], 47)
        self.assertEqual(
            observed["result_completeness"]["status"], "unknown",
            "generic truncated=false must not become a completeness claim",
        )
        self.assertEqual(observed["catalog"]["fingerprint"], "b" * 64)
        coverage = observed["catalog"]["schema_coverage"]
        self.assertEqual(coverage["schema_allowlist"], ["P2GO", "TUSINVC"])
        self.assertEqual(
            coverage["object_counts"], {"P2GO": 120, "TUSINVC": 30})
        self.assertTrue(coverage["complete"])
        latest = observed["catalog"]["latest_build"]
        self.assertFalse(latest["published"])
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["failure_category"], "metadata_unavailable")
        self.assertFalse(latest["schema_coverage"]["complete"])
        self.assertEqual(
            latest["schema_coverage"]["missing_schemas"],
            ["P2GO", "TUSINVC"],
        )
        serialized = json.dumps(observed)
        for forbidden in ("SUPERSECRET", "998877", "SELECT", "DO_NOT_LOG",
                          "BIND_SECRET", "ARCHIVE_INVOICE", "UNCONFIGURED",
                          "LATEST_BUILD_SECRET",
                          "rows", "sql_executed", "binds"):
            self.assertNotIn(forbidden, serialized)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)
            log.log_turn(
                surface="gui", provider="test", question="safe",
                calls=[observed], rounds=1, answer="ok",
                scope={"source": "p2go"},
                source_context={"default_schema": "P2GO",
                                "schema_allowlist": ["P2GO", "TUSINVC"]},
            )
            persisted = json.loads(
                (root / "questions.jsonl").read_text().splitlines()[0])
            stored_catalog = persisted["tools"][0]["catalog"]
            self.assertEqual(stored_catalog["latest_build"]["status"],
                             "failed")
            self.assertFalse(
                stored_catalog["latest_build"]["published"])
            self.assertNotIn("LATEST_BUILD_SECRET", json.dumps(persisted))

    def test_refusal_is_categorized_without_raw_error_or_object_name(self):
        detail = (
            "Schema OTHER is outside the selected source. Allowed schemas: "
            "P2GO, TUSINVC. SELECT * FROM OTHER.SECRET WHERE K=:secret")
        observed = observe_tool_call(
            tool="explain_query",
            output=json.dumps({"error": detail}),
            ms=1,
            ok=False,
            problem=detail,
            expected_source="p2go",
            allowed_schemas=["P2GO", "TUSINVC"],
        )

        self.assertEqual(observed["refusal_category"], "schema_boundary")
        self.assertEqual(
            observed["result_completeness"]["status"], "refused")
        serialized = json.dumps(observed)
        self.assertNotIn("OTHER", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("error", observed)

    def test_untrusted_result_source_is_not_persisted_as_canonical(self):
        observed = observe_tool_call(
            tool="run_sql",
            output=json.dumps({"source_database": "attacker-label",
                               "truncated": False,
                               "snapshot": {"id": "a" * 20,
                                            "status": "complete"},
                               "target_owners": ["P2GO"]}),
            ms=2,
            ok=True,
            expected_source="p2go",
            allowed_schemas=["P2GO"],
        )
        self.assertNotIn("result_source", observed)
        self.assertFalse(observed["result_source_verified"])
        self.assertNotIn("catalog", observed)
        self.assertNotIn("target_owners", observed)
        self.assertNotIn("result_completeness", observed)

    def test_native_relationship_basis_is_reduced_to_evidence_classes(self):
        observed = observe_tool_call(
            tool="join_path",
            output=json.dumps({
                "source_database": "p2go",
                "found": True,
                "from": {"name": "DO_NOT_LOG_A"},
                "to": {"name": "DO_NOT_LOG_B"},
                "hops": [
                    {"relationship": "foreign_key",
                     "confidence": "confirmed",
                     "constraint": "FK_DO_NOT_LOG"},
                    {"relationship": "view_dependency",
                     "confidence": "confirmed"},
                ],
                "graph_truncated": False,
            }),
            ms=12,
            ok=True,
            expected_source="p2go",
        )

        relation = observed["relationship_path"]
        self.assertTrue(relation["found"])
        self.assertEqual(
            relation["evidence_class"],
            ["foreign_key", "view_dependency"],
        )
        self.assertEqual(relation["confidence"], ["confirmed"])
        self.assertNotIn("DO_NOT_LOG", json.dumps(observed))

    def test_not_found_relationship_does_not_claim_observed_evidence(self):
        observed = observe_tool_call(
            tool="join_path",
            output=json.dumps({
                "source_database": "p2go", "found": False,
                "relationship_evidence_classes": [
                    "foreign_key", "view_dependency"],
            }),
            ms=4, ok=True, expected_source="p2go",
        )
        self.assertEqual(observed["relationship_path"], {"found": False})

    def test_persisted_turns_keep_finance_and_p2go_boundaries_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)
            finance = observe_tool_call(
                tool="get_trial_balance",
                output=json.dumps({
                    "source_database": "default",
                    "evidence_completeness": {"complete": True},
                }),
                ms=31,
                ok=True,
                expected_source="default",
            )
            p2go = observe_tool_call(
                tool="run_sql",
                output=json.dumps({
                    "source_database": "p2go",
                    "target_owners": ["P2GO", "TUSINVC"],
                    "truncated": False,
                }),
                ms=42,
                ok=True,
                expected_source="p2go",
                allowed_schemas=["P2GO", "TUSINVC"],
            )
            # Persistence re-selects the safe envelope even if an integration
            # accidentally hands it the richer live observer shape.
            p2go["args"] = {"sql": "SELECT PRIVATE_TABLE",
                             "token": "ARG_SECRET"}
            p2go["error"] = "RAW_ERROR_SECRET"
            log.log_turn(
                surface="gui", provider="test", question="finance request",
                calls=[finance], rounds=1, answer="ok",
                scope={"source": "default", "business_unit": "US001",
                       "ledger": "ACTUALS", "fiscal_year": 2026,
                       "period": 6},
                source_context={"default_schema": "SYSADM",
                                "schema_allowlist": ["SYSADM"]},
            )
            log.log_turn(
                surface="gui", provider="test", question="p2go request",
                calls=[p2go], rounds=1, answer="ok",
                scope={"source": "p2go"},
                source_context={"default_schema": "P2GO",
                                "schema_allowlist": ["P2GO", "TUSINVC"]},
            )

            report = qlog_report.analyze(root / "questions.jsonl")
            report_json = json.dumps(report)
            self.assertNotIn("finance request", report_json)
            self.assertNotIn("p2go request", report_json)
            self.assertEqual(set(report["sources"]), {"default", "p2go"})
            finance_summary = report["sources"]["default"]
            self.assertEqual(finance_summary["scopes"], [{
                "business_unit": "US001", "ledger": "ACTUALS",
                "fiscal_year": 2026, "period": 6,
            }])
            self.assertEqual(
                finance_summary["tools"][0]["completeness"],
                {"complete": 1},
            )
            p2go_summary = report["sources"]["p2go"]
            self.assertEqual(
                p2go_summary["source_context"]["schema_allowlist"],
                ["P2GO", "TUSINVC"],
            )
            self.assertEqual(
                p2go_summary["tools"][0]["target_owners"],
                ["P2GO", "TUSINVC"],
            )

            persisted = (root / "questions.jsonl").read_text()
            self.assertNotIn("error", persisted)
            self.assertNotIn("sql_executed", persisted)
            self.assertNotIn("ARG_SECRET", persisted)
            self.assertNotIn("RAW_ERROR_SECRET", persisted)
            self.assertNotIn("PRIVATE_TABLE", persisted)
            mode = stat.S_IMODE((root / "questions.jsonl").stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_curated_finance_source_uses_the_primary_tool_registry_basis(self):
        observed = observe_tool_call(
            tool="tb_integrity_check",
            output=json.dumps({"control_status": "passed"}),
            ms=8, ok=True, expected_source="default",
        )
        self.assertEqual(observed["result_source"], "default")
        self.assertTrue(observed["result_source_verified"])
        self.assertEqual(
            observed["result_source_basis"], "primary_tool_registry")
        self.assertEqual(observed["result_completeness"], {
            "control_status": "passed", "complete": True,
            "status": "complete",
        })

        incomplete = observe_tool_call(
            tool="tb_integrity_check",
            output=json.dumps({"control_status": "checks_incomplete"}),
            ms=9, ok=True, expected_source="default",
        )
        self.assertEqual(
            incomplete["result_completeness"]["status"], "incomplete")

    def test_log_rotation_bounds_local_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog(
                "questions.jsonl", root, max_bytes=1024, backups=2)
            for index in range(30):
                log.log_turn(
                    surface="gui", provider="test",
                    question=f"question {index} " + ("x" * 200),
                    calls=[], rounds=1, answer="ok",
                    scope={"source": "default"},
                )
            self.assertTrue((root / "questions.jsonl.1").exists())
            self.assertTrue((root / "questions.jsonl.2").exists())
            self.assertFalse((root / "questions.jsonl.3").exists())
            for path in root.glob("questions.jsonl*"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_logs_are_hardened_and_symlinks_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "questions.jsonl"
            backup = root / "questions.jsonl.1"
            active.write_text("", encoding="utf-8")
            backup.write_text("", encoding="utf-8")
            active.chmod(0o644)
            backup.chmod(0o644)
            QuestionLog("questions.jsonl", root)
            self.assertEqual(stat.S_IMODE(active.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            victim.chmod(0o644)
            active.unlink()
            active.symlink_to(victim)
            log = QuestionLog(
                "questions.jsonl", root, max_bytes=1024, backups=2)
            log.log_turn(
                surface="gui", provider="test", question="x" * 2000,
                calls=[], rounds=1, answer="ok",
                scope={"source": "default"},
            )
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
            self.assertTrue(active.is_symlink())

    def test_rotation_unlinks_backup_symlink_without_chmodding_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "questions.jsonl"
            active.write_text("x" * 1100, encoding="utf-8")
            active.chmod(0o600)
            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            victim.chmod(0o644)
            backup = root / "questions.jsonl.1"
            backup.symlink_to(victim)

            log = QuestionLog(
                "questions.jsonl", root, max_bytes=1024, backups=2)
            log.log_turn(
                surface="gui", provider="test", question="rotate",
                calls=[], rounds=1, answer="ok",
                scope={"source": "default"},
            )

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
            self.assertFalse(backup.is_symlink())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_questions_and_feedback_redact_credentials_locators_and_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)
            turn_id = log.log_turn(
                surface="gui", provider="test",
                question=("password=hunter2 DSN=FINANCE token=abc "
                          "SELECT * FROM P2GO.SECRET_TABLE"),
                calls=[], rounds=1, answer="ok",
                scope={"source": "p2go"},
            )
            self.assertTrue(log.has_turn(turn_id))
            log.log_feedback(
                turn_id, "bad", "Bearer abc.def service_name=nptg01pas")
            text = (root / "questions.jsonl").read_text()
            for secret in ("hunter2", "FINANCE", "abc.def", "nptg01pas",
                           "SECRET_TABLE"):
                self.assertNotIn(secret, text)
            self.assertIn("[SQL REDACTED]", text)
            self.assertIn("[REDACTED]", text)

    def test_every_sql_statement_family_is_redacted_from_local_text(self):
        samples = (
            "UPDATE TUSINVC.PRIVATE_EMP SET SALARY=999999 WHERE SSN='x'",
            "DELETE FROM P2GO.PRIVATE_EMP WHERE SSN='x'",
            "INSERT INTO P2GO.PRIVATE_EMP VALUES ('x')",
            "MERGE INTO P2GO.PRIVATE_EMP USING X ON (1=1)",
            "CREATE TABLE P2GO.PRIVATE_COPY (SSN VARCHAR2(20))",
            "SELECT 'literal-secret'",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)
            for statement in samples:
                log.log_turn(
                    surface="gui", provider="test", question=statement,
                    calls=[], rounds=1, answer="ok",
                    scope={"source": "p2go"},
                )
            text = (root / "questions.jsonl").read_text()
            self.assertEqual(text.count("[SQL REDACTED]"), len(samples))
            for private in ("PRIVATE_EMP", "SALARY", "SSN",
                            "literal-secret"):
                self.assertNotIn(private, text)

    def test_observability_failure_cannot_break_a_completed_answer(self):
        class Provider:
            name = "test"

            @staticmethod
            def send_user(_text):
                return LLMResponse(text="The answer completed.")

        class BrokenLog:
            @staticmethod
            def log_turn(**_kwargs):
                raise ValueError("telemetry failed")

        answer = asyncio.run(agent_turn(
            Provider(), object(), "hello", qlog=BrokenLog()))
        self.assertEqual(answer, "The answer completed.")

    def test_unbounded_catalog_version_is_dropped_without_conversion(self):
        observed = observe_tool_call(
            tool="describe_metadata_catalog",
            output=json.dumps({
                "source_database": "p2go",
                "snapshot": {"schema_version": "9" * 5000},
            }),
            ms=1, ok=True, expected_source="p2go",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)
            log.log_turn(
                surface="gui", provider="test", question="catalog",
                calls=[observed], rounds=1, answer="ok",
                scope={"source": "p2go"},
            )
            persisted = json.loads(
                (root / "questions.jsonl").read_text().splitlines()[0])
            self.assertNotIn(
                "version", persisted["tools"][0].get("catalog", {}))

    def test_summary_never_exports_the_protected_local_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)
            log.log_turn(
                surface="gui", provider="test",
                question="PRIVATE_PROMPT_SECRET customer detail",
                calls=[{"tool": "run_sql", "ok": False,
                        "refusal_category": "schema_boundary",
                        "result_completeness": {"status": "refused"}}],
                rounds=1,
                answer="unable to answer",
                scope={"source": "p2go"},
                source_context={"default_schema": "P2GO",
                                "schema_allowlist": ["P2GO"]},
            )
            path = root / "questions.jsonl"
            self.assertIn("PRIVATE_PROMPT_SECRET", path.read_text())
            report = qlog_report.analyze(path)
            self.assertNotIn("PRIVATE_PROMPT_SECRET", json.dumps(report))
            self.assertNotIn(
                "PRIVATE_PROMPT_SECRET", qlog_report.report_text(report))


if __name__ == "__main__":
    unittest.main()


class RedactionShapeTests(unittest.TestCase):
    """Credentials arrive shaped like env vars, not like prose.

    The first cut anchored every keyword with ``\\b``. Underscore is a word
    character, so there is no boundary between the "_" and the "A" of
    COUPA_API_KEY — the pattern matched "password=" in a sentence and missed
    COUPA_API_KEY=, GOOGLE_API_KEY=, ANTHROPIC_API_KEY= and client_secret:,
    which is most of what a person actually pastes. The question and feedback
    streams are written to logs/questions.jsonl verbatim otherwise.
    """

    SECRETS = (
        ("oracle dsn", "oracle+cx://SYSADM:Hunter2!@npp_db01:1521/FSPRD",
         "Hunter2!"),
        ("prose password", "connect password=Hunter2! to db", "Hunter2!"),
        ("pwd short form", "PWD=s3cr3t;UID=SYSADM", "s3cr3t"),
        ("bearer", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc",
         "eyJhbGciOiJIUzI1NiJ9"),
        ("vendor api key", "COUPA_API_KEY=ab12cd34ef56", "ab12cd34ef56"),
        ("client secret", "client_secret: GOCSPX-abcdefghijk",
         "GOCSPX-abcdefghijk"),
        ("google key", "GOOGLE_API_KEY=AIzaSyD-1234567890abcdefg",
         "AIzaSyD-1234567890abcdefg"),
        ("anthropic key", "ANTHROPIC_API_KEY=sk-ant-api03-AAAABBBB",
         "sk-ant-api03-AAAABBBB"),
        ("jdbc thin", "jdbc:oracle:thin:sysadm/Hunter2!@//npp_db01:1521/FSPRD",
         "Hunter2!"),
    )

    def test_no_secret_value_survives(self):
        from pstb.qlog import redact_private_text
        for label, raw, secret in self.SECRETS:
            with self.subTest(label=label):
                out = redact_private_text(raw, limit=400)
                self.assertNotIn(secret, out,
                                 f"{label} leaked into the question log")
                self.assertIn("REDACTED", out)

    def test_an_ordinary_question_is_left_alone(self):
        """Over-redaction would gut the learning loop this log exists for."""
        from pstb.qlog import redact_private_text
        for question in (
            "what is the trial balance for US001 period 6?",
            "which journals still need action before close?",
            "show me received not invoiced for US001",
            "why is cash up versus last year end?",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    redact_private_text(question, limit=400), question)
