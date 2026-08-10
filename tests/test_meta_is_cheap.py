"""Opening the page must not discover the default business unit.

"Can we just not load default BU, it is taking a lot to load." It was two of
the slowest things this app does, paid on every cold page load before the
first paint:

  effective_defaults() falls through to _ledger_scope_pairs(), whose
  verification probes PS_LEDGER; last_posted_period() is two MIN/MAX
  aggregates over the same table. A third MIN/MAX followed, for
  last_period_with_data.

And it bought almost nothing. The browse views that read the scope bar are
hidden — nav is display:none, Ask is the product — and the scope a chat runs
in is the one the person picks in the chooser, which comes from the catalog.
So /api/meta serves this only from caches something else already warmed, and
whoever actually needs the discovered scope asks /api/scope and waits.

The tests count QUERIES, not milliseconds: the cost is round trips against a
table the sqlite sample makes free.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}


class ColdMetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.gapp = gapp
        cls.client = TestClient(gapp.app, **LOOP)

    def setUp(self) -> None:
        # A genuinely cold process: no scope catalog, no discovered defaults.
        self.gapp.engine.invalidate_scope_cache()
        self.saved = dict(self.gapp._scope_cache)
        self.addCleanup(lambda: self.gapp._scope_cache.update(self.saved))
        self.addCleanup(self.gapp.engine.invalidate_scope_cache)
        self.gapp._scope_cache.update({"value": None, "expires": 0.0,
                                       "refreshing": False})

    def _queries_during(self, call):
        seen: list = []
        original = self.gapp.db.query

        def spy(sql, params=None, max_rows=None):
            seen.append(" ".join(str(sql).split()))
            return original(sql, params, max_rows)

        self.gapp.db.query = spy
        try:
            result = call()
        finally:
            self.gapp.db.query = original
        return result, seen

    def test_a_cold_meta_never_touches_the_ledger(self) -> None:
        body, queries = self._queries_during(
            lambda: self.client.get("/api/meta").json())
        ledger_hits = [q for q in queries if "PS_LEDGER" in q.upper()]
        self.assertEqual(ledger_hits, [], "\n".join(ledger_hits))
        self.assertFalse(body["scope_ready"])

    def test_a_cold_meta_is_a_handful_of_queries_at_most(self) -> None:
        # The calendar lookup is a small setup table and earns its place: it
        # is what puts a sensible year and period in the bar with no scope.
        _, queries = self._queries_during(
            lambda: self.client.get("/api/meta"))
        self.assertLessEqual(len(queries), 3, "\n".join(queries))

    def test_the_page_still_gets_everything_it_needs_to_paint(self) -> None:
        # Cold or not, the boot script reads these unconditionally. A missing
        # key is a TypeError in the browser and a page that never appears.
        body = self.client.get("/api/meta").json()
        for key in ("defaults", "scope", "current", "last_period_with_data",
                    "ledgers", "business_units", "financial_scopes",
                    "scope_ready", "scopes_ready", "build", "llm"):
            self.assertIn(key, body, key)
        self.assertIn("fiscal_year", body["current"])
        self.assertIn("period", body["current"])
        self.assertIn("business_unit", body["scope"])
        self.assertFalse(body["scope"]["discovered"],
                         "config values must never be presented as verified")

    def test_a_warm_meta_serves_the_discovered_scope(self) -> None:
        # Once something else has paid for discovery, /api/meta hands it over
        # — still without querying, and now flagged as verified.
        eff = self.gapp.engine.effective_defaults()
        self.gapp.engine.last_posted_period(eff["business_unit"],
                                            eff["ledger"])
        body, queries = self._queries_during(
            lambda: self.client.get("/api/meta").json())
        self.assertTrue(body["scope_ready"])
        self.assertEqual(body["scope"]["business_unit"], eff["business_unit"])
        ledger_hits = [q for q in queries if "PS_LEDGER" in q.upper()]
        # One is allowed here: last_period_with_data, which is only asked for
        # when the scope it measures against is already known.
        self.assertLessEqual(len(ledger_hits), 1, "\n".join(ledger_hits))

    def test_the_SECOND_warm_meta_queries_nothing_at_all(self) -> None:
        # The first warm call may fill the calendar and activity caches.
        # After that, "must return INSTANTLY" has to mean zero database
        # round trips — the first cut of this file allowed one PS_LEDGER
        # aggregate per page load FOREVER, plus two uncached calendar
        # queries the cold-path cap quietly tolerated, and on a WAN
        # deployment that was seconds of synchronous work in front of
        # every paint for the life of the process.
        eff = self.gapp.engine.effective_defaults()
        self.gapp.engine.last_posted_period(eff["business_unit"],
                                            eff["ledger"])
        self.client.get("/api/meta")            # fills the remaining caches
        _, queries = self._queries_during(
            lambda: self.client.get("/api/meta"))
        self.assertEqual(queries, [],
                         "a warm /api/meta must serve entirely from memory")


class WarmAccessorTests(unittest.TestCase):
    """The accessors exist to be safe to call from a page-serving path."""

    @classmethod
    def setUpClass(cls) -> None:
        from pstb.gui import app as gapp
        cls.engine = gapp.engine

    def setUp(self) -> None:
        self.engine.invalidate_scope_cache()
        self.addCleanup(self.engine.invalidate_scope_cache)

    def test_they_return_none_rather_than_discovering(self) -> None:
        def refuse(*_a, **_k):
            raise AssertionError("a warm accessor must never query")

        original = self.engine.db.query
        self.engine.db.query = refuse
        try:
            self.assertIsNone(self.engine.warm_effective_defaults())
            self.assertIsNone(
                self.engine.warm_last_posted_period("US001", "ACTUALS"))
        finally:
            self.engine.db.query = original

    def test_they_return_the_value_once_it_is_known(self) -> None:
        eff = self.engine.effective_defaults()
        self.assertEqual(self.engine.warm_effective_defaults(), eff)
        posted = self.engine.last_posted_period(eff["business_unit"],
                                                eff["ledger"])
        self.assertEqual(
            self.engine.warm_last_posted_period(eff["business_unit"],
                                                eff["ledger"]),
            posted)


if __name__ == "__main__":
    unittest.main()
