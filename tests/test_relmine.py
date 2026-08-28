"""Joins measured from data, and the health tool that spends them.

The custom schemas declare no foreign keys, so join_path had nothing to
walk exactly where join knowledge matters most — reconciliation-shaped
questions. The miner measures value containment under hard caps and
writes DERIVED edges the path-finder surfaces with a caveat; the health
tool turns those edges into orphan evidence. Every test that matters here
is a boundary: what the miner must NOT claim, what a probe must NOT scan,
and what a measured edge must never be mistaken for.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pstb import relmine
from pstb.config import Config
from pstb.db import Database
from pstb.metadata import MetadataBuildLimits, MetadataCatalog, build_catalog


class CandidatePairTests(unittest.TestCase):
    def _table(self, name, columns, node_id=None, score=0.5):
        return {"schema": "MAIN", "name": name, "node_id": node_id or name,
                "value_score": score,
                "columns": [{"name": n, "data_type": t}
                            for n, t in columns]}

    def test_cross_named_keys_pair_when_they_share_a_trigram(self):
        pairs = relmine.candidate_pairs([
            self._table("STAGE", [("INV_NBR", "TEXT")]),
            self._table("HDR", [("INVOICE_NO", "TEXT")])])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["left"]["column"], "INV_NBR")

    def test_amounts_and_dates_never_become_candidates(self):
        """Sums that happen to collide are not relationships, and dates
        match everything in the same fiscal month.

        TOTAL_ID is the load-bearing case: it carries a key suffix, so
        only the value-shape exclusion stands between it and a same-name
        pairing that would measure a meaningless containment."""
        pairs = relmine.candidate_pairs([
            self._table("A", [("GROSS_AMT", "NUMERIC"),
                              ("POST_DT", "TEXT"),
                              ("TOTAL_ID", "NUMERIC")]),
            self._table("B", [("NET_AMT", "NUMERIC"),
                              ("DUE_DT", "TEXT"),
                              ("TOTAL_ID", "NUMERIC")])])
        self.assertEqual(pairs, [])

    def test_a_declared_foreign_key_silences_the_miner(self):
        declared = {("MAIN", "STAGE", "INV_NBR",
                     "MAIN", "HDR", "INVOICE_NO"),
                    ("MAIN", "HDR", "INVOICE_NO",
                     "MAIN", "STAGE", "INV_NBR")}
        pairs = relmine.candidate_pairs([
            self._table("STAGE", [("INV_NBR", "TEXT")]),
            self._table("HDR", [("INVOICE_NO", "TEXT")])],
            declared=declared)
        self.assertEqual(pairs, [],
                         "mined edges answer only where the schema is silent")

    def test_type_families_must_match(self):
        pairs = relmine.candidate_pairs([
            self._table("A", [("ITEM_ID", "NUMBER")]),
            self._table("B", [("ITEM_ID", "VARCHAR2")])])
        self.assertEqual(pairs, [])

    def test_unrelated_key_names_do_not_pair(self):
        """VEND_CD vs BATCH_ID share a suffix class and nothing else;
        pairing them quadratically would burn the probe budget on noise."""
        pairs = relmine.candidate_pairs([
            self._table("A", [("VEND_CD", "TEXT")]),
            self._table("B", [("BATCH_ID", "TEXT")])])
        self.assertEqual(pairs, [])

    def test_the_pair_cap_is_a_hard_ceiling(self):
        tables = [self._table(f"T{i}", [("ITEM_ID", "TEXT")], node_id=f"t{i}")
                  for i in range(30)]
        pairs = relmine.candidate_pairs(tables, max_pairs=10)
        self.assertEqual(len(pairs), 10)


class OverlapClassificationTests(unittest.TestCase):
    def test_below_the_minimum_sample_nothing_is_claimed(self):
        """Three matching status codes prove only that both tables have
        status codes."""
        self.assertEqual(relmine.classify_overlap(3, 3), ("", 0.0))
        self.assertEqual(relmine.classify_overlap(19, 19), ("", 0.0))

    def test_thresholds(self):
        self.assertEqual(relmine.classify_overlap(100, 100)[0], "likely")
        self.assertEqual(relmine.classify_overlap(100, 95)[0], "possible")
        self.assertEqual(relmine.classify_overlap(100, 60)[0], "")


class ProbeSafetyTests(unittest.TestCase):
    def test_an_unsafe_identifier_is_skipped_never_quoted(self):
        class Db:
            dialect = "sqlite"

            def query(self, *a, **k):
                raise AssertionError("a query must never be issued")

        out = relmine.probe_containment(
            Db(), {"schema": "", "table": "T; DROP TABLE X", "column": "A"},
            {"schema": "", "table": "P", "column": "B"})
        self.assertEqual(out["sampled"], 0)
        self.assertIn("unsafe identifier", out.get("skipped", ""))

    def test_probe_queries_are_bounded_not_scans(self):
        issued = []

        class Db:
            dialect = "sqlite"

            def query(self, sql, params=None, max_rows=None):
                issued.append(" ".join(sql.split()))
                if "DISTINCT" in sql and "COUNT" not in sql:
                    return [{"v": f"X{i}"} for i in range(30)], False
                return [{"n": 30}], False

        relmine.probe_containment(
            Db(), {"schema": "", "table": "C", "column": "K"},
            {"schema": "", "table": "P", "column": "K"}, sample_rows=30)
        self.assertEqual(len(issued), 2)
        self.assertIn("LIMIT", issued[0])
        self.assertIn("IN (", issued[1])
        self.assertNotIn("JOIN", issued[1])


class MinedEdgeEndToEndTests(unittest.TestCase):
    """The whole super tech on a real FK-less database."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-relmine-")
        root = Path(self.temp.name)
        self.db_path = root / "p.db"
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            CREATE TABLE TU_STAGE (INV_NBR TEXT, LOAD_AMT NUMERIC);
            CREATE TABLE TU_HDR (INVOICE_NO TEXT PRIMARY KEY, VEND_CD TEXT);
        """)
        for i in range(80):
            con.execute("INSERT INTO TU_HDR VALUES (?,?)",
                        (f"I{i:04d}", f"V{i % 7}"))
        for i in range(70):
            con.execute("INSERT INTO TU_STAGE VALUES (?,?)",
                        (f"I{i:04d}", i * 10))
        con.execute("INSERT INTO TU_STAGE VALUES ('GHOST-1', 5)")
        con.commit()
        con.close()
        self.cfg = Config.sample(root)
        self.cfg.db.sqlite_path = str(self.db_path)
        self.cfg.sources = {}
        self.catalog_path = root / "c.db"

    def tearDown(self):
        self.temp.cleanup()

    def _build(self, **limit_overrides):
        db = Database(self.cfg)
        try:
            limits = MetadataBuildLimits(**limit_overrides) \
                if limit_overrides else None
            build_catalog(self.catalog_path, [("default", db)],
                          limits=limits, peopletools_source="default")
        finally:
            db.close()
        con = sqlite3.connect(self.catalog_path)
        con.row_factory = sqlite3.Row
        return con

    def test_the_undeclared_join_is_mined_with_its_evidence(self):
        con = self._build()
        edges = [dict(r) for r in con.execute(
            "SELECT * FROM edges WHERE kind='value_overlap_join'")]
        con.close()
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["authority"], "derived")
        self.assertEqual(edge["collector"], "relmine")
        attrs = json.loads(edge["attrs"])
        self.assertEqual(attrs["column_pairs"],
                         [{"column": "INV_NBR",
                           "referenced_column": "INVOICE_NO"}])
        self.assertGreaterEqual(attrs["overlap_pct"], 0.9)
        self.assertIn("sampled", edge["evidence"])

    def test_join_path_walks_the_mined_edge_with_the_caveat(self):
        self._build().close()
        catalog = MetadataCatalog(self.catalog_path)
        path = catalog.relationship_path("TU_STAGE", "TU_HDR",
                                         source="default")
        self.assertTrue(path["found"])
        hop = path["hops"][0]
        self.assertEqual(hop["relationship"], "value_overlap")
        self.assertIn("MEASURED, not declared", hop["caveat"])
        self.assertEqual(hop["column_pairs"][0]["left_column"], "INV_NBR")

    def test_relationships_of_labels_the_class(self):
        self._build().close()
        catalog = MetadataCatalog(self.catalog_path)
        ring = catalog.relationships_of("TU_STAGE", source="default")
        kinds = {r["relationship"] for r in ring["relationships"]}
        self.assertIn("value_overlap", kinds)

    def test_disabling_the_miner_leaves_no_edges(self):
        con = self._build(mine_value_joins=0)
        count = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='value_overlap_join'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_the_probe_budget_is_a_hard_ceiling(self):
        con = self._build(mine_max_probes=1)
        count = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='value_overlap_join'"
        ).fetchone()[0]
        hit = con.execute(
            "SELECT COUNT(*) FROM notes WHERE layer='value_joins' "
            "AND note LIKE '%limit%'").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)
        self.assertGreaterEqual(hit, 1,
                                "a silently exhausted budget reads as "
                                "'no undeclared joins exist'")

    def test_two_measured_pairs_between_one_table_pair_both_survive(self):
        """The edges table keys on (src, dst, kind): written naively, the
        second measured column pair between the same two tables would be
        silently discarded by ON CONFLICT — evidence lost with no trace.
        Pairs are merged into one edge instead, each with its own
        measurement."""
        con = sqlite3.connect(self.db_path)
        # A second referencing column with enough distinct values to clear
        # the min-sample bar (a low-cardinality column would be correctly
        # refused — three matching codes prove nothing).
        con.execute("ALTER TABLE TU_STAGE ADD COLUMN INV_REF TEXT")
        con.execute("UPDATE TU_STAGE SET INV_REF = INV_NBR "
                    "WHERE INV_NBR LIKE 'I%'")
        con.commit()
        con.close()
        con = self._build()
        edges = [dict(r) for r in con.execute(
            "SELECT * FROM edges WHERE kind='value_overlap_join'")]
        con.close()
        by_pairs = {}
        for edge in edges:
            attrs = json.loads(edge["attrs"])
            key = (edge["src"], edge["dst"])
            by_pairs[key] = attrs["column_pairs"]
        multi = [pairs for pairs in by_pairs.values() if len(pairs) > 1]
        self.assertTrue(multi,
                        "TU_STAGE now references INVOICE_NO through both "
                        "INV_NBR and INV_REF; both measured pairs must "
                        "ride one edge")
        columns = {pair["column"] for pairs in multi for pair in pairs}
        self.assertEqual(columns, {"INV_NBR", "INV_REF"})

    def test_a_declared_key_ranks_before_a_measured_edge(self):
        """When one node carries both, the ring must offer intent first:
        the path-finder takes neighbours in order, so a mis-ranked ring
        routes reconciliations through measurement while a declared key
        sits unused."""
        with tempfile.TemporaryDirectory(prefix="pstb-rank-") as root2:
            root2 = Path(root2)
            db_path = root2 / "p.db"
            con = sqlite3.connect(db_path)
            con.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE PARENT1 (BATCH_CD TEXT PRIMARY KEY);
                CREATE TABLE PARENT2 (INVOICE_NO TEXT PRIMARY KEY);
                CREATE TABLE CHILD (
                  BATCH_CD TEXT REFERENCES PARENT1(BATCH_CD),
                  INV_NBR TEXT);
            """)
            for i in range(40):
                con.execute("INSERT INTO PARENT1 VALUES (?)", (f"B{i:03d}",))
                con.execute("INSERT INTO PARENT2 VALUES (?)", (f"I{i:03d}",))
                con.execute("INSERT INTO CHILD VALUES (?,?)",
                            (f"B{i:03d}", f"I{i:03d}"))
            con.commit()
            con.close()
            cfg = Config.sample(root2)
            cfg.db.sqlite_path = str(db_path)
            cfg.sources = {}
            db = Database(cfg)
            try:
                build_catalog(root2 / "c.db", [("default", db)],
                              peopletools_source="default")
            finally:
                db.close()
            catalog = MetadataCatalog(root2 / "c.db")
            con = sqlite3.connect(root2 / "c.db")
            con.row_factory = sqlite3.Row
            node = con.execute(
                "SELECT * FROM nodes WHERE name='CHILD' "
                "AND kind='table'").fetchone()
            ring, _ = catalog._relationship_neighbours(
                con, node, "default")
            con.close()
        kinds = [r["relationship"] for r in ring
                 if r["relationship"] in ("foreign_key", "value_overlap")]
        self.assertIn("foreign_key", kinds)
        self.assertIn("value_overlap", kinds)
        self.assertLess(kinds.index("foreign_key"),
                        kinds.index("value_overlap"),
                        "the ring must offer declared intent before "
                        "measurement")


