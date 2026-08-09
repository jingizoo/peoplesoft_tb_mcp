"""Performance-shape guarantees for the web UI.

These live here rather than in scripts/smoke_test.py because they need
FastAPI; the smoke suite is deliberately stdlib-only so it can run on a box
before any virtualenv exists.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class AsyncDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from pstb.gui import app as gapp

        self.gapp = gapp
        self.TestClient = TestClient
        # Force a cold catalog so the split is actually exercised.
        gapp._scope_cache.update({"value": None, "expires": 0.0})

    def test_meta_does_not_build_the_scope_catalog(self) -> None:
        # The page must paint before discovery runs: building the catalog in
        # /api/meta held the first paint for as long as a WAN round trip took
        # (a minute on the real instance) with nothing on screen to explain it.
        meta = self.TestClient(self.gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)).get("/api/meta").json()
        self.assertFalse(meta.get("scopes_ready"))
        self.assertEqual(meta.get("financial_scopes"), [])

    def test_scopes_endpoint_builds_on_demand_and_then_meta_serves_it(self) -> None:
        client = self.TestClient(self.gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
        scopes = client.get("/api/scopes").json()
        self.assertTrue(scopes.get("ready"))
        self.assertGreaterEqual(len(scopes.get("scopes") or []), 1)
        warm = client.get("/api/meta").json()
        self.assertTrue(warm.get("scopes_ready"))
        self.assertTrue(warm.get("financial_scopes"))

    def test_mcp_session_is_established_once_at_startup(self) -> None:
        # One server for the process, not one per chat turn: the spawn plus
        # handshake measured ~320ms, paid on EVERY question, and discarded
        # every engine cache with it.
        with self.TestClient(self.gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as live:
            live.get("/api/meta")
            self.assertIsNotNone(self.gapp._MCP.get("session"),
                                 msg=str(self.gapp._MCP.get("error")))
            self.assertGreater(len(self.gapp._MCP.get("tools") or []), 20)

    def test_chat_survives_when_the_shared_session_is_unavailable(self) -> None:
        """With no shared session, chat must still reach a per-turn server.

        This asserts the MCP FALLBACK, not that a language model is
        installed: CI installs only the gui extra, so provider construction
        legitimately fails there with "ollama package not installed". That
        error proves the fallback worked — the turn got far enough to build a
        provider. A failure to spawn the server would surface as an MCP or
        session error instead, which is what this guards against.
        """
        saved = self.gapp._MCP.get("session")
        self.gapp._MCP["session"] = None
        try:
            with self.TestClient(self.gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as live:
                r = live.post("/api/chat", json={
                    "message": "What is our capitalization threshold?",
                    "scope": {}, "session_id": "fallbacksess01"})
                if r.status_code == 200:
                    return
                detail = r.text.lower()
                provider_missing = ("package not installed" in detail
                                    or "google-genai" in detail
                                    or "gcloud" in detail)
                self.assertTrue(
                    provider_missing,
                    msg=f"expected an LLM-provider error, got {r.text[:200]}")
                for mcp_symptom in ("mcp", "stdio", "cancel scope",
                                    "session is not", "broken pipe"):
                    self.assertNotIn(
                        mcp_symptom, detail,
                        msg=f"fallback did not reach a server: {r.text[:200]}")
        finally:
            self.gapp._MCP["session"] = saved


class BatchedProbeTests(unittest.TestCase):
    def test_scope_discovery_is_a_handful_of_round_trips(self) -> None:
        from pstb.config import Config
        from pstb.db import Database
        from pstb.engine import TBEngine

        cfg = Config.sample(ROOT)
        db = Database(cfg)
        engine = TBEngine(db, cfg)
        seen: list = []
        original = db.query

        def spy(sql, params=None, max_rows=None):
            seen.append(" ".join(str(sql).split()))
            return original(sql, params, max_rows=max_rows)

        db.query = spy
        engine.invalidate_scope_cache()
        engine._ledger_scope_pairs()
        db.query = original
        # One query per 50 pairs, not one per pair: each probe is milliseconds
        # of database work but a full network round trip.
        self.assertLessEqual(len(seen), 6, seen)
        unbounded = [s for s in seen
                     if "DISTINCT BUSINESS_UNIT" in s and " WHERE " not in s]
        self.assertEqual(unbounded, [], "discovery must never scan the ledger")


if __name__ == "__main__":
    unittest.main()


class BusinessUnitNameTests(unittest.TestCase):
    """The scope bar shows the unit's NAME, not only its code.

    "US001" identifies nothing to a reader who does not already know the
    installation, and on a multi-unit instance it is the difference between
    reading a number for the right company and the wrong one. The name only
    exists in the scope catalog — /api/meta carries a "(discovered)"
    placeholder, not a description — so if this payload ever stops carrying
    descr the chip silently reverts to bare codes with nothing failing.
    """

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from pstb.gui import app as gapp

        gapp._scope_cache.update({"value": None, "expires": 0.0})
        self.client = TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))

    def test_the_scope_catalog_carries_a_real_description(self) -> None:
        scopes = self.client.get("/api/scopes").json().get("scopes") or []
        self.assertTrue(scopes, "no scopes to check")
        named = [s for s in scopes
                 if (s.get("descr") or "").strip()
                 and s["descr"] != "(discovered)"
                 and s["descr"] != s["business_unit"]]
        self.assertTrue(
            named,
            "no scope carried a usable business-unit description, so the "
            f"scope bar can only show codes: {[s.get('descr') for s in scopes]}")

    def test_the_description_is_absent_rather_than_faked(self) -> None:
        # A site whose PS_BUS_UNIT_TBL_GL has no DESCR column must yield a
        # missing description, never the business unit code echoed back as if
        # it were a name — the UI treats those differently on purpose.
        for scope in self.client.get("/api/scopes").json().get("scopes") or []:
            descr = scope.get("descr")
            if descr is None:
                continue
            self.assertNotEqual(
                descr.strip(), scope["business_unit"],
                "descr echoed the code; the UI would render 'US001 US001'")


class StaleWhileRevalidateTests(unittest.TestCase):
    """Scope loading must never make a user wait twice.

    The catalog had a 60-second freshness window, and the request after
    expiry rebuilt it SYNCHRONOUSLY behind the lock — on a real instance a
    minute-plus, which is exactly the intermittent "loading of scope times
    out". Past the window the stale catalog is served instantly and one
    background thread refreshes; a restart seeds from disk so even the
    first request of the day is not a rebuild.
    """

    def _drain(self, seconds: float = 4.0) -> None:
        """Wait out any in-flight background refresh so state cannot bleed
        between tests sharing the module-level cache."""
        deadline = self.time.monotonic() + seconds
        while self.time.monotonic() < deadline:
            with self.gapp._scope_cache_lock:
                if not self.gapp._scope_cache["refreshing"]:
                    return
            self.time.sleep(0.05)

    def setUp(self) -> None:
        import time as _t

        from fastapi.testclient import TestClient

        from pstb.gui import app as gapp

        self.gapp = gapp
        self.time = _t
        self.client = TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
        self.original = gapp.engine.list_financial_scopes
        # A deterministic base: force builds the VERIFIED catalog
        # synchronously and spawns no background thread.
        gapp._scope_cache.update({"value": None, "expires": 0.0,
                                  "refreshing": False})
        self.client.get("/api/scopes", params={"force": "true"})
        self._drain()

    def tearDown(self) -> None:
        self.gapp.engine.list_financial_scopes = self.original
        self._drain()

    def test_a_stale_catalog_is_served_without_waiting(self) -> None:
        gapp, _t = self.gapp, self.time
        rebuild_started = _t.monotonic()

        def slow(**kw):
            _t.sleep(0.8)
            return self.original(**kw)

        gapp.engine.list_financial_scopes = slow
        gapp._scope_cache["expires"] = 0.0            # stale, value present
        t0 = _t.monotonic()
        out = self.client.get("/api/scopes").json()
        waited = _t.monotonic() - t0
        self.assertTrue(out["ready"])
        self.assertLess(waited, 0.5,
                        f"user waited {waited:.2f}s on the rebuild — "
                        "stale-while-revalidate is not serving stale")
        # The refresh must actually land.
        deadline = _t.monotonic() + 5
        while _t.monotonic() < deadline:
            with gapp._scope_cache_lock:
                if (_t.monotonic() < gapp._scope_cache["expires"]
                        and not gapp._scope_cache["refreshing"]):
                    break
            _t.sleep(0.05)
        else:
            self.fail("the background refresh never completed")

    def test_only_one_refresh_thread_is_spawned(self) -> None:
        gapp, _t = self.gapp, self.time
        calls = {"n": 0}

        def slow(**kw):
            calls["n"] += 1
            _t.sleep(0.5)
            return self.original(**kw)

        gapp.engine.list_financial_scopes = slow
        gapp._scope_cache.update({"expires": 0.0, "refreshing": False})
        for _ in range(5):
            self.client.get("/api/scopes")
        self._drain()
        self.assertEqual(calls["n"], 1,
                         "every stale request spawned its own rebuild — "
                         "a thundering herd against the real database")

    def test_the_catalog_survives_a_restart_via_disk(self) -> None:
        gapp = self.gapp
        persisted = gapp._load_persisted_scope_catalog()
        self.assertIsNotNone(persisted,
                             "no catalog on disk after a successful build — "
                             "every restart pays full discovery again")
        self.assertTrue(persisted.get("scopes"))

    def test_meta_serves_a_stale_catalog_immediately(self) -> None:
        # /api/meta may hand out a stale catalog (it is a list of business
        # units, not a balance) so a restarted server paints instantly.
        gapp = self.gapp
        gapp._scope_cache["expires"] = 0.0
        meta = self.client.get("/api/meta").json()
        self.assertTrue(meta.get("scopes_ready"))


class FirstBuildCannotTimeOutTests(unittest.TestCase):
    """#57's stale-serving needs one successful build to EVER exist. On an
    instance whose ledger existence probes reliably time out, the first
    synchronous build died, the UI offered retry, and retry repeated the
    same doomed build — no convergence, ever. The first build therefore
    skips the probes (setup reads only, milliseconds anywhere) and the
    verified catalog arrives via the background refresh when it can."""

    def setUp(self) -> None:
        import time as _t

        from fastapi.testclient import TestClient

        from pstb.gui import app as gapp

        self.gapp = gapp
        self.time = _t
        self.client = TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
        self.orig_probe = gapp.engine._with_ledger_data

    def tearDown(self) -> None:
        self.gapp.engine._with_ledger_data = self.orig_probe
        deadline = self.time.monotonic() + 4.0
        while self.time.monotonic() < deadline:
            with self.gapp._scope_cache_lock:
                if not self.gapp._scope_cache["refreshing"]:
                    break
            self.time.sleep(0.05)

    def test_verify_false_issues_zero_probes(self) -> None:
        gapp = self.gapp
        calls = {"n": 0}
        gapp.engine._with_ledger_data = (
            lambda pairs, *a, **k: (calls.__setitem__("n", calls["n"] + 1)
                                    or self.orig_probe(pairs)))
        out = gapp.engine.list_financial_scopes(include_activity=False,
                                                verify_pairs=False)
        self.assertEqual(calls["n"], 0)
        self.assertTrue(out.get("scopes"))

    def test_first_build_survives_probes_that_always_die(self) -> None:
        gapp, _t = self.gapp, self.time

        def doomed(pairs):
            _t.sleep(0.6)
            raise RuntimeError("probe timeout")

        gapp.engine._with_ledger_data = doomed
        gapp._scope_cache.update({"value": None, "expires": 0.0,
                                  "refreshing": False})
        t0 = _t.monotonic()
        out = self.client.get("/api/scopes").json()
        waited = _t.monotonic() - t0
        self.assertTrue(out.get("ready") and out.get("scopes"),
                        "no catalog on the doomed box — retry would loop "
                        "forever")
        self.assertLess(waited, 0.5,
                        f"first build waited {waited:.2f}s on the probes")


    def test_the_unverified_catalog_says_so(self) -> None:
        gapp = self.gapp
        gapp._scope_cache.update({"value": None, "expires": 0.0,
                                  "refreshing": True})   # suppress refresh
        self.client.get("/api/scopes")
        with gapp._scope_cache_lock:
            value = gapp._scope_cache["value"]
            gapp._scope_cache["refreshing"] = False
        self.assertIs(value.get("verified"), False)
        self.assertIn("not yet verified", value.get("note", ""))
