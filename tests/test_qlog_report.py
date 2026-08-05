"""The learning loop must be deterministic and traceable: same log, same
report, every suggestion reproducible from countable facts in the records."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import qlog_report  # noqa: E402


def _turn(q, *, tools=(), flags=(), failed=None, ts="2026-08-01T10:00:00",
          tid="t1"):
    return json.dumps({
        "type": "turn", "turn_id": tid, "ts": ts, "surface": "gui",
        "provider": "x", "question": q,
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
                            _turn("ok question", tid="a")])
        self.assertEqual(r2["turns"], 1)

    def test_report_text_orders_actions(self) -> None:
        slow = {"tool": "run_sql", "ok": True, "ms": 95_000}
        text = qlog_report.report_text(
            self._analyze([_turn("q", tools=[slow], tid="a")]))
        self.assertIn("What to do next, in order:", text)
        self.assertIn("1. ", text)


class EndpointTests(unittest.TestCase):
    def test_endpoint_returns_report_with_text(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app) as client:
            r = client.get("/api/question-report")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("suggestions", body)
            if body.get("turns"):
                self.assertIn("text", body)


if __name__ == "__main__":
    unittest.main()
