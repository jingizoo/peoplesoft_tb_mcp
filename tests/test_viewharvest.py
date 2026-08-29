"""What a view's author knew, and everything the harvest must not take.

A view is the one place a badly named schema writes down its own meaning:
`SELECT A.C1 AS INVOICE_NUMBER ... FROM TU_X7 A JOIN TU_Q2 B ON A.C2 =
B.C1` names two tables, names a column, and asserts a join — none of
which the data dictionary holds. The catalog has always refused to store
view SQL and still does; this harvest reads a definition, keeps
STRUCTURE, and drops the text.

So the tests that matter are boundaries. What a literal must never
become. What an expression must never be called. What an unresolvable
alias must never be attributed to. And what a declared-but-unenforced
join must never be mistaken for.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pstb import viewharvest
from pstb.config import Config
from pstb.db import Database
from pstb.metadata import (MetadataBuildLimits, MetadataCatalog,
                           build_catalog)


class NoiseStrippingTests(unittest.TestCase):
    def test_a_literal_can_never_be_read_as_a_join_operand(self):
        """The WHERE clause of a real view carries business rules and
        thresholds. Stripping literals BEFORE matching is what makes it
        impossible for one to be extracted by accident rather than
        merely unlikely."""
        sql = ("SELECT A.C1 AS INVOICE_NUMBER FROM TU_X7 A "
               "JOIN TU_Q2 B ON A.C2 = B.C1 "
               "WHERE A.C9 = 'B.C1' AND A.C4 > 10000")
        stripped = viewharvest.strip_noise(sql)
        self.assertNotIn("'B.C1'", stripped)
        joins = viewharvest.join_predicates(sql)
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0]["left_column"], "C2")

    def test_a_comment_cannot_smuggle_a_join(self):
        sql = ("SELECT A.C1 AS INV FROM TU_X7 A /* ON A.C8 = B.C8 */ "
               "JOIN TU_Q2 B ON A.C2 = B.C1 -- A.C7 = B.C7")
        joins = viewharvest.join_predicates(sql)
        self.assertEqual([(j["left_column"], j["right_column"])
                          for j in joins], [("C2", "C1")])

    def test_a_q_quoted_literal_can_neither_smuggle_nor_desync(self):
        """Oracle's alternative quoting: q'[ ... ]' holds anything,
        apostrophes included. A stripper that only knows '...' reads
        `q'[don` as the literal, leaves the rest as code, and lifts a
        join out of the INSIDE of a string. That edge then ranks at the
        declared tier AND silences the value-overlap miner for the pair
        -- a fabricated relationship suppressing the measurement that
        would have contradicted it."""
        for opener, closer in (("[", "]"), ("(", ")"), ("{", "}"),
                               ("<", ">"), ("!", "!")):
            sql = ("SELECT A.C1 AS INVOICE_NUMBER FROM TU_X7 A "
                   "JOIN TU_Q2 B ON A.C1 = B.C1 "
                   f"WHERE A.C9 = q'{opener}don't A.C7 = B.C7{closer}'")
            self.assertEqual(
                [(j["left_column"], j["right_column"])
                 for j in viewharvest.join_predicates(sql)],
                [("C1", "C1")], f"leaked through q'{opener}...{closer}'")

    def test_no_ordering_of_substitutions_could_have_been_correct(self):
        """The reason this is a scanner and not three regexes. Strip
        comments first and a string containing `--` eats a real join;
        strip strings first and a quote inside a comment eats the rest
        of the text. Only knowing which construct you are inside tells
        those apart, and both must work at once."""
        both = ("SELECT A.C1 FROM TU_X7 A JOIN TU_Q2 B ON A.C1 = B.C1 "
                "AND A.C8 = 'va--l'  -- it's a note\n"
                "AND A.C5 = B.C5")
        self.assertEqual(
            [(j["left_column"], j["right_column"])
             for j in viewharvest.join_predicates(both)],
            [("C1", "C1"), ("C5", "C5")])

    def test_a_quoted_identifier_is_code_and_passes_through(self):
        """Oracle permits "odd--name". Treating its contents as a
        comment would delete real code; treating an apostrophe inside it
        as a string would desynchronise everything after it."""
        text, complete = viewharvest.scrub(
            'SELECT A."odd--name" FROM TU_X7 A WHERE A."it\'s" = 1')
        self.assertTrue(complete)
        self.assertIn('"odd--name"', text)
        self.assertIn('"it\'s"', text)

    def test_an_unterminated_literal_yields_nothing_at_all(self):
        """Past an unterminated quote nothing can be classified as code
        or as literal. There is no safe partial answer, so every
        extractor returns empty rather than guessing."""
        sql = ("SELECT A.C1 AS INVOICE_NUMBER FROM TU_X7 A "
               "JOIN TU_Q2 B ON A.C1 = B.C1 WHERE A.C9 = 'oops")
        self.assertEqual(viewharvest.scrub(sql)[1], False)
        self.assertEqual(viewharvest.join_predicates(sql), [])
        self.assertEqual(viewharvest.column_vocabulary(sql), [])
        self.assertEqual(viewharvest.table_aliases(sql), {})
        for unterminated in ("SELECT A.C1 FROM TU_X7 A /* open",
                             "SELECT A.C1 FROM TU_X7 A WHERE X = q'[open"):
            self.assertEqual(viewharvest.scrub(unterminated)[1], False,
                             unterminated)

    def test_a_stripped_literal_cannot_glue_two_identifiers(self):
        """Replacing a literal with empty text would make A'x'.C1 read as
        A.C1 on a table that was never named."""
        stripped = viewharvest.strip_noise("A'weld'B")
        self.assertNotIn("AB", stripped.replace(" ", ""))


class AliasResolutionTests(unittest.TestCase):
    def test_schema_qualified_and_bare_sources_both_resolve(self):
        aliases = viewharvest.table_aliases(
            "SELECT * FROM SYSADM.PS_VOUCHER V JOIN TU_Q2 ON 1=1")
        self.assertEqual(aliases["V"], ("SYSADM", "PS_VOUCHER"))
        self.assertEqual(aliases["TU_Q2"], ("", "TU_Q2"))

    def test_a_keyword_is_never_read_as_an_alias(self):
        """`FROM TU_X7 WHERE ...` must not register a table aliased
        WHERE; a join through it would name an object that does not
        exist."""
        aliases = viewharvest.table_aliases(
            "SELECT C1 FROM TU_X7 WHERE C9 = 'OPEN'")
        self.assertNotIn("WHERE", aliases)
        self.assertEqual(aliases["TU_X7"], ("", "TU_X7"))

    def test_a_subquery_source_yields_no_attributable_columns(self):
        """Columns of a derived table belong to no catalog object, so a
        join through one would attribute the relationship to whichever
        table happened to be inside."""
        sql = ("SELECT S.C1 AS INVOICE_NUMBER FROM "
               "(SELECT C1, C2 FROM TU_X7) S JOIN TU_Q2 B ON S.C2 = B.C1")
        self.assertEqual(viewharvest.join_predicates(sql), [])
        self.assertEqual(viewharvest.column_vocabulary(sql), [])

    def test_an_unresolved_alias_skips_the_predicate(self):
        joins = viewharvest.join_predicates(
            "SELECT * FROM TU_X7 A JOIN TU_Q2 B ON A.C2 = ZZ.C1")
        self.assertEqual(joins, [])

    def test_a_self_join_says_nothing_new(self):
        joins = viewharvest.join_predicates(
            "SELECT * FROM TU_X7 A JOIN TU_X7 B ON A.C2 = B.C1")
        self.assertEqual(joins, [])

    def test_the_same_pair_written_twice_is_one_edge(self):
        joins = viewharvest.join_predicates(
            "SELECT * FROM TU_X7 A JOIN TU_Q2 B ON A.C2 = B.C1 "
            "WHERE B.C1 = A.C2")
        self.assertEqual(len(joins), 1)


class JoinPredicateTests(unittest.TestCase):
    def test_only_qualified_equals_qualified_survives(self):
        """A predicate against a bind, a function or a constant is a
        filter, not a relationship."""
        sql = ("SELECT * FROM TU_X7 A JOIN TU_Q2 B "
               "ON A.C2 = B.C1 AND A.C9 = 'OPEN' "
               "AND A.C4 = TRUNC(B.C4) AND B.C3 = :bind")
        joins = viewharvest.join_predicates(sql)
        self.assertEqual([(j["left_column"], j["right_column"])
                          for j in joins], [("C2", "C1")])

    def test_multi_column_joins_are_each_kept(self):
        joins = viewharvest.join_predicates(
            "SELECT * FROM TU_X7 A JOIN TU_Q2 B "
            "ON A.C2 = B.C1 AND A.C5 = B.C5")
        self.assertEqual(len(joins), 2)


class VocabularyTests(unittest.TestCase):
    def test_a_column_alias_is_the_rosetta_stone(self):
        entries = viewharvest.column_vocabulary(
            "SELECT A.C1 AS INVOICE_NUMBER FROM TU_X7 A")
        self.assertEqual(entries, [{"schema": "", "object": "TU_X7",
                                    "column": "C1",
                                    "means": "INVOICE_NUMBER"}])

    def test_an_expression_never_names_a_column(self):
        """`SUM(A.C4) AS TOTAL_DUE` describes a computation. Calling
        TOTAL_DUE a name for C4 would be false, and a false meaning
        misroutes every question that uses the word."""
        sql = ("SELECT SUM(A.C4) AS TOTAL_DUE, "
               "CASE WHEN A.C9 = 'O' THEN 1 ELSE 0 END AS IS_OPEN, "
               "A.C7 || B.C7 AS FULL_NAME, A.C1 AS INVOICE_NUMBER "
               "FROM TU_X7 A JOIN TU_Q2 B ON A.C2 = B.C1")
        entries = viewharvest.column_vocabulary(sql)
        self.assertEqual([e["means"] for e in entries], ["INVOICE_NUMBER"])

    def test_a_comma_inside_a_function_is_not_an_item_boundary(self):
        """`COALESCE(A.C1, B.C1) AS X` is ONE select item.

        A naive split on commas cuts it in two and the second half,
        ` B.C1) AS X`, reads like a column being named X. Today the
        stray paren is what stops it -- the full-match refuses the
        fragment. That is an accident of where the paren fell, not a
        decision, so the boundary is asserted here where it is made.
        """
        self.assertEqual(
            [item.strip() for item in viewharvest._split_select_items(
                " COALESCE(A.C1, B.C1) AS INVOICE_NUMBER,"
                " A.C3 AS PAYMENT_TERMS")],
            ["COALESCE(A.C1, B.C1) AS INVOICE_NUMBER",
             "A.C3 AS PAYMENT_TERMS"])
        entries = viewharvest.column_vocabulary(
            "SELECT COALESCE(A.C1, B.C1) AS INVOICE_NUMBER, "
            "A.C3 AS PAYMENT_TERMS FROM TU_X7 A JOIN TU_Q2 B ON A.C2=B.C1")
        self.assertEqual([e["means"] for e in entries], ["PAYMENT_TERMS"])

    def test_an_empty_alias_teaches_nothing(self):
        entries = viewharvest.column_vocabulary(
            "SELECT A.C1 AS ID, A.C2 AS VALUE, A.C3 AS STATUS, "
            "A.C4 AS PAYMENT_TERMS FROM TU_X7 A")
        self.assertEqual([e["means"] for e in entries], ["PAYMENT_TERMS"])

    def test_an_alias_equal_to_the_column_teaches_nothing(self):
        self.assertEqual(viewharvest.column_vocabulary(
            "SELECT A.INVOICE_NUMBER AS INVOICE_NUMBER FROM TU_X7 A"), [])

    def test_readable_words(self):
        self.assertEqual(viewharvest.readable_words("INVOICE_NUMBER"),
                         "invoice number")
        self.assertEqual(viewharvest.readable_words("GL$ACCT#2"),
                         "gl acct 2")


class HarvestEndToEndTests(unittest.TestCase):
    """The whole harvest on a schema whose table names say nothing."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-viewharvest-")
        root = Path(self.temp.name)
        self.db_path = root / "p.db"
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            CREATE TABLE TU_X7 (C1 TEXT, C2 TEXT, C4 NUMERIC, C9 TEXT);
            CREATE TABLE TU_Q2 (C1 TEXT, C7 TEXT);
            CREATE VIEW OPEN_INVOICES AS
              SELECT A.C1 AS INVOICE_NUMBER,
                     B.C7 AS VENDOR_NAME,
                     SUM(A.C4) AS TOTAL_DUE
                FROM TU_X7 A JOIN TU_Q2 B ON A.C2 = B.C1
               WHERE A.C9 = 'OPEN';
            CREATE VIEW INVOICE_STATES AS
              SELECT A.C9 AS PAYMENT_STATUS FROM TU_X7 A;
            CREATE TABLE TU_SHARED_A (
                SETID TEXT, BUSINESS_UNIT TEXT, AMOUNT NUMERIC);
            CREATE TABLE TU_SHARED_B (
                SETID TEXT, BUSINESS_UNIT TEXT, LABEL TEXT);
            CREATE VIEW COMPOSITE_ITEMS AS
              SELECT A.AMOUNT AS OPEN_AMOUNT
                FROM TU_SHARED_A A JOIN TU_SHARED_B B
                  ON A.SETID = B.SETID
                 AND A.BUSINESS_UNIT = B.BUSINESS_UNIT;
            CREATE VIEW RIVAL_ITEMS AS
              SELECT A.AMOUNT AS RIVAL_AMOUNT
                FROM TU_SHARED_A A JOIN TU_SHARED_B B
                  ON A.SETID = B.BUSINESS_UNIT;
        """)
        for i in range(60):
            con.execute("INSERT INTO TU_X7 VALUES (?,?,?,?)",
                        (f"I{i:04d}", f"V{i % 9}", i, "OPEN"))
            con.execute("INSERT INTO TU_Q2 VALUES (?,?)",
                        (f"V{i % 9}", f"Vendor {i % 9}"))
            # 60 distinct business units, deliberately: below the
            # miner's 20-value minimum the pair is unminable and the
            # silencing test would pass without silencing anything.
            con.execute("INSERT INTO TU_SHARED_A VALUES (?,?,?)",
                        ("SHARE", f"BU{i:03d}", i))
            con.execute("INSERT INTO TU_SHARED_B VALUES (?,?,?)",
                        ("SHARE", f"BU{i:03d}", f"L{i}"))
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

    def _notes(self, con):
        return " ".join(str(r[0]) for r in con.execute(
            "SELECT note FROM notes WHERE layer='view_vocabulary'"))

    def test_the_declared_join_becomes_a_labeled_edge(self):
        con = self._build()
        edges = [dict(r) for r in con.execute(
            "SELECT * FROM edges WHERE kind='view_declared_join' "
            "AND evidence LIKE '%OPEN_INVOICES%'")]
        con.close()
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["authority"], "declared")
        self.assertEqual(edge["collector"], "view_vocabulary")
        attrs = json.loads(edge["attrs"])
        self.assertEqual(attrs["column_pairs"],
                         [{"column": "C2", "referenced_column": "C1"}])
        self.assertEqual(attrs["declared_in_view"], "OPEN_INVOICES")
        self.assertIs(attrs["enforced"], False)

    def test_the_column_learns_the_word_a_person_used_for_it(self):
        con = self._build()
        terms = {(r["tbl"], r["col"], r["text"]) for r in con.execute(
            "SELECT o.name AS tbl, n.name AS col, t.text AS text "
            "FROM search_terms t JOIN nodes n ON n.id = t.node_id "
            "JOIN edges e ON e.dst = n.id AND e.kind='object_has_column' "
            "JOIN nodes o ON o.id = e.src "
            "WHERE t.facet LIKE 'view %'")}
        con.close()
        self.assertIn(("TU_X7", "C1", "INVOICE_NUMBER"), terms)
        self.assertIn(("TU_X7", "C1", "invoice number"), terms)
        self.assertIn(("TU_Q2", "C7", "VENDOR_NAME"), terms)
        # The aggregate names a computation, not C4.
        self.assertEqual([t for t in terms if t[2] == "TOTAL_DUE"], [])

    def test_the_definition_itself_is_never_stored(self):
        """The rule that made the catalog refuse view text in the first
        place: a definition can carry thresholds, comments and rules
        nobody audited into an artifact that outlives the connection."""
        con = self._build()
        leaked = con.execute(
            "SELECT COUNT(*) FROM search_terms "
            "WHERE text LIKE '%SELECT%' OR text LIKE '%FROM %' "
            "OR text = 'OPEN'").fetchone()[0]
        edge_text = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='view_declared_join' "
            "AND (attrs LIKE '%SELECT%' OR evidence LIKE '%SELECT%')"
        ).fetchone()[0]
        con.close()
        self.assertEqual(leaked, 0)
        self.assertEqual(edge_text, 0)

    def test_the_harvest_can_be_switched_off(self):
        con = self._build(harvest_view_vocabulary=False)
        edges = con.execute("SELECT COUNT(*) FROM edges "
                            "WHERE kind='view_declared_join'").fetchone()[0]
        note = self._notes(con)
        con.close()
        self.assertEqual(edges, 0)
        self.assertIn("switched off", note)

    def test_a_bitten_cap_is_disclosed_not_hidden(self):
        """A partial harvest is fine. A partial harvest presented as the
        whole schema's vocabulary is not: the caller would read an empty
        answer as 'nothing was declared' rather than 'we stopped
        looking'."""
        full = self._build()
        everything = {r[0] for r in full.execute(
            "SELECT text FROM search_terms WHERE facet LIKE 'view %'")}
        full.close()
        self.assertIn("PAYMENT_STATUS", everything)

        con = self._build(max_view_definitions=1)
        note = self._notes(con)
        capped = {r[0] for r in con.execute(
            "SELECT text FROM search_terms WHERE facet LIKE 'view %'")}
        con.close()
        self.assertIn("only the first 1 view definitions were read", note)
        self.assertIn("max_view_definitions", note)
        # The cap really stopped the read; it did not merely narrate one.
        self.assertNotIn("PAYMENT_STATUS", capped)

    def test_a_composite_join_keeps_every_column_it_was_given(self):
        """A composite join is ONE relationship on SEVERAL columns. An
        edge per column meant ON CONFLICT(src,dst,kind) DO NOTHING kept
        the first and dropped the rest, so SETID + BUSINESS_UNIT was
        stored as SETID alone -- and reported complete. On PeopleSoft
        SETID is a low-cardinality sharing key: that is a fan-out sold
        as a join, on the commonest key shape in the product."""
        con = self._build()
        edges = [dict(r) for r in con.execute(
            "SELECT * FROM edges WHERE kind='view_declared_join' "
            "AND evidence LIKE '%COMPOSITE_ITEMS%'")]
        con.close()
        self.assertEqual(len(edges), 1)
        attrs = json.loads(edges[0]["attrs"])
        self.assertEqual(attrs["column_pairs"],
                         [{"column": "SETID", "referenced_column": "SETID"},
                          {"column": "BUSINESS_UNIT",
                           "referenced_column": "BUSINESS_UNIT"}])
        self.assertIs(attrs["pairs_complete"], True)

        catalog = MetadataCatalog(self.catalog_path)
        hops = [hop for hop in catalog.relationships_of(
            "TU_SHARED_A", source="default")["relationships"]
            if hop["relationship"] == "view_declared_join"]
        self.assertEqual(len(hops), 1)
        self.assertEqual(
            [(p["left_column"], p["right_column"], p["ordinal"])
             for p in hops[0]["column_pairs"]],
            [("SETID", "SETID", 1), ("BUSINESS_UNIT", "BUSINESS_UNIT", 2)])
        self.assertTrue(hops[0]["column_pairs_complete"])

    def test_every_column_of_a_composite_join_silences_the_miner(self):
        """The collapse had a second victim. The miner is silenced per
        COLUMN PAIR, so a dropped pair was also a pair nobody suppressed
        -- the catalog asserted the join on one column and then measured
        the other, offering the same relationship twice under two
        different confidences."""
        con = self._build()
        mined = [json.loads(r[0])["column_pairs"] for r in con.execute(
            "SELECT e.attrs FROM edges e "
            "JOIN nodes l ON l.id=e.src JOIN nodes r ON r.id=e.dst "
            "WHERE e.kind='value_overlap_join' "
            "AND l.name IN ('TU_SHARED_A','TU_SHARED_B') "
            "AND r.name IN ('TU_SHARED_A','TU_SHARED_B')")]
        con.close()
        for pairs in mined:
            for pair in pairs:
                self.assertNotEqual(
                    (pair["column"], pair["referenced_column"]),
                    ("BUSINESS_UNIT", "BUSINESS_UNIT"))

    def test_a_second_condition_between_the_same_objects_is_disclosed(self):
        """Only one edge can exist per (src, dst, kind), so a second
        view joining the same objects differently is lost. Losing it
        silently would present one condition as the only way these
        objects relate."""
        catalog = MetadataCatalog(self.catalog_path)
        self._build().close()
        hops = [hop for hop in catalog.relationships_of(
            "TU_SHARED_A", source="default")["relationships"]
            if hop["relationship"] == "view_declared_join"]
        self.assertEqual(len(hops), 1)
        self.assertIn("other view(s) join these objects on DIFFERENT",
                      hops[0]["caveat"])

    def test_a_partly_unusable_condition_is_not_called_complete(self):
        """Grouping created this case and therefore had to answer it. If
        one predicate of a composite join names a column the catalog
        does not hold, the remaining predicates are still worth having
        -- but they are no longer the whole condition, and a caller told
        they were complete would join on strictly fewer columns than the
        author wrote."""
        from pstb import metadata
        rows = [{"schema_name": "MAIN", "view_name": "PARTIAL_VIEW",
                 "text": "SELECT A.AMOUNT AS X FROM TU_SHARED_A A "
                         "JOIN TU_SHARED_B B ON A.SETID = B.SETID "
                         "AND A.BUSINESS_UNIT = B.NOT_A_COLUMN"}]
        with unittest.mock.patch.object(
                metadata, "_view_definitions", return_value=(rows, "test")):
            con = self._build()
        edges = [dict(r) for r in con.execute(
            "SELECT * FROM edges WHERE kind='view_declared_join'")]
        con.close()
        self.assertEqual(len(edges), 1)
        attrs = json.loads(edges[0]["attrs"])
        self.assertEqual(attrs["column_pairs"],
                         [{"column": "SETID", "referenced_column": "SETID"}])
        self.assertIs(attrs["pairs_complete"], False)

        catalog = MetadataCatalog(self.catalog_path)
        hop = [h for h in catalog.relationships_of(
            "TU_SHARED_A", source="default")["relationships"]
            if h["relationship"] == "view_declared_join"][0]
        self.assertFalse(hop["column_pairs_complete"])
        self.assertIn("PART OF THE CONDITION IS MISSING", hop["caveat"])

    def test_a_truncated_definition_cannot_mint_a_phantom_column(self):
        """Oracle serves a long view definition through a VARCHAR2
        projection, so it arrives CUT. `A.C2 = B.C1` cut to `A.C2 = B.C`
        still parses as a join and would name a column that does not
        exist -- an edge no consumer downstream could tell from a real
        one. Both names must be columns the catalog actually holds."""
        from pstb import metadata
        con = self._build()
        rows = [{"schema_name": "MAIN", "view_name": "CUT_VIEW",
                 "text": "SELECT A.C1 FROM TU_X7 A JOIN TU_Q2 B "
                         "ON A.C2 = B.C"}]
        before = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='view_declared_join' "
            "AND evidence LIKE '%OPEN_INVOICES%'").fetchone()[0]
        con.close()
        with unittest.mock.patch.object(
                metadata, "_view_definitions",
                return_value=(rows, "test")):
            con = self._build()
        after = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='view_declared_join' "
            "AND evidence LIKE '%CUT_VIEW%'").fetchone()[0]
        con.close()
        self.assertEqual(before, 1)
        self.assertEqual(after, 0)

    def test_a_declared_join_silences_the_miner_for_that_pair(self):
        """Measuring a pair a person already wrote down spends probes to
        buy a weaker second opinion. Foreign keys get this rule; an
        asserted join earns it too."""
        con = self._build()
        mined = [dict(r) for r in con.execute(
            "SELECT l.name AS l, r.name AS r, e.attrs AS attrs "
            "FROM edges e JOIN nodes l ON l.id=e.src "
            "JOIN nodes r ON r.id=e.dst "
            "WHERE e.kind='value_overlap_join'")]
        con.close()
        for edge in mined:
            pairs = {(p["column"], p["referenced_column"])
                     for p in json.loads(edge["attrs"])["column_pairs"]}
            self.assertNotIn(("C2", "C1"), pairs)
            self.assertNotIn(("C1", "C2"), pairs)

    def test_join_path_walks_it_but_refuses_to_compile_it(self):
        """Intent is not integrity. The path is worth surfacing; the SQL
        is not worth writing, because nothing ever checked it."""
        self._build().close()
        catalog = MetadataCatalog(self.catalog_path)
        path = catalog.relationship_path("TU_X7", "TU_Q2", source="default")
        self.assertTrue(path["found"])
        self.assertEqual(path["relationship_evidence_classes"],
                         ["view_declared_join"])
        self.assertFalse(path["queryable_join"])
        self.assertIn("DECLARED", path["basis"])
        hop = path["hops"][0]
        self.assertEqual(hop["relationship"], "view_declared_join")
        self.assertIn("not enforced", hop["caveat"])
        self.assertEqual(hop["declared_in_view"], "OPEN_INVOICES")

    def test_the_hop_reads_in_the_direction_it_is_walked(self):
        """Walked from TU_Q2, the pair must invert: a hop that reports
        the far table's column as the near one would compile a join on
        the wrong side if any consumer ever trusted it."""
        self._build().close()
        catalog = MetadataCatalog(self.catalog_path)
        hops = [hop for hop in catalog.relationships_of(
            "TU_Q2", source="default")["relationships"]
            if hop["relationship"] == "view_declared_join"]
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0]["direction"], "referenced_by")
        self.assertEqual(hops[0]["column_pairs"][0]["left_column"], "C1")
        self.assertEqual(hops[0]["column_pairs"][0]["right_column"], "C2")


