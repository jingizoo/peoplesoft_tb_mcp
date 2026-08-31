"""Demand steers the miner's working set -- as a hint, never a hijack.

Phase 6 closes the flywheel's last open loop: failed questions already
become demand terms (#173), and the miner already spends a bounded probe
budget (#174) -- but the two never met, so 40 of ~8,500 live tables were
measured with zero input from 257 logged failures. These tests hold the
three promises that make the connection safe: no term text (which can
carry a party name) ever enters the artifact; the boost is bounded below
one organic score component so measurement still rules; and the working
set stays deterministic, shadow-free, and populated-only however loud
the demand gets.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pstb import metadata as metadata_module
from pstb.config import Config
from pstb.db import Database
from pstb.metadata import (DEMAND_BOOST_CAP, DEMAND_BOOST_PER_HIT,
                           MetadataBuildLimits, MetadataError,
                           build_catalog)

PARTY_TERM = "ridgeline pharmaceuticals"


def _turn(question, *, failed=True, source="default"):
    return json.dumps({"type": "turn", "turn_id": uuid.uuid4().hex,
                       "failed": failed, "source_database": source,
                       "question": question})


def _build(root, script, *, questions=(), inserts=30, torn_tail=False,
           question_log=None, **limit_overrides):
    dbp = Path(root) / "p.db"
    con = sqlite3.connect(dbp)
    con.executescript(script)
    for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        cols = con.execute(f"PRAGMA table_info({row[0]})").fetchall()
        for i in range(inserts):
            con.execute(
                f"INSERT INTO {row[0]} VALUES "
                f"({','.join(['?'] * len(cols))})",
                tuple(f"{c[1]}{i}" for c in cols))
    con.commit()
    con.close()
    log_path = ""
    if questions or question_log is not None:
        log = Path(root) / "questions.jsonl"
        lines = list(questions)
        if torn_tail:
            lines.append('{"type": "turn", "turn_id": "torn')
        log.write_text("\n".join(lines) + "\n")
        log_path = str(log) if question_log is None else question_log
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


class LimitBackfillTests(unittest.TestCase):
    def test_every_mine_budget_now_has_a_floor_and_a_ceiling(self):
        """The recorded hole, finally closed: validate() skipped every
        mine_* field, so a config typo of 240,000 probes passed. Two
        reviews wrote that lesson down before this test existed."""
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


class DemandSignalTests(unittest.TestCase):
    SCRIPT = """
        CREATE TABLE TU_REBATE_HDR (INV_NO TEXT, AMT TEXT);
        CREATE TABLE TU_ZOTHER (K TEXT, V TEXT);
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-demand-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_hits_land_on_the_demanded_object_only(self):
        con = _build(self.root, self.SCRIPT, questions=[
            _turn(f"rebate accrual for {PARTY_TERM}") for _ in range(3)
        ], torn_tail=True)
        hits = {row["name"]: json.loads(row["components"]).get("demand_hits")
                for row in con.execute(
                    "SELECT name, components FROM object_profiles "
                    "WHERE kind='table'")}
        con.close()
        self.assertGreaterEqual(hits["TU_REBATE_HDR"], 1)
        self.assertIsNone(hits["TU_ZOTHER"])

    def test_no_term_text_ever_enters_the_artifact(self):
        """A demand term is a chunk of redacted user prose and can carry
        a party name -- verified on the real log. The artifact travels;
        questions must not. Counts are the only thing that crosses."""
        con = _build(self.root, self.SCRIPT, questions=[
            _turn(f"rebate accrual for {PARTY_TERM}")])
        con.close()
        raw = (self.root / "c.db").read_bytes().lower()
        self.assertNotIn(b"ridgeline", raw)
        self.assertNotIn(b"pharmaceuticals", raw)
        self.assertNotIn(b"rebate accrual", raw)

    def test_value_score_is_never_rewritten_by_demand(self):
        """Demand lives BESIDE the measurement. A boost baked into
        value_score would show users a 'value score' that silently
        included popularity -- the disclose-never-rewrite rule applies
        to numbers too."""
        with_demand = _build(self.root, self.SCRIPT, questions=[
            _turn(f"rebate accrual for {PARTY_TERM}")])
        scores_with = {row["name"]: row["value_score"]
                       for row in with_demand.execute(
                           "SELECT name, value_score FROM object_profiles")}
        with_demand.close()
        with tempfile.TemporaryDirectory() as other:
            without = _build(Path(other), self.SCRIPT)
            scores_without = {row["name"]: row["value_score"]
                              for row in without.execute(
                                  "SELECT name, value_score "
                                  "FROM object_profiles")}
            without.close()
        self.assertEqual(scores_with, scores_without)

    def test_a_torn_tail_never_kills_the_build(self):
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn("rebate totals")], torn_tail=True)
        note = " ".join(row[0] for row in con.execute(
            "SELECT note FROM notes WHERE layer='demand_signal'"))
        con.close()
        self.assertIn("term(s) matched", note)

    def test_the_off_switch_writes_no_hits_and_says_so(self):
        con = _build(self.root, self.SCRIPT,
                     questions=[_turn(f"rebate for {PARTY_TERM}")],
                     mine_demand_terms=0)
        hits = [json.loads(row["components"]).get("demand_hits")
                for row in con.execute(
                    "SELECT components FROM object_profiles")]
        note = " ".join(row[0] for row in con.execute(
            "SELECT note FROM notes WHERE layer='demand_signal'"))
        con.close()
        self.assertEqual(set(hits), {None})
        self.assertIn("disabled", note)

    def test_an_unconfigured_log_is_silent_not_noted(self):
        """CI and the sample build have no question log; a note on every
        such build would train people to skim notes. Absence of the
        feature is not a silent rewrite of anything."""
        con = _build(self.root, self.SCRIPT)
        notes = [row[0] for row in con.execute(
            "SELECT note FROM notes WHERE layer='demand_signal'")]
        con.close()
        self.assertEqual(notes, [])

    def test_an_unreadable_log_never_degrades_the_snapshot(self):
        """partial=True marks the WHOLE snapshot degraded and stamps
        every answer in the product with a partial warning -- the wrong
        verdict for an optional hint whose input is absent. Unavailable,
        degrading nothing: the PSQRYRECORD precedent."""
        # A DELTA assertion, not an absolute one: this bare fixture has
        # other legitimately-partial layers, so "partial != yes" would
        # test the wrong thing. The claim is that the unreadable log
        # changes NOTHING about the snapshot verdict relative to a
        # control build of the same database.
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

    def test_config_yaml_reaches_the_view_and_demand_knobs(self):
        """The #178 view knobs were documented as config keys but
        existed only as CLI flags -- a config.yaml entry was silently
        ignored while the docs said otherwise."""
        from pstb.config import MetadataCatalogCfg, _apply_section
        cfg = MetadataCatalogCfg()
        # Through _apply_section -- the path config.yaml actually takes,
        # which copies ONLY keys declared as fields. Direct setattr
        # works on any instance and proved nothing: the first sabotage
        # run deleted the fields and this test still passed.
        _apply_section(cfg, {"mine_demand_terms": 7,
                             "max_view_definitions": 999})
        limits = MetadataBuildLimits.from_config(cfg)
        self.assertEqual(limits.mine_demand_terms, 7)
        self.assertEqual(limits.max_view_definitions, 999)

    def test_another_sources_failures_do_not_count_here(self):
        con = _build(self.root, self.SCRIPT, questions=[
            _turn("rebate accrual missing", source="p2go")])
        hits = [json.loads(row["components"]).get("demand_hits")
                for row in con.execute(
                    "SELECT components FROM object_profiles")]
        con.close()
        self.assertEqual(set(hits), {None})


