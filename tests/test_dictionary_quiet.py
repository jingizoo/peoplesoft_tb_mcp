"""The suggestion ladder stops hammering the dictionary.

The DBA's finding on the DEV deployment: the table-list probe against
ALL_OBJECTS was the hottest statement in the shared pool -- up to eight
LIKE executions per unresolved table name, on a dictionary that sits
behind PeopleSoft row-security policies. These tests hold the fix: one
bounded object-name read per PROCESS (cached beside the columns cache,
dropped by clear_catalog), the same shrinking-prefix suggestion
semantics served from that cache, and zero new dictionary reads on
every later miss.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pstb.config import Config
from pstb.db import Database
from pstb.engine import TBEngine


def _env(root):
    db_path = Path(root) / "p.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE PS_JRNL_LN (BU TEXT, JRNL_ID TEXT);"
        "CREATE TABLE PS_JRNL_HEADER (BU TEXT, JRNL_ID TEXT);"
        "CREATE TABLE PS_VENDOR (VENDOR_ID TEXT);"
        "CREATE VIEW PS_JRNL_V AS SELECT BU FROM PS_JRNL_LN;")
    for n in range(8):
        con.execute("INSERT INTO PS_JRNL_LN VALUES ('US001', ?)",
                    (f"J{n}",))
    con.commit()
    con.close()
    cfg = Config.sample(Path(root))
    cfg.db.sqlite_path = str(db_path)
    cfg.sources = {}
    return cfg, Database(cfg)


class _Counting(Database):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.executed = []

    def query(self, sql, params=None, max_rows=None):
        self.executed.append(sql)
        return super().query(sql, params, max_rows)


class ObjectNameCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-sugg-")
        cfg, _db = _env(self.temp.name)
        _db.close()
        self.cfg = cfg
        self.db = _Counting(cfg)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_one_read_per_process_and_identity_on_reuse(self):
        first = self.db.object_names()
        second = self.db.object_names()
        self.assertIs(first, second)
        self.assertEqual(len(self.db.executed), 1)
        self.assertIn(("MAIN", "PS_JRNL_LN"), first)
        self.assertIn(("MAIN", "PS_JRNL_V"), first)

    def test_clear_catalog_drops_the_cache(self):
        self.db.object_names()
        self.db.clear_catalog()
        self.db.object_names()
        self.assertEqual(len(self.db.executed), 2)

    def test_the_oracle_read_is_scoped_and_like_free(self):
        """The whole point: ONE owner-scoped read of ALL_OBJECTS with no
        LIKE and no per-prefix repetition."""
        cfg = Config.sample(Path(self.temp.name))
        cfg.db.backend = "oracle"
        cfg.db.schema = "SYSADM"
        cfg.db.schemas = ["SYSADM"]
        db = Database(cfg)
        seen = []

        def scripted(sql, params=None, max_rows=None):
            seen.append((sql, dict(params or {})))
            return ([{"s": "SYSADM", "n": "PS_LEDGER"}], False)

        db.query = scripted
        pairs = db.object_names()
        self.assertEqual(pairs, (("SYSADM", "PS_LEDGER"),))
        self.assertEqual(len(seen), 1)
        sql, params = seen[0]
        self.assertIn("ALL_OBJECTS", sql)
        self.assertNotIn("LIKE", sql.upper())
        self.assertEqual(params, {"o0": "SYSADM"})


class SuggestionLadderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-sugg2-")
        self.cfg, db = _env(self.temp.name)
        db.close()
        self.db = _Counting(self.cfg)
        self.engine = TBEngine(self.db, self.cfg)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _ladder_probes(self):
        """The offending statement class: pattern probes (LIKE :pat).
        Per-candidate equality lookups (_approx_rows existence/count)
        are a different, cheap class and deliberately not counted."""
        return [s for s in self.db.executed if ":pat" in s]

    def _cache_loads(self):
        return [s for s in self.db.executed
                if "NOT LIKE 'sqlite_%'" in s]

    def test_a_typo_still_finds_its_record(self):
        suggestions = self.engine._suggest_tables("PS_JRNL_LINE")
        self.assertIn("PS_JRNL_LN", suggestions)

    def test_a_qualified_typo_comes_back_qualified(self):
        suggestions = self.engine._suggest_tables("MAIN.PS_JRNL_LINE")
        self.assertTrue(any(s.startswith("MAIN.") for s in suggestions))

    def test_repeated_misses_never_probe_the_dictionary(self):
        """THE fix: up to eight LIKE :pat probes per miss became ZERO
        -- ever -- and the object list is read exactly once per
        process, however many misses follow."""
        for miss in ("PS_JRNL_LINE", "PS_VENDOR_MASTER", "PS_JRNL_HDR",
                     "PS_VOUCHERX", "PS_JRNL_LINE", "PS_LEDGER_KK"):
            self.engine._suggest_tables(miss)
        self.assertEqual(self._ladder_probes(), [])
        self.assertEqual(len(self._cache_loads()), 1)

    def test_an_unreadable_dictionary_degrades_to_no_suggestions(self):
        def refuse():
            raise RuntimeError("dictionary unavailable")

        with patch.object(self.db, "object_names", refuse):
            self.assertEqual(
                self.engine._suggest_tables("PS_JRNL_LINE"), [])

    def test_populated_tables_outrank_empty_lookalikes(self):
        """PS_JRNL_LN has rows and PS_JRNL_HEADER has none; the
        populated record ranks above the empty look-alike. (The view is
        left out of the comparison -- counting through it reaches the
        base table's rows.)"""
        suggestions = self.engine._suggest_tables("PS_JRNL")
        self.assertIn("PS_JRNL_LN", suggestions)
        self.assertIn("PS_JRNL_HEADER", suggestions)
        self.assertLess(suggestions.index("PS_JRNL_LN"),
                        suggestions.index("PS_JRNL_HEADER"))


if __name__ == "__main__":
    unittest.main()
