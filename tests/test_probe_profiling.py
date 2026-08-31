"""The probe is the only thing that can answer questions macOS cannot.

Everything it reports decides what gets built next, and it runs exactly
once, by hand, on a machine this developer cannot reach. So the things
worth testing are not its numbers -- those come from the instance -- but
its PROMISES: that it reads only the data dictionary, that it writes
nothing, that a line of stored source can never reach its output, and
that a section which cannot be read degrades into a labelled line
instead of taking the rest of the run down with it.
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import re
import unittest
from pathlib import Path

from pstb.db import DbError

REPO = Path(__file__).resolve().parent.parent
SOURCE_SENTINEL = "SECRET-PACKAGE-BODY-LINE-9999"


def _probe():
    spec = importlib.util.spec_from_file_location(
        "probe_profiling", REPO / "scripts" / "probe_profiling.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDatabase:
    """An Oracle-shaped double that records every statement it is given."""

    dialect = "oracle"

    def __init__(self, unreadable=(), rows_for=None):
        self.seen: list[str] = []
        self.unreadable = tuple(unreadable)
        self.rows_for = rows_for or {}

        class _DbCfg:
            schemas = ["SYSADM", "TUSER"]

        class _Cfg:
            db = _DbCfg()

        self.cfg = _Cfg()

    def query(self, sql, params=None, max_rows=None):
        self.seen.append(sql)
        for needle in self.unreadable:
            # a plain string raises the grant-style error; a
            # (needle, message) pair raises that exact message, so a
            # test can present a timeout vs an overflow and watch the
            # probe respond differently.
            if isinstance(needle, tuple):
                needle, message = needle
                if needle in sql:
                    raise DbError(message)
                continue
            if needle in sql:
                raise DbError(f"ORA-00942: table or view does not exist "
                              f"[{needle}]")
        for needle, rows in self.rows_for.items():
            if needle in sql:
                return list(rows), []
        # Every scalar query gets a number; every listing gets one row
        # carrying a sentinel in each text-ish field.
        return [{"n": 7, "owner": "TUSER", "name": "PKG_LOAD",
                 "type": "PACKAGE BODY", "lines": 120, "objects": 3,
                 "ncols": 12,
                 # The sentinel stands for ALL_SOURCE.TEXT alone. Column
                 # NAMES are metadata the probe prints on purpose; source
                 # text is the thing that must never appear.
                 "text": SOURCE_SENTINEL, "names": "SETID, BUSINESS_UNIT"}], []

    def close(self):
        pass


class _Registry:
    def __init__(self, db):
        self._db = db

    def resolve_name(self, name):
        return name or "default"

    def get(self, name):
        return self._db


def _run(module, db, argv=()):
    out = io.StringIO()
    module.load_config = lambda *a, **k: object()
    module.Database = lambda cfg: db
    module.SourceRegistry = lambda cfg, primary: _Registry(db)
    with contextlib.redirect_stdout(out):
        code = module.main(list(argv))
    return code, out.getvalue()


class ProbePromiseTests(unittest.TestCase):
    def setUp(self):
        self.module = _probe()

    def test_every_statement_is_a_read_of_the_data_dictionary(self):
        """The probe runs on a production box under a read-only account.
        A single non-SELECT, or one read of a business table, is the
        thing that loses the grant everything else depends on."""
        db = FakeDatabase()
        code, _ = _run(self.module, db)
        self.assertEqual(code, 0)
        self.assertTrue(db.seen)
        for sql in db.seen:
            head = sql.lstrip().split(None, 1)[0].upper()
            self.assertIn(head, {"SELECT", "WITH"}, sql)
            for target in re.findall(r"\bFROM\s+([A-Za-z_$#][\w$#]*)", sql,
                                     re.I):
                self.assertTrue(
                    target.upper().startswith(("ALL_", "DBA_", "V$", "USER_")),
                    f"reads {target!r}, which is not a dictionary view: {sql}")

    def test_no_line_of_stored_source_can_reach_the_output(self):
        """ALL_SOURCE.TEXT is application code: it carries literals,
        thresholds, credentials people should not have inlined, and
        comments about customers. The probe's output is copied off the
        work box by hand into a chat window."""
        db = FakeDatabase()
        _, printed = _run(self.module, db)
        self.assertNotIn(SOURCE_SENTINEL, printed)
        for sql in db.seen:
            projection = sql.upper().split("FROM", 1)[0]
            self.assertNotIn(
                " TEXT", projection.replace("LENGTH(TEXT)", ""),
                f"projects raw source text: {sql}")

    def test_an_unreadable_section_does_not_take_the_run_down(self):
        """One missing grant must cost one line, not every finding after
        it. main() has no try/except around the sections, so this holds
        only because each query goes through the _rows/_one seam."""
        db = FakeDatabase(unreadable=("ALL_SOURCE", "ALL_OBJECTS",
                                      "DBA_TAB_MODIFICATIONS"))
        code, printed = _run(self.module, db)
        self.assertEqual(code, 0)
        self.assertIn("UNREADABLE", printed)
        self.assertIn("GRANTS", printed)
        self.assertIn("Nothing was written", printed)

    def test_the_go_no_go_ratio_is_computed_and_explained(self):
        """A bare count of readable units is unreadable as evidence. The
        RATIO is the finding: 4,000 units and 12 readable is a privilege
        gap, and the operator must not file it as 'we have no PL/SQL'."""
        db = FakeDatabase(rows_for={
            # Insertion order matters: both unit queries contain
            # "FROM ALL_OBJECTS", and COPIES now contains an EXISTS of
            # its own -- the LINE = 1 probe is the one distinctive
            # marker of the readable-units query.
            "S.LINE = 1": [{"n": 8}],
            "FROM ALL_OBJECTS": [{"n": 400}],
        })
        _, printed = _run(self.module, db)
        self.assertIn("2.0% readable", printed)
        self.assertIn("GO/NO-GO", printed)
        self.assertIn("grant to", printed)

    def test_a_zero_denominator_does_not_crash_the_probe(self):
        db = FakeDatabase(rows_for={"FROM ALL_OBJECTS": [{"n": 0}]})
        code, printed = _run(self.module, db)
        self.assertEqual(code, 0)
        self.assertNotIn("%", printed.split("GO/NO-GO")[0][-200:])

    def test_the_expensive_sections_can_be_skipped(self):
        """They aggregate over one row per line of stored code. An
        operator who has already been burned by a timeout needs the
        cheap findings without them."""
        db = FakeDatabase()
        _, full = _run(self.module, db)
        _, skipped = _run(self.module, db, ["--skip-expensive"])
        self.assertIn("PLSQL SHAPE", full)
        self.assertNotIn("PLSQL SHAPE", skipped)
        self.assertIn("COVERAGE", skipped)
        self.assertIn("GRANTS", skipped)

    def test_no_skippable_run_ever_scans_source_lines(self):
        """ALL_SOURCE is one row per LINE of stored code. The go/no-go
        ratio and a GRANTS row both scanned it while wearing cheap
        clothes -- the ratio's COUNT(DISTINCT ...) read every line of
        the schema's source, and the owners count read every line of the
        INSTANCE's, in a section --skip-expensive does not gate. Both
        timed out on the real box at 180s. The invariant: outside the
        sections marked EXPENSIVE, every query that touches ALL_SOURCE
        is either first-row bounded or a per-unit LINE=1 probe."""
        db = FakeDatabase()
        code, _ = _run(self.module, db, ["--skip-expensive"])
        self.assertEqual(code, 0)
        touching = [sql for sql in db.seen if "ALL_SOURCE" in sql.upper()]
        self.assertTrue(touching)
        for sql in touching:
            self.assertTrue(
                "ROWNUM" in sql.upper() or "S.LINE = 1" in sql,
                f"scans source lines outside the expensive gate: {sql}")

    def test_status_all_lists_everything_instead_of_crashing(self):
        """The runbook said `--status all`; the store said only the five
        decided states were words. The operator's traceback is why the
        store now speaks the operator's word for the empty filter."""
        import tempfile
        from pathlib import Path as _P
        from pstb.source_knowledge import SourceKnowledge
        with tempfile.TemporaryDirectory() as tmp:
            sk = SourceKnowledge(_P(tmp) / "sk.db", source="default",
                                 source_fingerprint="sha256:" + "0" * 64)
            sk.propose(object_id="tbl:MAIN.TU_X7", schema="MAIN",
                       object_name="TU_X7", object_kind="table",
                       meaning="Vendor invoice staging rows")
            self.assertEqual(sk.list_proposals("all"),
                             sk.list_proposals(""))

    def test_copies_scans_only_live_tables(self):
        """Measured on the real instance: 76,044 of 84,532 tables are
        empty, and the unfiltered aggregate read ten times the columns
        to rank copies of tables the profiler refuses anyway. It timed
        out at 180s; the live filter is what makes it finish."""
        db = FakeDatabase()
        _run(self.module, db)
        copies = [sql for sql in db.seen if "ORA_HASH" in sql]
        self.assertTrue(copies)
        for sql in copies:
            self.assertIn("NUM_ROWS > 0", sql)

    def test_a_copies_timeout_is_not_retried_but_an_overflow_is(self):
        """The fallback exists for one error -- LISTAGG overflowing 4000
        chars -- and retrying on ANY error made a timeout cost two full
        timeouts back to back."""
        timeout = FakeDatabase(unreadable=[
            ("ORA_HASH", "Query exceeded the 180s timeout. Narrow the "
                         "scope or raise db.query_timeout_seconds.")])
        _run(self.module, timeout)
        self.assertEqual(
            sum(1 for sql in timeout.seen if "ORA_HASH" in sql), 1)
        overflow = FakeDatabase(unreadable=[
            ("LISTAGG", "ORA-01489: result of string concatenation is "
                        "too long")])
        _run(self.module, overflow)
        self.assertEqual(
            sum(1 for sql in overflow.seen if "ORA_HASH" in sql), 2)

    def test_the_awr_grant_row_reads_one_row_not_millions(self):
        """DBA_HIST_SQLSTAT held 4.7M rows when first measured; counting
        them on every probe is a timeout waiting to happen, and it
        happened. The grants question is reach, nothing else."""
        db = FakeDatabase()
        _run(self.module, db)
        awr = [sql for sql in db.seen if "DBA_HIST_SQLSTAT" in sql]
        self.assertTrue(awr)
        for sql in awr:
            self.assertIn("ROWNUM", sql.upper())

    def test_the_owners_sweep_is_deliberately_unscoped(self):
        """The package that loads a custom schema is often owned next
        door. A scoped sweep would report zero while the code sits one
        owner away, and the operator would conclude there is none."""
        db = FakeDatabase()
        _, printed = _run(self.module, db)
        owners_sql = [s for s in db.seen
                      if "GROUP BY OWNER" in s and "FROM ALL_SOURCE" in s
                      and "NAME" not in s.split("GROUP BY")[1]]
        self.assertTrue(owners_sql)
        self.assertNotIn("OWNER IN", owners_sql[0])
        self.assertIn("UNSCOPED", printed)

    def test_a_non_oracle_source_is_refused_before_any_query(self):
        db = FakeDatabase()
        db.dialect = "sqlite"
        code, printed = _run(self.module, db)
        self.assertEqual(code, 2)
        self.assertEqual(db.seen, [])
        self.assertIn("not oracle", printed)

    def test_the_docstring_does_not_promise_what_the_probe_stopped_doing(self):
        """It used to say the whole run is cheap. Two sections aggregate
        over ALL_SOURCE and one pattern-matches every line of it; an
        operator who trusts a stale promise runs them on a busy box."""
        text = (REPO / "scripts" / "probe_profiling.py").read_text()
        doc = text.split('"""')[1]
        self.assertIn("EXPENSIVE", doc)
        self.assertIn("TIMES OUT", doc)
        self.assertNotIn("it is cheap", doc)


if __name__ == "__main__":
    unittest.main()
