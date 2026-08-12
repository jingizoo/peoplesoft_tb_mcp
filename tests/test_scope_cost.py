"""What scope discovery costs, and what it says when it costs a lot.

Reported as "list_financial_scopes is taking enormous time". Discovery has
one fast path and three fallbacks, and the fast path needs two setup grants
this repo cannot assume. Without them the catalog is built by running one
`SELECT DISTINCT LEDGER FROM PS_LEDGER WHERE BUSINESS_UNIT = :bu` per unit,
serially, up to 250 times — and none of that appeared anywhere in the
payload, so a minute of waiting looked identical to a slow network.

Four properties, in the order they matter:

  1. The fallback is BOUNDED by a wall clock and says what it covered.
     A budget that silently returned a partial catalog would be the same
     bug with a shorter timer, so the note names the units it reached.
  2. The next call resumes where the last one stopped. A fixed budget over
     a fixed order means the same first units forever and the rest never.
  3. A second call inside the TTL costs nothing. The MCP server is a
     separate process from the GUI, so the GUI's cache never helped a
     model that called this mid-conversation — every call rebuilt.
  4. An access-filtered catalog is never cached and never served from
     cache. It is one user's reach.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import engine as eng  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.security import Access  # noqa: E402


def _engine(db_cls=Database):
    cfg = load_config(str(ROOT / "config.yaml"))
    return TBEngine(db_cls(cfg), cfg)


class _NoSetupGrant(Database):
    """The grant-limited instance: the two setup records are refused."""

    def query(self, sql, params=None, **kw):
        if "PS_BUS_UNIT_LED" in sql or "PS_LED_GRP_TBL" in sql:
            raise DbError("ORA-00942: table or view does not exist")
        return super().query(sql, params, **kw)


class _SlowProbe(_NoSetupGrant):
    """...and every per-unit ledger probe costs real time."""

    UNITS = [f"BU{i:03d}" for i in range(40)]
    SECONDS = 0.05

    def __init__(self, cfg):
        super().__init__(cfg)
        self.probes: list = []
        self.clock = 0.0

    def query(self, sql, params=None, **kw):
        if "PS_BUS_UNIT_TBL_GL" in sql and "DISTINCT LEDGER" not in sql:
            return ([{"business_unit": b} for b in self.UNITS], False)
        if "DISTINCT LEDGER" in sql.upper():
            self.probes.append((params or {}).get("bu"))
            self.clock += self.SECONDS
            return ([{"ledger": "ACTUALS"}], False)
        return super().query(sql, params, **kw)


class FallbackBudgetTests(unittest.TestCase):
    """The prime suspect: a serial per-unit loop with no ceiling."""

    def setUp(self) -> None:
        self.saved = eng.SCOPE_FALLBACK_BUDGET_SECONDS
        self.engine = _engine(_SlowProbe)
        # A monotonic clock the fake probe advances, so the budget is
        # exercised without the test actually sleeping.
        self.real = time.monotonic
        db = self.engine.db
        time.monotonic = lambda: self.real() + db.clock
        self.addCleanup(setattr, time, "monotonic", self.real)
        self.addCleanup(setattr, eng, "SCOPE_FALLBACK_BUDGET_SECONDS",
                        self.saved)

    def test_it_stops_at_the_budget_instead_of_probing_every_unit(self):
        eng.SCOPE_FALLBACK_BUDGET_SECONDS = 0.3      # ~6 probes at 0.05s
        out = self.engine.list_financial_scopes(include_activity=False,
                                                verify_pairs=False)
        probes = len(self.engine.db.probes)
        self.assertGreater(probes, 0, "it must still answer something")
        self.assertLess(probes, len(_SlowProbe.UNITS),
                        "the budget did not bind — this is the unbounded "
                        "loop the report was about")
        self.assertTrue(out["truncated"])

    def test_a_partial_catalog_says_so_in_words(self) -> None:
        eng.SCOPE_FALLBACK_BUDGET_SECONDS = 0.3
        out = self.engine.list_financial_scopes(include_activity=False,
                                                verify_pairs=False)
        self.assertEqual(out["source"], "per-unit-probe")
        self.assertIn("of 40 business units", out["note"])
        self.assertIn("PS_BUS_UNIT_LED", out["note"],
                      "name the grant that would make this one query")

    def test_the_next_call_resumes_where_this_one_stopped(self) -> None:
        # Otherwise the budget guarantees a permanently partial catalog:
        # the same first units every time, the rest never.
        eng.SCOPE_FALLBACK_BUDGET_SECONDS = 0.3
        self.engine.list_financial_scopes(include_activity=False,
                                          verify_pairs=False)
        first = list(self.engine.db.probes)
        self.engine.invalidate_scope_cache()
        self.engine.db.probes.clear()
        self.engine.list_financial_scopes(include_activity=False,
                                          verify_pairs=False)
        second = self.engine.db.probes
        self.assertTrue(second)
        self.assertNotEqual(first[0], second[0])
        self.assertFalse(set(first) >= set(second),
                         "the second pass covered no new units")

    def test_a_generous_budget_still_covers_everything(self) -> None:
        eng.SCOPE_FALLBACK_BUDGET_SECONDS = 3600.0
        out = self.engine.list_financial_scopes(include_activity=False,
                                                verify_pairs=False)
        self.assertEqual(len(self.engine.db.probes), len(_SlowProbe.UNITS))
        self.assertEqual(len(out["scopes"]), len(_SlowProbe.UNITS))

    def test_the_boot_prime_never_reaches_the_fallback_at_all(self) -> None:
        # setup_only is load-bearing: this is the site where the fallback is
        # slowest, and boot is the worst moment to pay it.
        out = self.engine.list_financial_scopes(
            include_activity=False, verify_pairs=False, setup_only=True)
        self.assertEqual(self.engine.db.probes, [])
        self.assertEqual(out["scopes"], [])
        self.assertEqual(out["source"], "deferred")


class CacheTests(unittest.TestCase):
    """The MCP process rebuilt the catalog on every single call."""

    def setUp(self) -> None:
        self.engine = _engine()
        self.seen: list = []
        original = self.engine.db.query

        def spy(sql, params=None, **kw):
            self.seen.append(sql)
            return original(sql, params, **kw)

        self.engine.db.query = spy

    def _build(self, **kw):
        return self.engine.list_financial_scopes(include_activity=False,
                                                 **kw)

    def test_a_second_build_inside_the_ttl_is_free(self) -> None:
        self._build()
        self.seen.clear()
        self._build()
        self.assertEqual(self.seen, [])

    def test_the_flags_are_part_of_the_key(self) -> None:
        # verify_pairs=False and True are different answers; serving one
        # for the other would report unverified pairs as verified.
        self._build(verify_pairs=True)
        self.seen.clear()
        self._build(verify_pairs=False)
        self.assertTrue(self.seen, "a different flag combination reused the "
                                   "cached answer")

    def test_invalidating_really_rebuilds(self) -> None:
        self._build()
        self.engine.invalidate_scope_cache()
        self.seen.clear()
        self._build()
        self.assertTrue(self.seen)

    def test_one_users_narrowed_catalog_is_never_reused(self) -> None:
        restricted = Access(oprid="FIN_US001", units=frozenset({"US001"}))
        self.engine.list_financial_scopes(include_activity=False,
                                          access=restricted)
        self.seen.clear()
        # The next asker must not be served the previous one's reach.
        self.engine.list_financial_scopes(include_activity=False)
        self.assertTrue(self.seen, "an access-filtered build was cached")

    def test_a_restricted_user_never_reads_the_shared_cache(self) -> None:
        self._build()                      # populate as everyone
        self.seen.clear()
        out = self.engine.list_financial_scopes(
            include_activity=False,
            access=Access(oprid="X", units=frozenset({"NOPE"})))
        self.assertEqual(out["scopes"], [],
                         "the shared catalog leaked into a narrowed one")

    def test_the_enrichment_read_stops_repeating(self) -> None:
        self.engine.invalidate_scope_cache()
        self.engine._business_unit_enrichment()
        self.seen.clear()
        self.engine._business_unit_enrichment()
        self.assertEqual(self.seen, [])


class DescribeCacheTests(unittest.TestCase):
    def test_an_absent_record_is_described_once_not_forever(self) -> None:
        db = _engine().db
        db.columns("PS_NOT_A_RECORD_AT_ALL")
        seen: list = []
        original = db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        db.query = spy
        db.columns("PS_NOT_A_RECORD_AT_ALL")
        self.assertEqual(seen, [], "on Oracle this is an ALL_TAB_COLUMNS "
                                   "read per absent record per call")

    def test_a_failed_describe_is_not_remembered_as_absence(self) -> None:
        # A dropped connection is not evidence that a record does not
        # exist, and caching it would hide the record until a restart.
        class Flaky(Database):
            broken = True

            def query(self, sql, params=None, **kw):
                if self.broken and "PS_LEDGER" in str(params or sql):
                    raise DbError("ORA-03113: end-of-file on communication")
                return super().query(sql, params, **kw)

        db = _engine(Flaky).db
        self.assertEqual(db.columns("PS_LEDGER"), set())
        db.broken = False
        self.assertIn("BUSINESS_UNIT", db.columns("PS_LEDGER"))


class DisclosureTests(unittest.TestCase):
    def test_a_verified_build_says_so(self) -> None:
        out = _engine().list_financial_scopes(include_activity=False,
                                              verify_pairs=True)
        self.assertTrue(out["verified"])
        self.assertEqual(out["source"], "setup")

    def test_skipping_verification_is_reported_not_implied(self) -> None:
        # Past the probe cap nothing is verified, and the payload used to
        # look identical to a fully probed one.
        saved = eng.SCOPE_PROBE_CAP
        eng.SCOPE_PROBE_CAP = 1
        try:
            out = _engine().list_financial_scopes(include_activity=False,
                                                  verify_pairs=True)
        finally:
            eng.SCOPE_PROBE_CAP = saved
        self.assertFalse(out["verified"])
        self.assertIn("NOT verified", out["note"])

    def test_an_unverified_build_is_not_labelled_verified(self) -> None:
        out = _engine().list_financial_scopes(include_activity=False,
                                              verify_pairs=False)
        self.assertFalse(out["verified"])

    def test_the_truncation_note_quotes_the_cap_that_actually_cut_it(self):
        from pstb.engine import INTERNAL_ROW_CAP, SCOPE_ROW_CAP
        saved = eng.SCOPE_ROW_CAP
        eng.SCOPE_ROW_CAP = 1
        try:
            out = _engine().list_financial_scopes(include_activity=False,
                                                  verify_pairs=False)
        finally:
            eng.SCOPE_ROW_CAP = saved
        self.assertTrue(out["truncated"])
        # The note quotes the cap that ACTUALLY cut — the patched one here.
        # It used to quote INTERNAL_ROW_CAP, twenty times larger than the
        # limit doing the cutting, so the operator went looking for a
        # 100,000-pair installation they do not have.
        self.assertIn("safety cap of 1 ", out["note"])
        self.assertNotIn(str(INTERNAL_ROW_CAP), out["note"])
        self.assertEqual(SCOPE_ROW_CAP, 5_000, "the real cap moved")


if __name__ == "__main__":
    unittest.main()
