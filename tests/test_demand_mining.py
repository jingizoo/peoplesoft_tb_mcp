"""Demand steers the miner's working set -- as a hint, never a hijack.

Phase 6 closes the flywheel's last open loop: failed questions already
become demand terms (#173), and the miner already spends a bounded probe
budget (#174) -- but the two never met, so 40 of ~8,500 live tables were
measured with zero input from 257 logged failures.

The first build of this feature was convicted by its own design review,
with the headline verified live: on a schema full of *_DATE columns, the
substring matcher credited seven ARBITRARY noise tables (chosen by
node-id lottery) the same as the demanded one. These tests hold the
repaired promises: matching is precision-first over ELIGIBLE tables only
and a too-generic term credits nobody; no term text (which can carry a
party name) ever enters the artifact OR the SQL layer; the boost is
integer basis points capped below one organic score component; behavior
flows through memory all-or-nothing so a half-failed persist cannot
half-steer; and every build carries exactly one demand note, whatever
happened.
"""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pstb import metadata as metadata_module
from pstb.config import Config
from pstb.db import Database
from pstb.metadata import (DEMAND_BOOST_CAP_BP, DEMAND_BOOST_PER_HIT_BP,
                           MetadataBuildLimits, MetadataError,
                           _collect_demand_signal, _collect_value_joins,
                           build_catalog)

PARTY_TERM = "ridgeline pharmaceuticals"


def _turn(question, *, failed=True, source="default"):
    return json.dumps({"type": "turn", "turn_id": uuid.uuid4().hex,
                       "failed": failed, "source_database": source,
                       "question": question})


def _seed(root, script, *, empty_tables=()):
    dbp = Path(root) / "p.db"
    con = sqlite3.connect(dbp)
    con.executescript(script)
    for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        if row[0] in empty_tables:
            continue
        cols = con.execute(f"PRAGMA table_info({row[0]})").fetchall()
        for i in range(25):
            con.execute(
                f"INSERT INTO {row[0]} VALUES "
                f"({','.join(['?'] * len(cols))})",
                tuple(f"{c[1]}{i}" for c in cols))
    con.commit()
    con.close()
    return dbp


def _log(root, questions, *, torn_tail=False):
    log = Path(root) / "questions.jsonl"
    lines = list(questions)
    if torn_tail:
        lines.append('{"type": "turn", "turn_id": "torn')
    log.write_text("\n".join(lines) + "\n")
    return str(log)


def _build(root, script, *, questions=(), torn_tail=False,
           question_log=None, empty_tables=(), **limit_overrides):
    dbp = _seed(root, script, empty_tables=empty_tables)
    log_path = ""
    if questions or torn_tail or question_log is not None:
        log_path = _log(root, questions, torn_tail=torn_tail)
        if question_log is not None:
            log_path = question_log
    cfg = Config.sample(root)
    cfg.db.sqlite_path = str(dbp)
    cfg.sources = {}
    db = Database(cfg)
    try:
        limits = MetadataBuildLimits(**limit_overrides) \
            if limit_overrides else None
        build_catalog(Path(root) / "c.db", [("default", db)],
                      limits=limits, peopletools_source="default",
                      question_log=log_path)
    finally:
        db.close()
    con = sqlite3.connect(Path(root) / "c.db")
    con.row_factory = sqlite3.Row
    return con


def _hits(con):
    return {row["name"]: json.loads(row["components"]).get("demand_hits")
            for row in con.execute(
                "SELECT name, components FROM object_profiles")}


def _notes(con, layer="demand_signal"):
    return [row[0] for row in con.execute(
        "SELECT note FROM notes WHERE layer=?", (layer,))]


class _FakeState:
    def __init__(self, con, limits):
        self.con = con
        self.limits = limits
        self.demand_hits = {}
        self.edges = []
        self.notes = []

    def edge(self, *args, **kwargs):
        self.edges.append((args, kwargs))

    def note(self, *args, **kwargs):
        self.notes.append((args, kwargs))

    def limit(self, *args, **kwargs):
        self.notes.append(("limit", args, kwargs))


