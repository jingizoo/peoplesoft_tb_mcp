"""Curated tools must survive a site whose records lack optional columns.

The deployment target is a real PeopleSoft instance whose record shapes differ
from the reference layout by release and by customisation. Before this suite,
dropping one decorative column from PS_GL_ACCOUNT_TBL did not merely fail one
check — it raised out of the shared context builder and aborted the entire
close_readiness playbook with a traceback.

These tests build a real SQLite database with those columns removed and assert
the tools adapt. The distinction they enforce throughout:

  optional column absent  -> answer the question, label degrades to NULL
  required column absent  -> fail loudly; a wrong number is worse than an error
  probe could not run     -> report skipped, NEVER "clean"

That last one is the dangerous case. Each control probe defaults to an empty
list on failure, and an empty list reads exactly like "nothing found".
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config, load_config  # noqa: E402
from pstb.db import Database, _blank_literals, _bound_params, bind_names  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.playbooks import PlaybookRunner  # noqa: E402


def _mutated_db(dest: Path, drops: list) -> Path:
    """Copy the sample and physically remove columns from it."""
    shutil.copy(ROOT / "sample_data" / "ps_sample.db", dest)
    con = sqlite3.connect(dest)
    # The sample ships convenience views over these tables; SQLite refuses to
    # drop a column any view still references, and a real site would not have
    # the repo's own views anyway.
    for (v,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
        con.execute(f"DROP VIEW {v}")
    for table, col in drops:
        con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    con.commit()
    con.close()
    return dest


class ShapeToleranceTests(unittest.TestCase):
    DROPS = [("PS_GL_ACCOUNT_TBL", "EFF_STATUS"),
             ("PS_JRNL_HEADER", "OPRID"),
             ("PS_JRNL_HEADER", "SOURCE")]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-shape-"))
        path = _mutated_db(self.tmp / "mutated.db", self.DROPS)
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(path)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_columns_really_are_missing(self) -> None:
        # Guard the guard: if the copy silently kept the columns, every other
        # assertion in this class would pass while proving nothing.
        for table, col in self.DROPS:
            self.assertNotIn(col.upper(), self.db.columns(table),
                             f"{table}.{col} was supposed to be dropped")

    def test_trial_balance_still_balances_without_eff_status(self) -> None:
        tb = self.engine.trial_balance()
        rows = tb.get("rows") or []
        self.assertTrue(rows, "trial balance returned no rows")
        dr = sum(float(r.get("debit") or 0) for r in rows)
        cr = sum(float(r.get("credit") or 0) for r in rows)
        self.assertAlmostEqual(dr, cr, places=2)

    def test_integrity_check_reports_the_balance_it_can_compute(self) -> None:
        ic = self.engine.tb_integrity_check()
        self.assertTrue(ic["balanced"])
        self.assertAlmostEqual(ic["total_debits"], ic["total_credits"], places=2)

    def test_unposted_journals_survive_missing_oprid_and_source(self) -> None:
        ic = self.engine.tb_integrity_check()
        self.assertEqual(ic["checks_incomplete"], [],
                         "a control probe failed on a merely-decorative column")
        rows = ic["unposted_journals"]
        self.assertTrue(rows, "the sample has an unposted journal to find")
        # The finding survives; the labels degrade to NULL rather than to an
        # ORA-00904.
        self.assertTrue(all(r.get("journal_id") for r in rows))
        self.assertTrue(all(r.get("oprid") is None for r in rows))

    def test_close_readiness_matches_the_intact_database(self) -> None:
        # The real contract: a site missing these columns gets the same answer,
        # not a degraded one and certainly not a traceback.
        mutated = PlaybookRunner(self.engine).run("close_readiness")

        intact_cfg = load_config(str(ROOT / "config.yaml"))
        intact_db = Database(intact_cfg)
        try:
            intact = PlaybookRunner(
                TBEngine(intact_db, intact_cfg)).run("close_readiness")
        finally:
            intact_db.close()

        self.assertEqual(mutated["verdict"], intact["verdict"])
        self.assertEqual(
            [(s["step"], s["status"]) for s in mutated["steps"]],
            [(s["step"], s["status"]) for s in intact["steps"]],
            "a missing decorative column changed the close verdict")


class FailedProbeIsNeverCleanTests(unittest.TestCase):
    """A probe that could not run must report skipped, never 'clean'."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-probe-"))
        # Drop a column the unposted-journal check genuinely needs, so the
        # probe fails outright rather than adapting.
        path = _mutated_db(self.tmp / "broken.db",
                           [("PS_JRNL_HEADER", "JRNL_HDR_STATUS")])
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(path)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failed_probe_is_named_and_blocks_a_passed_status(self) -> None:
        ic = self.engine.tb_integrity_check()
        names = [c["check"] for c in ic["checks_incomplete"]]
        self.assertIn("unposted_journals", names)
        self.assertEqual(ic["control_status"], "checks_incomplete")
        self.assertNotEqual(ic["control_status"], "passed")

    def test_the_balance_verdict_survives_a_failed_control_probe(self) -> None:
        # Losing the journal check must not cost the balance answer.
        ic = self.engine.tb_integrity_check()
        self.assertTrue(ic["balanced"])

    def test_playbook_step_reports_skipped_not_clean(self) -> None:
        result = PlaybookRunner(self.engine).run("close_readiness")
        step = next(s for s in result["steps"] if s["step"] == "unposted")
        self.assertEqual(
            step["status"], "skipped",
            "a journal check that never read a journal reported: "
            f"{step['headline']!r}")
        self.assertNotIn("every journal in scope is posted", step["headline"])
        self.assertEqual(result["verdict"], "incomplete")
        self.assertIn("NOT a pass", result["summary"])


