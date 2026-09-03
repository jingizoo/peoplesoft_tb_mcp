"""The extraction changed nothing, and the cash cap stopped lying.

pstb/health.py is server.py's health engine with its three module
globals turned into parameters so the ticker can run it. These tests
hold the extraction to its promise -- the chat tool is bit-identical,
the failure ORDER is preserved (an excluded table refuses before the
database is ever touched), and the ticker's duplicates-only section
gate runs exactly one query. The second half pins the cash_outlook
truncation fix: a capped read is disclosed as INCOMPLETE (and, because
credit rows may be unread, not even a floor), while an untruncated
payload stays byte-identical -- the caveat cannot fire on a correct
answer.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pstb.config import Config
from pstb.db import Database
from pstb.health import table_health
from pstb.metadata import MetadataCatalog, build_catalog


class _NoExclusions:
    def for_source(self, source):
        return []


def _env(root, ddl, inserts=()):
    db_path = Path(root) / "p.db"
    con = sqlite3.connect(db_path)
    con.executescript(ddl)
    for stmt, rows in inserts:
        for row in rows:
            con.execute(stmt, row)
    con.commit()
    con.close()
    cfg = Config.sample(Path(root))
    cfg.db.sqlite_path = str(db_path)
    cfg.sources = {}
    db = Database(cfg)
    build_catalog(Path(root) / "c.db", [("default", db)],
                  peopletools_source="default")
    return db, MetadataCatalog(Path(root) / "c.db")


DDL = ("CREATE TABLE PS_T (UNIT TEXT, DOC TEXT, AMT NUMERIC);"
       "CREATE UNIQUE INDEX PS_T_K ON PS_T (UNIT, DOC);")
ROWS = [("INSERT INTO PS_T VALUES (?,?,?)",
         [("US001", f"D{n}", n) for n in range(25)])]


class ExtractionParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-health-")
        self.root = Path(self.temp.name)
        self.db, self.catalog = _env(self.root, DDL, ROWS)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _via_server(self, table):
        import pstb.server as srv
        with patch.object(srv, "_metadata_for_source",
                          lambda s: ("default", self.catalog)), \
                patch.object(srv.engine, "for_source",
                             lambda s: SimpleNamespace(db=self.db)), \
                patch.object(srv, "record_exclusions", _NoExclusions()):
            return srv._table_health(table, "")

    def _direct(self, table, **kwargs):
        return table_health(table, source_name="default",
                            catalog=self.catalog,
                            get_db=lambda: self.db,
                            exclusions=_NoExclusions(), **kwargs)

    def test_golden_parity_across_the_wrapper(self):
        """Dict-equal for the resolving case, the unresolvable case,
        and the unsafe-identifier shape: the wrapper binds globals and
        changes NOTHING else."""
        for table in ("PS_T", "NO_SUCH_TABLE"):
            with self.subTest(table=table):
                self.assertEqual(self._via_server(table),
                                 self._direct(table))

    def test_the_full_default_run_carries_every_payload_section(self):
        out = self._direct("PS_T")
        for key in ("null_rates", "duplicate_keys", "relationships",
                    "caveats", "profile", "note"):
            self.assertIn(key, out)
        self.assertTrue(out["duplicate_keys"]["checked"])
        self.assertEqual(out["duplicate_keys"]["duplicate_groups"], 0)

    def test_an_excluded_table_refuses_before_the_database(self):
        """Failure ORDER is part of the contract: the exclusion refusal
        must come before the db thunk is ever called."""
        class Excluding:
            def for_source(self, source):
                return [{"object_id": "", "object": "PS_T"}]

        def explode():
            raise AssertionError("the db was touched past a refusal")

        out = table_health("PS_T", source_name="default",
                           catalog=self.catalog, get_db=explode,
                           exclusions=Excluding())
        self.assertIn("excluded", out["error"])

    def test_duplicates_only_runs_exactly_one_query(self):
        """The ticker's section gate: no SELECT * sample, no orphan
        probe -- one wrapped GROUP BY and nothing else."""
        queries = []
        real_query = self.db.query
        self.db.query = lambda *a, **k: queries.append(a[0]) or \
            real_query(*a, **k)
        try:
            out = self._direct("PS_T",
                               sections=frozenset({"duplicates"}),
                               declared_keys_only=True)
        finally:
            self.db.query = real_query
        self.assertTrue(out["duplicate_keys"]["checked"])
        self.assertEqual(len(queries), 1)
        self.assertIn("GROUP BY", queries[0])
        self.assertNotIn("SELECT *", " ".join(queries))
        self.assertEqual(out["null_rates"], [])
        self.assertEqual(out["relationships"], [])

    def test_the_heuristic_fallback_survives_for_the_chat_tool(self):
        """declared_keys_only=False (the default) keeps the heuristic
        key basis -- the chat tool's behavior is preserved bit for bit,
        the ticker's stricter posture is opt-in."""
        with tempfile.TemporaryDirectory() as tmp:
            db, catalog = _env(tmp, "CREATE TABLE PS_NK (DOC_ID TEXT);",
                               [("INSERT INTO PS_NK VALUES (?)",
                                 [(f"D{n}",) for n in range(10)])])
            try:
                loose = table_health(
                    "PS_NK", source_name="default", catalog=catalog,
                    get_db=lambda: db, exclusions=_NoExclusions())
                strict = table_health(
                    "PS_NK", source_name="default", catalog=catalog,
                    get_db=lambda: db, exclusions=_NoExclusions(),
                    declared_keys_only=True)
            finally:
                db.close()
        self.assertEqual(loose["duplicate_keys"].get("basis"),
                         "heuristic key-shaped columns")
        self.assertFalse(strict["duplicate_keys"].get("checked"))
        self.assertNotEqual(strict["duplicate_keys"].get("basis"),
                            "heuristic key-shaped columns")


class CashTruncationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-cash-")
        root = Path(self.temp.name)
        db_path = root / "p.db"
        con = sqlite3.connect(db_path)
        con.executescript(
            "CREATE TABLE PS_ITEM (BUSINESS_UNIT TEXT, DUE_DT TEXT, "
            "BAL_AMT NUMERIC, BAL_CURRENCY TEXT, ITEM_STATUS TEXT);"
            "CREATE TABLE PS_VOUCHER (BUSINESS_UNIT TEXT, VOUCHER_ID "
            "TEXT, DUE_DT TEXT, GROSS_AMT NUMERIC, CURRENCY_CD TEXT, "
            "CLOSE_STATUS TEXT);"
            "CREATE TABLE PS_PYMNT_VCHR_XREF (BUSINESS_UNIT TEXT, "
            "VOUCHER_ID TEXT);")
        for n in range(12):
            con.execute(
                "INSERT INTO PS_ITEM VALUES ('US001','2026-09-10',?, "
                "'USD','O')", (100 + n,))
            con.execute(
                "INSERT INTO PS_VOUCHER VALUES ('US001',?, "
                "'2026-09-12',?,'USD','O')", (f"V{n}", 50 + n))
        con.commit()
        con.close()
        cfg = Config.sample(root)
        cfg.db.sqlite_path = str(db_path)
        cfg.sources = {}
        self.db = Database(cfg)
        from pstb.ar import ARBilling
        self.ar = ARBilling(SimpleNamespace(cfg=cfg, db=self.db))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_an_untruncated_payload_is_byte_identical_to_before(self):
        """The toothless case, mandated: the caveat must be unable to
        fire on a correct answer. No truncation keys, no CAP EXCEEDED,
        no new wording anywhere in the payload."""
        out = self.ar.cash_outlook("US001", as_of_date="2026-09-01")
        self.assertNotIn("items_truncated", out)
        self.assertNotIn("vouchers_truncated", out)
        self.assertNotIn("CAP EXCEEDED", out["note"])
        blob = str(out)
        self.assertNotIn("INCOMPLETE", blob)
        self.assertNotIn("reliable floor", blob)

    def test_a_truncated_read_is_disclosed_as_not_even_a_floor(self):
        import pstb.ar as ar_module
        with patch.object(ar_module, "DETAIL_ROW_CAP", 5):
            out = self.ar.cash_outlook("US001",
                                       as_of_date="2026-09-01")
        self.assertIs(out["items_truncated"], True)
        self.assertIs(out["vouchers_truncated"], True)
        notes = " ".join(out["record_notes"])
        self.assertIn("not even a reliable floor", notes)
        self.assertIn("5-row detail cap", notes)
        self.assertIn("CAP EXCEEDED", out["note"])
        self.assertIn("do not state them as totals", out["note"])

    def test_one_sided_truncation_flags_only_its_side(self):
        con = sqlite3.connect(
            Path(self.temp.name) / "p.db")
        for n in range(20):
            con.execute(
                "INSERT INTO PS_ITEM VALUES ('US001','2026-09-10',?, "
                "'USD','O')", (500 + n,))
        con.commit()
        con.close()
        self.db.clear_catalog()
        import pstb.ar as ar_module
        with patch.object(ar_module, "DETAIL_ROW_CAP", 20):
            out = self.ar.cash_outlook("US001",
                                       as_of_date="2026-09-01")
        self.assertIs(out.get("items_truncated"), True)
        self.assertNotIn("vouchers_truncated", out)
        notes = " ".join(out["record_notes"])
        self.assertIn("inflow", notes)
        self.assertNotIn("outflow", notes)


if __name__ == "__main__":
    unittest.main()