# The alias fixture: a view teaching SUPPLIER_NAME makes TU_Q2 reachable
# through the exact-alias route, so demand can fire in tests without
# relying on lucky table names.
ALIAS_SCRIPT = """
    CREATE TABLE TU_Q2 (C1 TEXT, C7 TEXT);
    CREATE VIEW VENDOR_NAMES AS SELECT C7 AS SUPPLIER_NAME FROM TU_Q2;
"""
ALIAS_QUESTIONS = ("supplier name wrong on invoice",
                   "supplier name missing", "bad supplier name again")


class LimitBackfillTests(unittest.TestCase):
    def test_every_mine_budget_now_has_a_floor_and_a_ceiling(self):
        """The recorded hole, finally closed: validate() skipped every
        mine_* field, so a config typo of 240,000 probes passed."""
        for name, floor, ceiling in (
            ("mine_max_tables", 1, 500),
            ("mine_max_pairs", 1, 2_000),
            ("mine_sample_rows", 1, 1_000),
            ("mine_max_probes", 1, 5_000),
            ("mine_demand_terms", 0, 50),
        ):
            with self.subTest(field=name):
                with self.assertRaises(MetadataError):
                    MetadataBuildLimits(**{name: floor - 1}).validate()
                with self.assertRaises(MetadataError):
                    MetadataBuildLimits(**{name: ceiling + 1}).validate()
                MetadataBuildLimits(**{name: floor}).validate()
                MetadataBuildLimits(**{name: ceiling}).validate()

    def test_zero_demand_terms_means_off_and_is_legal(self):
        MetadataBuildLimits(mine_demand_terms=0).validate()

    def test_config_yaml_reaches_the_demand_knob(self):
        """Through _apply_section, the path config.yaml actually takes:
        direct setattr works on any instance and proves nothing."""
        from pstb.config import MetadataCatalogCfg, _apply_section
        cfg = MetadataCatalogCfg()
        _apply_section(cfg, {"mine_demand_terms": 7})
        self.assertEqual(
            MetadataBuildLimits.from_config(cfg).mine_demand_terms, 7)


class PrecisionMatchTests(unittest.TestCase):
    """The demonstrated failure IS the fixture: *_DATE noise columns
    everywhere, an EMPTY name-alike, a view -- and only whole-segment,
    alias, or exact-name targets may be credited."""

    SCRIPT = "\n".join(
        ["CREATE TABLE TU_VOUCHER_DATE (VOUCHER_ID TEXT, DUE_DATE TEXT);"]
        + [f"CREATE TABLE TU_NOISE{i:02d} (K TEXT, PAY_DATE TEXT);"
           for i in range(12)]
        # the name-alike differs in signature ON PURPOSE: an identical
        # copy is excluded as a shadow before liveness is even asked, and
        # a fixture double-guarded like that proved the empty-table rule
        # untestable in the sabotage run.
        + ["CREATE TABLE TU_VOUCHER_DATE_OLD "
           "(VOUCHER_ID TEXT, DUE_DATE TEXT, ARCHIVE_TAG TEXT);",
           "CREATE VIEW VOUCHER_DATE_V AS "
           "SELECT VOUCHER_ID FROM TU_VOUCHER_DATE;"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-precision-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_hits_land_on_the_demanded_object_only(self):
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn("voucher date missing on invoices")
                                for _ in range(3)],
                     torn_tail=True,
                     empty_tables=("TU_VOUCHER_DATE_OLD",))
        hits = _hits(con)
        con.close()
        self.assertGreaterEqual(hits["TU_VOUCHER_DATE"] or 0, 1)
        for name, value in hits.items():
            if name != "TU_VOUCHER_DATE":
                self.assertIsNone(
                    value, f"{name} was credited: the substring lottery "
                           "is back")

    def test_an_empty_name_alike_is_never_credited(self):
        """76,044 of 84,532 real-box tables are measured EMPTY; credit
        landing on them is structurally inert and steals the signal."""
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn("voucher date wrong")],
                     empty_tables=("TU_VOUCHER_DATE_OLD",))
        hits = _hits(con)
        con.close()
        self.assertIsNone(hits["TU_VOUCHER_DATE_OLD"])
        self.assertIsNone(hits.get("VOUCHER_DATE_V"))

    def test_generic_term_credits_nobody(self):
        """A term matching more than 8 eligible tables is too generic to
        mean anything and contributes NOTHING -- the first build credited
        an arbitrary eight chosen by node-id order."""
        script = "\n".join(
            f"CREATE TABLE STOCK_ITEM_{i:02d} (K TEXT, V TEXT);"
            for i in range(9))
        with tempfile.TemporaryDirectory() as tmp:
            con = _build(Path(tmp), script,
                         questions=[_turn("stock item counts wrong")
                                    for _ in range(3)])
            hits = _hits(con)
            note = " ".join(_notes(con))
            con.close()
        self.assertEqual(set(hits.values()), {None})
        self.assertIn("none matched an eligible table", note)

    def test_alias_route_sees_view_taught_vocabulary(self):
        """The demand slot sits AFTER the view harvest so the alias
        route can see what a view author wrote down -- including
        underscore-vs-space normalisation, because SUPPLIER_NAME taught
        by a view and 'supplier name' asked by a person are the same
        words."""
        with tempfile.TemporaryDirectory() as tmp:
            con = _build(Path(tmp), ALIAS_SCRIPT,
                         questions=[_turn(q) for q in ALIAS_QUESTIONS])
            hits = _hits(con)
            con.close()
        self.assertGreaterEqual(hits["TU_Q2"] or 0, 1)


