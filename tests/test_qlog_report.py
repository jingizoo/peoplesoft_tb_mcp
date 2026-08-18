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
