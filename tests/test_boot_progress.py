"""A slow start has to be legible, and it must not be a blocked start.

Two separate complaints produced this file, and they have the same root:
opening the page looked identical whether the server was reading PS_LEDGER
or had hung. The page showed a looping logo either way, and nothing it
polled could have told the difference because nothing reported what the
server was doing.

So there are two properties to hold onto, and the second is the one that
quietly rots:

  1. /api/boot names the step in flight, with its live elapsed time.
  2. /api/boot cannot itself block on the database. An endpoint whose whole
     job is to explain a slow query is worthless if a slow query stalls it.

And one more, upstream of both: the web server must accept connections
before the answer engine is up. Awaiting the MCP handshake in the lifespan
meant uvicorn printed a URL that then refused to connect for as long as an
Oracle logon took — the wait moved somewhere the page could not report it
at all.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.gui import progress  # noqa: E402

LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}


class StepTests(unittest.TestCase):
    def setUp(self) -> None:
        progress.reset()
        self.addCleanup(progress.reset)

    def test_the_whole_sequence_is_declared_before_it_runs(self) -> None:
        # A bar that only learns step 4 exists when step 4 starts cannot be
        # determinate, and "3 of 5" is most of the reassurance on offer.
        snap = progress.snapshot()
        self.assertEqual(snap["total"], len(progress.BOOT_STEPS))
        self.assertEqual(snap["completed"], 0)
        self.assertTrue(all(s["status"] == progress.PENDING
                            for s in snap["steps"]))

    def test_a_running_step_reports_how_long_it_has_been_running(self) -> None:
        progress.begin("defaults")
        running = [s for s in progress.snapshot()["steps"]
                   if s["status"] == progress.RUNNING]
        self.assertEqual([s["key"] for s in running], ["defaults"])
        self.assertGreaterEqual(running[0]["ms"], 0)

    def test_a_failure_records_its_reason_and_still_raises(self) -> None:
        with self.assertRaises(ValueError):
            with progress.step("period"):
                raise ValueError("ORA-12170: TNS:Connect timeout occurred")
        slot = {s["key"]: s for s in progress.snapshot()["steps"]}["period"]
        self.assertEqual(slot["status"], progress.FAILED)
        self.assertIn("ORA-12170", slot["note"])
        self.assertIn("period", progress.snapshot()["failed"])

    def test_a_later_refresh_does_not_restate_a_finished_step(self) -> None:
        # The background scope refresh reuses the boot-time code path and
        # runs every 15 minutes forever. Re-opening a settled step would
        # make a long-running server's boot look permanently unfinished.
        with progress.step("scopes"):
            pass
        first = {s["key"]: s for s in progress.snapshot()["steps"]}["scopes"]
        progress.begin("scopes")
        progress.end("scopes")
        again = {s["key"]: s for s in progress.snapshot()["steps"]}["scopes"]
        self.assertEqual(again["status"], progress.DONE)
        self.assertEqual(again["ms"], first["ms"])

    def test_ready_only_once_every_step_has_settled(self) -> None:
        self.assertFalse(progress.snapshot()["ready"])
        for key, _ in progress.BOOT_STEPS:
            progress.end(key)
        self.assertTrue(progress.snapshot()["ready"])


class BootEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.TestClient, cls.gapp = TestClient, gapp

    def test_it_answers_while_the_database_step_is_still_running(self) -> None:
        progress.reset()
        self.addCleanup(progress.reset)
        progress.begin("period")
        body = self.TestClient(self.gapp.app, **LOOP).get("/api/boot").json()
        running = [s for s in body["steps"] if s["status"] == progress.RUNNING]
        self.assertEqual([s["key"] for s in running], ["period"])
        self.assertTrue(running[0]["label"],
                        "a key is for code; the page shows the label")

    def test_it_does_not_touch_the_database(self) -> None:
        # The point of the endpoint is to be answerable when the database
        # is not. A future edit that reads one row here would pass every
        # other test and reintroduce the original silence.
        def refuse(*a, **k):
            raise AssertionError("/api/boot must not query the database")

        client = self.TestClient(self.gapp.app, **LOOP)
        with patch.object(self.gapp.db, "query", refuse), \
                patch.object(self.gapp.engine, "list_financial_scopes", refuse):
            r = client.get("/api/boot")
        self.assertEqual(r.status_code, 200)

    def test_it_says_whether_the_answer_engine_is_up_yet(self) -> None:
        body = self.TestClient(self.gapp.app, **LOOP).get("/api/boot").json()
        self.assertIn(body["mcp_session"]["state"],
                      {"starting", "ready", "degraded", "stopped"})


class NonBlockingStartupTests(unittest.TestCase):
    """Uvicorn serves nothing until lifespan startup returns."""

    def test_the_app_serves_while_the_answer_engine_is_still_connecting(self) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp

        entered = asyncio.Event()

        @contextlib.asynccontextmanager
        async def never_connects(*_a, **_k):
            entered.set()
            await asyncio.sleep(3600)       # an Oracle logon that never lands
            yield None, None                # pragma: no cover

        progress.reset()
        self.addCleanup(progress.reset)
        with patch("mcp.client.stdio.stdio_client", never_connects):
            # Entering the context manager runs lifespan startup. If that
            # awaited the handshake this line would hang for an hour.
            with TestClient(gapp.app, **LOOP) as client:
                body = client.get("/api/boot").json()
        steps = {s["key"]: s for s in body["steps"]}
        self.assertEqual(steps["engine"]["status"], progress.RUNNING)
        self.assertEqual(body["mcp_session"]["state"], "starting")
        self.assertFalse(body["mcp_session"]["shared"])


if __name__ == "__main__":
    unittest.main()