class PrivacyTests(unittest.TestCase):
    # The view-taught alias makes "rebate accrual" MATCH: the persist
    # loop must actually run with party terms in scope, or every seal
    # below is tested against dead code.
    SCRIPT = """
        CREATE TABLE TU_REBATE_HDR (INV_NO TEXT, AMT TEXT);
        CREATE VIEW REBATES AS SELECT AMT AS REBATE_ACCRUAL
          FROM TU_REBATE_HDR;
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-privacy-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_term_text_ever_enters_the_artifact(self):
        con = _build(self.root, self.SCRIPT, questions=[
            _turn(f"rebate accrual for {PARTY_TERM}")])
        self.assertTrue(any(_hits(con).values()))   # persist really ran
        con.close()
        raw = (self.root / "c.db").read_bytes().lower()
        # "rebate accrual" itself is legitimately present: the VIEW
        # teaches it as schema vocabulary. The seal is on QUESTION
        # prose -- the party name -- and on the differential sweep
        # below, which cancels schema-taught bytes against a control.
        self.assertNotIn(b"ridgeline", raw)
        self.assertNotIn(b"pharmaceuticals", raw)

    def test_differential_privacy_sweep(self):
        """Format-level seal: every mined term, at the widest knob, on a
        party-name-rich log -- the set of terms present only in the
        demand build must be empty against a no-demand control."""
        from pstb import demand as demand_module
        from pstb import qlog_report
        questions = [
            _turn(f"rebate accrual for {PARTY_TERM}"),
            _turn("acme master agreement totals missing"),
            _turn(f"invoice INV_88374 for {PARTY_TERM} unmatched"),
        ]
        con = _build(self.root, self.SCRIPT, questions=questions,
                     mine_demand_terms=50)
        con.close()
        demand_bytes = (self.root / "c.db").read_bytes().lower()
        with tempfile.TemporaryDirectory() as other:
            control = _build(Path(other), self.SCRIPT)
            control.close()
            control_bytes = (Path(other) / "c.db").read_bytes().lower()
        turns, _ = qlog_report.load(self.root / "questions.jsonl")
        terms = demand_module.failed_question_terms(
            turns, source="default", max_terms=50)
        self.assertTrue(terms)   # the sweep must actually sweep terms
        leaked = [entry["term"] for entry in terms
                  if entry["term"].encode() in demand_bytes
                  and entry["term"].encode() not in control_bytes]
        self.assertEqual(leaked, [])

    def test_terms_never_reach_the_sql_layer(self):
        """Matching is dict lookups over preloaded maps -- no per-term
        query, LIKE, or bound value. A trace callback records every
        statement the collector runs; the distinctive term must appear
        in none of them."""
        con = _build(self.root, self.SCRIPT)
        statements = []
        con.set_trace_callback(statements.append)
        state = _FakeState(con, MetadataBuildLimits())
        log = _log(self.root, [_turn("zqxqv rebate totals wrong")
                               for _ in range(3)])
        _collect_demand_signal(state, "default", log)
        con.set_trace_callback(None)
        con.close()
        self.assertTrue(statements)
        for sql in statements:
            self.assertNotIn("ZQXQV", sql.upper())

    def test_notes_carry_type_names_and_basenames_never_paths(self):
        import pstb.qlog_report as qlog_report
        with patch.object(qlog_report, "load",
                          side_effect=OSError("/secret/host/path/q.jsonl")):
            con = _build(self.root, self.SCRIPT,
                         questions=[_turn("rebate totals")])
        note = " ".join(_notes(con))
        con.close()
        self.assertIn("OSError", note)
        self.assertNotIn("/secret", note)


class NoteTaxonomyTests(unittest.TestCase):
    SCRIPT = "CREATE TABLE TU_X (A TEXT);"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-notes-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_every_build_carries_exactly_one_demand_note(self):
        """The silent branch is deleted: unconfigured, disabled, missing,
        empty, and applied are all DISCLOSED states now, one note each."""
        con = _build(self.root, self.SCRIPT)
        notes = _notes(con)
        con.close()
        self.assertEqual(len(notes), 1)
        self.assertIn("no question log is configured", notes[0])

    def test_the_off_switch_notes_even_with_a_log_present(self):
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn("rebate totals")],
                     mine_demand_terms=0)
        hits = _hits(con)
        notes = _notes(con)
        con.close()
        self.assertEqual(set(hits.values()), {None})
        self.assertEqual(len(notes), 1)
        self.assertIn("switched off", notes[0])

    def test_absent_log_is_not_reported_as_no_failures(self):
        """The first build fed a missing path to the loader, got zero
        turns back, and reported a path typo as 'no failed questions are
        logged' -- a misdiagnosis pointing away from the fix."""
        con = _build(self.root, self.SCRIPT,
                     question_log=str(self.root / "nope.jsonl"))
        notes = _notes(con)
        con.close()
        self.assertEqual(len(notes), 1)
        self.assertIn("no question log exists at nope.jsonl", notes[0])
        self.assertNotIn("none are failed", notes[0])

    def test_an_empty_log_reports_turn_counts(self):
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn("all good", failed=False)])
        notes = _notes(con)
        con.close()
        self.assertIn("none are failed turns for this source",
                      " ".join(notes))

    def test_a_torn_tail_never_kills_the_build(self):
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn("rebate totals")], torn_tail=True)
        notes = _notes(con)
        con.close()
        self.assertEqual(len(notes), 1)

    def test_another_sources_failures_do_not_count_here(self):
        con = _build(self.root, self.SCRIPT, questions=[
            _turn("rebate accrual missing", source="p2go")])
        hits = _hits(con)
        notes = _notes(con)
        con.close()
        self.assertEqual(set(hits.values()), {None})
        self.assertIn("none are failed turns for this source",
                      " ".join(notes))

    def test_an_unreadable_log_never_degrades_the_snapshot(self):
        """status=unavailable, never partial: partial marks the WHOLE
        snapshot degraded and would stamp every product answer over a
        missing telemetry file."""
        with tempfile.TemporaryDirectory() as other:
            control = _build(Path(other), self.SCRIPT)
            control_partial = control.execute(
                "SELECT value FROM meta WHERE key='partial'").fetchone()[0]
            control.close()
        import pstb.qlog_report as qlog_report
        with patch.object(qlog_report, "load",
                          side_effect=OSError("disk gone")):
            con = _build(self.root, self.SCRIPT,
                         questions=[_turn("rebate totals")])
        row = con.execute(
            "SELECT status, partial FROM notes "
            "WHERE layer='demand_signal'").fetchone()
        broken_partial = con.execute(
            "SELECT value FROM meta WHERE key='partial'").fetchone()[0]
        con.close()
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["partial"], 0)
        self.assertEqual(broken_partial, control_partial)

    def test_value_score_is_never_rewritten_by_demand(self):
        with_demand = _build(self.root, ALIAS_SCRIPT,
                             questions=[_turn(q) for q in ALIAS_QUESTIONS])
        scores_with = {row["name"]: row["value_score"]
                       for row in with_demand.execute(
                           "SELECT name, value_score FROM object_profiles")}
        boosted = _hits(with_demand)
        with_demand.close()
        self.assertTrue(any(boosted.values()))   # demand really fired
        with tempfile.TemporaryDirectory() as other:
            without = _build(Path(other), ALIAS_SCRIPT)
            scores_without = {row["name"]: row["value_score"]
                              for row in without.execute(
                                  "SELECT name, value_score "
                                  "FROM object_profiles")}
            without.close()
        self.assertEqual(scores_with, scores_without)

    def test_components_updates_stay_sorted(self):
        """sort_keys on the rewritten components JSON: the artifact diffs
        cleanly build-over-build instead of churning on dict order."""
        con = _build(self.root, ALIAS_SCRIPT,
                     questions=[_turn(q) for q in ALIAS_QUESTIONS])
        raw = con.execute(
            "SELECT components FROM object_profiles WHERE name='TU_Q2'"
        ).fetchone()[0]
        con.close()
        self.assertIn("demand_hits", json.loads(raw))
        self.assertEqual(raw, json.dumps(json.loads(raw), sort_keys=True))


