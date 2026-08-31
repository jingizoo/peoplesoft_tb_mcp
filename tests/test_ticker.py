"""The exception ticker: every promise that makes it safe to leave on.

A standing loop against a production reporting account earns its right to
exist through exactly three guarantees -- it cannot spend more than its
budget, nothing row-shaped can outlive a tick, and a dead or stale runner
reads as UNKNOWN rather than as a clean ledger. Every test here holds one
of those lines or the diff semantics that make the feed truthful.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from pstb.db import DbError
from pstb.engine import EngineError
from pstb.ticker import (CheckOutcome, EVENT_KINDS, TickerError,
                         TickerLimits, TickerRunner, TickerStore,
                         reduce_tb_integrity, store_path)

FINGERPRINT = "sha256:" + "0" * 64
ROW_SENTINEL = "SENTINEL JANE DOE"
AMOUNT_SENTINEL = "9876543.21"


def _limits(**overrides):
    return TickerLimits.from_config(None, **overrides)


def _store(root, **limit_overrides):
    return TickerStore(Path(root) / "t.db", source="default",
                       fingerprint=FINGERPRINT,
                       limits=_limits(**limit_overrides))


def _outcome(check_id="tb_integrity:default:US001", status="passed",
             counts=None, fy=2026, per=6, narrowed=False, category=""):
    return CheckOutcome(
        check_id=check_id, source="default", business_unit="US001",
        status=status, counts=dict(counts or {}), fiscal_year=fy,
        period=per, narrowed=narrowed, error_category=category)


class LimitTests(unittest.TestCase):
    def test_every_field_has_a_floor_and_a_ceiling(self):
        """MetadataBuildLimits.validate() skipped its mine_* budgets, so
        a typo of 240,000 probes would have passed. A STANDING loop
        multiplies any such mistake by every tick it survives, so here
        the bounds are enumerated and every field is tested against
        both ends."""
        for name, (floor, ceiling) in TickerLimits._BOUNDS.items():
            with self.subTest(field=name):
                with self.assertRaises(TickerError):
                    _limits(**{name: floor - 1})
                with self.assertRaises(TickerError):
                    _limits(**{name: ceiling + 1})
                _limits(**{name: floor})
                _limits(**{name: ceiling})
        # The loop above derives its expectations from the same table it
        # is testing -- a sabotage that loosened the table would satisfy
        # it perfectly (a sabotage run proved exactly that). These
        # literals are the independent second witness: the values an
        # unattended loop must never be allowed to take, written down
        # where no refactor of _BOUNDS can quietly move them.
        for name, value in (("cadence_minutes", 1),
                            ("cadence_minutes", 10 ** 6),
                            ("max_queries_per_tick", 0),
                            ("max_queries_per_tick", 100_000),
                            ("max_seconds_per_tick", 0),
                            ("max_seconds_per_tick", 86_400),
                            ("history_per_check", 0),
                            ("events_kept", 10 ** 9),
                            ("failure_trip", 0),
                            ("failure_trip", 1_000)):
            with self.subTest(field=name, value=value), \
                    self.assertRaises(TickerError):
                _limits(**{name: value})

    def test_a_non_number_is_refused_with_the_field_named(self):
        with self.assertRaises(TickerError) as caught:
            _limits(cadence_minutes="fast")
        self.assertIn("cadence_minutes", str(caught.exception))

    def test_the_enable_switch_must_be_a_literal_boolean(self):
        """The quoted string "false" is truthy in Python. That must not
        be what turns on a standing database loop."""
        from pstb.config import TickerCfg, _validate_ticker
        cfg = TickerCfg()
        cfg.enabled = "false"
        with self.assertRaises(RuntimeError):
            _validate_ticker(cfg)
        cfg.enabled = 1
        with self.assertRaises(RuntimeError):
            _validate_ticker(cfg)
        cfg.enabled = False
        _validate_ticker(cfg)


class ReductionTests(unittest.TestCase):
    def test_row_payloads_reduce_to_lengths_and_verdicts(self):
        result = {
            "control_status": "exceptions_found", "balanced": False,
            "fiscal_year": 2026, "through_period": 6,
            "issues": ["a", "b"],
            "suspense_balances": [{"account": "1999",
                                   "ending": AMOUNT_SENTINEL}],
            "unposted_journals": [{"journal_id": ROW_SENTINEL,
                                   "oprid": "VP1"}],
            "out_of_balance_journals": [],
            "accounts_missing_definition": [], "checks_incomplete": [],
            "checks_narrowed": ["unposted"],
            "retained_earnings_roll": {"status": "mismatch"},
        }
        status, counts, fy, per, narrowed = reduce_tb_integrity(result)
        self.assertEqual(status, "exceptions_found")
        self.assertEqual((fy, per), (2026, 6))
        self.assertTrue(narrowed)
        self.assertEqual(counts["suspense_accounts"], 1)
        self.assertEqual(counts["unposted_journals"], 1)
        self.assertEqual(counts["re_roll_mismatch"], 1)
        self.assertEqual(counts["unbalanced"], 1)
        for value in counts.values():
            self.assertIsInstance(value, int)

    def test_an_unknown_status_becomes_error_not_a_new_vocabulary(self):
        status, *_ = reduce_tb_integrity({"control_status": "meh"})
        self.assertEqual(status, "error")


class StoreBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-ticker-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_row_value_can_reach_the_file(self):
        """The two-stage rule from qlog: the reducer is one line of
        defence, the write boundary is the second. A check that puts a
        sentinel row value into its counts must be REFUSED, and a clean
        tick's raw file bytes must be free of the sentinels planted in
        its inputs."""
        store = _store(self.root)
        with self.assertRaises(TickerError):
            store.record_tick(
                [_outcome(counts={"journal": ROW_SENTINEL})], [])
        with self.assertRaises(TickerError):
            store.record_tick(
                [_outcome(counts={"JRNL 0001": 2})], [])
        status, counts, fy, per, nar = reduce_tb_integrity({
            "control_status": "exceptions_found",
            "unposted_journals": [{"journal_id": ROW_SENTINEL,
                                   "amount": AMOUNT_SENTINEL}],
            "fiscal_year": 2026, "through_period": 6})
        store.record_tick(
            [_outcome(status=status, counts=counts, fy=fy, per=per)],
            ["tb_integrity:default:US001"])
        raw = store.path.read_bytes()
        self.assertNotIn(ROW_SENTINEL.encode(), raw)
        self.assertNotIn(AMOUNT_SENTINEL.encode(), raw)

    def test_a_boolean_is_not_a_count(self):
        """isinstance(True, int) is True -- the bool trap. A True that
        sneaks in as a count would diff as 1 and print as True."""
        store = _store(self.root)
        with self.assertRaises(TickerError):
            store.record_tick([_outcome(counts={"balanced": True})], [])

    def test_free_error_text_is_classified_never_kept(self):
        store = _store(self.root)
        store.record_tick(
            [_outcome(status="error",
                      category="ORA-01017: invalid username/password")],
            ["tb_integrity:default:US001"])
        raw = store.path.read_bytes()
        self.assertNotIn(b"ORA-01017", raw)
        feed = store.read_feed()
        self.assertEqual(feed["rows"][0]["error_category"], "credentials")

    def test_a_malformed_row_fails_the_whole_read_closed(self):
        store = _store(self.root)
        store.record_tick([_outcome()], ["tb_integrity:default:US001"])
        con = sqlite3.connect(store.path)
        con.execute("UPDATE outcomes SET status='fabricated'")
        con.commit()
        con.close()
        feed = store.read_feed()
        self.assertFalse(feed["readable"])
        self.assertEqual(feed["rows"], [])
        self.assertTrue(feed["stale"])

    def test_a_changed_endpoint_archives_the_baselines_loudly(self):
        """Baselines diffed against a different database would report
        'exception cleared' about data nobody fixed. The old store is
        archived beside itself -- disclosed, never silently rewritten --
        and the first tick of the new store carries baseline_reset."""
        store = _store(self.root)
        store.record_tick([_outcome(status="exceptions_found",
                                    counts={"issues": 3})],
                          ["tb_integrity:default:US001"])
        moved = TickerStore(store.path, source="default",
                            fingerprint="sha256:" + "f" * 64,
                            limits=_limits())
        events = moved.record_tick([_outcome(status="passed")],
                                   ["tb_integrity:default:US001"])
        kinds = [e["kind"] for e in events]
        self.assertIn("baseline_reset", kinds)
        self.assertNotIn("cleared", kinds)
        archived = list(self.root.glob("t.db.superseded-*"))
        self.assertEqual(len(archived), 1)

    def test_history_and_events_are_pruned_to_their_caps(self):
        store = _store(self.root, history_per_check=10, events_kept=50)
        for i in range(30):
            store.record_tick(
                [_outcome(status="exceptions_found",
                          counts={"issues": i})],
                ["tb_integrity:default:US001"],
                tick_at=f"2026-08-01T{i:02d}:00:00+00:00")
        con = sqlite3.connect(store.path)
        history = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        events = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        con.close()
        self.assertLessEqual(history, 10)
        self.assertLessEqual(events, 50)


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-tickdiff-")
        self.store = _store(Path(self.temp.name))
        self.sched = ["tb_integrity:default:US001"]

    def tearDown(self):
        self.temp.cleanup()

    def _kinds(self, *outcome_sequences):
        out = []
        for i, outcome in enumerate(outcome_sequences):
            events = self.store.record_tick(
                [outcome], self.sched,
                tick_at=f"2026-08-0{i + 1}T10:00:00+00:00")
            out.append([e["kind"] for e in events])
        return out

    def test_first_sight_is_a_baseline_not_a_new_exception(self):
        kinds = self._kinds(_outcome(status="exceptions_found",
                                     counts={"issues": 5}))
        self.assertEqual(kinds, [["baseline"]])

    def test_a_metric_moving_up_is_worsened_with_the_delta(self):
        self.store.record_tick(
            [_outcome(status="exceptions_found",
                      counts={"issues": 2, "unposted_journals": 1})],
            self.sched, tick_at="2026-08-01T10:00:00+00:00")
        events = self.store.record_tick(
            [_outcome(status="exceptions_found",
                      counts={"issues": 2, "unposted_journals": 4})],
            self.sched, tick_at="2026-08-01T10:30:00+00:00")
        self.assertEqual(events[0]["kind"], "worsened")
        self.assertEqual(events[0]["metric"], "unposted_journals")
        self.assertEqual((events[0]["before_n"], events[0]["after_n"]),
                         (1, 4))

    def test_cleared_only_from_exceptions_recovered_from_failures(self):
        kinds = self._kinds(
            _outcome(status="exceptions_found", counts={"issues": 1}),
            _outcome(status="passed"),
            _outcome(status="error", category="timeout"),
            _outcome(status="passed"))
        self.assertEqual(kinds[1], ["cleared"])
        self.assertEqual(kinds[2], ["error"])
        self.assertEqual(kinds[3], ["recovered"])

    def test_a_period_roll_is_a_new_baseline_never_a_clearing(self):
        """The monitor script wrote this lesson down: diffed across a
        period roll, every cleared item reads as fixed and every new
        one as a regression."""
        self.store.record_tick(
            [_outcome(status="exceptions_found", counts={"issues": 7},
                      per=6)],
            self.sched, tick_at="2026-08-01T10:00:00+00:00")
        events = self.store.record_tick(
            [_outcome(status="passed", per=7)],
            self.sched, tick_at="2026-09-01T10:00:00+00:00")
        self.assertEqual([e["kind"] for e in events], ["baseline"])

    def test_a_scope_change_suppresses_the_comparison(self):
        """A probe that narrowed to the current period checks LESS than
        it did; its lower numbers must not read as improvement."""
        self.store.record_tick(
            [_outcome(status="exceptions_found",
                      counts={"unposted_journals": 40})],
            self.sched, tick_at="2026-08-01T10:00:00+00:00")
        events = self.store.record_tick(
            [_outcome(status="exceptions_found",
                      counts={"unposted_journals": 2}, narrowed=True)],
            self.sched, tick_at="2026-08-01T10:30:00+00:00")
        self.assertEqual([e["kind"] for e in events], ["scope_changed"])

    def test_a_check_that_stops_being_scheduled_is_an_event(self):
        """The loudest signal there is: a feed that stays green because
        nothing updates it is this feature's stated worst failure."""
        self.store.record_tick([_outcome()], self.sched,
                               tick_at="2026-08-01T10:00:00+00:00")
        events = self.store.record_tick(
            [], [], tick_at="2026-08-01T10:30:00+00:00")
        self.assertEqual([e["kind"] for e in events],
                         ["no_longer_scheduled"])
        feed = self.store.read_feed()
        self.assertTrue(feed["rows"][0]["retired"])

    def test_every_emitted_kind_is_in_the_closed_vocabulary(self):
        self.assertLessEqual(
            {"baseline", "worsened", "improved", "cleared", "recovered",
             "scope_changed", "no_longer_scheduled", "baseline_reset"},
            EVENT_KINDS)


