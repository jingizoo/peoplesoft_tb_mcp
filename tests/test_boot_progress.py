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


class PrimeCostTests(unittest.TestCase):
    """Warming the catalog at boot must not race the first dashboard.

    Discovery has two halves. The setup reads are hundreds of rows and cost
    milliseconds anywhere. The ledger existence probes are batched DISTINCTs
    over the balance table and can each eat the whole query timeout on a bad
    plan. Priming both at startup put the heavy half in direct competition
    with whatever view the person opened first — same database, same Oracle
    session pool — and made every dashboard slow for the first minutes after
    a restart. Reported as "it became very slow, all dashboards, not sure
    what it is attempting".
    """

    def setUp(self) -> None:
        from pstb.gui import app as gapp
        self.gapp = gapp
        self.saved = dict(gapp._scope_cache)
        self.addCleanup(lambda: gapp._scope_cache.update(self.saved))
        gapp._scope_cache.update({"value": None, "expires": 0.0,
                                  "refreshing": False})

    def test_the_boot_prime_never_verifies_pairs(self) -> None:
        import threading

        seen: list = []
        done = threading.Event()

        def record(include_activity=True, verify_pairs=True,
                   setup_only=False):
            seen.append({"include_activity": include_activity,
                         "verify_pairs": verify_pairs,
                         "setup_only": setup_only})
            done.set()
            return {"scopes": []}

        with patch.object(self.gapp.engine, "list_financial_scopes", record):
            self.gapp._prime_scope_catalog()
            self.assertTrue(done.wait(10), "the prime thread never ran")
        # setup_only is load-bearing: without it, a site whose setup
        # records are not granted fell through to the per-BU PS_LEDGER
        # probes AT BOOT — the exact expensive half this test exists to
        # keep out of the boot path.
        self.assertEqual(seen, [{"include_activity": False,
                                 "verify_pairs": False,
                                 "setup_only": True}])

    def test_an_empty_setup_only_prime_is_not_cached(self) -> None:
        # No setup grants -> empty catalog. Caching it would serve "no
        # business units exist" until the TTL; deferring means the first
        # real request builds honestly.
        import threading

        done = threading.Event()

        def empty(**_k):
            done.set()
            return {"scopes": []}

        with patch.object(self.gapp.engine, "list_financial_scopes", empty):
            self.gapp._prime_scope_catalog()
            self.assertTrue(done.wait(10))
            import time as _t
            for _ in range(50):          # let the thread finish its writes
                if self.gapp._scope_cache["value"] is None:
                    break
                _t.sleep(0.05)
        self.assertIsNone(self.gapp._scope_cache["value"])

    def test_the_prime_does_not_rebuild_a_catalog_it_already_has(self) -> None:
        # A persisted catalog from the last run is seeded at import. Paying
        # for discovery anyway would be the same competition by another name.
        import threading

        called = threading.Event()

        def refuse(*_a, **_k):
            called.set()
            return {"scopes": []}

        self.gapp._scope_cache.update({"value": {"scopes": [{"x": 1}]}})
        with patch.object(self.gapp.engine, "list_financial_scopes", refuse):
            self.gapp._prime_scope_catalog()
            self.assertFalse(called.wait(2))

    def test_meta_flags_the_primed_catalog_as_unverified(self) -> None:
        # Otherwise the page treats it as final, never calls /api/scopes,
        # and the pairs are never confirmed against ledger data at all.
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp

        gapp._scope_cache.update(
            {"value": {"scopes": [], "verified": False}, "expires": 0.0})
        meta = TestClient(gapp.app, **LOOP).get("/api/meta").json()
        self.assertTrue(meta["scopes_ready"])
        self.assertFalse(meta["scopes_verified"])


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
