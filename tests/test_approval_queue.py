"""Approving a taught fact should not require a terminal.

Two governed queues existed and both were CLI-only: site facts taught in
conversation, and per-source metadata-meaning proposals. Neither reaches an
answer until a human approves it, which makes the approval step part of the
product rather than an admin chore — and a queue you can only empty over SSH
is a queue that does not get emptied. There were four pending facts on the
development machine, none decided, from 2026-08-08.

The gate is the same one the question-log diagnostics already use:
machine-local (an SSH tunnel arrives as loopback) AND, when row security is
on, a configured privileged operator. Approving is a governance action, not
something a shared-VPN reader should reach.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.testclient import TestClient

from pstb.gui import app as gui
from pstb.memory import SiteMemory


def _client():
    # Loopback + a real Host header: the app refuses anything else before a
    # handler is reached, which is a different control and has its own tests.
    return TestClient(gui.app, client=("127.0.0.1", 5555),
                      base_url="http://localhost")


class ApprovalQueueTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "site_memory.json"
        memory = SiteMemory(self.path)
        self.first = memory.propose("PS_TU_FILE_INTFC: holds inbound interface files",
                                    kind="record")["fact"]["id"]
        self.second = memory.propose("PS_TU_FILE_INTFC: the interface configures files",
                                     kind="record")["fact"]["id"]
        self._patch = patch.object(gui, "_site_memory",
                                   lambda: SiteMemory(self.path))
        self._patch.start()
        self.client = _client()

    def tearDown(self):
        self._patch.stop()
        self._dir.cleanup()

    def _counts(self):
        return json.loads(self.path.read_text()) and SiteMemory(
            self.path).list_facts()["counts"]

    # ------------------------------------------------------------ listing
    def test_pending_items_are_listed_with_what_a_decision_needs(self):
        body = self.client.get("/api/approvals").json()
        self.assertEqual(body["pending_total"], 2)
        item = next(i for i in body["items"] if i["id"] == self.first)
        for field in ("queue", "id", "text", "subject", "origin", "status"):
            self.assertIn(field, item)
        self.assertEqual(item["queue"], "memory")
        self.assertEqual(item["status"], "pending")

    def test_status_filter_is_validated(self):
        self.assertEqual(self.client.get("/api/approvals?status=all").status_code,
                         200)
        bad = self.client.get("/api/approvals?status=whenever")
        self.assertEqual(bad.status_code, 400)

    # ----------------------------------------------------------- deciding
    def test_approve_and_reject_persist(self):
        ok = self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.first, "decision": "approve"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["status"], "approved")
        no = self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.second, "decision": "reject"})
        self.assertEqual(no.json()["status"], "rejected")
        self.assertEqual(self._counts(),
                         {"approved": 1, "pending": 0, "rejected": 1})

    def test_a_decision_records_who_made_it(self):
        body = self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.first, "decision": "approve"}).json()
        self.assertTrue(body["decided_by"],
                        "an approval with no attribution is not an audit trail")
        stored = next(f for f in SiteMemory(self.path).list_facts()["facts"]
                      if f["id"] == self.first)
        self.assertEqual(stored["decided_by"], body["decided_by"])

    def test_only_an_approved_fact_becomes_active(self):
        """The whole point: rejecting must not leave it usable."""
        self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.second, "decision": "reject"})
        approved = SiteMemory(self.path).approved()
        self.assertEqual([f["id"] for f in approved], [])

    def test_bad_input_is_refused_by_name(self):
        for payload, expected in (
            ({"queue": "memory", "id": "nope", "decision": "approve"}, 404),
            ({"queue": "memory", "id": self.first, "decision": "maybe"}, 400),
            ({"queue": "memory", "decision": "approve"}, 400),
            ({"queue": "source_knowledge", "id": "x",
              "decision": "approve"}, 400),
            ({"queue": "invented", "id": "x", "decision": "approve"}, 400),
        ):
            with self.subTest(payload=payload):
                r = self.client.post("/api/approvals/decide", json=payload)
                self.assertEqual(r.status_code, expected)
                self.assertTrue(str(r.json().get("detail") or "").strip(),
                                "a refusal with no reason is a dead end")

    # -------------------------------------------------------------- gate
    def test_both_endpoints_require_the_operator_gate(self):
        """Same gate as the question log: not reachable from a shared VPN."""
        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")

        with patch.object(gui, "_require_question_log_operator", refuse):
            self.assertEqual(self.client.get("/api/approvals").status_code, 403)
            self.assertEqual(
                self.client.post("/api/approvals/decide", json={
                    "queue": "memory", "id": self.first,
                    "decision": "approve"}).status_code, 403)
        self.assertEqual(self._counts()["pending"], 2,
                         "a refused request still changed the queue")


class ApprovalPanelTests(unittest.TestCase):
    """The browser half, checked against the shipped file."""

    @classmethod
    def setUpClass(cls):
        cls.page = (Path(gui.__file__).parent / "static" / "index.html").read_text()

    def test_the_panel_posts_with_a_content_type(self):
        """This FastAPI enforces it; without the header the decide 422s.

        Found by clicking the button in a browser — the request reached the
        error path and the row showed a badge instead of the decision.
        """
        block = self.page[self.page.index("/api/approvals/decide"):][:400]
        self.assertIn("Content-Type", block)
        self.assertIn("application/json", block)

    def test_an_error_object_is_not_rendered_as_object_Object(self):
        """FastAPI's 422 detail is a list, not a string.

        The first cut interpolated e.message straight into the badge, so the
        one place a reader most needs the reason showed "[object Object]".
        """
        self.assertIn("function errText(e)", self.page)
        self.assertIn("errText(e)", self.page)

    def test_the_panel_replaces_itself_rather_than_nesting(self):
        """A refresh that appends inside the old panel leaves a stale count.

        Observed: the decision reached the server and the heading above it
        still read the previous number of pending items.
        """
        start = self.page.index("async function loadApprovals(")
        block = self.page[start:start + 500]
        self.assertIn("existing.remove()", block)


if __name__ == "__main__":
    unittest.main()