class _FakeState:
    """The slice of _Writer the miner's selection actually touches."""

    def __init__(self, con, limits):
        self.con = con
        self.limits = limits
        self.edges = []
        self.notes = []

    def edge(self, *args, **kwargs):
        self.edges.append((args, kwargs))

    def note(self, *args, **kwargs):
        self.notes.append((args, kwargs))

    def limit(self, *args, **kwargs):
        self.notes.append(("limit", args, kwargs))


class WorkingSetTests(unittest.TestCase):
    """The selection arithmetic, exercised directly against a doctored
    artifact so scores and hits are exact rather than emergent."""

    SCRIPT = """
        CREATE TABLE TU_AAA (K TEXT, V TEXT);
        CREATE TABLE TU_BBB (K TEXT, V TEXT);
        CREATE TABLE TU_CCC (K TEXT, V TEXT);
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-workset-")
        self.root = Path(self.temp.name)
        self.con = _build(self.root, self.SCRIPT)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def _doctor(self, scores, hits=None, shadow=None):
        for name, score in scores.items():
            self.con.execute(
                "UPDATE object_profiles SET value_score=? WHERE name=?",
                (score, name))
        for name, count in (hits or {}).items():
            row = self.con.execute(
                "SELECT node_id, components FROM object_profiles "
                "WHERE name=?", (name,)).fetchone()
            components = json.loads(row["components"])
            components["demand_hits"] = count
            self.con.execute(
                "UPDATE object_profiles SET components=? WHERE node_id=?",
                (json.dumps(components), row["node_id"]))
        if shadow:
            ids = {row["name"]: row["node_id"] for row in self.con.execute(
                "SELECT name, node_id FROM object_profiles")}
            self.con.execute(
                "INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
                (ids[shadow], ids[[n for n in ids if n != shadow][0]],
                 "shadow_of", "likely", "test", "profiling", "derived",
                 "now", "{}"))
        self.con.commit()

    def _selected(self, mine_max_tables=2):
        captured = {}

        def spy(tables, **kwargs):
            captured["names"] = [t["name"] for t in tables]
            return []

        limits = MetadataBuildLimits(mine_max_tables=mine_max_tables)
        state = _FakeState(self.con, limits)
        with patch.object(metadata_module.relmine,
                          "candidate_pairs", spy):
            metadata_module._collect_value_joins(
                state, "default", SimpleNamespace(dialect="sqlite"))
        return captured.get("names", [])

    def test_demand_lifts_a_marginal_table_into_the_set(self):
        self._doctor({"TU_AAA": 0.50, "TU_BBB": 0.48, "TU_CCC": 0.40},
                     hits={"TU_CCC": 2})
        # 0.40 + 2*0.05 = 0.50 ties AAA and beats BBB's 0.48.
        self.assertIn("TU_CCC", self._selected(mine_max_tables=2))

    def test_the_boost_is_capped_so_measurement_still_rules(self):
        """However demanded, a table gains at most DEMAND_BOOST_CAP: a
        score gap wider than the cap cannot be shouted across. A hint,
        never a hijack."""
        self._doctor({"TU_AAA": 0.70, "TU_BBB": 0.60, "TU_CCC": 0.40},
                     hits={"TU_CCC": 50})
        self.assertNotIn("TU_CCC", self._selected(mine_max_tables=2))
        self.assertLess(DEMAND_BOOST_CAP, 0.2)
        self.assertEqual(DEMAND_BOOST_PER_HIT * 3, 0.15000000000000002)

    def test_a_shadow_no_longer_consumes_a_working_set_slot(self):
        """The old ORDER BY ... LIMIT cut BEFORE the shadow filter, so a
        shadow in the top-N silently shrank the set below N."""
        self._doctor({"TU_AAA": 0.90, "TU_BBB": 0.50, "TU_CCC": 0.40},
                     shadow="TU_AAA")
        selected = self._selected(mine_max_tables=2)
        self.assertEqual(len(selected), 2)
        self.assertNotIn("TU_AAA", selected)

    def test_demand_cannot_resurrect_a_shadow(self):
        self._doctor({"TU_AAA": 0.90, "TU_BBB": 0.50, "TU_CCC": 0.40},
                     hits={"TU_AAA": 50}, shadow="TU_AAA")
        self.assertNotIn("TU_AAA", self._selected(mine_max_tables=2))

    def test_equal_scores_break_ties_by_name_deterministically(self):
        """The old ORDER BY had no tiebreak at all: two builds of one
        database could mine different sets and both claim to be the
        catalog. Python's sort is STABLE, so a fixture whose storage
        order happens to equal name order cannot tell an explicit
        tiebreak from rowid luck -- the first sabotage run proved
        exactly that. The rows are physically re-stored in REVERSE name
        order first, so only a real name tiebreak yields AAA, BBB."""
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
        fetch_order = [r[0] for r in self.con.execute(
            "SELECT name FROM object_profiles")]
        self.assertEqual(fetch_order, ["TU_CCC", "TU_BBB", "TU_AAA"])
        first = self._selected(mine_max_tables=2)
        second = self._selected(mine_max_tables=2)
        self.assertEqual(first, second)
        self.assertEqual(first, ["TU_AAA", "TU_BBB"])


if __name__ == "__main__":
    unittest.main()
