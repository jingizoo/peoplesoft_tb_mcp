"""Ranking objects by what they contain, never by what they are called.

The custom schemas this has to work on are mismanaged: names are wrong or
absent, and live tables sit beside dated copies of themselves. Every
judgement here therefore has to survive two questions -- does it work when
the name is useless, and does it stay quiet when it is only guessing.

The second matters more. A ranking that confidently prefers the wrong
table is worse than no ranking, because nothing downstream will question
it: a wrong "prefer PS_VOUCHER_BKP" reads exactly like a right one.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pstb import metadata, profiling as P
from pstb.config import Config
from pstb.db import Database
from pstb.metadata import MetadataCatalog, build_catalog


class ColumnSignatureTests(unittest.TestCase):
    def test_a_copy_keeps_its_signature_through_order_and_length(self):
        """CREATE TABLE AS SELECT routinely loses declared lengths and
        column order. Folding either in would make a copy look like a
        different table, which is the one thing this must not do."""
        original = [{"name": "BUSINESS_UNIT", "data_type": "VARCHAR2(5)"},
                    {"name": "VOUCHER_ID", "data_type": "VARCHAR2(8)"},
                    {"name": "GROSS_AMT", "data_type": "NUMBER(15,2)"}]
        backup = [{"name": "GROSS_AMT", "data_type": "NUMBER"},
                  {"name": "VOUCHER_ID", "data_type": "VARCHAR2(30)"},
                  {"name": "BUSINESS_UNIT", "data_type": "VARCHAR2"}]
        self.assertEqual(P.column_signature(original),
                         P.column_signature(backup))

    def test_a_different_column_set_is_a_different_signature(self):
        a = [{"name": "A", "data_type": "NUMBER"}]
        b = [{"name": "A", "data_type": "NUMBER"},
             {"name": "B", "data_type": "NUMBER"}]
        self.assertNotEqual(P.column_signature(a), P.column_signature(b))

    def test_a_retyped_column_is_a_different_signature(self):
        a = [{"name": "AMT", "data_type": "NUMBER(15,2)"}]
        b = [{"name": "AMT", "data_type": "VARCHAR2(15)"}]
        self.assertNotEqual(P.column_signature(a), P.column_signature(b))

    def test_an_unreadable_shape_is_empty_not_a_false_match(self):
        """Two tables we could not read columns for must not collide."""
        self.assertEqual(P.column_signature([]), "")
        self.assertEqual(P.column_signature([{"name": ""}]), "")


class NameRelationTests(unittest.TestCase):
    """The second of two signals, and only ever consulted once shapes match."""

    def test_recognised_copy_markers(self):
        for other in ("PS_VOUCHER_OLD", "PS_VOUCHER_BKP", "PS_VOUCHER_BACKUP",
                      "PS_VOUCHER_COPY", "PS_VOUCHER_ARCH", "PS_VOUCHER_TMP",
                      "PS_VOUCHER_2024", "PS_VOUCHER_20240115",
                      "OLD_PS_VOUCHER", "PS_VOUCHEROLD"):
            with self.subTest(other=other):
                self.assertEqual(P.name_relation("PS_VOUCHER", other),
                                 P.COPY_MARKER)

    def test_numbered_siblings(self):
        """PeopleTools allocates numbered temp instances by design."""
        for other in ("PS_AP_TAO1", "PS_AP_TAO9", "PS_AP_TAO_2"):
            with self.subTest(other=other):
                self.assertEqual(P.name_relation("PS_AP_TAO", other),
                                 P.NUMBERED_SIBLING)

    # ------------------------------------------------------- the quiet half
    def test_an_unrelated_name_is_not_a_copy_however_alike_the_shape(self):
        """Schemas are full of legitimately identical shapes.

        PS_JRNL_HEADER and PS_VOUCHER can share a column signature and be
        entirely different things; claiming one supersedes the other would
        point every journal question at vouchers.
        """
        for a, b in (("PS_VOUCHER", "PS_JRNL_HEADER"),
                     ("PS_LEDGER", "PS_LEDGER_BUDG"),
                     ("PS_ITEM", "PS_ITEM_ACTIVITY"),
                     ("PS_BI_HDR", "PS_BI_LINE")):
            with self.subTest(a=a, b=b):
                self.assertEqual(P.name_relation(a, b), "",
                                 "an unrecognised word is a real difference")

    def test_a_meaningful_suffix_is_not_a_marker(self):
        self.assertEqual(P.name_relation("PS_VOUCHER", "PS_VOUCHER_LINE"), "")
        self.assertEqual(P.name_relation("PS_VOUCHER", "PS_VOUCHER_PAY"), "")

    def test_identical_names_relate_to_nothing(self):
        self.assertEqual(P.name_relation("PS_VOUCHER", "PS_VOUCHER"), "")

    def test_a_long_unrecognised_tail_is_refused(self):
        """Two extra words is the cap; beyond that it is a different table."""
        self.assertEqual(
            P.name_relation("PS_A", "PS_A_OLD_STAGING_REGION_TWO"), "")


class LivenessTests(unittest.TestCase):
    def test_never_analyzed_is_unknown_and_never_empty(self):
        """Oracle reports NUM_ROWS as NULL for a table nobody has analyzed.

        Reading that as zero would retire live tables wholesale -- in a
        schema where nothing is analyzed, it would retire all of them.
        """
        self.assertEqual(P.liveness(None), P.UNKNOWN)
        self.assertNotEqual(P.liveness(None), P.EMPTY)

    def test_zero_is_empty_and_a_count_is_populated(self):
        self.assertEqual(P.liveness(0), P.EMPTY)
        self.assertEqual(P.liveness(1), P.POPULATED)
        self.assertEqual(P.liveness(4_000_000), P.POPULATED)

    def test_an_unmeasurable_dialect_forces_unknown(self):
        self.assertEqual(P.liveness(500, analyzed=False), P.UNKNOWN)

    def test_junk_is_unknown_rather_than_an_exception(self):
        for value in ("", "lots", -1, object()):
            with self.subTest(value=value):
                self.assertEqual(P.liveness(value), P.UNKNOWN)


class ValueScoreTests(unittest.TestCase):
    def _p(self, **kw):
        base = {"liveness": P.POPULATED, "row_estimate": 1000,
                "column_count": 10, "populated_columns": 10,
                "reference_count": 2}
        base.update(kw)
        return base

    def test_an_empty_table_scores_no_population_at_all(self):
        out = P.value_score(self._p(liveness=P.EMPTY, row_estimate=0))
        self.assertEqual(out["components"]["population"], 0.0)
        self.assertEqual(out["components"]["breadth"], 0.0)

    def test_a_bigger_better_connected_table_outranks_a_thinner_one(self):
        big = P.value_score(self._p(row_estimate=900_000, reference_count=12))
        small = P.value_score(self._p(row_estimate=5, reference_count=0))
        self.assertGreater(big["score"], small["score"])

    def test_unmeasured_is_reported_as_a_prior_not_a_measurement(self):
        """It is allowed to outrank a measured near-empty table -- but a
        reader has to be able to see that is what happened."""
        out = P.value_score(self._p(liveness=P.UNKNOWN, row_estimate=None,
                                    populated_columns=None))
        self.assertEqual(out["components"]["population_basis"], "unmeasured")
        self.assertEqual(out["components"]["population"], 0.5)

    def test_a_view_inherits_liveness_from_what_it_selects_from(self):
        """A view is never in table statistics; what it reads can be."""
        out = P.value_score(self._p(liveness=P.UNKNOWN, row_estimate=None,
                                    populated_columns=None,
                                    inherited_rows=500_000))
        self.assertEqual(out["components"]["population_basis"], "inherited")
        self.assertGreater(out["components"]["population"], 0.5)

    def test_a_measured_table_says_so(self):
        self.assertEqual(
            P.value_score(self._p())["components"]["population_basis"],
            "measured")

    def test_unmeasured_column_population_is_not_read_as_all_empty(self):
        none = P.value_score(self._p(populated_columns=None))
        zero = P.value_score(self._p(populated_columns=0))
        self.assertGreater(none["score"], zero["score"])

    def test_the_score_is_bounded(self):
        out = P.value_score(self._p(row_estimate=10 ** 12,
                                    reference_count=10_000,
                                    populated_columns=999))
        self.assertLessEqual(out["score"], 1.0)
        self.assertGreaterEqual(out["score"], 0.0)


class ShadowCandidateTests(unittest.TestCase):
    SIG = P.column_signature([{"name": "BUSINESS_UNIT", "data_type": "VARCHAR2"},
                              {"name": "VOUCHER_ID", "data_type": "VARCHAR2"}])

    def _p(self, name, rows, **kw):
        base = {"node_id": f"table:{name}", "name": name,
                "signature": self.SIG, "row_estimate": rows,
                "liveness": P.liveness(rows), "reference_count": 0}
        base.update(kw)
        return base

    def test_a_dated_copy_points_at_the_live_table(self):
        found = P.shadow_candidates([self._p("PS_VOUCHER", 50_000),
                                     self._p("PS_VOUCHER_2024", 41_000)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["canonical"], "PS_VOUCHER")
        self.assertEqual(found[0]["shadow"], "PS_VOUCHER_2024")
        self.assertEqual(found[0]["relation"], P.COPY_MARKER)

    def test_shape_alone_proposes_nothing(self):
        """The whole false-positive guard: identical columns, unrelated
        names, and no claim made."""
        self.assertEqual(
            P.shadow_candidates([self._p("PS_VOUCHER", 50_000),
                                 self._p("PS_PAYMENT", 44_000)]), [])

    def test_a_name_alone_proposes_nothing(self):
        other = self._p("PS_VOUCHER_BKP", 10)
        other["signature"] = P.column_signature(
            [{"name": "SOMETHING_ELSE", "data_type": "NUMBER"}])
        self.assertEqual(
            P.shadow_candidates([self._p("PS_VOUCHER", 50_000), other]), [])

    def test_an_unreadable_shape_is_never_grouped(self):
        blank_a = self._p("PS_VOUCHER", 10, signature="")
        blank_b = self._p("PS_VOUCHER_OLD", 10, signature="")
        self.assertEqual(P.shadow_candidates([blank_a, blank_b]), [])

    def test_it_never_points_at_an_empty_canonical(self):
        """A backup kept because the original was truncated is a real
        thing; sending an answer to the empty one would be worse than the
        ambiguity it is resolving."""
        self.assertEqual(
            P.shadow_candidates([self._p("PS_VOUCHER", 0),
                                 self._p("PS_VOUCHER_BKP", 90_000)]), [])

    def test_statistics_never_promote_a_marked_copy_over_its_base(self):
        """PS_VOUCHER unanalyzed, PS_VOUCHER_OLD analyzed at 900 rows.

        Ranking on the row estimate alone would make the copy canonical
        and send every voucher question to the superseded table. Whether a
        table has statistics reflects whether a DBA ever ran gather_stats;
        whether it is called _OLD reflects a human deciding it was done
        with. The second is evidence and the first is not.
        """
        found = P.shadow_candidates([self._p("PS_VOUCHER", None),
                                     self._p("PS_VOUCHER_OLD", 900)])
        self.assertEqual([f["canonical"] for f in found], ["PS_VOUCHER"])
        self.assertEqual([f["shadow"] for f in found], ["PS_VOUCHER_OLD"])

    def test_numbered_temp_instances_all_resolve_to_the_base(self):
        rows = [self._p("PS_AP_TAO", 5_000)]
        rows += [self._p(f"PS_AP_TAO{n}", 0) for n in range(1, 5)]
        found = P.shadow_candidates(rows)
        self.assertEqual(len(found), 4)
        self.assertEqual({f["canonical"] for f in found}, {"PS_AP_TAO"})
        self.assertEqual({f["relation"] for f in found},
                         {P.NUMBERED_SIBLING})

    def test_the_shortest_name_breaks_a_tie(self):
        """Common in a schema where nothing has ever been analyzed, so
        every candidate is UNKNOWN with no rows to separate them."""
        found = P.shadow_candidates([self._p("PS_VOUCHER_BKP", None),
                                     self._p("PS_VOUCHER", None)])
        self.assertEqual([f["canonical"] for f in found], ["PS_VOUCHER"])

    def test_a_lone_object_is_never_its_own_shadow(self):
        self.assertEqual(P.shadow_candidates([self._p("PS_VOUCHER", 10)]), [])


class ProfileCollectorTests(unittest.TestCase):
    """The collector end to end, against a database that has a real copy in it.

    The bundled sample has no shadow tables, so nothing in it exercises the
    half of this that matters most.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-profile-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "primary.db"
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            CREATE TABLE PS_VOUCHER (
              BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, GROSS_AMT NUMERIC);
            -- same shape, marked name: the copy
            CREATE TABLE PS_VOUCHER_BKP (
              BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, GROSS_AMT NUMERIC);
            -- same shape, UNRELATED name: must not be paired with anything
            CREATE TABLE PS_PAYMENT_STAGE (
              BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, GROSS_AMT NUMERIC);
            -- different shape, related name: must not be paired either
            CREATE TABLE PS_VOUCHER_LINE (
              BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, LINE_NBR INTEGER);
            CREATE TABLE PS_DEAD_WEIGHT (JUNK TEXT);
        """)
        for i in range(40):
            con.execute("INSERT INTO PS_VOUCHER VALUES (?,?,?)",
                        ("US001", f"V{i:05d}", i * 10))
        for i in range(9):
            con.execute("INSERT INTO PS_VOUCHER_BKP VALUES (?,?,?)",
                        ("US001", f"V{i:05d}", i * 10))
        for i in range(5):
            con.execute("INSERT INTO PS_PAYMENT_STAGE VALUES (?,?,?)",
                        ("US001", f"P{i:05d}", i))
        con.commit()
        con.close()

        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(self.db_path)
        cfg.sources = {}
        self.catalog_path = self.root / "catalog.db"
        db = Database(cfg)
        try:
            build_catalog(self.catalog_path, [("default", db)],
                          peopletools_source="default")
        finally:
            db.close()
        self.con = sqlite3.connect(self.catalog_path)
        self.con.row_factory = sqlite3.Row

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def _profiles(self):
        return {r["name"]: r for r in
                self.con.execute("SELECT * FROM object_profiles")}

    def _shadows(self):
        return [(r["s"], r["c"], r["attrs"]) for r in self.con.execute(
            "SELECT s.name AS s, d.name AS c, e.attrs AS attrs FROM edges e "
            "JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
            "WHERE e.kind='shadow_of'")]

    def test_every_object_is_profiled(self):
        names = set(self._profiles())
        self.assertLessEqual(
            {"PS_VOUCHER", "PS_VOUCHER_BKP", "PS_PAYMENT_STAGE",
             "PS_VOUCHER_LINE", "PS_DEAD_WEIGHT"}, names)

    def test_the_marked_copy_points_at_the_live_table(self):
        pairs = [(s, c) for s, c, _ in self._shadows()]
        self.assertIn(("PS_VOUCHER_BKP", "PS_VOUCHER"), pairs)

    def test_an_unrelated_table_of_the_same_shape_is_left_alone(self):
        """PS_PAYMENT_STAGE has PS_VOUCHER's exact columns and is a
        different thing. Naming it a copy would send every voucher
        question to a staging table."""
        touched = {s for s, _, _ in self._shadows()} | {
            c for _, c, _ in self._shadows()}
        self.assertNotIn("PS_PAYMENT_STAGE", touched)

    def test_a_related_name_with_a_different_shape_is_left_alone(self):
        touched = {s for s, _, _ in self._shadows()}
        self.assertNotIn("PS_VOUCHER_LINE", touched)

    def test_the_edge_carries_the_evidence_that_produced_it(self):
        attrs = next(json.loads(a) for s, _, a in self._shadows()
                     if s == "PS_VOUCHER_BKP")
        self.assertEqual(attrs["relation"], "copy_marker")
        self.assertEqual(attrs["canonical"], "PS_VOUCHER")

    def test_an_empty_table_is_reported_empty_and_ranked_last(self):
        profiles = self._profiles()
        self.assertEqual(profiles["PS_DEAD_WEIGHT"]["liveness"], "empty")
        self.assertLess(profiles["PS_DEAD_WEIGHT"]["value_score"],
                        profiles["PS_VOUCHER"]["value_score"])

    def test_the_busiest_table_outranks_its_own_backup(self):
        profiles = self._profiles()
        self.assertGreater(profiles["PS_VOUCHER"]["value_score"],
                           profiles["PS_VOUCHER_BKP"]["value_score"])

    def test_the_score_carries_the_components_that_produced_it(self):
        components = json.loads(self._profiles()["PS_VOUCHER"]["components"])
        self.assertEqual(components["population_basis"], "measured")
        for key in ("population", "breadth", "connectivity"):
            self.assertIn(key, components)

    def test_the_profile_records_what_it_read(self):
        self.assertTrue(self._profiles()["PS_VOUCHER"]["evidence"].strip(),
                        "a ranking with no stated source is not reviewable")


class SqliteCountGuardTests(unittest.TestCase):
    """The sqlite branch interpolates a table name; nothing else may."""

    def test_only_the_sample_branch_interpolates_and_it_is_guarded(self):
        source = Path(metadata.__file__).read_text()
        body = source[source.index("def _table_statistics"):
                      source.index("def _collect_profile")]
        self.assertIn("_SAFE_IDENT", body,
                      "the one interpolated identifier must be checked")
        oracle = body[body.index('if dialect == "oracle"'):
                      body.index('if dialect == "sqlite"')]
        self.assertNotIn("{name}", oracle,
                         "the Oracle branch must stay fully parameterised")
        self.assertIn("_owner_scope(", oracle,
                      "owners are bound through the shared helper, which is "
                      "what keeps a configured schema name out of the SQL")

    def test_the_guard_refuses_anything_that_is_not_a_bare_identifier(self):
        for bad in ('a"; DROP TABLE x --', "a b", "1abc", "", "a-b", "a;b"):
            with self.subTest(bad=bad):
                self.assertFalse(metadata._SAFE_IDENT.fullmatch(bad))
        for good in ("PS_VOUCHER", "_x", "T1"):
            with self.subTest(good=good):
                self.assertTrue(metadata._SAFE_IDENT.fullmatch(good))

class UsefulnessReachesTheModelTests(unittest.TestCase):
    """Ranking objects in a table nobody reads changes nothing.

    The model calls get_metadata_context before it chooses what to query,
    so "is this the live table" has to arrive there or the whole profiling
    pass is inert.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-useful-")
        self.root = Path(self.temp.name)
        db_path = self.root / "primary.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE PS_VOUCHER (BUSINESS_UNIT TEXT, VOUCHER_ID TEXT);
            CREATE TABLE PS_VOUCHER_OLD (BUSINESS_UNIT TEXT, VOUCHER_ID TEXT);
            CREATE TABLE PS_QUIET (ONLY_COL TEXT);
        """)
        for i in range(30):
            con.execute("INSERT INTO PS_VOUCHER VALUES (?,?)", ("US001", str(i)))
        con.commit()
        con.close()
        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(db_path)
        cfg.sources = {}
        self.catalog_path = self.root / "catalog.db"
        db = Database(cfg)
        try:
            build_catalog(self.catalog_path, [("default", db)],
                          peopletools_source="default")
        finally:
            db.close()
        self.catalog = MetadataCatalog(self.catalog_path)

    def tearDown(self):
        self.temp.cleanup()

    def _useful(self, name):
        return self.catalog.context(name, source="default").get("usefulness")

    def test_asking_about_a_copy_names_the_table_to_use_instead(self):
        prefer = self._useful("PS_VOUCHER_OLD")["prefer_instead"]
        self.assertEqual(prefer["object"], "PS_VOUCHER")
        self.assertEqual(prefer["canonical_rows"], 30)
        self.assertTrue(prefer["why"].strip(),
                        "a redirection with no reason is not reviewable")

    def test_the_live_table_is_not_redirected_anywhere(self):
        self.assertNotIn("prefer_instead", self._useful("PS_VOUCHER"))

    def test_an_empty_object_says_so_and_says_to_confirm(self):
        useful = self._useful("PS_QUIET")
        self.assertEqual(useful["liveness"], "empty")
        self.assertIn("Confirm", useful["caveat"])

    def test_a_populated_object_carries_its_measurement_and_its_basis(self):
        useful = self._useful("PS_VOUCHER")
        self.assertEqual(useful["liveness"], "populated")
        self.assertEqual(useful["row_estimate"], 30)
        self.assertEqual(useful["basis"], "measured")
        self.assertTrue(useful["evidence"].strip())

    def test_a_catalog_built_before_profiles_existed_still_answers(self):
        """An artifact on disk outlives the code that wrote it.

        A deployment upgrading pstb without rebuilding its catalog must not
        lose get_metadata_context entirely because an enhancement's table
        is missing.
        """
        con = sqlite3.connect(self.catalog_path)
        con.execute("DROP TABLE object_profiles")
        con.execute("DELETE FROM edges WHERE kind='shadow_of'")
        con.commit()
        con.close()
        result = MetadataCatalog(self.catalog_path).context(
            "PS_VOUCHER", source="default")
        self.assertTrue(result["found"])
        self.assertEqual(result["usefulness"], {})

class TruncatedColumnsTests(unittest.TestCase):
    """At real scale the column cap bites and signatures go partial.

    The instance this targets has 3,186,495 columns against a default
    max_fields of 500,000, so most tables get no columns at all -- safe,
    because an empty signature is never grouped -- and the one straddling
    the cut keeps a PARTIAL list, which is not safe. A partial signature is
    a subset of the truth, not a shorter truth, and on a PeopleSoft schema
    where families of tables share their leading key columns it can equal a
    smaller table's complete signature. That is a false "prefer that one
    instead" manufactured entirely by a build limit.

    PS_KEYS_OLD below really has four columns and really is not a copy of
    the two-column PS_KEYS. Cut to its first two it looks exactly like one,
    and its name supplies the second signal.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-trunc-")
        self.root = Path(self.temp.name)
        db_path = self.root / "primary.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE PS_KEYS (K1 TEXT, K2 TEXT);
            CREATE TABLE PS_KEYS_OLD (K1 TEXT, K2 TEXT, C3 TEXT, C4 TEXT);
        """)
        con.commit()
        con.close()
        self.cfg = Config.sample(self.root)
        self.cfg.db.sqlite_path = str(db_path)
        self.cfg.sources = {}
        self.catalog_path = self.root / "catalog.db"

    def tearDown(self):
        self.temp.cleanup()

    def _build(self, max_fields):
        db = Database(self.cfg)
        try:
            build_catalog(self.catalog_path, [("default", db)],
                          limits=metadata.MetadataBuildLimits(
                              max_fields=max_fields),
                          peopletools_source="default")
        finally:
            db.close()
        con = sqlite3.connect(self.catalog_path)
        con.row_factory = sqlite3.Row
        try:
            profiles = {r["name"]: dict(r) for r in
                        con.execute("SELECT * FROM object_profiles")}
            pairs = [(r["s"], r["c"]) for r in con.execute(
                "SELECT s.name AS s, d.name AS c FROM edges e "
                "JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
                "WHERE e.kind='shadow_of'")]
            return profiles, pairs
        finally:
            con.close()

    def test_with_room_the_real_shape_is_kept_and_no_copy_is_claimed(self):
        """The control: four columns collected, the tables differ, silence."""
        profiles, pairs = self._build(max_fields=100)
        self.assertEqual(profiles["PS_KEYS_OLD"]["column_count"], 4)
        self.assertEqual(pairs, [])

    def test_a_build_limit_cannot_manufacture_a_copy(self):
        """max_fields=4 cuts PS_KEYS_OLD to exactly PS_KEYS's columns.

        Without the guard this yields ('PS_KEYS_OLD', 'PS_KEYS') -- a
        confident redirection to a table with half the columns, produced by
        nothing but where the cap happened to fall.
        """
        profiles, pairs = self._build(max_fields=4)
        self.assertEqual(pairs, [],
                         "a subset signature must never be matched")
        self.assertEqual(profiles["PS_KEYS_OLD"]["signature"], "",
                         "the truncated object must carry no signature")
        self.assertEqual(profiles["PS_KEYS"]["signature"].count("|") + 1, 2,
                         "the complete object keeps its signature")

    def test_a_fully_collected_neighbour_is_unaffected(self):
        profiles, _ = self._build(max_fields=4)
        self.assertTrue(profiles["PS_KEYS"]["signature"])
        self.assertEqual(profiles["PS_KEYS"]["liveness"], "empty")


if __name__ == "__main__":
    unittest.main()
