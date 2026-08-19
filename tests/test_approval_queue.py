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
from types import SimpleNamespace
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


class ApprovalBadgeCountTests(unittest.TestCase):
    """The count endpoint deliberately skips the operator gate.

    #157 shipped the panel and the report back was "I still cannot see an
    approval link". The panel lives behind a machine-local gate, so an
    operator reading the app over the VPN got no sign the queue existed --
    four facts sat undecided for eleven days because no screen mentioned
    them. A badge cannot prompt anyone to open a tunnel if the badge is
    itself behind the tunnel.

    So /api/approvals/count is ungated where /api/approvals is not. That is
    a deliberate hole in a security boundary and it needs pinning from both
    sides: it must stay reachable when the operator gate refuses, and it
    must never carry anything but the integer.
    """

    SECRET = "PS_TU_SECRET_INTFC: the vendor bank routing record"

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "site_memory.json"
        memory = SiteMemory(self.path)
        self.only = memory.propose(self.SECRET, kind="record")["fact"]["id"]
        self._patch = patch.object(gui, "_site_memory",
                                   lambda: SiteMemory(self.path))
        self._patch.start()
        self.client = _client()

    def tearDown(self):
        self._patch.stop()
        self._dir.cleanup()

    def _refuse_operator(self):
        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")
        return patch.object(gui, "_require_question_log_operator", refuse)

    def test_the_count_survives_the_gate_that_hides_the_queue(self):
        """The entire reason this endpoint exists separately."""
        with self._refuse_operator():
            self.assertEqual(self.client.get("/api/approvals").status_code, 403,
                             "precondition: the queue itself must be gated")
            r = self.client.get("/api/approvals/count")
            self.assertEqual(r.status_code, 200,
                             "a badge behind the tunnel cannot advertise the "
                             "tunnel")
            self.assertEqual(r.json(), {"pending": 1, "readable": True})

    def test_the_count_carries_a_number_and_nothing_else(self):
        """What crosses the gate is one integer -- no content, ever."""
        raw = self.client.get("/api/approvals/count").text
        self.assertNotIn("SECRET_INTFC", raw)
        self.assertNotIn("bank routing", raw)
        self.assertNotIn(self.only, raw)
        self.assertEqual(set(self.client.get("/api/approvals/count").json()),
                         {"pending", "readable"},
                         "a new key here is a new thing crossing the gate")

    def test_the_count_route_is_not_in_the_open_paths_set(self):
        """What actually keeps it non-public.

        Sign-in is enforced by _row_security_guard for every /api/ path
        outside _OPEN_PATHS. Adding this route there -- an easy thing to
        reach for, since the badge is meant to be widely visible -- would
        make the count reachable with no session at all.
        """
        self.assertNotIn("/api/approvals/count", gui._OPEN_PATHS)

    def test_ungated_does_not_mean_public(self):
        """Skipping the operator gate must not skip signing in."""
        def not_signed_in(_request):
            raise HTTPException(status_code=401, detail="sign in")
        with patch.object(gui.cfg.security, "enabled", True), \
                patch.object(gui, "access_for_request", not_signed_in):
            self.assertEqual(
                self.client.get("/api/approvals/count").status_code, 401)

    def test_an_unreadable_queue_shows_nothing_not_an_error(self):
        """A badge is an affordance. A broken one must not become a card."""
        def boom():
            raise RuntimeError("site memory is on a disk that went away")
        with patch.object(gui, "_site_memory", boom):
            r = self.client.get("/api/approvals/count")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"pending": 0, "readable": False})

    def test_a_decided_item_stops_being_counted(self):
        self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.only, "decision": "reject"})
        self.assertEqual(
            self.client.get("/api/approvals/count").json()["pending"], 0)