class EvidenceRankTests(unittest.TestCase):
    """When the database enforces a join and a view merely asserts one,
    the enforced answer must win. Rank is the whole point of adding a
    third class: it sits between what is guaranteed and what is
    measured, and it must never displace either neighbour."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-viewrank-")
        root = Path(self.temp.name)
        db_path = root / "p.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE PARENT (PARENT_ID TEXT PRIMARY KEY, C7 TEXT);
            CREATE TABLE LOOKUP (VENDOR_CD TEXT PRIMARY KEY);
            CREATE TABLE CHILD (
                CHILD_ID TEXT PRIMARY KEY, PARENT_ID TEXT, ALT_KEY TEXT,
                VENDOR_CD TEXT,
                FOREIGN KEY (PARENT_ID) REFERENCES PARENT(PARENT_ID));
            CREATE VIEW CHILD_ROLLUP AS
              SELECT C.CHILD_ID AS DOCUMENT_NUMBER
                FROM CHILD C JOIN PARENT P ON C.ALT_KEY = P.C7;
        """)
        for i in range(40):
            con.execute("INSERT INTO PARENT VALUES (?,?)", (f"P{i}", f"K{i}"))
            con.execute("INSERT INTO LOOKUP VALUES (?)", (f"V{i}",))
            con.execute("INSERT INTO CHILD VALUES (?,?,?,?)",
                        (f"C{i}", f"P{i}", f"K{i}", f"V{i}"))
        con.commit()
        con.close()
        cfg = Config.sample(root)
        cfg.db.sqlite_path = str(db_path)
        cfg.sources = {}
        self.catalog_path = root / "c.db"
        db = Database(cfg)
        try:
            build_catalog(self.catalog_path, [("default", db)],
                          peopletools_source="default")
        finally:
            db.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_an_enforced_key_outranks_an_asserted_join(self):
        catalog = MetadataCatalog(self.catalog_path)
        path = catalog.relationship_path("CHILD", "PARENT",
                                         source="default")
        self.assertEqual(path["relationship_evidence_classes"],
                         ["foreign_key"])
        self.assertTrue(path["queryable_join"])
        self.assertIn("PARENT_ID", path["sql"])
        self.assertNotIn("ALT_KEY", path["sql"])

    def test_an_asserted_join_outranks_a_measured_one(self):
        """The middle tier has to be a tier, not a label. A person who
        wrote the join in a view knew something; containment measured
        from 40 sampled values knows only that the values line up. When
        both reach the same node the assertion must be offered first."""
        catalog = MetadataCatalog(self.catalog_path)
        kinds = [hop["relationship"] for hop in catalog.relationships_of(
            "CHILD", source="default")["relationships"]
            if hop["relationship"] in ("view_declared_join",
                                       "value_overlap")]
        self.assertIn("view_declared_join", kinds)
        self.assertIn("value_overlap", kinds)
        self.assertLess(kinds.index("view_declared_join"),
                        kinds.index("value_overlap"))

    def test_both_classes_are_still_offered_to_a_reader(self):
        """Outranked is not hidden: the asserted join is a real thing a
        person wrote and stays visible with its label, ordered after
        the key rather than dropped."""
        catalog = MetadataCatalog(self.catalog_path)
        kinds = [hop["relationship"] for hop in catalog.relationships_of(
            "CHILD", source="default")["relationships"]]
        self.assertEqual(kinds[0], "foreign_key")
        self.assertIn("view_declared_join", kinds)


