from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pstb.workflows import WorkflowError, WorkflowStore, list_workflow_specs


class FakePlaybooks:
    def __init__(self, verdicts=None):
        self.verdicts = list(verdicts or ["passed"])
        self.calls = []

    def run(self, playbook, **scope):
        self.calls.append((playbook, scope))
        verdict = self.verdicts.pop(0)
        return {
            "playbook": playbook, "verdict": verdict,
            "attention_count": 2 if verdict == "exceptions_found" else 0,
            "skipped_count": 1 if verdict == "incomplete" else 0,
            "as_of": "2026-06-30",
            "steps": [{
                "headline": "Supplier SECRET VENDOR has voucher V0001",
                "detail": {"amount": 9876543.21},
            }],
        }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workflows"
        self.store = WorkflowStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, name="month_end_close"):
        return self.store.start(
            name, business_unit="US001", ledger="ACTUALS",
            fiscal_year=2026, period=6)

    def test_specs_sequence_existing_deterministic_playbooks(self):
        specs = list_workflow_specs()["workflows"]
        close = next(row for row in specs if row["name"] == "month_end_close")
        self.assertEqual(close["phases"], [
            "ap_completeness", "close_readiness", "post_close_watch"])

    def test_run_stops_at_review_gate_and_does_not_run_next_phase(self):
        state = self._start()
        runner = FakePlaybooks(["exceptions_found", "passed"])
        first = self.store.run_next(state["id"], runner)
        self.assertTrue(first["review_required"])
        self.assertEqual(first["state"]["status"], "awaiting_review")
        again = self.store.run_next(state["id"], runner)
        self.assertIsNone(again["result"])
        self.assertEqual(len(runner.calls), 1)

    def test_running_lease_prevents_duplicate_execution(self):
        state = self._start("receivables_review")
        path = self.path / f"{state['id']}.json"
        raw = json.loads(path.read_text())
        raw["status"] = "running"
        raw["phases"][0]["status"] = "running"
        raw["phases"][0]["lease_expires_at"] = "2999-01-01T00:00:00+00:00"
        path.write_text(json.dumps(raw))
        runner = FakePlaybooks(["passed"])
        got = self.store.run_next(state["id"], runner)
        self.assertIn("already running", got["detail"])
        self.assertEqual(runner.calls, [])

    def test_expired_running_lease_is_resumable(self):
        state = self._start("receivables_review")
        path = self.path / f"{state['id']}.json"
        raw = json.loads(path.read_text())
        raw["status"] = "running"
        raw["phases"][0]["status"] = "running"
        raw["phases"][0]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(raw))
        got = self.store.run_next(state["id"], FakePlaybooks(["passed"]))
        self.assertEqual(got["state"]["status"], "awaiting_review")
        self.assertEqual(got["state"]["phases"][0]["attempts"], 1)

    def test_checkpoint_contains_no_live_financial_details(self):
        state = self.store.start(
            "receivables_review", business_unit="US001")
        got = self.store.run_next(state["id"], FakePlaybooks(["exceptions_found"]))
        persisted = (self.path / f"{state['id']}.json").read_text()
        self.assertNotIn("SECRET VENDOR", persisted)
        self.assertNotIn("9876543", persisted)
        self.assertNotIn("V0001", persisted)
        self.assertNotIn("started_by", persisted)
        phase = got["state"]["phases"][0]
        self.assertEqual(phase["verdict"], "exceptions_found")
        self.assertEqual(len(phase["result_hash"]), 64)
        mode = stat.S_IMODE((self.path / f"{state['id']}.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_building_checkpoint_is_private_before_first_content_write(self):
        observed_modes = []
        original_dump = json.dump

        def inspect_then_dump(value, handle, **kwargs):
            building = next(self.path.glob("*.building"))
            observed_modes.append(stat.S_IMODE(building.stat().st_mode))
            return original_dump(value, handle, **kwargs)

        with mock.patch("pstb.workflows.json.dump", side_effect=inspect_then_dump):
            self._start("receivables_review")
        self.assertEqual(observed_modes, [0o600])

    def test_accept_advances_and_fresh_store_resumes(self):
        state = self._start()
        runner = FakePlaybooks(["passed", "passed"])
        ran = self.store.run_next(state["id"], runner)
        accepted = self.store.review(
            state["id"], "accept",
            expected_revision=ran["state"]["revision"])
        persisted = (self.path / f"{state['id']}.json").read_text()
        self.assertNotIn("reviewed_by", persisted)
        self.assertTrue(accepted["phases"][0]["human_reviewed"])
        self.assertEqual(accepted["active_phase"], 2)
        reopened = WorkflowStore(self.path)
        second = reopened.run_next(state["id"], runner)
        self.assertEqual(second["state"]["phases"][1]["playbook"],
                         "close_readiness")

    def test_incomplete_is_never_recorded_as_pass(self):
        state = self._start("receivables_review")
        got = self.store.run_next(state["id"], FakePlaybooks(["incomplete"]))
        phase = got["state"]["phases"][0]
        self.assertEqual(phase["verdict"], "incomplete")
        self.assertEqual(phase["skipped_count"], 1)
        self.assertEqual(got["state"]["status"], "awaiting_review")

    def test_rerun_clears_old_result_metadata_but_keeps_attempt_count(self):
        state = self._start("receivables_review")
        ran = self.store.run_next(state["id"], FakePlaybooks(["passed"]))
        rerun = self.store.review(
            state["id"], "rerun",
            expected_revision=ran["state"]["revision"])
        phase = rerun["phases"][0]
        self.assertEqual(phase["status"], "pending")
        self.assertEqual(phase["attempts"], 1)
        self.assertNotIn("result_hash", phase)
        self.assertEqual(rerun["status"], "pending")

    def test_revision_guard_prevents_stale_review_click(self):
        state = self._start("receivables_review")
        self.store.run_next(state["id"], FakePlaybooks(["passed"]))
        with self.assertRaisesRegex(WorkflowError, "changed since"):
            self.store.review(state["id"], "accept", expected_revision=1)

    def test_review_mutations_require_a_positive_displayed_revision(self):
        state = self._start("receivables_review")
        self.store.run_next(state["id"], FakePlaybooks(["passed"]))
        for action in ("accept", "rerun", "cancel"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(WorkflowError, "positive displayed"):
                    self.store.review(state["id"], action)

    def test_superseded_expired_runner_cannot_checkpoint_stale_result(self):
        state = self._start("receivables_review")
        path = self.path / f"{state['id']}.json"
        store = self.store

        class ReclaimedDuringRun:
            def run(self, playbook, **scope):
                raw = json.loads(path.read_text())
                raw["phases"][0]["lease_expires_at"] = (
                    "2000-01-01T00:00:00+00:00")
                path.write_text(json.dumps(raw))
                newer = store.run_next(
                    state["id"], FakePlaybooks(["passed"]))
                self.newer = newer
                return {
                    "verdict": "exceptions_found", "attention_count": 99,
                    "skipped_count": 0, "as_of": "2026-06-30",
                }

        runner = ReclaimedDuringRun()
        stale = store.run_next(state["id"], runner)
        self.assertIn("lost its lease", stale["detail"])
        self.assertIsNone(stale["result"])
        current = store.get(state["id"])
        self.assertEqual(current["phases"][0]["attempts"], 2)
        self.assertEqual(current["phases"][0]["verdict"], "passed")
        self.assertEqual(current["phases"][0]["attention_count"], 0)
        self.assertNotIn("execution_token", current["phases"][0])

    def test_unknown_workflow_and_bad_id_refuse(self):
        with self.assertRaisesRegex(WorkflowError, "unknown workflow"):
            self.store.start("model_decides_everything")
        with self.assertRaisesRegex(WorkflowError, "invalid workflow id"):
            self.store.get("../../secrets")


if __name__ == "__main__":
    unittest.main()