class _StubEngine:
    def __init__(self, result=None, exc=None, veto=None):
        self.result = result or {"control_status": "passed",
                                 "balanced": True,
                                 "fiscal_year": 2026,
                                 "through_period": 6}
        self.exc = exc
        self.veto = veto
        self.integrity_calls = []
        self.db = SimpleNamespace(_credentials_refused="")

    def _require_records_allowed(self, records, *, action):
        if self.veto is not None:
            raise self.veto

    def tb_integrity_check(self, business_unit="", ledger=""):
        self.integrity_calls.append(business_unit)
        if self.exc is not None:
            raise self.exc
        return self.result


def _runner(engine, root, *, units=("US001",), watch_invoicing=False,
            modules=None, **limit_overrides):
    ticker_cfg = SimpleNamespace(
        enabled=True, business_units=list(units), ledger="",
        watch_invoicing=watch_invoicing,
        **{name: limit_overrides.get(name, getattr(TickerLimits, name))
           for name in TickerLimits._BOUNDS})
    cfg = SimpleNamespace(ticker=ticker_cfg,
                          defaults=SimpleNamespace(business_unit="US001"),
                          root=root)
    context = SimpleNamespace(engine=engine, cfg=cfg, source="default",
                              modules=modules)
    return TickerRunner(
        lambda: context,
        store_factory=lambda ctx, limits: TickerStore(
            Path(root) / "t.db", source="default",
            fingerprint=FINGERPRINT, limits=limits))