class TierExhaustionTests(unittest.TestCase):
    """A short weak path must not shadow a longer guaranteed one.

    LEG_A -> MIDDLE -> LEG_B is two enforced foreign keys and compiles
    to join SQL. A view also asserts LEG_A -> LEG_B directly, one hop.
    Shortest-path alone takes the assertion, and the caller loses both
    the SQL and the guarantee to save a hop. Each tier is therefore
    exhausted before the next is consulted -- the same rule that already
    kept measurement from shadowing declaration.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-viewtier-")
        root = Path(self.temp.name)
        db_path = root / "p.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE LEG_B (B_ID TEXT PRIMARY KEY, TAG TEXT);
            CREATE TABLE MIDDLE (
                MID_ID TEXT PRIMARY KEY, B_ID TEXT,
                FOREIGN KEY (B_ID) REFERENCES LEG_B(B_ID));
            CREATE TABLE LEG_A (
                A_ID TEXT PRIMARY KEY, MID_ID TEXT, TAG TEXT,
                FOREIGN KEY (MID_ID) REFERENCES MIDDLE(MID_ID));
            CREATE VIEW SHORTCUT AS
              SELECT A.A_ID AS DOCUMENT_NUMBER
                FROM LEG_A A JOIN LEG_B B ON A.TAG = B.TAG;
        """)
        for i in range(30):
            con.execute("INSERT INTO LEG_B VALUES (?,?)", (f"B{i}", f"T{i}"))
            con.execute("INSERT INTO MIDDLE VALUES (?,?)", (f"M{i}", f"B{i}"))
            con.execute("INSERT INTO LEG_A VALUES (?,?,?)",
                        (f"A{i}", f"M{i}", f"T{i}"))
        con.commit()
        con.close()
        cfg = Config.sample(root)
        cfg.db.sqlite_path = str(db_path)
        cfg.sources = {}
        self.catalog_path = root / "c.db"
        db = Database(cfg)
        try:
            build_catalog(self.catalog_path, [("default", db)],
                          peopletools_source="default")
        finally:
            db.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_the_assertion_exists_and_is_the_shorter_route(self):
        """Without this the tier test passes vacuously: if the view
        harvest never produced the shortcut, there was never a shorter
        route for the tiers to have to refuse."""
        con = sqlite3.connect(self.catalog_path)
        shortcut = con.execute(
            "SELECT l.name, r.name FROM edges e "
            "JOIN nodes l ON l.id = e.src JOIN nodes r ON r.id = e.dst "
            "WHERE e.kind='view_declared_join'").fetchall()
        con.close()
        self.assertEqual(shortcut, [("LEG_A", "LEG_B")])
        catalog = MetadataCatalog(self.catalog_path)
        kinds = [hop["relationship"] for hop in catalog.relationships_of(
            "LEG_A", source="default")["relationships"]]
        self.assertIn("view_declared_join", kinds)

    def test_two_enforced_hops_beat_one_asserted_hop(self):
        catalog = MetadataCatalog(self.catalog_path)
        path = catalog.relationship_path("LEG_A", "LEG_B", source="default")
        self.assertTrue(path["found"])
        self.assertEqual(path["hop_count"], 2)
        self.assertEqual(path["relationship_evidence_classes"],
                         ["foreign_key"])
        self.assertTrue(path["queryable_join"])


if __name__ == "__main__":
    unittest.main()
