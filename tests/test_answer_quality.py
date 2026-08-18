"""Privacy-safe deterministic answer-quality collection."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from pstb.client.chat import agent_turn
from pstb.client.llm_base import LLMResponse, ToolCall
from pstb.qlog import FeedbackAlreadyRecorded, QuestionLog
from pstb.quality import (
    RUNTIME_GROUNDING_BASIS,
    runtime_groundedness,
)


class RuntimeGroundingTests(unittest.TestCase):
    def test_general_answer_is_not_a_vacuous_grounding_pass(self):
        result = runtime_groundedness(
            intent="general", evidence_calls=0,
            successful_evidence_calls=0, failed_evidence_calls=0)
        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["reason_codes"], [])

    def test_data_answer_with_successful_evidence_passes_narrow_runtime_basis(self):
        result = runtime_groundedness(
            intent="data", evidence_calls=1,
            successful_evidence_calls=1, failed_evidence_calls=0)
        self.assertEqual(result["status"], "passed")

    def test_policy_prose_remains_unknown_without_semantic_entailment(self):
        result = runtime_groundedness(
            intent="policy", evidence_calls=1,
            successful_evidence_calls=1, failed_evidence_calls=0)
        self.assertEqual(result["status"], "unknown")

    def test_hard_guard_failure_is_blocked_even_with_a_successful_call(self):
        result = runtime_groundedness(
            intent="data", evidence_calls=1,
            successful_evidence_calls=1, failed_evidence_calls=0,
            guard_blocked=True, unsupported_figures=["private-value"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["counts"]["unsupported_figure_count"], 1)
        self.assertIn("ungrounded_figure", result["reason_codes"])
        self.assertNotIn("private-value", json.dumps(result))

    def test_recovered_source_mismatch_is_visible_but_not_a_false_failure(self):
        result = runtime_groundedness(
            intent="data", evidence_calls=2,
            successful_evidence_calls=1, failed_evidence_calls=1,
            source_mismatch_count=1)
        self.assertEqual(result["status"], "passed")
        self.assertIn("source_mismatch", result["reason_codes"])

    def test_heuristic_misattribution_prevents_a_false_pass(self):
        result = runtime_groundedness(
            intent="data", evidence_calls=1,
            successful_evidence_calls=1, failed_evidence_calls=0,
            source_misattribution_count=1)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("source_misattribution", result["reason_codes"])


class QuestionLogQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = QuestionLog("questions.jsonl", self.root)
        self.turn_id = self.log.log_turn(
            surface="gui", provider="test", question="safe question",
            calls=[], rounds=0, answer="safe answer",
            scope={"source": "default"})

    def records(self):
        return [json.loads(line) for line in
                (self.root / "questions.jsonl").read_text().splitlines()]

    def test_quality_record_reselects_only_bounded_contract_fields(self):
        self.assertTrue(self.log.log_quality(self.turn_id, {
            "status": "passed",
            "reason_codes": [],
            "counts": {
                "evidence_calls": 2,
                "successful_evidence_calls": 2,
                "failed_evidence_calls": 0,
                "unsupported_figure_count": 0,
                "unverified_verdict_count": 0,
                "source_mismatch_count": 0,
                "source_misattribution_count": 0,
                "object_name": "P2GO.PRIVATE_TABLE",
            },
            "question": "PRIVATE QUESTION",
            "answer": "PRIVATE ANSWER",
            "payload": {"rows": [{"secret": "DO_NOT_LOG"}]},
            "sql": "SELECT * FROM P2GO.PRIVATE_TABLE",
        }))
        quality = self.records()[-1]
        self.assertEqual(quality["type"], "quality")
        self.assertEqual(quality["basis"], RUNTIME_GROUNDING_BASIS)
        self.assertEqual(quality["groundedness"]["status"], "passed")
        serialized = json.dumps(quality)
        for forbidden in ("PRIVATE", "DO_NOT_LOG", "SELECT", "rows",
                          "object_name", "question", "answer", "payload"):
            self.assertNotIn(forbidden, serialized)

    def test_invalid_quality_vocabularies_are_rejected(self):
        with self.assertRaises(ValueError):
            self.log.log_quality(self.turn_id, {
                "status": "excellent", "reason_codes": [], "counts": {}})
        with self.assertRaises(ValueError):
            self.log.log_quality(self.turn_id, {
                "status": "unknown", "reason_codes": ["raw database error"],
                "counts": {}})
        with self.assertRaises(ValueError):
            self.log.log_quality(
                self.turn_id, {"status": "unknown", "counts": {}},
                basis="some-other-rubric")

    def test_feedback_is_categorized_and_bad_feedback_opens_review(self):
        self.assertTrue(self.log.log_feedback(
            self.turn_id, "bad",
            categories=["wrong_number", "not_relevant", "wrong_number"]))
        records = self.records()
        feedback = records[-2]
        review = records[-1]
        self.assertEqual(feedback["verdict"], "bad")
        self.assertEqual(feedback["categories"],
                         ["not_relevant", "wrong_number"])
        self.assertNotIn("note", feedback)
        self.assertEqual(review["status"], "open")
        self.assertEqual(self.log.review_status(self.turn_id), "open")

    def test_feedback_and_review_enums_are_strict(self):
        with self.assertRaises(ValueError):
            self.log.log_feedback(self.turn_id, "maybe")
        with self.assertRaises(ValueError):
            self.log.log_feedback(
                self.turn_id, "bad", categories=["arbitrary private text"])
        with self.assertRaises(ValueError):
            self.log.log_feedback(
                self.turn_id, "good", categories=["not_relevant"])
        with self.assertRaises(ValueError):
            self.log.log_feedback(
                self.turn_id, "bad", categories=["other"] * 8)
        with self.assertRaises(ValueError):
            self.log.log_feedback(
                self.turn_id, "bad", "P2GO.SECRET_TABLE was wrong",
                categories=["other"])
        with self.assertRaises(ValueError):
            self.log.log_review(self.turn_id, "secret-table-name")

    def test_feedback_is_one_shot_and_review_state_only_moves_forward(self):
        self.assertTrue(self.log.log_feedback(self.turn_id, "good"))
        with self.assertRaises(FeedbackAlreadyRecorded):
            self.log.log_feedback(
                self.turn_id, "bad", categories=["not_relevant"])
        self.assertTrue(self.log.log_review(self.turn_id, "triaged"))
        # Idempotent retries do not append another record.
        before = len(self.records())
        self.assertTrue(self.log.log_review(self.turn_id, "triaged"))
        self.assertEqual(len(self.records()), before)
        with self.assertRaises(ValueError):
            self.log.log_review(self.turn_id, "open")
        self.assertTrue(self.log.log_review(self.turn_id, "verified"))
        with self.assertRaises(ValueError):
            self.log.log_review(self.turn_id, "dismissed")

    def test_relative_log_refuses_a_linked_parent_outside_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        linked_root = self.root / "app"
        linked_root.mkdir()
        (linked_root / "logs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            QuestionLog("logs/questions.jsonl", linked_root)
        self.assertFalse((outside / "questions.jsonl").exists())

    def test_log_parent_swap_after_construction_fails_closed(self):
        app_root = self.root / "swap-app"
        app_root.mkdir()
        outside = self.root / "swap-outside"
        outside.mkdir()
        log = QuestionLog("logs/questions.jsonl", app_root)
        log.path.parent.rmdir()
        log.path.parent.symlink_to(outside, target_is_directory=True)
        turn_id = log.log_turn(
            surface="gui", provider="test", question="safe",
            calls=[], rounds=0, answer="safe", scope={"source": "default"})
        self.assertFalse(log.has_turn(turn_id))
        self.assertFalse((outside / "questions.jsonl").exists())

    def test_dashboard_reader_refuses_parent_swap_after_construction(self):
        from pstb import qlog_report

        app_root = self.root / "report-swap-app"
        app_root.mkdir()
        outside = self.root / "report-swap-outside"
        outside.mkdir()
        log = QuestionLog("logs/questions.jsonl", app_root)
        self.assertTrue(log.log_turn(
            surface="gui", provider="test", question="safe",
            calls=[], rounds=0, answer="safe",
            scope={"source": "default"}))

        pinned = app_root / "original-logs"
        log.path.parent.rename(pinned)
        log.path.parent.symlink_to(outside, target_is_directory=True)
        malicious = {
            "type": "turn", "turn_id": "e" * 32,
            "ts": "2026-08-01T00:00:00+00:00", "surface": "gui",
            "provider": "test", "source_database": "default",
            "scope": {"source": "default"}, "question": "private",
            "tools": [], "rounds": 1, "answer_chars": 1,
            "failed": False, "flags": [],
        }
        (outside / "questions.jsonl").write_text(
            json.dumps(malicious) + "\n", encoding="utf-8")

        report = qlog_report.analyze(log)
        self.assertEqual(report["turns"], 0)
        self.assertNotIn("private", json.dumps(report))

    def test_disabled_or_failed_log_does_not_advertise_feedback_id(self):
        disabled = QuestionLog("", self.root)
        turn_id = disabled.log_turn(
            surface="gui", provider="test", question="safe",
            calls=[], rounds=0, answer="safe", scope={"source": "default"})
        self.assertEqual(turn_id, "")

    def test_feedback_refreshes_parent_turn_before_rotation_evicts_it(self):
        from pstb import qlog_report

        path = self.root / "retained.jsonl"
        target_id = "a" * 32
        current_id = "b" * 32
        target = {
            "type": "turn", "turn_id": target_id,
            "ts": "2026-08-01T00:00:00+00:00", "surface": "gui",
            "provider": "test", "source_database": "default",
            "scope": {"source": "default"}, "question": "target",
            "tools": [], "rounds": 1, "answer_chars": 1,
            "failed": False, "flags": [],
        }
        current = {**target, "turn_id": current_id,
                   "question": "x" * 750}
        path.with_name(path.name + ".1").write_text(
            json.dumps(target) + "\n", encoding="utf-8")
        path.write_text(json.dumps(current) + "\n", encoding="utf-8")
        log = QuestionLog(str(path), self.root, max_bytes=1024, backups=1)
        self.assertTrue(log.has_turn(target_id))
        self.assertTrue(log.log_feedback(
            target_id, "bad", categories=["not_relevant"]))
        report = qlog_report.analyze(path)
        self.assertTrue(log.has_turn(target_id))
        self.assertIn(target_id, {
            row["turn_id"] for row in report["review_queue"]})
        self.assertEqual(report["failed"], 1)

    def test_review_refresh_preserves_latest_quality_feedback_and_review(self):
        """A lifecycle update must not orphan sibling evidence at rotation."""
        from pstb import qlog_report

        path = self.root / "complete-bundle.jsonl"
        target_id = "c" * 32
        current_id = "d" * 32
        target = {
            "type": "turn", "turn_id": target_id,
            "ts": "2026-08-01T00:00:00+00:00", "surface": "gui",
            "provider": "test", "source_database": "default",
            "scope": {"source": "default"}, "question": "target",
            "tools": [], "rounds": 1, "answer_chars": 1,
            "failed": False, "flags": [],
        }
        quality = {
            "type": "quality", "turn_id": target_id,
            "ts": "2026-08-01T00:01:00+00:00",
            "basis": RUNTIME_GROUNDING_BASIS,
            "groundedness": {
                "status": "blocked",
                "reason_codes": ["ungrounded_figure"],
                "counts": {"unsupported_figure_count": 1},
            },
        }
        feedback = {
            "type": "feedback", "turn_id": target_id,
            "ts": "2026-08-01T00:02:00+00:00", "verdict": "bad",
            "categories": ["wrong_number"],
        }
        review = {
            "type": "review", "turn_id": target_id,
            "ts": "2026-08-01T00:03:00+00:00", "status": "open",
        }
        current = {
            **target, "turn_id": current_id,
            "ts": "2026-08-02T00:00:00+00:00", "question": "x" * 750,
        }
        path.with_name(path.name + ".1").write_text(
            "".join(json.dumps(row) + "\n" for row in (
                target, quality, feedback, review)),
            encoding="utf-8",
        )
        path.write_text(json.dumps(current) + "\n", encoding="utf-8")

        log = QuestionLog(str(path), self.root, max_bytes=1024, backups=1)
        self.assertTrue(log.log_review(target_id, "triaged"))

        report = qlog_report.analyze(path)
        finance = report["sources"]["default"]
        queue_row = next(
            row for row in report["review_queue"]
            if row["turn_id"] == target_id)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(
            finance["quality"]["groundedness"]["blocked"], 1)
        self.assertEqual(finance["quality"]["feedback"]["bad"], 1)
        self.assertEqual(queue_row["groundedness"], "blocked")
        self.assertEqual(queue_row["feedback"], "bad")
        self.assertEqual(queue_row["feedback_categories"], ["wrong_number"])
        self.assertEqual(queue_row["review_status"], "triaged")

    def test_latest_review_status_survives_reload(self):
        self.log.log_review(self.turn_id, "triaged")
        self.log.log_review(self.turn_id, "fixed")
        self.assertEqual(self.log.review_status(self.turn_id), "fixed")
        reloaded = QuestionLog("questions.jsonl", self.root)
        self.assertEqual(reloaded.review_status(self.turn_id), "fixed")


class ScriptedProvider:
    name = "test"

    def __init__(self, responses):
        self.responses = list(responses)

    def _next(self):
        if not self.responses:
            raise AssertionError("scripted provider ran out of responses")
        return self.responses.pop(0)

    def send_user(self, _text):
        return self._next()

    def send_tool_results(self, _results):
        return self._next()


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    async def call_tool(self, name, arguments):
        del name, arguments
        await asyncio.sleep(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(self.payload))],
            is_error=False)


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _data_turn(self, answer):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        qlog = QuestionLog("questions.jsonl", root)
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(
                id="c1", name="get_trial_balance", args={})]),
            LLMResponse(text=answer),
        ])
        payload = {
            "scope_status": "ok", "business_unit": "US001",
            "ledger": "ACTUALS", "fiscal_year": 2026, "period": 6,
            "ending_balance": 1234.56,
        }
        with mock.patch("pstb.client.chat.MAX_NUDGES", 0):
            result = await agent_turn(
                provider, FakeSession(payload), "What is the trial balance?",
                qlog=qlog, surface="gui",
                scope={"source": "default", "business_unit": "US001",
                       "ledger": "ACTUALS"})
        records = [json.loads(line) for line in
                   (root / "questions.jsonl").read_text().splitlines()]
        return result, records

    async def test_runtime_appends_passed_grounding_after_the_turn(self):
        answer, records = await self._data_turn(
            "The ending balance is 1,234.56.")
        self.assertEqual(answer, "The ending balance is 1,234.56.")
        self.assertEqual([record["type"] for record in records],
                         ["turn", "quality"])
        self.assertEqual(
            records[1]["groundedness"]["status"], "passed")

    async def test_runtime_logs_blocked_without_persisting_fabricated_value(self):
        answer, records = await self._data_turn(
            "The ending balance is 9,999.99.")
        self.assertIn("I withheld that answer", answer)
        quality = records[1]
        self.assertEqual(quality["groundedness"]["status"], "blocked")
        self.assertEqual(
            quality["groundedness"]["counts"]["unsupported_figure_count"], 1)
        self.assertNotIn("9,999.99", json.dumps(quality))

    async def test_quality_append_failure_cannot_break_answer_or_turn_id(self):
        class BrokenQualityLog:
            @staticmethod
            def log_turn(**_kwargs):
                return "a" * 32

            @staticmethod
            def log_quality(_turn_id, _groundedness):
                raise OSError("disk unavailable")

        meta = {}
        answer = await agent_turn(
            ScriptedProvider([LLMResponse(text="Completed answer.")]),
            object(), "hello", qlog=BrokenQualityLog(), turn_meta=meta)
        self.assertEqual(answer, "Completed answer.")
        self.assertEqual(meta["turn_id"], "a" * 32)


class ProtectedReviewApiTests(unittest.TestCase):
    def setUp(self):
        from pstb.gui import app as gapp

        self.gapp = gapp
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = QuestionLog("questions.jsonl", Path(self.tmp.name))
        self.turn_id = self.log.log_turn(
            surface="gui", provider="test", question="q", calls=[],
            rounds=0, answer="a", scope={"source": "default"})

    def test_feedback_api_rejects_invalid_verdict_and_categories(self):
        with mock.patch.object(self.gapp, "qlog", self.log):
            with self.assertRaises(HTTPException) as invalid_verdict:
                self.gapp.feedback({"turn_id": self.turn_id,
                                    "verdict": "maybe"})
            self.assertEqual(invalid_verdict.exception.status_code, 400)
            with self.assertRaises(HTTPException) as invalid_category:
                self.gapp.feedback({
                    "turn_id": self.turn_id, "verdict": "bad",
                    "categories": ["raw-private-reason"],
                })
            self.assertEqual(invalid_category.exception.status_code, 400)

    def test_review_api_requires_privilege_and_keeps_bounded_status(self):
        restricted = SimpleNamespace(privileged=False)
        privileged = SimpleNamespace(privileged=True)
        local_request = SimpleNamespace(
            scope={"client": ("127.0.0.1", 50000)})
        with mock.patch.object(self.gapp, "qlog", self.log), \
                mock.patch.object(self.gapp.cfg.security, "enabled", True), \
                mock.patch.object(self.gapp, "access_for_request",
                                  return_value=restricted):
            with self.assertRaises(HTTPException) as denied:
                self.gapp.update_question_review(
                    {"turn_id": self.turn_id, "status": "triaged"},
                    local_request)
            self.assertEqual(denied.exception.status_code, 403)
        with mock.patch.object(self.gapp, "qlog", self.log), \
                mock.patch.object(self.gapp.cfg.security, "enabled", True), \
                mock.patch.object(self.gapp, "access_for_request",
                                  return_value=privileged):
            result = self.gapp.update_question_review(
                {"turn_id": self.turn_id, "status": "triaged"},
                local_request)
            self.assertEqual(result["status"], "triaged")
            read = self.gapp.question_review(local_request, self.turn_id)
            self.assertEqual(read, {"turn_id": self.turn_id,
                                    "status": "triaged"})

    def test_review_api_is_machine_local_even_for_privileged_scope(self):
        privileged = SimpleNamespace(privileged=True)
        remote_request = SimpleNamespace(
            scope={"client": ("10.0.0.5", 50000)})
        with mock.patch.object(self.gapp, "qlog", self.log), \
                mock.patch.object(self.gapp.cfg.security, "enabled", True), \
                mock.patch.object(self.gapp, "access_for_request",
                                  return_value=privileged):
            with self.assertRaises(HTTPException) as denied:
                self.gapp.question_review(remote_request, self.turn_id)
        self.assertEqual(denied.exception.status_code, 403)

    def test_http_review_routes_validate_and_round_trip_status(self):
        from fastapi.testclient import TestClient

        with mock.patch.object(self.gapp, "qlog", self.log), \
                mock.patch.object(self.gapp.cfg.security, "enabled", False), \
                TestClient(
                    self.gapp.app, base_url="http://127.0.0.1:8000",
                    client=("127.0.0.1", 50000),
                ) as client:
            invalid = client.post("/api/question-review", json={
                "turn_id": self.turn_id, "status": "private free text"})
            self.assertEqual(invalid.status_code, 400)
            updated = client.post("/api/question-review", json={
                "turn_id": self.turn_id, "status": "verified"})
            self.assertEqual(updated.status_code, 200)
            read = client.get(
                "/api/question-review", params={"turn_id": self.turn_id})
            self.assertEqual(read.status_code, 200)
            self.assertEqual(read.json(), {
                "turn_id": self.turn_id, "status": "verified"})

    def test_feedback_duplicate_and_oversize_body_are_refused(self):
        from fastapi.testclient import TestClient

        with mock.patch.object(self.gapp, "qlog", self.log), \
                mock.patch.object(self.gapp.cfg.security, "enabled", False), \
                TestClient(
                    self.gapp.app, base_url="http://127.0.0.1:8000",
                    client=("127.0.0.1", 50000),
                ) as client:
            first = client.post("/api/feedback", json={
                "turn_id": self.turn_id, "verdict": "good"})
            self.assertEqual(first.status_code, 200)
            duplicate = client.post("/api/feedback", json={
                "turn_id": self.turn_id, "verdict": "good"})
            self.assertEqual(duplicate.status_code, 409)
            oversized = client.post(
                "/api/feedback",
                content=b"{" + b" " * (9 * 1024) + b"}",
                headers={"content-type": "application/json"},
            )
            self.assertEqual(oversized.status_code, 413)
            chunked = client.post(
                "/api/feedback",
                content=iter([b"{" + b" " * (20 * 1024) + b"}"]),
                headers={"content-type": "application/json",
                         "transfer-encoding": "chunked"},
            )
            self.assertEqual(chunked.status_code, 411)


if __name__ == "__main__":
    unittest.main()
