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
        meta = self.TestClient(self.gapp.app).get("/api/meta").json()
        self.assertFalse(meta.get("scopes_ready"))
        self.assertEqual(meta.get("financial_scopes"), [])

    def test_scopes_endpoint_builds_on_demand_and_then_meta_serves_it(self) -> None:
        client = self.TestClient(self.gapp.app)
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
        with self.TestClient(self.gapp.app) as live:
            live.get("/api/meta")
            self.assertIsNotNone(self.gapp._MCP.get("session"),
                                 msg=str(self.gapp._MCP.get("error")))
            self.assertGreater(len(self.gapp._MCP.get("tools") or []), 20)

    def test_chat_survives_when_the_shared_session_is_unavailable(self) -> None:
        # Degrade to a per-turn server rather than failing the conversation.
        saved = self.gapp._MCP.get("session")
        self.gapp._MCP["session"] = None
        try:
            with self.TestClient(self.gapp.app) as live:
                r = live.post("/api/chat", json={
                    "message": "What is our capitalization threshold?",
                    "scope": {}, "session_id": "fallbacksess01"})
                self.assertEqual(r.status_code, 200, r.text[:200])
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