class AllOrNothingTests(unittest.TestCase):
    """journal_mode=OFF has no rollback: behavior must flow through
    memory, so a persist loop that dies midway steers NOTHING while the
    note says exactly that."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-atomic-")
        self.root = Path(self.temp.name)
        script = """
            CREATE TABLE TU_Q2 (C1 TEXT, C7 TEXT);
            CREATE TABLE TU_Z9 (C1 TEXT, C8 TEXT);
            CREATE VIEW VENDOR_NAMES AS SELECT C7 AS SUPPLIER_NAME
              FROM TU_Q2;
            CREATE VIEW ORDER_NAMES AS SELECT C8 AS PURCHASE_ORDER
              FROM TU_Z9;
        """
        dbp = _seed(self.root, script)
        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(dbp)
        cfg.sources = {}
        db = Database(cfg)
        try:
            build_catalog(self.root / "c.db", [("default", db)],
                          peopletools_source="default")
        finally:
            db.close()
        self.con = sqlite3.connect(self.root / "c.db")
        self.con.row_factory = sqlite3.Row
        self.log = _log(self.root, [
            _turn("supplier name wrong"), _turn("supplier name missing"),
            _turn("supplier name again"), _turn("purchase order stuck"),
            _turn("purchase order missing"), _turn("purchase order bad")])

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def test_partial_persist_cannot_half_boost(self):
        class FailingCon:
            def __init__(self, inner):
                self._inner = inner
                self._updates = 0

            def execute(self, sql, *args):
                if "SET components" in sql:
                    self._updates += 1
                    if self._updates >= 2:
                        raise sqlite3.OperationalError("disk I/O error")
                return self._inner.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        state = _FakeState(FailingCon(self.con), MetadataBuildLimits())
        result = _collect_demand_signal(state, "default", self.log)
        self.assertEqual(result, "unrecorded")
        self.assertEqual(state.demand_hits, {})
        note = " ".join(str(n) for n in state.notes)
        self.assertIn("could not be recorded", note)
        # and the miner, fed that state, steers nothing
        def spy(tables, **kwargs):
            return []

        with patch.object(metadata_module.relmine, "candidate_pairs", spy):
            _collect_value_joins(state, "default",
                                 SimpleNamespace(dialect="sqlite"))
        steering = [n for n in state.notes
                    if "demand steering" in str(n)]
        self.assertEqual(steering, [])

    def test_disclosure_lands_before_behavior(self):
        """state.demand_hits is assigned LAST: if the applied-note write
        itself fails, the outer guard fires with an empty dict and the
        disclosure can never contradict the behavior."""
        class NoteFails(_FakeState):
            def note(self, *args, **kwargs):
                raise RuntimeError("note write failed")

        state = NoteFails(self.con, MetadataBuildLimits())
        with self.assertRaises(RuntimeError):
            _collect_demand_signal(state, "default", self.log)
        self.assertEqual(state.demand_hits, {})


class WorkingSetTests(unittest.TestCase):
    SCRIPT = """
        CREATE TABLE TU_AAA (K TEXT, V TEXT);
        CREATE TABLE TU_BBB (K TEXT, V TEXT);
        CREATE TABLE TU_CCC (K TEXT, V TEXT);
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-select-")
        self.root = Path(self.temp.name)
        self.con = _build(self.root, self.SCRIPT)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def _doctor(self, scores, shadow=None):
        for name, score in scores.items():
            self.con.execute(
                "UPDATE object_profiles SET value_score=? WHERE name=?",
                (score, name))
        if shadow:
            ids = self._ids()
            other = next(n for n in ids if n != shadow)
            self.con.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
                (ids[shadow], ids[other], "shadow_of", "likely", "test",
                 "profiling", "derived", "2026-01-01T00:00:00", "{}"))
        self.con.commit()

    def _ids(self):
        return {row["name"]: row["node_id"] for row in self.con.execute(
            "SELECT name, node_id FROM object_profiles")}

    def _selected(self, hits=None, mine_max_tables=2):
        captured = {}

        def spy(tables, **kwargs):
            captured["tables"] = tables
            return []

        limits = MetadataBuildLimits(mine_max_tables=mine_max_tables)
        state = _FakeState(self.con, limits)
        state.demand_hits = dict(hits or {})
        with patch.object(metadata_module.relmine,
                          "candidate_pairs", spy):
            _collect_value_joins(state, "default",
                                 SimpleNamespace(dialect="sqlite"))
        return ([t["name"] for t in captured.get("tables", [])],
                captured.get("tables", []), state)

    def test_demand_lifts_a_marginal_table_into_the_set(self):
        """Integer basis points make the engineered tie EXACT: 0.40 plus
        two hits is 4000 + 1000 = 5000bp, tying 0.50 to the digit -- the
        float constants this replaced landed on 0.5000000000000001 and
        the tie was unreachable."""
        self._doctor({"TU_AAA": 0.50, "TU_BBB": 0.48, "TU_CCC": 0.40})
        ids = self._ids()
        names, _, _ = self._selected(hits={ids["TU_CCC"]: 2})
        self.assertEqual(names, ["TU_AAA", "TU_CCC"])

    def test_the_boost_is_capped_so_measurement_still_rules(self):
        """Fifty hits still cap at 1500bp -- 0.15 on a 0-1 score, below
        the smallest organic component (0.25) -- so an unbridgeable
        0.16 gap stays unbridgeable however loud the demand."""
        self._doctor({"TU_AAA": 0.70, "TU_BBB": 0.60, "TU_CCC": 0.44})
        ids = self._ids()
        names, _, _ = self._selected(hits={ids["TU_CCC"]: 50})
        self.assertNotIn("TU_CCC", names)
        self.assertEqual(min(50 * DEMAND_BOOST_PER_HIT_BP,
                             DEMAND_BOOST_CAP_BP), 1500)

    def test_a_shadow_no_longer_consumes_a_working_set_slot(self):
        self._doctor({"TU_AAA": 0.90, "TU_BBB": 0.50, "TU_CCC": 0.40},
                     shadow="TU_AAA")
        names, _, _ = self._selected()
        self.assertEqual(len(names), 2)
        self.assertNotIn("TU_AAA", names)

    def test_demand_cannot_resurrect_a_shadow(self):
        self._doctor({"TU_AAA": 0.90, "TU_BBB": 0.50, "TU_CCC": 0.40},
                     shadow="TU_AAA")
        ids = self._ids()
        names, _, _ = self._selected(hits={ids["TU_AAA"]: 50})
        self.assertNotIn("TU_AAA", names)

    def test_equal_scores_break_ties_by_name_deterministically(self):
        """Rows physically re-stored in REVERSE name order first: with a
        polite fixture, Python's stable sort made an explicit tiebreak
        indistinguishable from rowid luck -- a sabotage run proved it."""
        self._doctor({"TU_AAA": 0.50, "TU_BBB": 0.50, "TU_CCC": 0.50})
        rows = [dict(r) for r in self.con.execute(
            "SELECT * FROM object_profiles")]
        rows.sort(key=lambda r: r["name"], reverse=True)
        self.con.execute("DELETE FROM object_profiles")
        keys = list(rows[0].keys())
        self.con.executemany(
            f"INSERT INTO object_profiles ({','.join(keys)}) VALUES "
            f"({','.join(['?'] * len(keys))})",
            [tuple(r[k] for k in keys) for r in rows])
        self.con.commit()
        first, _, _ = self._selected()
        second, _, _ = self._selected()
        self.assertEqual(first, second)
        self.assertEqual(first, ["TU_AAA", "TU_BBB"])

    def test_organic_score_reaches_relmine(self):
        """Demand decides who is in the room; pair-probe priority under
        the budget stays purely evidential. The value_score handed to
        candidate_pairs is the STORED organic score, boost excluded."""
        self._doctor({"TU_AAA": 0.50, "TU_BBB": 0.48, "TU_CCC": 0.40})
        ids = self._ids()
        _, tables, _ = self._selected(hits={ids["TU_CCC"]: 2})
        by_name = {t["name"]: t["value_score"] for t in tables}
        self.assertAlmostEqual(by_name["TU_CCC"], 0.40, places=4)

    def test_steering_note_counts_boosted_in_set_even_when_zero(self):
        """Silence about a no-op is still silence: hits that never
        reached the cut still produce a '0 of N' steering note."""
        self._doctor({"TU_AAA": 0.90, "TU_BBB": 0.80, "TU_CCC": 0.10})
        ids = self._ids()
        _, _, state = self._selected(hits={ids["TU_CCC"]: 1})
        steering = [str(n) for n in state.notes
                    if "demand steering" in str(n)]
        self.assertEqual(len(steering), 1)
        self.assertIn("0 of 2", steering[0])

    def test_disabled_mining_notes_instead_of_silence(self):
        limits = MetadataBuildLimits(mine_value_joins=0)
        state = _FakeState(self.con, limits)
        _collect_value_joins(state, "default",
                             SimpleNamespace(dialect="sqlite"))
        self.assertIn("switched off",
                      " ".join(str(n) for n in state.notes))


class ProcessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-boundary-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_multi_source_hits_do_not_bleed(self):
        """state.demand_hits resets on every collector entry: source
        two's miner must see zero demand influence when only source one
        has failures."""
        dbp1 = _seed(self.root, ALIAS_SCRIPT)
        root2 = self.root / "second"
        root2.mkdir()
        dbp2 = _seed(root2, ALIAS_SCRIPT)
        log = _log(self.root, [_turn(q) for q in ALIAS_QUESTIONS])
        cfg1 = Config.sample(self.root)
        cfg1.db.sqlite_path = str(dbp1)
        cfg1.sources = {}
        cfg2 = Config.sample(root2)
        cfg2.db.sqlite_path = str(dbp2)
        cfg2.sources = {}
        db1, db2 = Database(cfg1), Database(cfg2)
        try:
            build_catalog(self.root / "c.db",
                          [("default", db1), ("p2go", db2)],
                          peopletools_source="default",
                          question_log=log)
        finally:
            db1.close()
            db2.close()
        con = sqlite3.connect(self.root / "c.db")
        con.row_factory = sqlite3.Row
        p2go_hits = [json.loads(r["components"]).get("demand_hits")
                     for r in con.execute(
                         "SELECT components FROM object_profiles "
                         "WHERE source='p2go'")]
        default_hits = [json.loads(r["components"]).get("demand_hits")
                        for r in con.execute(
                            "SELECT components FROM object_profiles "
                            "WHERE source='default'")]
        p2go_steering = [r[0] for r in con.execute(
            "SELECT note FROM notes WHERE source='p2go' "
            "AND note LIKE '%demand steering%'")]
        con.close()
        self.assertTrue(any(default_hits))
        self.assertEqual(set(p2go_hits), {None})
        self.assertEqual(p2go_steering, [])

    def test_concurrent_writer_and_rotation_fuzz(self):
        """A real second PROCESS appends and rotates a tiny-cap log
        while the collector reads in a loop: never an exception."""
        con = _build(self.root, "CREATE TABLE TU_Q2 (C1 TEXT, C7 TEXT);")
        log_dir = self.root / "logs"
        log_dir.mkdir()
        log = log_dir / "q.jsonl"
        writer = subprocess.Popen([sys.executable, "-B", "-c", f"""
import sys
sys.path.insert(0, {str(Path.cwd())!r})
from pstb.qlog import QuestionLog
ql = QuestionLog({str(log)!r}, __import__("pathlib").Path({str(self.root)!r}),
                 max_bytes=2048)
for i in range(120):
    ql.log_turn(surface="test", provider="test",
                question="supplier name wrong " * 5,
                calls=[{{"tool": "run_sql", "ok": False}}], rounds=1,
                answer="", scope={{"source": "default"}})
"""], stderr=subprocess.PIPE, text=True)
        try:
            state = _FakeState(con, MetadataBuildLimits())
            reads = 0
            while writer.poll() is None or reads < 10:
                _collect_demand_signal(state, "default", str(log))
                reads += 1
                if reads > 10_000:   # child hung; communicate() reports
                    break
        finally:
            _, err = writer.communicate(timeout=60)
        con.close()
        self.assertEqual(writer.returncode, 0, err)
        # the writer's tiny cap really rotated: reads raced live renames
        self.assertTrue(list(log_dir.glob("q.jsonl.*")))

    def test_script_wires_the_signal_end_to_end(self):
        """A tool is not a capability: the build SCRIPT must resolve the
        configured log and pass it through, or the collector exists and
        nothing fires it."""
        import importlib.util
        dbp = _seed(self.root, ALIAS_SCRIPT)
        _log(self.root, [_turn(q) for q in ALIAS_QUESTIONS])
        (self.root / "config.yaml").write_text(f"""
db:
  backend: sqlite
  sqlite_path: {dbp}
tools:
  question_log: questions.jsonl
""")
        spec = importlib.util.spec_from_file_location(
            "build_script",
            Path.cwd() / "scripts" / "build_metadata_catalog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = io.StringIO()
        with redirect_stdout(out):
            code = module.main(["--config",
                                str(self.root / "config.yaml")])
        self.assertEqual(code, 0)
        self.assertIn("demand steering from up to", out.getvalue())
        from pstb.config import load_config
        from pstb.metadata import source_catalog_path
        cfg = load_config(str(self.root / "config.yaml"))
        con = sqlite3.connect(source_catalog_path(cfg, "default"))
        con.row_factory = sqlite3.Row
        hits = _hits(con)
        con.close()
        self.assertTrue(any(hits.values()))


if __name__ == "__main__":
    unittest.main()