class _MutatedCase(unittest.TestCase):
    """Base for cases that need a sample with specific columns removed."""

    DROPS: list = []

    def setUp(self) -> None:
        if not self.DROPS:
            self.skipTest("base class")
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-mut-"))
        path = _mutated_db(self.tmp / "m.db", self.DROPS)
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(path)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
            shutil.rmtree(self.tmp, ignore_errors=True)


class StatisticsCodeTests(_MutatedCase):
    """The shared basis clause is the widest blast radius in the codebase:
    every ledger query funnels through it, so one ORA-00904 there takes out
    every GL number the product can produce."""

    DROPS = [("PS_LEDGER", "STATISTICS_CODE")]

    def test_trial_balance_survives_a_ledger_without_statistics_code(self) -> None:
        tb = self.engine.trial_balance()
        rows = tb.get("rows") or []
        self.assertTrue(rows)
        dr = sum(float(r.get("ending_dr") or 0) for r in rows)
        self.assertGreater(dr, 0)

    def test_the_totals_are_unchanged(self) -> None:
        # A record with no STATISTICS_CODE column cannot hold a statistical
        # row, so dropping the predicate is exactly right, not merely
        # tolerable — the number must come out identical.
        mutated = self.engine.trial_balance()
        intact_cfg = load_config(str(ROOT / "config.yaml"))
        intact_db = Database(intact_cfg)
        try:
            intact = TBEngine(intact_db, intact_cfg).trial_balance()
        finally:
            intact_db.close()
        self.assertEqual(
            sum(float(r.get("ending_dr") or 0) for r in mutated["rows"]),
            sum(float(r.get("ending_dr") or 0) for r in intact["rows"]))


class JournalDrillTests(_MutatedCase):
    DROPS = [("PS_JRNL_HEADER", "SOURCE"), ("PS_JRNL_HEADER", "OPRID"),
             ("PS_JRNL_HEADER", "DESCR254_MIXED"),
             ("PS_JRNL_LN", "LINE_DESCR")]

    def test_drill_survives_missing_labels(self) -> None:
        # drill_to_journals is the tool that proves a balance; it must not die
        # because nobody's name is attached to the journal.
        out = self.engine.drill_to_journals(account="6100", period=1)
        rows = out["journal_lines"]
        self.assertTrue(rows, "drill returned no journal lines")
        self.assertTrue(all(r.get("journal_id") for r in rows))
        self.assertTrue(all(r.get("amount") is not None for r in rows))