class RefusalIsActionableTests(unittest.TestCase):
    """"Use an SSH tunnel" is a direction, not a remedy.

    The reader still had to work out the port and the flag order. The
    refusal now prints the line they can paste, using the port this
    process is really serving on rather than a placeholder.
    """

    def test_the_hint_quotes_the_port_actually_being_served(self):
        with patch.object(gui, "_SERVED_PORT", 8642):
            hint = gui.tunnel_hint()
        self.assertIn("8642", hint)
        self.assertIn("ssh -L", hint)
        self.assertNotIn("8000", hint,
                         "a hardcoded default is the bug, not the fix")

    def test_main_binds_the_hint_to_the_real_port(self):
        """A hint that lies is worse than no hint."""
        src = Path(gui.__file__).read_text()
        body = src[src.index("def main()"):]
        self.assertIn("_SERVED_PORT = int(args.port)", body)

    def test_the_operator_refusal_hands_over_a_pasteable_command(self):
        """The gate in front of the queue, refusing a non-loopback peer."""
        request = SimpleNamespace(scope={"client": ("10.4.1.9", 51000)})
        with patch.object(gui, "_SERVED_PORT", 8642):
            with self.assertRaises(HTTPException) as caught:
                gui._require_question_log_operator(request)
        detail = str(caught.exception.detail)
        self.assertIn("ssh -L 8642:localhost:8642", detail)
        self.assertIn("http://localhost:8642", detail)

    def test_the_loopback_refusal_names_the_port_it_is_serving(self):
        """The outer middleware had its own copy, hardcoded to 8000.

        The CLI has defaulted to 8016 since it grew a --port flag, so the
        one line a locked-out reader was told to paste could not have
        worked on any default deployment. It now reads the bound port off
        the ASGI scope.
        """
        client = TestClient(gui.app, client=("10.4.1.9", 51000),
                            base_url="http://localhost:8642")
        r = client.get("/api/approvals")
        self.assertEqual(r.status_code, 403)
        reason = str(r.json().get("error") or "")
        self.assertIn("ssh -L 8642:localhost:8642", reason)
        self.assertNotIn("8000", reason)

    def test_one_formatter_words_the_remedy(self):
        """Three copies of this sentence had already drifted apart."""
        self.assertEqual(gui.localguard.tunnel_command(8642),
                         "ssh -L 8642:localhost:8642 <this-host>")
        app_src = Path(gui.__file__).read_text()
        guard_src = Path(gui.localguard.__file__).read_text()
        self.assertEqual(app_src.count("ssh -L"), 1,
                         "main()'s startup banner is the only other one")
        self.assertEqual(guard_src.count("ssh -L"), 1,
                         "the formatter itself")

    def test_the_fallback_port_matches_what_the_cli_actually_defaults_to(self):
        app_src = Path(gui.__file__).read_text()
        flag = app_src[app_src.index('ap.add_argument("--port"'):][:160]
        self.assertIn(f"default={gui.localguard.DEFAULT_PORT}", flag)


class ApprovalDiscoverabilityPanelTests(unittest.TestCase):
    """The browser half, checked against the shipped file."""

    @classmethod
    def setUpClass(cls):
        cls.page = (Path(gui.__file__).parent / "static"
                    / "index.html").read_text()

    def test_the_nav_carries_the_badge(self):
        nav = self.page[self.page.index('data-v="diag"'):][:200]
        self.assertIn('id="approvalbadge"', nav,
                      "the count has to appear where the operator is looking")

    def test_the_badge_never_delays_the_first_paint(self):
        """Fire and forget: an affordance must not cost a round trip."""
        boot = self.page[self.page.index("bootSay('Drawing the workspace')"):]
        call = boot[:400]
        self.assertIn("refreshApprovalBadge()", call)
        self.assertNotIn("await refreshApprovalBadge()", call)

    def test_a_decision_updates_the_badge(self):
        """Otherwise the count still says 4 after you have emptied the queue."""
        block = self.page[self.page.index("/api/approvals/decide"):][:700]
        self.assertIn("refreshApprovalBadge()", block)

    def test_the_panel_does_not_paraphrase_the_servers_refusal(self):
        """Two copies of a remedy drift; the server's is the one with the port."""
        start = self.page.index("async function loadApprovals(")
        block = self.page[start:start + 900]
        self.assertIn("errText(e)", block)
        self.assertNotIn("SSH tunnel", block,
                         "the panel restated the remedy in its own words, so "
                         "it could not learn the real port")

if __name__ == "__main__":
    unittest.main()