def _payables(exceptions=(), voucher_count=11, complete=True):
    calls = []

    def open_payables(business_unit="", as_of_date=""):
        calls.append(business_unit)
        return {"pipeline_exceptions": list(exceptions),
                "voucher_count": voucher_count,
                "point_in_time_complete": complete,
                "open_total": float(AMOUNT_SENTINEL),
                "by_vendor": [{"vendor_id": "V1", "vendor": ROW_SENTINEL}]}

    stub = SimpleNamespace(open_payables=open_payables)
    stub.calls = calls
    return stub


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-tickrun-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_the_budget_is_reserved_before_a_check_starts(self):
        """Six units at a worst case of 8 queries each against a budget
        of 16: exactly two may run, and the other four appear in the
        feed as budget-cut rather than silently missing."""
        engine = _StubEngine()
        runner = _runner(engine, self.root,
                         units=[f"BU{i}" for i in range(6)],
                         max_queries_per_tick=16)
        summary = runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
        self.assertTrue(summary["partial"])
        self.assertEqual(len(engine.integrity_calls), 2)
        feed = _store(self.root).read_feed()
        cut = [r for r in feed["rows"] if r["status"] == "not_run"]
        self.assertEqual(len(cut), 4)
        self.assertEqual({r["error_category"] for r in cut}, {"budget"})

    def test_the_operator_veto_stops_the_check_before_the_database(self):
        engine = _StubEngine(
            veto=EngineError("PS_LEDGER is excluded by operator decision"))
        runner = _runner(engine, self.root)
        runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
        self.assertEqual(engine.integrity_calls, [])
        row = _store(self.root).read_feed()["rows"][0]
        self.assertEqual(row["status"], "refused")
        self.assertEqual(row["error_category"], "operator_exclusion")

    def test_refused_credentials_are_terminal_for_the_process(self):
        """Roughly ten retried logons locks the service account for
        every consumer. One credentials failure stops the loop until a
        person restarts the process, with the category on record."""
        engine = _StubEngine(
            exc=DbError("ORA-01017: invalid username/password"))
        runner = _runner(engine, self.root)
        runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
        self.assertEqual(runner.state, "tripped")
        blocked = runner.run_tick_once(now="2026-08-01T10:30:00+00:00")
        self.assertFalse(blocked["ran"])
        self.assertEqual(len(engine.integrity_calls), 1)

    def test_repeated_failure_trips_the_breaker(self):
        engine = _StubEngine()
        runner = _runner(engine, self.root, failure_trip=2)
        with patch.object(TickerStore, "record_tick",
                          side_effect=OSError("disk full")):
            runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
            self.assertEqual(runner.state, "idle")
            runner.run_tick_once(now="2026-08-01T10:30:00+00:00")
        self.assertEqual(runner.state, "tripped")
        self.assertFalse(
            runner.run_tick_once(now="2026-08-01T11:00:00+00:00")["ran"])

    def test_an_overlapping_tick_skips_and_says_so(self):
        """Stacking missed ticks against a production database is a
        retry storm with a schedule."""
        runner = _runner(_StubEngine(), self.root)
        self.assertTrue(runner._tick_lock.acquire(blocking=False))
        try:
            result = runner.run_tick_once()
        finally:
            runner._tick_lock.release()
        self.assertFalse(result["ran"])
        self.assertIn("still running", result["reason"])

    def test_the_engine_is_resolved_on_every_tick(self):
        """The console reload swaps the GUI's module globals; a runner
        that captured them once keeps checking the previous database --
        the documented failure the scope catalog's generation counter
        exists for."""
        first, second = _StubEngine(), _StubEngine()
        engines = iter([first, second])
        holder = {}

        def resolve():
            engine = next(engines)
            holder["cfg"] = _runner(engine, self.root)._resolve().cfg
            return SimpleNamespace(engine=engine, cfg=holder["cfg"],
                                   source="default")

        runner = TickerRunner(
            resolve,
            store_factory=lambda ctx, limits: TickerStore(
                self.root / "t.db", source="default",
                fingerprint=FINGERPRINT, limits=limits))
        runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
        runner.run_tick_once(now="2026-08-01T10:30:00+00:00")
        self.assertEqual(len(first.integrity_calls), 1)
        self.assertEqual(len(second.integrity_calls), 1)


class StalenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-tickstale-")
        self.store = _store(Path(self.temp.name))
        self.store.record_tick([_outcome()],
                               ["tb_integrity:default:US001"],
                               tick_at="2026-08-01T10:00:00+00:00")

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_within_twice_the_cadence(self):
        feed = self.store.read_feed(now="2026-08-01T10:45:00+00:00")
        self.assertFalse(feed["stale"])

    def test_stale_beyond_it(self):
        feed = self.store.read_feed(now="2026-08-01T12:00:00+00:00")
        self.assertTrue(feed["stale"])

    def test_an_unreadable_timestamp_fails_closed_to_stale(self):
        """An old result presented as current is this feature's worst
        failure mode. Corruption must land on the safe side."""
        con = sqlite3.connect(self.store.path)
        con.execute("UPDATE meta SET value=? WHERE key='last_tick'",
                    (json.dumps({"at": "not a timestamp"}),))
        con.commit()
        con.close()
        feed = self.store.read_feed(now="2026-08-01T10:05:00+00:00")
        self.assertTrue(feed["stale"])

    def test_a_tick_from_the_future_is_stale_not_fresh(self):
        """Clock skew makes every age computation untrustworthy; clamped
        to zero it would present precisely the wrong verdict."""
        feed = self.store.read_feed(now="2026-08-01T08:00:00+00:00")
        self.assertTrue(feed["stale"])

    def test_no_tick_at_all_is_stale_not_clean(self):
        with tempfile.TemporaryDirectory() as other:
            feed = _store(Path(other)).read_feed()
            self.assertTrue(feed["stale"])
            self.assertEqual(feed["rows"], [])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        import pstb.gui.app as gui
        self.gui = gui
        self.client = TestClient(gui.app, client=("127.0.0.1", 5555),
                                 base_url="http://localhost")

    def test_disabled_is_a_calm_note_with_no_store_header(self):
        body_response = self.client.get("/api/exceptions")
        self.assertEqual(body_response.status_code, 200)
        self.assertEqual(body_response.headers.get("cache-control"),
                         "no-store, private")
        body = body_response.json()
        self.assertFalse(body["enabled"])
        self.assertTrue(body["stale"])
        self.assertIn("ticker.enabled", body["note"])

    def test_an_unknown_source_is_refused_by_name(self):
        registry = SimpleNamespace(
            names=lambda: ["default", "p2go"],
            resolve_name=lambda s="": (s or "default"))
        with patch.object(self.gui.engine, "registry", registry):
            r = self.client.get("/api/exceptions?source=nosuch")
        self.assertEqual(r.status_code, 404)
        self.assertIn("p2go", r.json()["detail"])

    def test_the_count_endpoint_degrades_never_errors(self):
        r = self.client.get("/api/exceptions/count")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"pending": 0, "readable": False})

    def test_rows_are_narrowed_to_the_callers_units_on_the_way_out(self):
        """The store is shared; the reach is not. A restricted caller
        must not see another unit's tie-out deltas."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from pstb.metadata import source_fingerprint
            # The fingerprint is computed INSIDE the patched context:
            # cfg.root resolves the sample's relative sqlite path, so a
            # fingerprint taken outside it describes a different endpoint
            # and the store would (correctly) archive itself as foreign.
            with patch.object(self.gui.cfg, "root", root):
                fingerprint = source_fingerprint(self.gui.cfg, "default")
            store = TickerStore(
                store_path(root, "default"), source="default",
                fingerprint=fingerprint,
                limits=_limits())
            store.record_tick(
                [_outcome(check_id="tb_integrity:default:US001",
                          status="exceptions_found", counts={"issues": 1}),
                 CheckOutcome(check_id="tb_integrity:default:EU002",
                              source="default", business_unit="EU002",
                              status="exceptions_found",
                              counts={"issues": 2})],
                ["tb_integrity:default:US001",
                 "tb_integrity:default:EU002"])
            access = SimpleNamespace(all_units=False,
                                     allows=lambda bu: bu == "US001")
            # Patch access_for_request -- the binding the handler REALLY
            # uses. The first version of this test patched
            # current_access(), which the unit-free middleware branch
            # never binds, so the test passed while production served
            # every unit's rows to every caller.
            with patch.object(self.gui.cfg.ticker, "enabled", True), \
                    patch.object(self.gui.cfg, "root", root), \
                    patch.object(self.gui, "access_for_request",
                                 lambda request: access):
                body = self.client.get("/api/exceptions").json()
        units = {row["business_unit"] for row in body["rows"]}
        self.assertEqual(units, {"US001"})
        # The events array is the second channel: a worsened-delta about
        # EU002 is EU002's data whichever array carries it.
        event_checks = {e["check_id"] for e in body["events"]}
        self.assertNotIn("tb_integrity:default:EU002", event_checks)

    def test_an_enabled_feed_serves_rows_and_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from pstb.metadata import source_fingerprint
            # The fingerprint is computed INSIDE the patched context:
            # cfg.root resolves the sample's relative sqlite path, so a
            # fingerprint taken outside it describes a different endpoint
            # and the store would (correctly) archive itself as foreign.
            with patch.object(self.gui.cfg, "root", root):
                fingerprint = source_fingerprint(self.gui.cfg, "default")
            store = TickerStore(
                store_path(root, "default"), source="default",
                fingerprint=fingerprint,
                limits=_limits())
            store.record_tick(
                [_outcome(status="exceptions_found",
                          counts={"issues": 2})],
                ["tb_integrity:default:US001"],
                tick_at="2026-08-01T10:00:00+00:00")
            with patch.object(self.gui.cfg.ticker, "enabled", True), \
                    patch.object(self.gui.cfg, "root", root):
                body = self.client.get("/api/exceptions").json()
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["status"], "exceptions_found")
        self.assertTrue(body["stale"])   # that tick is long past
        self.assertIn("counts are bounded", body["counts_note"])


class SynthesisFixTests(unittest.TestCase):
    """Each of these holds a defect the design review found in the first
    build of this feature -- every one verified real before being fixed,
    and every one of the shape this codebase keeps finding: a control
    that looks like it is working."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-tickfix-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_failed_tick_does_not_rebaseline_a_standing_exception(self):
        """A failure outcome knows nothing about fiscal scope. Reported
        as fy=0, the differ read it as a period roll and re-baselined --
        so every outage erased the exception history on both sides."""
        store = _store(self.root)
        sched = ["tb_integrity:default:US001"]
        store.record_tick(
            [_outcome(status="exceptions_found",
                      counts={"issues": 5}, fy=2026, per=6)],
            sched, tick_at="2026-08-01T10:00:00+00:00")
        events = store.record_tick(
            [_outcome(status="error", counts={}, fy=0, per=0,
                      category="timeout")],
            sched, tick_at="2026-08-01T10:30:00+00:00")
        self.assertEqual([e["kind"] for e in events], ["error"])
        events = store.record_tick(
            [_outcome(status="exceptions_found",
                      counts={"issues": 5}, fy=2026, per=6)],
            sched, tick_at="2026-08-01T11:00:00+00:00")
        # Recovery to the SAME standing exception: a status change back,
        # never a fresh baseline and never "5 new issues".
        self.assertEqual([e["kind"] for e in events], ["new_exceptions"])

    def test_a_failure_preserves_the_last_good_counts(self):
        store = _store(self.root)
        sched = ["tb_integrity:default:US001"]
        store.record_tick(
            [_outcome(status="exceptions_found", counts={"issues": 5})],
            sched, tick_at="2026-08-01T10:00:00+00:00")
        store.record_tick(
            [_outcome(status="error", counts={}, fy=0, per=0,
                      category="timeout")],
            sched, tick_at="2026-08-01T10:30:00+00:00")
        row = store.read_feed()["rows"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["counts"], {"issues": 5})

    def test_the_read_path_can_refuse_but_never_archive(self):
        """A GET that renames files on a metadata mismatch is a write
        disguised as a read -- and it ran without the writer's lock."""
        store = _store(self.root)
        store.record_tick([_outcome()], ["tb_integrity:default:US001"])
        reader = TickerStore(store.path, source="default",
                             fingerprint="sha256:" + "f" * 64,
                             limits=_limits())
        feed = reader.read_feed()
        self.assertFalse(feed["readable"])
        self.assertIn("different endpoint", feed["note"])
        self.assertTrue(store.path.exists())
        self.assertEqual(list(self.root.glob("*.superseded-*")), [])

    def test_the_veto_gate_names_every_table_the_check_reads(self):
        """Measured, not recalled: the first gate named two of seven
        tables, and a veto the operator believes but does not have is
        worse than none. This test re-measures against the sample and
        fails when tb_integrity grows a read the gate does not cover."""
        import re as _re
        from pstb.config import Config
        from pstb.db import Database
        from pstb.engine import TBEngine
        from pstb.ticker import TB_INTEGRITY_READS
        seen = set()

        class Audited(Database):
            def query(self, sql, params=None, max_rows=None):
                seen.update(
                    m.upper() for m in _re.findall(
                        r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", sql, _re.I))
                return super().query(sql, params, max_rows)

        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(
            Path(__file__).resolve().parent.parent /
            "sample_data" / "ps_sample.db")
        db = Audited(cfg)
        try:
            TBEngine(db, cfg).tb_integrity_check(business_unit="US001")
        finally:
            db.close()
        measured = {t for t in seen if t.startswith(("PS_", "XX_"))}
        self.assertTrue(measured)
        self.assertLessEqual(measured, set(TB_INTEGRITY_READS))

    def test_a_tick_of_pure_errors_counts_toward_the_breaker(self):
        """The store wrote fine, so the first breaker never saw it: a
        dead database was retried forever, each attempt holding sessions
        up to the query timeout, while the runner reported idle."""
        engine = _StubEngine(exc=EngineError("ORA-12541: no listener"))
        runner = _runner(engine, self.root, failure_trip=2)
        runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
        self.assertEqual(runner.state, "idle")
        self.assertEqual(runner.consecutive_failures, 1)
        runner.run_tick_once(now="2026-08-01T10:30:00+00:00")
        self.assertEqual(runner.state, "tripped")

    def test_a_veto_everywhere_does_not_trip_the_breaker(self):
        """An operator who excluded every checked record chose that
        state; the feed shows refused rows. Tripping on it would punish
        the veto."""
        engine = _StubEngine(veto=EngineError("records are excluded"))
        runner = _runner(engine, self.root, failure_trip=2)
        for i in range(3):
            runner.run_tick_once(now=f"2026-08-01T1{i}:00:00+00:00")
        self.assertEqual(runner.state, "idle")

    def test_the_loop_obeys_a_disable_flipped_mid_flight(self):
        """The console reload swaps config under a running loop; a loop
        that keeps querying after the operator turned it off is a loop
        the operator cannot turn off."""
        engine = _StubEngine()
        runner = _runner(engine, self.root)
        context = runner._resolve()
        context.cfg.ticker.enabled = False
        result = runner.run_tick_once(now="2026-08-01T10:00:00+00:00")
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(engine.integrity_calls, [])

    def test_only_one_runner_thread_per_config_root(self):
        """Nothing stops a second GUI process locally; two tickers on
        one store is double the database load. The loser goes passive:
        it serves the feed and never queries."""
        first = _runner(_StubEngine(), self.root)
        second = _runner(_StubEngine(), self.root)
        first.start()
        try:
            second.start()
            self.assertEqual(second.state, "passive")
            self.assertIsNone(second._thread)
            self.assertIsNotNone(first._thread)
        finally:
            first.stop()
            second.stop()


class InvoicePipelineTests(unittest.TestCase):
    """The second check: stuck vendor invoices, counts only, off unless
    the operator turns it on. The AP surface it deliberately does NOT
    schedule -- the ap_completeness playbook -- is structurally always
    incomplete and calls an external API per run; this one is one
    bounded query against the local database."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-tickap-")
        self.root = Path(self.temp.name)
        self.stuck = [
            {"voucher_id": "VCHR-SENTINEL-1", "vendor_id": "V-SENT",
             "amount": float(AMOUNT_SENTINEL),
             "why": "recycle status"},
            {"voucher_id": "VCHR-SENTINEL-2", "vendor_id": "V-SENT",
             "amount": 12.5, "why": "not posted"},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_the_reducer_keeps_counts_and_nothing_row_shaped(self):
        from pstb.ticker import reduce_open_payables
        status, counts, narrowed = reduce_open_payables({
            "pipeline_exceptions": self.stuck, "voucher_count": 11,
            "point_in_time_complete": True})
        self.assertEqual(status, "exceptions_found")
        self.assertEqual(counts, {"stuck_vouchers": 2, "in_recycle": 1,
                                  "unposted": 1, "result_capped": 0})
        self.assertFalse(narrowed)
        for value in counts.values():
            self.assertIsInstance(value, int)

    def test_open_volume_is_deliberately_not_a_metric(self):
        """The open-voucher total rises with ordinary volume; a
        'worsened' event on every entered voucher is churn that trains
        an operator to stop reading the feed."""
        from pstb.ticker import reduce_open_payables
        _, counts, _ = reduce_open_payables({
            "pipeline_exceptions": [], "voucher_count": 9_999,
            "point_in_time_complete": True})
        self.assertNotIn("open_vouchers", counts)
        self.assertNotIn("voucher_count", counts)

    def test_an_incomplete_answer_is_narrowed_and_a_capped_one_counted(self):
        from pstb.ticker import reduce_open_payables
        _, counts, narrowed = reduce_open_payables({
            "pipeline_exceptions": [], "voucher_count": 10_000,
            "point_in_time_complete": False})
        self.assertTrue(narrowed)
        self.assertEqual(counts["result_capped"], 1)

    def test_off_by_default_the_check_is_not_even_scheduled(self):
        """Two defaults, both off: an explicit False, and -- the case
        the first sabotage run proved this test was blind to -- a config
        that predates the field entirely. getattr's fallback IS the
        default for every deployment that never edited its config, so
        the absent-attribute case is the one that guards real installs."""
        engine = _StubEngine()
        runner = _runner(engine, self.root, modules=_payables())
        runner.run_tick_once(now="2026-08-30T10:00:00+00:00")
        rows = _store(self.root).read_feed()["rows"]
        self.assertEqual([r["check_id"] for r in rows],
                         ["tb_integrity:default:US001"])
        delattr(runner._resolve().cfg.ticker, "watch_invoicing")
        runner.run_tick_once(now="2026-08-30T10:30:00+00:00")
        rows = _store(self.root).read_feed()["rows"]
        self.assertEqual([r["check_id"] for r in rows],
                         ["tb_integrity:default:US001"])

    def test_no_row_value_survives_the_tick(self):
        """Sentinels planted in every field the payload could leak from
        -- voucher id, vendor name, the open total -- and the raw store
        bytes must be free of all of them after a full tick."""
        engine = _StubEngine()
        modules = _payables(exceptions=self.stuck)
        runner = _runner(engine, self.root, watch_invoicing=True,
                         modules=modules)
        runner.run_tick_once(now="2026-08-30T10:00:00+00:00")
        self.assertEqual(modules.calls, ["US001"])
        raw = (self.root / "t.db").read_bytes()
        self.assertNotIn(b"VCHR-SENTINEL", raw)
        self.assertNotIn(ROW_SENTINEL.encode(), raw)
        self.assertNotIn(AMOUNT_SENTINEL.encode(), raw)
        row = next(r for r in _store(self.root).read_feed()["rows"]
                   if r["check_id"].startswith("ap_pipeline"))
        self.assertEqual(row["counts"]["stuck_vouchers"], 2)

    def test_the_budget_reserves_the_second_checks_cost_too(self):
        """tb costs 8 and fits an 8-query budget exactly; the AP check's
        4 must then be refused BEFORE it runs, and the row says budget."""
        engine = _StubEngine()
        modules = _payables()
        runner = _runner(engine, self.root, watch_invoicing=True,
                         modules=modules, max_queries_per_tick=8)
        summary = runner.run_tick_once(now="2026-08-30T10:00:00+00:00")
        self.assertTrue(summary["partial"])
        self.assertEqual(modules.calls, [])
        row = next(r for r in _store(self.root).read_feed()["rows"]
                   if r["check_id"].startswith("ap_pipeline"))
        self.assertEqual(row["status"], "not_run")
        self.assertEqual(row["error_category"], "budget")

    def test_the_operator_veto_stops_it_before_the_database(self):
        engine = _StubEngine(
            veto=EngineError("PS_VOUCHER is excluded by operator decision"))
        modules = _payables()
        runner = _runner(engine, self.root, watch_invoicing=True,
                         modules=modules)
        runner.run_tick_once(now="2026-08-30T10:00:00+00:00")
        self.assertEqual(modules.calls, [])
        row = next(r for r in _store(self.root).read_feed()["rows"]
                   if r["check_id"].startswith("ap_pipeline"))
        self.assertEqual(row["status"], "refused")
        self.assertEqual(row["error_category"], "operator_exclusion")

    def test_a_context_without_the_module_pack_is_an_error_row_not_a_crash(self):
        """The attribute is deleted outright, not set to None: a bare
        attribute access on that context RAISES, and only the getattr
        guard turns it into an error row. modules=None reaches the same
        row through the generic failure classifier, which is why the
        first sabotage run found this test toothless against removing
        the guard."""
        engine = _StubEngine()
        runner = _runner(engine, self.root, watch_invoicing=True,
                         modules=None)
        delattr(runner._resolve(), "modules")
        summary = runner.run_tick_once(now="2026-08-30T10:00:00+00:00")
        self.assertTrue(summary["ran"])
        row = next(r for r in _store(self.root).read_feed()["rows"]
                   if r["check_id"].startswith("ap_pipeline"))
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_category"], "tool_error")

    def test_flipping_the_flag_off_is_an_event_not_a_silence(self):
        engine = _StubEngine()
        runner = _runner(engine, self.root, watch_invoicing=True,
                         modules=_payables())
        runner.run_tick_once(now="2026-08-30T10:00:00+00:00")
        runner._resolve().cfg.ticker.watch_invoicing = False
        runner.run_tick_once(now="2026-08-30T10:30:00+00:00")
        feed = _store(self.root).read_feed()
        ap_row = next(r for r in feed["rows"]
                      if r["check_id"].startswith("ap_pipeline"))
        self.assertTrue(ap_row["retired"])
        kinds = {(e["kind"], e["check_id"]) for e in feed["events"]}
        self.assertIn(("no_longer_scheduled",
                       "ap_pipeline:default:US001"), kinds)

    def test_the_veto_gate_names_every_table_the_check_may_read(self):
        """Measured, not recalled -- the tb gate's lesson. The declared
        list is a SUPERSET: PS_PYMNT_VCHR_XREF is read only when the
        voucher table has no CLOSE_STATUS, and a gate that names what a
        check MIGHT read is the only one an operator can rely on."""
        import re as _re
        from pstb.config import Config
        from pstb.db import Database
        from pstb.engine import TBEngine
        from pstb.modules import ModulePacks
        from pstb.ticker import AP_PIPELINE_READS
        seen = set()

        class Audited(Database):
            def query(self, sql, params=None, max_rows=None):
                seen.update(
                    m.upper() for m in _re.findall(
                        r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", sql, _re.I))
                return super().query(sql, params, max_rows)

        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(
            Path(__file__).resolve().parent.parent /
            "sample_data" / "ps_sample.db")
        db = Audited(cfg)
        try:
            ModulePacks(TBEngine(db, cfg)).open_payables(
                business_unit="US001")
        finally:
            db.close()
        measured = {t for t in seen if t.startswith(("PS_", "XX_"))}
        self.assertTrue(measured)
        self.assertLessEqual(measured, set(AP_PIPELINE_READS))

    def test_the_check_never_touches_an_external_system(self):
        """ap_completeness calls the Coupa API on every run, which is
        why it must never be scheduled. Asserted on the module's ACTUAL
        imports and calls, not a raw grep -- the first version failed on
        its own explanatory comment, the exact toothless-by-overreach
        shape a guard must not have."""
        import ast as _ast
        import pstb.ticker as ticker_module
        source = Path(ticker_module.__file__).read_text()
        tree = _ast.parse(source)
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, _ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        for module in imported:
            for banned in ("coupa", "requests", "httpx", "urllib"):
                self.assertNotIn(banned, str(module).lower(), module)
        self.assertNotIn(".coupa", source.lower())

    def test_the_invoicing_flag_must_be_a_literal_boolean(self):
        from pstb.config import TickerCfg, _validate_ticker
        cfg = TickerCfg()
        cfg.watch_invoicing = "true"
        with self.assertRaises(RuntimeError):
            _validate_ticker(cfg)
        cfg.watch_invoicing = True
        _validate_ticker(cfg)


if __name__ == "__main__":
    unittest.main()
