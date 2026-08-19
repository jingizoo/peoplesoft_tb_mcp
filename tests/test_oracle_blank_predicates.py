"""Oracle stores '' as NULL, so `x <> ''` matches nothing. Ban the form.

This is the shape of the defect: on SQLite the empty string is a real value
distinct from NULL, so `AND L.PO_ID <> ''` filters exactly as written and
every test passes. On Oracle the same predicate is `x <> NULL`, which is
UNKNOWN for EVERY row — including the populated ones — so the query returns
an empty set. Nothing raises. The control simply reports nothing to see.

Measured on the bundled sample with Oracle's parse rule emulated, before the
fix:

    SQLite      over_order 1  not_received 1  no_receipt 1  never_invoiced 1
    Oracle      over_order 0  not_received 0  no_receipt 0  never_invoiced 4

Three exception categories report a clean three-way match while real breaks
exist, and never_invoiced inflates because the PO-linked voucher rows have
vanished from the anti-join. An accountant sees a green workbench.

A companion `IS NOT NULL` does not save it — the UNKNOWN from the second
predicate still fails the AND, which is why three of the four sites had one
and were broken anyway.
"""
from __future__ import annotations

import pathlib
import re
import unittest

from pstb import queries as q

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Modules whose SQL runs against the CONFIGURED database (Oracle in
# production). The graph and catalog artifacts are SQLite files this project
# creates itself, where '' is a real value and the comparison is honest.
_SQLITE_ARTIFACT_MODULES = {"metadata.py", "procgraph.py"}

_BLANK_COMPARE = re.compile(r"""(?<![<>!])(=|<>)\s*''""")

# SQL here is built from implicitly-concatenated string literals spanning
# several lines, so the two things that make a hit legitimate — the
# sqlite3 cursor that runs it, and the `IS NULL` half of the defensive pair
# — routinely sit on a DIFFERENT line from the comparison itself. Judging a
# line in isolation produced four false positives in entitygraph.py (reads
# of the local graph file) and five in psquery.py (correct IS NULL pairs).
# Look at the statement around the hit instead.
_WINDOW = 6
# A sqlite3 cursor on an artifact this project builds itself, where the
# empty string is a real value and the comparison means what it says.
_ARTIFACT_CURSOR = re.compile(r"\bcon\.execute\(|\bconn\.execute\(")


def _offending_lines():
    """(path, lineno, line) for empty-string compares that reach Oracle."""
    for path in sorted(ROOT.joinpath("pstb").rglob("*.py")):
        if path.name in _SQLITE_ARTIFACT_MODULES or path.name == "queries.py":
            continue                      # queries.py defines and explains it
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if not _BLANK_COMPARE.search(line):
                continue
            window = "\n".join(lines[max(0, index - _WINDOW):index + 2])
            if _ARTIFACT_CURSOR.search(window):
                continue                  # local SQLite artifact, not Oracle
            if "IS NULL" in window:
                continue                  # the correct defensive pair
            yield path, index + 1, line


class BlankStringPredicateTests(unittest.TestCase):
    def test_the_helper_is_the_both_dialect_form(self):
        self.assertEqual(q.nonblank("L.PO_ID"), "LENGTH(TRIM(L.PO_ID)) > 0")

    def test_no_module_compares_a_column_to_the_empty_string(self):
        """The negative form is always wrong; the positive form is a trap.

        `x <> ''` silently returns nothing on Oracle. `x = ''` silently
        returns nothing too — which is only safe when an `IS NULL` sits
        beside it, and relying on every future author to remember that is
        how three of these four shipped. Use queries.nonblank(), or its
        negation, and the question does not arise.
        """
        offenders = [
            f"{path.relative_to(ROOT)}:{number}: {line.strip()[:88]}"
            for path, number, line in _offending_lines()
        ]
        self.assertEqual(
            offenders, [],
            "compare against the empty string reaches Oracle, where '' IS "
            "NULL and the predicate matches no rows:\n  "
            + "\n  ".join(offenders)
            + "\nUse queries.nonblank(col).")

    def test_the_four_repaired_sites_use_the_helper(self):
        """Pin the specific queries the blocker was found in."""
        for module, needle in (
            ("procurement.py", "q.nonblank('L.PO_ID')"),
            ("procurement.py", "q.nonblank('PO_ID')"),
            ("modules.py", "q.nonblank('V.INVOICE_ID')"),
            ("entitygraph.py", "q.nonblank('L.IDENTIFIER')"),
        ):
            with self.subTest(module=module, needle=needle):
                text = ROOT.joinpath("pstb", module).read_text()
                self.assertIn(needle, text)


class OracleEmulationTests(unittest.TestCase):
    """Run the real control under Oracle's rule and demand identical answers.

    A static ban stops the literal coming back. This catches the same defect
    arriving by another route — a helper that builds the predicate, a column
    compared to a bind that happens to be blank.
    """

    @staticmethod
    def _exceptions(emulate_oracle):
        from pstb.config import load_config
        from pstb.db import Database
        from pstb.engine import TBEngine
        from pstb.modules import ModulePacks
        from pstb.procurement import Procurement

        cfg = load_config(None)
        db = Database(cfg)
        original = db.query

        def query(sql, binds=None, **kw):
            if emulate_oracle:
                # Oracle's own parse rule: '' is NULL, so a comparison
                # against it is UNKNOWN for every row.
                sql = re.sub(r"(\S+)\s*<>\s*''",
                             r"1=0 AND \1 IS NOT NULL", sql)
            return original(sql, binds, **kw)

        db.query = query
        control = Procurement(ModulePacks(TBEngine(db, cfg)))
        found = control.match_exceptions(business_unit="US001", months=24)
        return {key: len(value)
                for key, value in (found.get("exceptions") or {}).items()
                if isinstance(value, list)}

    def test_match_exceptions_agrees_across_dialects(self):
        sqlite_answer = self._exceptions(False)
        oracle_answer = self._exceptions(True)
        self.assertTrue(sqlite_answer, "the sample lost its match exceptions")
        self.assertEqual(
            oracle_answer, sqlite_answer,
            "the three-way match reports different exceptions on Oracle than "
            "on SQLite — a blank-string predicate is filtering everything out")

    def test_the_sample_actually_exercises_the_predicate(self):
        """Guard the guard: with no exceptions, the test above proves nothing."""
        self.assertTrue(
            any(count > 0 for count in self._exceptions(False).values()),
            "no match exceptions in the sample, so a dialect difference "
            "could not be observed either way")


if __name__ == "__main__":
    unittest.main()