class DepartmentFilterRefusesTests(_MutatedCase):
    """A filter that cannot be applied must refuse, not return everything."""

    DROPS = [("PS_JRNL_LN", "DEPTID")]

    def test_department_filter_refuses_instead_of_ignoring(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.engine.drill_to_journals(account="6100", period=1,
                                          dept="10000")
        self.assertIn("DEPTID", str(ctx.exception))

    def test_the_same_drill_without_a_department_still_works(self) -> None:
        out = self.engine.drill_to_journals(account="6100", period=1)
        self.assertTrue(out["journal_lines"])


class CustomerNameTests(_MutatedCase):
    """NAME1 is a label in most queries and a search predicate in one. The
    predicate must not degrade to NULL: UPPER(NULL) LIKE :q matches nothing,
    so the site would be told a customer does not exist when it does."""

    DROPS = [("PS_CUSTOMER", "NAME1")]

    def test_aging_still_reports_balances_without_customer_names(self) -> None:
        from pstb.ar import ARBilling

        aging = ARBilling(self.engine).aging()
        self.assertTrue(aging.get("customers") or aging.get("totals"),
                        f"aging returned nothing: {list(aging)}")

    def test_customer_search_falls_back_to_id_and_says_so(self) -> None:
        from pstb.ar import ARBilling

        ar = ARBilling(self.engine)
        out = ar.search_customers(query="C1001")
        self.assertTrue(out["customers"],
                        "ID search stopped working when NAME1 went missing")
        notes = " ".join(out.get("record_notes") or [])
        self.assertIn("NAME1", notes)
        self.assertIn("name", notes.lower())

    def test_a_name_search_does_not_silently_return_nothing(self) -> None:
        from pstb.ar import ARBilling

        # The failure this guards: searching a real customer's name returns
        # zero rows with no explanation, which reads as "no such customer".
        out = ARBilling(self.engine).search_customers(query="ACME")
        self.assertTrue(out.get("record_notes"),
                        "a name search on a record with no NAME1 returned "
                        "no rows and no explanation")


class ContextContainmentTests(unittest.TestCase):
    """One unreadable input must cost its own steps, not the whole playbook.

    The other tests here cannot cover this: once the queries adapt, nothing
    raises, so the containment is never exercised. It still has to hold — the
    adapted queries cover the shapes anticipated, and this covers the ones not
    anticipated. DbError and EngineError are unrelated RuntimeError subclasses,
    so a narrowed catch here silently reopens the abort path.
    """

    def setUp(self) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()

    def _run_with_failing(self, attr: str, exc: Exception) -> dict:
        def boom(*a, **k):
            raise exc

        target = self.engine if hasattr(self.engine, attr) else None
        runner = PlaybookRunner(self.engine)
        holder = target or runner.ar
        original = getattr(holder, attr)
        setattr(holder, attr, boom)
        try:
            return runner.run("close_readiness")
        finally:
            setattr(holder, attr, original)

    def test_a_db_error_does_not_abort_the_playbook(self) -> None:
        from pstb.db import DbError

        result = self._run_with_failing(
            "tb_integrity_check",
            DbError("Column not found — no such column: ACCTG_DT"))
        self.assertEqual(result["verdict"], "incomplete")
        self.assertIn("NOT a pass", result["summary"])
        # The steps that do not need the ledger integrity check still ran.
        ran = [s for s in result["steps"] if s["status"] != "skipped"]
        self.assertTrue(ran, "one failed input skipped every single step")

    def test_an_unexpected_error_type_is_also_contained(self) -> None:
        # Drivers raise their own exception types; the containment must not
        # depend on enumerating them.
        result = self._run_with_failing(
            "tb_integrity_check", KeyError("ORACLE_DSN"))
        self.assertEqual(result["verdict"], "incomplete")

    def test_a_failing_ar_input_does_not_abort_the_playbook(self) -> None:
        from pstb.db import DbError

        result = self._run_with_failing("aging", DbError("PS_ITEM unreadable"))
        self.assertEqual(
            [s["step"] for s in result["steps"]
             if s["status"] == "skipped"], ["ar_tie"],
            "an unreadable AR record skipped steps that do not use AR")


class BindFilteringTests(unittest.TestCase):
    """opt_expr() may drop a predicate that carried a bind; the surviving
    parameter must not reach the driver (DPY-4008 in oracledb thin mode)."""

    def test_unreferenced_binds_are_dropped(self) -> None:
        sql = "SELECT 1 FROM T WHERE A = :bu AND 1 = 1"
        self.assertEqual(_bound_params(sql, {"bu": "US001", "setid": "SHARE"}),
                         {"bu": "US001"})

    def test_referenced_binds_are_kept_untouched(self) -> None:
        sql = "SELECT 1 FROM T WHERE A = :bu AND B = :setid"
        params = {"bu": "US001", "setid": "SHARE"}
        self.assertEqual(_bound_params(sql, params), params)

    def test_bind_name_matching_is_case_insensitive(self) -> None:
        # Dropping a bind the statement does use would turn a working query
        # into a missing-parameter error, so the comparison must not be
        # defeated by case.
        self.assertEqual(_bound_params("SELECT :BU FROM T", {"bu": 1}),
                         {"bu": 1})

    def test_a_bind_inside_a_string_literal_is_not_a_bind(self) -> None:
        sql = "SELECT 'text with :setid inside' FROM T WHERE A = :bu"
        self.assertEqual(bind_names(sql), {"bu"})
        self.assertEqual(_bound_params(sql, {"bu": 1, "setid": 2}), {"bu": 1})

    def test_literal_blanking_preserves_length(self) -> None:
        sql = "SELECT 'abc''def' FROM T WHERE X = :y"
        self.assertEqual(len(_blank_literals(sql)), len(sql))
        self.assertNotIn("abc", _blank_literals(sql))


if __name__ == "__main__":
    unittest.main()
