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

    def __init__(self, cfg):
        super().__init__(cfg)
        self.probes: list = []      # per-unit DISTINCTs, which must stay 0
        self.seen: list = []

    def query(self, sql, params=None, **kw):
        self.seen.append(sql)
        # The refusal comes FIRST: the setup-pairs statement names both
        # records, so matching on PS_LED_GRP_TBL before this would answer
        # the very query the fixture exists to refuse.
        if "PS_BUS_UNIT_LED" in sql:
            raise DbError("ORA-00942: table or view does not exist")
        if "PS_BUS_UNIT_TBL_GL" in sql and "DISTINCT LEDGER" not in sql:
            return ([{"business_unit": b} for b in self.UNITS], False)
        if "DISTINCT LEDGER" in sql.upper() and "PS_LEDGER" in sql:
            # The 43-second statement. Recorded, never welcome.
            self.probes.append((params or {}).get("bu"))
            return ([{"ledger": "ACTUALS"}], False)
        if "PS_LED_GRP_TBL" in sql:
            return ([{"ledger": "ACTUALS"}, {"ledger": "BUDGET"}], False)
        if "EXISTS" in sql.upper() and "PS_LEDGER" in sql:
            p = params or {}
            out = [{"bu": p[f"b{i}"], "led": p[f"l{i}"]}
                   for i in range(len(p) // 2)
                   if p.get(f"l{i}") == "ACTUALS"]
            return (out, False)
        return super().query(sql, params, **kw)


class GridProbeTests(unittest.TestCase):
    """The prime suspect: one unbounded DISTINCT per business unit.

    Measured on the real instance: `SELECT DISTINCT LEDGER FROM PS_LEDGER
    WHERE BUSINESS_UNIT = :bu` took 42.8 and 31.8 seconds, and a single AR
    aging call issued 268 statements in 355 seconds — roughly one probe per
    unit plus the real work. DISTINCT cannot short-circuit: it reads every
    ledger row belonging to that unit to return three strings.
    """

    def setUp(self) -> None:
        self.engine = _engine(_SlowProbe)

    def _pairs(self):
        return self.engine.list_financial_scopes(include_activity=False,
                                                 verify_pairs=False)

    def test_the_per_unit_DISTINCT_is_never_issued(self) -> None:
        self._pairs()
        self.assertEqual(self.engine.db.probes, [],
                         "one DISTINCT over the ledger per business unit is "
                         "the query that took 43 seconds on the real box")

    def test_it_probes_the_grid_with_short_circuiting_EXISTS(self) -> None:
        self._pairs()
        exists = [s for s in self.engine.db.seen
                  if "EXISTS" in s.upper() and "PS_LEDGER" in s]
        self.assertTrue(exists, self.engine.db.seen)
        self.assertNotIn("DISTINCT LEDGER", " ".join(exists).upper())

    def test_the_statement_count_stops_scaling_with_units(self) -> None:
        # 40 units used to be 40 statements. Batched EXISTS is one
        # statement per 50 PAIRS, so the count is a small constant here.
        self._pairs()
        ledger_reads = [s for s in self.engine.db.seen if "PS_LEDGER" in s]
        self.assertLess(len(ledger_reads), len(_SlowProbe.UNITS) // 2,
                        f"{len(ledger_reads)} ledger statements for "
                        f"{len(_SlowProbe.UNITS)} units")

    def test_it_says_which_grant_would_make_this_one_query(self) -> None:
        out = self._pairs()
        self.assertEqual(out["source"], "bu-grid-probe")
        self.assertIn("PS_BUS_UNIT_LED", out["note"])

    def test_a_grid_that_proves_empty_falls_through_instead_of_lying(self):
        # A guessed ledger name that matches nothing must NOT be offered
        # for every unit. The setup-derived path keeps its list when
        # everything probes empty; a guess has not earned that.
        kept = self.engine._with_ledger_data([("BU000", "NOPE")],
                                             keep_all_on_empty=False)
        self.assertEqual(kept, [])
        self.assertEqual(
            self.engine._with_ledger_data([("BU000", "NOPE")]),
            [("BU000", "NOPE")],
            "the setup path must still keep an all-empty probe")

    def test_the_boot_prime_never_reaches_the_fallback_at_all(self) -> None:
        # setup_only is load-bearing: this is the site where discovery is
        # slowest, and boot is the worst moment to pay for it.
        out = self.engine.list_financial_scopes(
            include_activity=False, verify_pairs=False, setup_only=True)
        self.assertEqual([s for s in self.engine.db.seen
                          if "PS_LEDGER" in s], [])
        self.assertEqual(out["scopes"], [])
        self.assertEqual(out["source"], "deferred")


class DefaultsTests(unittest.TestCase):
    """Resolving an omitted business unit must not build a catalog.

    This is the path AR aging, close-readiness and every unscoped tool take,
    and it was calling _ledger_scope_pairs() to answer "is the configured
    pair real?" — on the reported instance, 268 statements to confirm one.
    """

    def test_the_configured_pair_is_CONFIRMED_not_rediscovered(self) -> None:
        engine = _engine()
        engine.invalidate_scope_cache()
        seen: list = []
        original = engine.db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        engine.db.query = spy
        out = engine.effective_defaults()
        self.assertEqual(out["business_unit"],
                         engine.cfg.defaults.business_unit)
        self.assertLessEqual(len(seen), 2, seen)
        self.assertFalse(any("DISTINCT" in s.upper() and "PS_LEDGER" in s
                             for s in seen), seen)

    def test_the_ledger_list_never_comes_from_the_balance_table(self) -> None:
        # An incomplete dropdown costs an entry; a DISTINCT over PS_LEDGER
        # costs half a minute on the box that reported this.
        class NoSetup(Database):
            def query(self, sql, params=None, **kw):
                if "PS_BUS_UNIT_LED" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        engine = _engine(NoSetup)
        self.assertEqual(engine._setup_ledgers("US001"), [])
        out = engine.effective_defaults()
        self.assertEqual(out["ledgers"], [engine.cfg.defaults.ledger])

    def test_a_wrong_configured_pair_still_discovers(self) -> None:
        engine = _engine()
        engine.cfg.defaults.business_unit = "NOSUCHBU"
        engine.invalidate_scope_cache()
        out = engine.effective_defaults()
        self.assertNotEqual(out["business_unit"], "NOSUCHBU")
        self.assertTrue(out["discovered"])

    def test_an_unreadable_ledger_does_not_break_the_fast_path(self) -> None:
        class Broken(Database):
            def query(self, sql, params=None, **kw):
                if "PS_LEDGER" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        out = _engine(Broken).effective_defaults()
        self.assertEqual(out["business_unit"],
                         load_config(str(ROOT / "config.yaml")
                                     ).defaults.business_unit)


class AgingPathTests(unittest.TestCase):
    """The reported symptom, end to end.

    On the real instance one `get_ar_aging` call issued 268 statements in
    355 seconds. Aging does not call list_financial_scopes — it resolves an
    omitted business unit through effective_defaults, and THAT was building
    the whole catalog. On a grant-limited site that is one DISTINCT over
    PS_LEDGER per business unit, two of which were timed at 43 and 32
    seconds.
    """

    class _NoGrant(Database):
        def query(self, sql, params=None, **kw):
            if "PS_BUS_UNIT_LED" in sql:
                raise DbError("ORA-00942: table or view does not exist")
            return super().query(sql, params, **kw)

    def _aging_statements(self, db_cls):
        from pstb.ar import ARBilling
        engine = _engine(db_cls)
        seen: list = []
        original = engine.db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        engine.db.query = spy
        ARBilling(engine).aging(business_unit="")
        return seen

    def test_aging_never_asks_the_ledger_which_ledgers_exist(self) -> None:
        for label, cls in (("granted", Database),
                           ("grant-limited", self._NoGrant)):
            stmts = self._aging_statements(cls)
            distincts = [s for s in stmts
                         if "DISTINCT LEDGER" in s.upper()
                         and "PS_LEDGER" in s]
            self.assertEqual(distincts, [], f"{label}: {distincts}")

    def test_a_missing_setup_grant_no_longer_changes_the_cost(self) -> None:
        # It used to be the difference between 3 statements and 268.
        granted = len(self._aging_statements(Database))
        limited = len(self._aging_statements(self._NoGrant))
        self.assertLessEqual(abs(limited - granted), 2,
                             f"granted={granted} limited={limited}")


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
