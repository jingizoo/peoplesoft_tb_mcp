"""The learning loop must be deterministic and traceable: same log, same
report, every suggestion reproducible from countable facts in the records."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import qlog_report  # noqa: E402
from pstb.security import Access  # noqa: E402


def _turn(q, *, tools=(), flags=(), failed=None, ts="2026-08-01T10:00:00",
          tid="t1", source="default"):
    return json.dumps({
        "type": "turn", "turn_id": tid, "ts": ts, "surface": "gui",
        "provider": "x", "question": q, "source_database": source,
        "scope": {"source": source},
        "tools": list(tools), "rounds": 1, "answer_chars": 100,
        "failed": bool(flags) if failed is None else failed,
        "flags": list(flags)})


def _quality(tid, status, *, reasons=(), counts=None,
             ts="2026-08-01T10:00:10",
             basis="runtime_evidence_guards_v1"):
    record = {
        "type": "quality", "turn_id": tid, "ts": ts,
        "groundedness": {
            "status": status,
            "reason_codes": list(reasons),
            "counts": counts or {},
        },
    }
    if basis is not None:
        record["basis"] = basis
    return json.dumps(record)


def _feedback(tid, verdict, *, categories=(), note="",
              ts="2026-08-01T10:01:00"):
    return json.dumps({
        "type": "feedback", "turn_id": tid, "ts": ts,
        "verdict": verdict, "categories": list(categories), "note": note,
    })


def _review(tid, status, *, ts="2026-08-01T10:02:00", **extra):
    return json.dumps({
        "type": "review", "turn_id": tid, "ts": ts,
        "status": status, **extra,
    })


class ReportTests(unittest.TestCase):
    def _analyze(self, lines):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as f:
            f.write("\n".join(lines))
        return qlog_report.analyze(f.name)

    def test_error_rate_rule_fires_only_past_the_floor(self) -> None:
        bad = {"tool": "get_x", "ok": False, "ms": 5, "error": "boom"}
        ok = {"tool": "get_x", "ok": True, "ms": 5}
        r = self._analyze([
            _turn("q1", tools=[bad], flags=["tool_error"], tid="a"),
            _turn("q2", tools=[bad], flags=["tool_error"], tid="b"),
            _turn("q3", tools=[ok], tid="c"),
        ])
        self.assertTrue(any("get_x" in s and "calls failed" in s
                            for s in r["suggestions"]))
        r2 = self._analyze([_turn("q1", tools=[bad],
                                  flags=["tool_error"], tid="a")])
        self.assertFalse(any("calls failed" in s for s in r2["suggestions"]),
                         "one failure of one call must not page anyone")

    def test_slow_tool_becomes_an_index_candidate(self) -> None:
        slow = {"tool": "run_sql", "ok": True, "ms": 95_000}
        r = self._analyze([_turn("top 20", tools=[slow], tid="a")])
        self.assertTrue(any("index or partition candidate" in s
                            for s in r["suggestions"]))

    def test_failure_rates_are_never_combined_across_sources(self) -> None:
        bad = {"tool": "run_sql", "ok": False, "ms": 5,
               "refusal_category": "schema_boundary"}
        ok = {"tool": "run_sql", "ok": True, "ms": 5}
        # Four calls would cross the global minimum, but neither source has
        # enough observations to justify its own rate.
        r = self._analyze([
            _turn("finance 1", tools=[bad], tid="f1", source="default"),
            _turn("finance 2", tools=[ok], tid="f2", source="default"),
            _turn("p2go 1", tools=[bad], tid="p1", source="p2go"),
            _turn("p2go 2", tools=[ok], tid="p2", source="p2go"),
        ])
        self.assertFalse(any("calls failed" in s for s in r["suggestions"]))
        self.assertEqual(set(r["sources"]), {"default", "p2go"})

        r2 = self._analyze([
            _turn("finance", tools=[ok], tid="f", source="default"),
            _turn("p2go 1", tools=[bad], tid="p1", source="p2go"),
            _turn("p2go 2", tools=[bad], tid="p2", source="p2go"),
            _turn("p2go 3", tools=[ok], tid="p3", source="p2go"),
        ])
        suggestions = [s for s in r2["suggestions"] if "calls failed" in s]
        self.assertEqual(len(suggestions), 1)
        self.assertIn("[p2go]", suggestions[0])
        self.assertNotIn("[default]", suggestions[0])

    def test_repeated_failed_question_clusters_across_variants(self) -> None:
        rows = [
            _turn("top 20 customers for US001", flags=["gave_up"], tid="a"),
            _turn("top 25 customers for EU001", flags=["gave_up"], tid="b"),
            _turn("top 20 customers for US002", flags=["gave_up"], tid="c"),
        ]
        r = self._analyze(rows)
        self.assertTrue(any("asked 3x and failed" in s
                            for s in r["suggestions"]),
                        f"variants did not cluster: {r['suggestions']}")

    def test_thumbs_down_counts_as_failed(self) -> None:
        fb = json.dumps({"type": "feedback", "turn_id": "a",
                         "ts": "2026-08-01T10:01:00", "verdict": "bad"})
        r = self._analyze([_turn("fine answer, wrong number", tid="a"), fb])
        self.assertEqual(r["failed"], 1)
        self.assertEqual(r["flags"].get("user_bad"), 1)

    def test_missing_file_and_torn_lines_are_calm(self) -> None:
        r = qlog_report.analyze("/nonexistent/questions.jsonl")
        self.assertEqual(r["turns"], 0)
        self.assertIn("nothing actionable", qlog_report.report_text(r))
        r2 = self._analyze(['{"type": "turn", "tor',  # torn write
                            "[]",  # valid JSON, wrong record shape
                            _turn("ok question", tid="a")])
        self.assertEqual(r2["turns"], 1)

    def test_report_text_orders_actions(self) -> None:
        slow = {"tool": "run_sql", "ok": True, "ms": 95_000}
        text = qlog_report.report_text(
            self._analyze([_turn("q", tools=[slow], tid="a")]))
        self.assertIn("What to do next, in order:", text)
        self.assertIn("1. ", text)

    def test_cli_text_includes_source_scope_tool_and_health_facts(self):
        tool = {
            "tool": "join_path", "ok": True, "ms": 123,
            "result_source_verified": True,
            "result_completeness": {"status": "complete"},
            "relationship_path": {"found": True},
            "catalog": {"status": "complete", "snapshot_id": "a" * 20,
                        "latest_build": {"status": "complete",
                                         "published": True}},
        }
        record = json.loads(_turn("q", tools=[tool], tid="a",
                                  source="p2go"))
        record["source_context"] = {
            "canonical_source": "p2go", "default_schema": "P2GO",
            "schema_allowlist": ["P2GO", "TUSINVC"],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as handle:
            handle.write(json.dumps(record))
            path = handle.name
        text = qlog_report.report_text(qlog_report.analyze(path))
        self.assertIn("schema boundary: P2GO, TUSINVC", text)
        self.assertIn("catalog: complete", text)
        self.assertIn("relationships: 1 found", text)
        self.assertIn("tool join_path", text)
        self.assertIn("max=123ms", text)
        self.assertIn("verified=1", text)

    def test_quality_rates_and_samples_are_never_blended_across_sources(self):
        lines = [
            _turn("finance pass", tid="f1", source="default",
                  ts="2026-08-01T10:00:00"),
            _quality("f1", "passed", counts={
                "evidence_calls": 2, "successful_evidence_calls": 2}),
            _feedback("f1", "good"),
            _turn("finance blocked", tid="f2", source="default",
                  ts="2026-08-02T10:00:00"),
            _quality("f2", "blocked", reasons=["ungrounded_figure"],
                     counts={"unsupported_figure_count": 1}),
            _feedback("f2", "bad",
                      categories=["not_relevant", "wrong_number"]),
            _review("f2", "triaged"),
            _turn("p2go unknown", tid="p1", source="p2go",
                  ts="2026-08-01T11:00:00"),
            _quality("p1", "unknown", reasons=["no_evidence"]),
            _review("p1", "open"),
            _turn("p2go n/a", tid="p2", source="p2go",
                  ts="2026-08-02T11:00:00"),
            _quality("p2", "not_applicable"),
            _feedback("p2", "good"),
        ]
        report = self._analyze(lines)
        self.assertNotIn("quality", report,
                         "a top-level rate would blend source populations")

        finance = report["sources"]["default"]["quality"]
        grounded = finance["groundedness"]
        self.assertEqual((grounded["assessed"], grounded["passed"],
                          grounded["blocked"]), (2, 1, 1))
        self.assertEqual(grounded["records"], 2)
        self.assertEqual(grounded["unscored"], 0)
        self.assertEqual(grounded["coverage_rate"], 1.0)
        self.assertEqual(grounded["pass_rate"], 0.5)
        self.assertTrue(grounded["sample_warning"])
        self.assertEqual(grounded["reason_counts"], {
            "ungrounded_figure": 1})
        self.assertEqual(grounded["counts"]["evidence_calls"], 2)
        self.assertEqual(grounded["counts"]["unsupported_figure_count"], 1)
        self.assertEqual(finance["feedback"]["helpfulness"], 0.5)
        self.assertEqual(finance["feedback"]["category_counts"], {
            "not_relevant": 1, "wrong_number": 1})
        self.assertEqual(finance["user_rated_relevance"], {
            "assessed": 2, "relevant": 1, "not_relevant": 1,
            "rate": 0.5, "sample_warning": True,
        })
        self.assertEqual(finance["review_status_counts"], {"triaged": 1})

        p2go = report["sources"]["p2go"]["quality"]
        self.assertEqual(p2go["groundedness"]["assessed"], 0)
        self.assertEqual(p2go["groundedness"]["unknown"], 1)
        self.assertEqual(p2go["groundedness"]["not_applicable"], 1)
        self.assertIsNone(p2go["groundedness"]["pass_rate"])
        self.assertEqual(p2go["user_rated_relevance"]["rate"], 1.0)
        self.assertEqual(p2go["review_status_counts"], {"open": 1})

        queue = report["review_queue"]
        self.assertEqual({row["turn_id"] for row in queue}, {"f2", "p1"})
        self.assertEqual({row["source_database"] for row in queue},
                         {"default", "p2go"})
        self.assertEqual(
            finance["trends"][0]["groundedness_rate"], 1.0)
        self.assertEqual(
            finance["trends"][1]["groundedness_rate"], 0.0)

    def test_bad_feedback_for_other_reasons_is_not_called_irrelevant(self):
        report = self._analyze([
            _turn("slow but relevant", tid="a"),
            _feedback("a", "bad", categories=["too_slow"]),
            _turn("helpful", tid="b"),
            _feedback("b", "good"),
        ])
        quality = report["sources"]["default"]["quality"]
        self.assertEqual(quality["feedback"]["responses"], 2)
        self.assertEqual(quality["feedback"]["helpfulness"], 0.5)
        self.assertEqual(quality["user_rated_relevance"]["assessed"], 1)
        self.assertEqual(quality["user_rated_relevance"]["relevant"], 1)
        self.assertEqual(
            quality["user_rated_relevance"]["not_relevant"], 0)

    def test_unknown_mechanical_status_is_not_implicitly_a_failure_queue(self):
        report = self._analyze([
            _turn("policy prose", tid="a"),
            _quality("a", "unknown", reasons=["no_evidence"]),
        ])
        quality = report["sources"]["default"]["quality"]
        self.assertEqual(quality["groundedness"]["unknown"], 1)
        self.assertEqual(quality["review_status_counts"], {})
        self.assertEqual(report["review_queue"], [])

    def test_only_audited_mechanical_basis_counts_as_scored_coverage(self):
        report = self._analyze([
            _turn("valid", tid="valid"),
            _quality("valid", "passed"),
            _turn("legacy", tid="legacy"),
            _quality("legacy", "passed", basis=None),
            _turn("other rubric", tid="other"),
            _quality("other", "passed", basis="semantic_judge_v9"),
        ])
        grounded = report["sources"]["default"]["quality"]["groundedness"]
        self.assertEqual(grounded["records"], 1)
        self.assertEqual(grounded["unscored"], 2)
        self.assertEqual(grounded["coverage_rate"], 0.3333)
        self.assertEqual(grounded["basis_counts"], {
            "runtime_evidence_guards_v1": 1})
        self.assertEqual(
            report["sources"]["default"]["quality"]["trends"][0]
            ["groundedness_unscored"], 2)

    def test_rotations_load_oldest_to_newest_and_latest_records_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "questions.jsonl"
            turn = _turn("PRIVATE QUESTION", tid="same")
            # Rotation retains exactly .1-.3. A stale .4 must not reappear in
            # dashboard history after the configured retention window.
            (Path(str(path) + ".4")).write_text(
                _turn("expired", tid="expired"))
            (Path(str(path) + ".2")).write_text(
                "\n".join([turn, _quality("same", "blocked",
                                                   reasons=["tool_error"])]))
            # A copied turn at a rotation boundary is one turn, not two.
            (Path(str(path) + ".1")).write_text(
                "\n".join([turn, _quality("same", "passed"),
                           _feedback("same", "bad",
                                     categories=["not_relevant"])]))
            path.write_text("\n".join([
                turn, _feedback("same", "good"),
                _review("same", "verified"),
            ]))

            report = qlog_report.analyze(path)
            self.assertEqual(report["turns"], 1)
            quality = report["sources"]["default"]["quality"]
            self.assertEqual(quality["groundedness"]["passed"], 1)
            self.assertEqual(quality["groundedness"]["blocked"], 0)
            self.assertEqual(quality["feedback"]["good"], 1)
            self.assertEqual(quality["feedback"]["bad"], 0)
            self.assertEqual(quality["review_status_counts"],
                             {"verified": 1})
            self.assertEqual(report["review_queue"][0]["review_status"],
                             "verified")

    def test_active_review_work_sorts_first_and_queue_reports_truncation(self):
        lines = [
            _turn("old active", tid="active", ts="2026-07-01T00:00:00"),
            _feedback("active", "bad", categories=["not_relevant"]),
            _review("active", "open"),
        ]
        for index in range(101):
            tid = f"done{index}"
            lines.extend([
                _turn("done", tid=tid, ts="2026-08-01T10:00:00"),
                _review(tid, "verified"),
            ])
        report = self._analyze(lines)
        self.assertEqual(report["review_queue_total"], 102)
        self.assertTrue(report["review_queue_truncated"])
        self.assertEqual(len(report["review_queue"]), 100)
        self.assertEqual(report["review_queue"][0]["turn_id"], "active")
        self.assertEqual(report["review_queue"][0]["review_status"], "open")
        self.assertTrue(any("review item(s) remain active" in suggestion
                            for suggestion in report["suggestions"]))
        text = qlog_report.report_text(report)
        self.assertIn("review queue: showing 100 of 102 item(s) (truncated)",
                      text)
        self.assertNotIn("nothing actionable", text)

    def test_bad_feedback_without_explicit_review_is_actionable_open_work(self):
        report = self._analyze([
            _turn("bad", tid="bad"),
            _feedback("bad", "bad", categories=["incomplete"]),
        ])
        self.assertEqual(report["review_queue_total"], 1)
        self.assertFalse(report["review_queue_truncated"])
        self.assertEqual(report["review_queue"][0]["review_status"], "open")
        self.assertNotIn("nothing actionable", qlog_report.report_text(report))

    def test_single_failure_below_threshold_is_not_called_clean(self):
        report = self._analyze([
            _turn("one failure", tid="failed", failed=True,
                  flags=["tool_error"]),
            _quality("failed", "unknown", reasons=["tool_error"]),
        ])
        self.assertEqual(report["suggestions"], [])
        text = qlog_report.report_text(report)
        self.assertIn("below the repeat/tool-error suggestion thresholds", text)
        self.assertNotIn("nothing actionable", text)

    def test_queue_freshness_uses_latest_feedback_or_review_event(self):
        lines = [
            _turn("new complaint on old answer", tid="newcomplaint",
                  ts="2026-01-01T00:00:00"),
            _feedback("newcomplaint", "bad", categories=["incomplete"],
                      ts="2026-12-01T00:00:00"),
        ]
        for index in range(100):
            tid = f"older{index}"
            lines.extend([
                _turn("older complaint", tid=tid,
                      ts="2026-08-01T00:00:00"),
                _feedback(tid, "bad", categories=["incomplete"],
                          ts="2026-08-02T00:00:00"),
            ])
        report = self._analyze(lines)
        self.assertEqual(report["review_queue_total"], 101)
        self.assertEqual(report["review_queue_active_total"], 101)
        self.assertTrue(report["review_queue_truncated"])
        self.assertEqual(report["review_queue"][0]["turn_id"],
                         "newcomplaint")

    def test_terminal_review_history_is_not_reported_as_pending(self):
        report = self._analyze([
            _turn("fixed", tid="fixed"),
            _feedback("fixed", "bad", categories=["wrong_number"]),
            _review("fixed", "verified"),
        ])
        self.assertEqual(report["review_queue_total"], 1)
        self.assertEqual(report["review_queue_active_total"], 0)
        self.assertFalse(any("remain active" in item
                             for item in report["suggestions"]))

    def test_report_ignores_linked_and_out_of_retention_rotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "questions.jsonl"
            path.write_text(_turn("safe", tid="safe"), encoding="utf-8")
            victim = Path(tmp) / "victim.jsonl"
            victim.write_text(
                _turn("PRIVATE_LINKED_QUESTION", tid="linked"),
                encoding="utf-8",
            )
            Path(str(path) + ".1").symlink_to(victim)
            Path(str(path) + ".21").write_text(
                _turn("PRIVATE_EXPIRED_QUESTION", tid="expired"),
                encoding="utf-8",
            )

            report = qlog_report.analyze(path)
            self.assertEqual(report["turns"], 1)
            serialized = json.dumps(report)
            self.assertNotIn("linked", serialized)
            self.assertNotIn("expired", serialized)
            self.assertNotIn("PRIVATE", serialized)

    def test_quality_report_exposes_only_allowlisted_structural_fields(self):
        report = self._analyze([
            _turn("PRIVATE_PROMPT SELECT * FROM P2GO.SECRET", tid="safe",
                  tools=[{"tool": "run_sql", "ok": True}]),
            json.dumps({
                "type": "quality", "turn_id": "safe",
                "basis": "runtime_evidence_guards_v1",
                "answer": "PRIVATE_ANSWER", "rationale": "SECRET_OBJECT",
                "groundedness": {
                    "status": "blocked",
                    "reason_codes": ["source_mismatch", "PRIVATE_REASON"],
                    "counts": {"source_mismatch_count": 1,
                               "PRIVATE_COUNT": 999},
                },
            }),
            _feedback("safe", "bad", categories=["wrong_source", "PRIVATE"],
                      note="PRIVATE_FEEDBACK"),
            _review("safe", "open", note="PRIVATE_REVIEW"),
        ])
        serialized = json.dumps(report)
        for private in ("PRIVATE_PROMPT", "SECRET", "PRIVATE_ANSWER",
                        "PRIVATE_REASON", "PRIVATE_COUNT",
                        "PRIVATE_FEEDBACK", "PRIVATE_REVIEW"):
            self.assertNotIn(private, serialized)
        row = report["review_queue"][0]
        self.assertEqual(set(row), {
            "ts", "turn_id", "source_database", "groundedness",
            "grounding_reasons", "feedback", "feedback_categories",
            "review_status", "tools",
        })
        self.assertEqual(row["tools"], ["run_sql"])


class StaticDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "pstb" / "gui" / "static" /
                    "index.html").read_text(encoding="utf-8")

    def test_dashboard_keeps_source_quality_panels_and_denominators(self):
        self.assertIn("function renderQualitySource(s)", self.html)
        self.assertIn("source-specific; no cross-database average", self.html)
        self.assertIn("qualitySourceLabel(source)", self.html)
        self.assertIn("source==='default'?'Finance'", self.html)
        self.assertIn("qualityRate(g.passed,g.assessed)", self.html)
        self.assertIn("qualityRate(u.relevant,u.assessed)", self.html)
        self.assertIn("qualityRate(f.good,f.responses)", self.html)
        self.assertIn("q.user_rated_relevance", self.html)
        self.assertIn("User-rated relevance proxy", self.html)
        self.assertIn("g.unscored", self.html)
        self.assertIn("turns scored", self.html)
        self.assertIn("small sample", self.html)

    def test_dashboard_shows_reasons_trends_and_safe_review_queue(self):
        self.assertIn("Mechanical grounding reasons", self.html)
        self.assertIn("User feedback reasons", self.html)
        self.assertIn("Recent quality trend", self.html)
        self.assertIn("Answer review queue", self.html)
        self.assertIn("renderQualityQueue(rows,total,truncated)", self.html)
        self.assertIn("active work first", self.html)
        self.assertIn("r.review_queue_total", self.html)
        self.assertIn("r.review_queue_active_total", self.html)
        self.assertIn("r.review_queue_truncated", self.html)
        self.assertIn("below the repeat/tool-error suggestion thresholds",
                      self.html)
        self.assertIn("machine-local operator diagnostics", self.html)
        self.assertIn("safe local IDs and structural reasons only", self.html)
        self.assertIn("row.review_status", self.html)
        self.assertIn("class=\"review-status\"", self.html)
        self.assertIn("fetch('/api/question-review'", self.html)
        self.assertNotIn("row.question", self.html)
        self.assertNotIn("row.answer", self.html)
        self.assertNotIn("row.note", self.html)

    def test_dashboard_explains_protected_quality_report_access(self):
        self.assertIn("err.status=r.status", self.html)
        self.assertIn("e2.status===403", self.html)
        self.assertIn("operator access required", self.html)
        self.assertIn("Quality diagnostics are machine-local", self.html)
        self.assertIn("through an SSH tunnel", self.html)

    def test_chat_collects_positive_and_categorized_negative_feedback(self):
        self.assertIn("postAnswerFeedback(turnId,verdict,categories)",
                      self.html)
        self.assertIn("Mark this answer helpful", self.html)
        self.assertIn("Tell us what should improve", self.html)
        for category in ("not_relevant", "unsupported_claim", "wrong_number",
                         "wrong_source", "incomplete", "too_slow", "other"):
            self.assertIn("'" + category + "'", self.html)
        self.assertIn("Choose at least one reason.", self.html)
        self.assertNotIn("Optional detail (stored locally and redacted)",
                         self.html)
        self.assertNotIn("categories:categories||[],note:", self.html)


class EndpointTests(unittest.TestCase):
    def test_endpoint_returns_report_with_text(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
            r = client.get("/api/question-report")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("suggestions", body)
            if body.get("turns"):
                self.assertIn("text", body)

    def test_report_is_privileged_only_when_row_security_is_enabled(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        restricted = Access(
            oprid="FIN_US001", units=frozenset({"US001"}))
        privileged = Access(
            oprid="ADMIN", all_units=True, privileged=True)
        with mock.patch.object(gapp.cfg.security, "enabled", True), \
                mock.patch.object(gapp, "access_for_request",
                                  return_value=restricted), \
                TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                           client=("127.0.0.1", 50000)) as client:
            self.assertEqual(
                client.get("/api/question-report").status_code, 403)
        with mock.patch.object(gapp.cfg.security, "enabled", True), \
                mock.patch.object(gapp, "access_for_request",
                                  return_value=privileged), \
                TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                           client=("127.0.0.1", 50000)) as client:
            self.assertEqual(
                client.get("/api/question-report").status_code, 200)


if __name__ == "__main__":
    unittest.main()
