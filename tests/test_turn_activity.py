"""The live turn indicator must describe THIS question and no other.

Reported from the floor: "when I ask show AR aging it goes to previous
failed requests, says close readiness". Nothing was misrouting. The
activity slot was keyed by browser session, and a new question did not
claim it until deep inside the request — after scope discovery, after the
engine setup, after waiting for any turn already running in that tab. For
the whole of that stretch the poll returned the PREVIOUS question's steps,
and a previous question that had died mid-tool left an event that still
claimed to be running. So an AR aging reported itself as a running
close_readiness, forever, until the first real tool call replaced it.

Two properties fix it and both are easy to lose again:

  1. A turn's events are addressed by a token the browser mints per
     question. A poll carrying a different token gets nothing, not
     somebody else's turn.
  2. A turn that dies mid-tool leaves no event claiming to be running.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.gui import app as gapp  # noqa: E402

SESSION = "browsertab000001"
LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}


class SlotTests(unittest.TestCase):
    def setUp(self) -> None:
        with gapp._activity_lock:
            gapp._activity.clear()
        self.addCleanup(gapp._activity.clear)

    def test_a_new_turn_does_not_inherit_the_previous_turns_steps(self) -> None:
        gapp._activity_begin(SESSION, "turn-1")
        gapp._activity_add(SESSION, "turn-1", {
            "status": "running", "tool": "run_playbook",
            "args": "playbook=close_readiness"})
        gapp._activity_begin(SESSION, "turn-2", "Checking the financial scope")
        with gapp._activity_lock:
            slot = gapp._activity[SESSION]
        self.assertEqual(slot["events"], [])
        self.assertEqual(slot["phase"], "Checking the financial scope")

    def test_a_finished_turn_cannot_write_into_its_successor(self) -> None:
        # The previous turn is still unwinding while the next one has
        # already claimed the slot — the ordering that produced the report.
        gapp._activity_begin(SESSION, "turn-1")
        gapp._activity_begin(SESSION, "turn-2")
        gapp._activity_add(SESSION, "turn-1", {
            "status": "done", "tool": "run_playbook", "ms": 41000})
        gapp._activity_phase(SESSION, "turn-1", "Asking claude")
        gapp._activity_done(SESSION, "turn-1")
        with gapp._activity_lock:
            slot = gapp._activity[SESSION]
        self.assertEqual(slot["events"], [])
        self.assertNotEqual(slot["phase"], "Asking claude")
        self.assertTrue(slot["active"], "turn-2 is still running")

    def test_a_turn_that_dies_mid_tool_leaves_nothing_running(self) -> None:
        gapp._activity_begin(SESSION, "turn-1")
        gapp._activity_add(SESSION, "turn-1", {
            "status": "running", "tool": "get_ar_aging", "args": "bu=US001"})
        gapp._activity_done(SESSION, "turn-1")
        with gapp._activity_lock:
            events = gapp._activity[SESSION]["events"]
        self.assertEqual([e["status"] for e in events], ["failed"])


class ActivityEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        cls.client = TestClient(gapp.app, **LOOP)

    def setUp(self) -> None:
        with gapp._activity_lock:
            gapp._activity.clear()
        self.addCleanup(gapp._activity.clear)

    def _get(self, turn: str = ""):
        url = f"/api/activity?session_id={SESSION}"
        if turn:
            url += f"&turn={turn}"
        return self.client.get(url).json()

    def test_a_poll_for_another_turn_gets_nothing_and_says_stale(self) -> None:
        gapp._activity_begin(SESSION, "turn-2")
        gapp._activity_add(SESSION, "turn-2", {
            "status": "running", "tool": "get_ar_aging", "args": ""})
        body = self._get("turn-1")
        self.assertTrue(body["stale"])
        self.assertEqual(body["events"], [])
        self.assertFalse(body["active"])

    def test_the_owning_turn_sees_its_own_steps(self) -> None:
        gapp._activity_begin(SESSION, "turn-2", "Validating the scope")
        body = self._get("turn-2")
        self.assertFalse(body["stale"])
        self.assertTrue(body["active"])
        self.assertEqual(body["phase"], "Validating the scope")

    def test_the_pre_tool_phase_is_reported_at_all(self) -> None:
        # The stretch before the first tool call is where a slow turn looks
        # dead: scope validation, a queued turn, spawning an engine.
        gapp._activity_begin(SESSION, "turn-9", "Checking the financial scope")
        self.assertEqual(self._get("turn-9")["phase"],
                         "Checking the financial scope")
        gapp._activity_phase(SESSION, "turn-9",
                             "Waiting for the previous question in this tab "
                             "to finish")
        self.assertIn("previous question", self._get("turn-9")["phase"])

    def test_an_unknown_session_is_not_an_error(self) -> None:
        body = self.client.get("/api/activity?session_id=nosuchsession01").json()
        self.assertFalse(body["active"])
        self.assertEqual(body["events"], [])


class ChatClaimsTheSlotImmediatelyTests(unittest.TestCase):
    """The claim must happen at the TOP of /api/chat.

    Every await before it — scope discovery, engine spawn, the queue wait —
    is time the poll would otherwise spend describing the last question.
    """

    def test_the_slot_is_claimed_before_scope_discovery_runs(self) -> None:
        from starlette.testclient import TestClient

        seen: dict = {}

        def slow_catalog(*_a, **_k):
            # Whatever the slot says HERE is what a poll would return
            # during the opening seconds of the request.
            with gapp._activity_lock:
                seen["slot"] = dict(gapp._activity.get(SESSION) or {})
            raise RuntimeError("stop the turn here; the claim is the subject")

        with gapp._activity_lock:
            gapp._activity.clear()
        self.addCleanup(gapp._activity.clear)
        gapp._activity_begin(SESSION, "turn-old")
        gapp._activity_add(SESSION, "turn-old", {
            "status": "running", "tool": "run_playbook",
            "args": "playbook=close_readiness"})

        original = gapp._financial_scope_catalog
        gapp._financial_scope_catalog = slow_catalog
        try:
            TestClient(gapp.app, **LOOP).post("/api/chat", json={
                "message": "show me the AR aging", "session_id": SESSION,
                "scope": {}, "turn_token": "turn-new"})
        finally:
            gapp._financial_scope_catalog = original

        self.assertEqual(seen["slot"].get("turn"), "turn-new")
        self.assertEqual(seen["slot"].get("events"), [],
                         "the previous close_readiness step must be gone "
                         "before the first await, not after it")
        self.assertTrue(seen["slot"].get("phase"))


if __name__ == "__main__":
    unittest.main()
