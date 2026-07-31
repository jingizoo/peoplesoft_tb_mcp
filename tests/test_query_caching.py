"""Setup-lookup caching: the same answer must not be re-derived every turn.

Every tool call re-resolved the setid, the ledger list, the calendar and the
FX rate before it could touch a single fact. On the sample database those are
free; over a WAN each one is a round trip, and aging issued seventeen of them
before returning a row. The counts below are the contract — they are upper
bounds, so an optimisation that lowers them passes and a regression that
reintroduces a per-call lookup fails.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402


class QueryCountRecorder:
    """Counts statements without changing what they return."""

    def __init__(self, db) -> None:
        self.db = db
        self.sql: list = []
        self._orig = db.query

    def __enter__(self):
        def spy(sql, params=None, max_rows=None):
            self.sql.append(" ".join(str(sql).split()))
            return self._orig(sql, params, max_rows=max_rows)

        self.db.query = spy
        return self

    def __exit__(self, *exc) -> None:
        self.db.query = self._orig

    @property
    def count(self) -> int:
        return len(self.sql)


class SetupLookupCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)
        self.ar = ARBilling(self.engine)

    def tearDown(self) -> None:
        self.db.close()

    def test_aging_stops_repeating_its_setup_lookups(self) -> None:
        with QueryCountRecorder(self.db) as cold:
            self.ar.aging()
        with QueryCountRecorder(self.db) as warm:
            self.ar.aging()
        self.assertLessEqual(
            cold.count, 16,
            f"first aging call issued {cold.count} statements:\n"
            + "\n".join(s[:90] for s in cold.sql))
        # The second call may only re-run the queries that read facts. If this
        # climbs back toward the cold count, a setup lookup has escaped its
        # cache again.
        self.assertLessEqual(
            warm.count, 5,
            f"second aging call re-issued {warm.count} statements:\n"
            + "\n".join(s[:90] for s in warm.sql))

    def test_ledger_and_calendar_lookups_are_answered_once(self) -> None:
        bu = self.engine.cfg.defaults.business_unit
        self.engine.list_ledgers(bu)
        self.engine.resolve_period("")
        self.engine.resolve_setid(bu)
        self.engine.base_currency_for(bu)
        with QueryCountRecorder(self.db) as rec:
            for _ in range(5):
                self.engine.list_ledgers(bu)
                self.engine.resolve_ledger_for(bu, "")
                self.engine.resolve_setid(bu)
                self.engine.base_currency_for(bu)
        self.assertEqual(
            rec.count, 0,
            "repeated setup lookups still hit the database: "
            + "; ".join(s[:70] for s in rec.sql))

    def test_invalidate_clears_every_cache_it_owns(self) -> None:
        # A stale cache that survives a refresh is worse than no cache: the
        # scope chip would report a period the ledger no longer has.
        bu = self.engine.cfg.defaults.business_unit
        self.engine.list_ledgers(bu)
        self.engine.resolve_period("")
        self.engine.invalidate_scope_cache()
        for name in ("_ledgers_cache", "_periods_cache", "_posted_cache",
                     "_rate_cache"):
            self.assertEqual(getattr(self.engine, name), {},
                             f"{name} survived invalidate_scope_cache()")
        with QueryCountRecorder(self.db) as rec:
            self.engine.list_ledgers(bu)
        self.assertGreater(rec.count, 0,
                           "list_ledgers served a cleared cache from memory")


if __name__ == "__main__":
    unittest.main()
