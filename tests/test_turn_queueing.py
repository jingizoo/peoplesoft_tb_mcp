"""One slow query must not kill the questions asked after it.

From a screenshot of a real session: three questions in a row, each ending
in "Timed out after 180s". Only the FIRST one was slow. The browser abandons
its request at 180s but the server turn keeps running against the database,
and it holds its conversation's lock the whole time — so the next question
queued behind a turn nobody was waiting for any more, spent its own 180s in
that queue and died identically, and so did the one after that. Nothing on
screen connected the second and third failures to the first.

Clear was no escape either: it waited on the same lock, so the one control
offered for a wedged conversation hung behind the wedge.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.gui import app as gapp  # noqa: E402


class BusyDescriptionTests(unittest.TestCase):
    def test_it_names_the_tool_and_how_long_it_has_run(self) -> None:
        entry = gapp._ProviderSession(provider=object(), touched=0.0)
        entry.busy_since = time.monotonic() - 212
        entry.busy_tool = "get_ar_aging"
        described = entry.describe_busy()
        self.assertIn("get_ar_aging", described)
        self.assertIn("21", described)          # ~212s, not "a while"
        self.assertGreater(entry.busy_for(), 200)

    def test_an_idle_conversation_reports_no_elapsed_time(self) -> None:
        entry = gapp._ProviderSession(provider=object(), touched=0.0)
        self.assertEqual(entry.busy_for(), 0.0)


class AbandonedTurnTests(unittest.TestCase):
    """The refusal must be IMMEDIATE once the holder is past the browser's
    own patience. Waiting politely is what produced the cascade."""

    def setUp(self) -> None:
        self.entry = gapp._ProviderSession(provider=object(), touched=0.0)

    def test_a_holder_past_the_client_budget_is_treated_as_abandoned(self) -> None:
        async def scenario():
            await self.entry.lock.acquire()
            try:
                self.entry.busy_since = time.monotonic() - (
                    gapp._ABANDONED_AFTER + 30)
                self.entry.busy_tool = "get_ar_aging"
                return (self.entry.lock.locked()
                        and self.entry.busy_for() > gapp._ABANDONED_AFTER)
            finally:
                self.entry.lock.release()

        self.assertTrue(asyncio.run(scenario()))

    def test_a_holder_inside_the_budget_is_waited_for(self) -> None:
        async def scenario():
            await self.entry.lock.acquire()
            try:
                self.entry.busy_since = time.monotonic() - 12
                return self.entry.busy_for() > gapp._ABANDONED_AFTER
            finally:
                self.entry.lock.release()

        self.assertFalse(asyncio.run(scenario()),
                         "a question that is merely slow must still queue")

    def test_the_queue_wait_is_bounded_and_releases_cleanly(self) -> None:
        # The cancelled acquire must not leave the lock half-taken, or the
        # conversation is wedged for good by the very guard meant to save it.
        async def scenario():
            await self.entry.lock.acquire()
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(self.entry.lock.acquire(),
                                           timeout=0.05)
            finally:
                self.entry.lock.release()
            await asyncio.wait_for(self.entry.lock.acquire(), timeout=1)
            self.entry.lock.release()
            return True

        self.assertTrue(asyncio.run(scenario()))

    def test_the_budgets_leave_room_to_answer(self) -> None:
        # A queue wait as long as the client timeout would spend the whole
        # request budget waiting and none of it working.
        self.assertLess(gapp._QUEUE_WAIT, gapp._ABANDONED_AFTER)


class ClearEscapesAStuckTurnTests(unittest.TestCase):
    def test_clear_returns_without_waiting_for_the_stuck_query(self) -> None:
        class Provider:
            def reset(self):
                time.sleep(30)          # the wedged turn, still running

        store = gapp._ProviderSessionStore()

        async def scenario():
            entry = store.get_or_create(("sess", "claude", "US001", "ACTUALS",
                                         0, 0), Provider)
            await entry.lock.acquire()          # a turn is mid-flight
            try:
                started = time.monotonic()
                cleared = await store.reset_session("sess")
                return cleared, time.monotonic() - started
            finally:
                entry.lock.release()

        cleared, elapsed = asyncio.run(scenario())
        self.assertEqual(cleared, 1)
        self.assertLess(elapsed, 10,
                        "Clear is the only way out of a wedged conversation; "
                        "it cannot itself wait on the wedge")

    def test_the_next_question_gets_a_fresh_lock_not_the_stuck_one(self) -> None:
        # This is what Clear actually promises. The old entry is detached, so
        # the next question builds a new conversation and does not queue.
        store = gapp._ProviderSessionStore()
        key = ("sess", "claude", "US001", "ACTUALS", 0, 0)

        async def scenario():
            stuck = store.get_or_create(key, lambda: object())
            await stuck.lock.acquire()
            try:
                await store.reset_session("sess")
                fresh = store.get_or_create(key, lambda: object())
                return stuck is not fresh and not fresh.lock.locked()
            finally:
                stuck.lock.release()

        self.assertTrue(asyncio.run(scenario()))


if __name__ == "__main__":
    unittest.main()