class TableHealthToolTests(unittest.TestCase):
    """The connector: counts-only quality evidence over the mined graph."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-health-")
        root = Path(self.temp.name)
        db_path = root / "p.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE TU_STAGE (INV_NBR TEXT, LOAD_AMT NUMERIC,
                                   BATCH_CD TEXT);
            CREATE TABLE TU_HDR (INVOICE_NO TEXT PRIMARY KEY, VEND_CD TEXT);
        """)
        for i in range(80):
            con.execute("INSERT INTO TU_HDR VALUES (?,?)",
                        (f"I{i:04d}", f"V{i % 7}"))
        for i in range(70):
            con.execute("INSERT INTO TU_STAGE VALUES (?,?,?)",
                        (f"I{i:04d}", i * 10, "B1"))
        con.execute("INSERT INTO TU_STAGE VALUES ('GHOST-1', 5, NULL)")
        con.execute("INSERT INTO TU_STAGE VALUES ('GHOST-2', 6, NULL)")
        con.commit()
        con.close()
        cfg = Config.sample(root)
        cfg.db.sqlite_path = str(db_path)
        cfg.sources = {}
        self.db = Database(cfg)
        self.catalog_path = root / "c.db"
        build_catalog(self.catalog_path, [("default", self.db)],
                      peopletools_source="default")
        self.catalog = MetadataCatalog(self.catalog_path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _run(self, table):
        import pstb.server as srv
        with patch.object(srv, "_metadata_for_source",
                          lambda s: ("default", self.catalog)), \
                patch.object(srv.engine, "for_source",
                             lambda s: type("B", (), {"db": self.db})()):
            return srv._table_health(table, "")

    def test_orphans_surface_through_the_mined_relationship(self):
        out = self._run("TU_STAGE")
        rel = out["relationships"][0]
        self.assertEqual(rel["relationship"], "value_overlap")
        self.assertEqual(rel["orphans_in_sample"], 2)
        self.assertIn("MEASURED", rel["caveat"])

    def test_null_rates_are_sampled_and_labeled(self):
        out = self._run("TU_STAGE")
        batch = next(r for r in out["null_rates"]
                     if r["column"] == "BATCH_CD")
        self.assertEqual(batch["nulls"], 2)
        self.assertTrue(any("not the full population" in c
                            for c in out["caveats"]))

    def test_no_row_value_appears_anywhere_in_the_payload(self):
        """Counts and percentages only: GHOST-1 is a row value and must
        never ride out in a health payload."""
        out = self._run("TU_STAGE")
        self.assertNotIn("GHOST", json.dumps(out))
        self.assertNotIn("I0001", json.dumps(out))

    def test_an_unresolved_table_returns_the_catalogs_refusal(self):
        out = self._run("NO_SUCH_TABLE")
        self.assertIn("error", out)

    def test_the_tool_is_admitted_to_the_silo(self):
        from pstb.guards import SOURCE_SILO_TOOLS
        self.assertIn("get_table_health", SOURCE_SILO_TOOLS)

    def test_both_prompts_route_to_it(self):
        """A tool is not a capability until the prompt routes to it — and
        the silo prompt REPLACES the base one, so each needs its own
        routing or secondary-workspace turns never learn it exists."""
        from pstb.client.prompt import source_silo_prompt
        from pstb.client import prompt as prompt_module
        silo = source_silo_prompt("p2go")
        self.assertIn("get_table_health", silo)
        base_source = Path(prompt_module.__file__).read_text()
        start = base_source.index("Data-quality and tie-out questions")
        self.assertIn("get_table_health", base_source[start:start + 700])


if __name__ == "__main__":
    unittest.main()
