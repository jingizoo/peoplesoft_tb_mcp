"""Master data belongs to the period reported, not to the day you asked.

P1 #7 of the external review. PeopleSoft master data is effective-dated:
PS_GL_ACCOUNT_TBL and PSTREEDEFN hold one row per EFFDT, and the correct
row is the latest on or before the date the ANSWER is about. Every read
used ``today``.

The consequence is not a missing label, it is a wrong one. The bundled
sample renames account 4100 from "Service Revenue" to "Services &
Subscription Revenue" effective 2026-01-01. Pulled during FY2026, an
FY2025 comparative showed the FY2026 name — so the same statement printed
in December and in January disagreed, with no ledger row having changed
and nothing in either payload explaining why.

Trees are worse: a reorganisation moves accounts between nodes, so a
prior-year departmental rollup changes its SHAPE, not just its labels.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pstb.config import load_config
from pstb.db import Database
from pstb.engine import TBEngine
from pstb.queries import asof_expr

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data" / "ps_sample.db"

ACCOUNT = "4100"
OLD_NAME = "Service Revenue"
NEW_NAME = "Services & Subscription Revenue"
RENAME_EFFDT = "2026-01-01"


def _engine(db_path: Path, root: Path) -> TBEngine:
    (root / "config.yaml").write_text(
        "db:\n  backend: sqlite\n"
        f"  sqlite_path: {db_path}\n"
        "defaults:\n  business_unit: US001\n  ledger: ACTUALS\n")
    cfg = load_config(str(root / "config.yaml"))
    return TBEngine(Database(cfg), cfg)


class AccountEffectiveDatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE.exists():
            raise unittest.SkipTest("run scripts/seed_sample_data.py first")
        con = sqlite3.connect(SAMPLE)
        names = {r[0]: r[1] for r in con.execute(
            "SELECT EFFDT, DESCR FROM PS_GL_ACCOUNT_TBL WHERE ACCOUNT = ?",
            (ACCOUNT,))}
        con.close()
        if names.get(RENAME_EFFDT) != NEW_NAME:
            raise unittest.SkipTest(
                f"sample no longer renames {ACCOUNT} at {RENAME_EFFDT}")
        cls.cfg = load_config(None)
        cls.engine = TBEngine(Database(cls.cfg), cls.cfg)

    def _descr(self, fy: int, per: int):
        tb = self.engine.trial_balance(
            business_unit="US001", fiscal_year=fy, period=per, account=ACCOUNT)
        row = next((r for r in tb["rows"] if r["account"] == ACCOUNT), None)
        return (row or {}).get("descr"), tb["master_data"]

    def test_a_prior_year_keeps_the_name_it_had_then(self):
        descr, meta = self._descr(2025, 12)
        self.assertEqual(descr, OLD_NAME,
                         "an FY2025 comparative relabelled itself with the "
                         "FY2026 rename — the exact defect in review item 7")
        self.assertEqual(meta["effective_date"], "2025-12-31")
        self.assertEqual(meta["basis"], "period end")

    def test_the_current_year_shows_the_current_name(self):
        descr, meta = self._descr(2026, 6)
        self.assertEqual(descr, NEW_NAME)
        self.assertEqual(meta["effective_date"], "2026-06-30")

    def test_the_two_years_disagree_which_is_the_whole_point(self):
        self.assertNotEqual(self._descr(2025, 12)[0], self._descr(2026, 6)[0])

    def test_the_payload_says_which_vintage_it_read(self):
        _, meta = self._descr(2025, 12)
        self.assertIn("account description", meta["applies_to"])
        self.assertEqual(meta["effective_date"], "2025-12-31")

    def test_period_end_date_resolves_from_the_calendar(self):
        self.assertEqual(self.engine.period_end_date(2025, 12), "2025-12-31")
        self.assertEqual(self.engine.period_end_date(2026, 6), "2026-06-30")

    def test_adjustment_periods_use_the_year_end(self):
        """998 has no calendar row of its own on most installations."""
        self.assertEqual(self.engine.period_end_date(2025, 998), "2025-12-31")

    def test_a_nonsense_period_asks_for_nothing_rather_than_guessing(self):
        for fy, per in ((0, 6), (2025, 0), (-1, -1)):
            with self.subTest(fy=fy, per=per):
                self.assertEqual(self.engine.period_end_date(fy, per), "")


class AsOfExpressionTests(unittest.TestCase):
    """The seam every effective-dated read goes through."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(load_config(None))

    def test_a_date_becomes_a_bind_not_a_literal(self):
        params: dict = {}
        expr = asof_expr(self.db, params, "2025-12-31")
        self.assertEqual(expr, ":asof")
        self.assertEqual(params["asof"], "2025-12-31")

    def test_blank_keeps_today_for_genuinely_current_questions(self):
        params: dict = {}
        self.assertEqual(asof_expr(self.db, params, ""), self.db.today_expr())
        self.assertNotIn("asof", params,
                         "an unused bind on a query that never references it "
                         "is an ORA-01036 waiting on Oracle")

    def test_a_timestamp_is_trimmed_to_a_date(self):
        params: dict = {}
        asof_expr(self.db, params, "2025-12-31T23:59:59")
        self.assertEqual(params["asof"], "2025-12-31")

    def test_the_bind_name_can_be_varied_for_a_second_join(self):
        params: dict = {}
        self.assertEqual(
            asof_expr(self.db, params, "2025-12-31", key="tree_asof"),
            ":tree_asof")
        self.assertEqual(params["tree_asof"], "2025-12-31")


class MissingCalendarTests(unittest.TestCase):
    """A site whose calendar cannot answer must be told, not guessed at."""

    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE.exists():
            raise unittest.SkipTest("run scripts/seed_sample_data.py first")
        cls._dir = tempfile.TemporaryDirectory()
        root = Path(cls._dir.name)
        db_path = root / "nocal.db"
        shutil.copy(SAMPLE, db_path)
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM PS_CAL_DETP_TBL WHERE FISCAL_YEAR = 2025")
        con.commit()
        con.close()
        cls.engine = _engine(db_path, root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._dir.cleanup()

    def test_no_calendar_row_falls_back_to_today_and_says_so(self):
        self.assertEqual(self.engine.period_end_date(2025, 12), "")
        tb = self.engine.trial_balance(
            business_unit="US001", fiscal_year=2025, period=12,
            account=ACCOUNT)
        meta = tb["master_data"]
        self.assertIsNone(meta["effective_date"])
        self.assertIn("today", meta["basis"])
        self.assertIn("may be later than the period", meta["basis"])

    def test_the_trial_balance_still_answers(self):
        """Fail-closed on labels would be worse than a disclosed fallback."""
        tb = self.engine.trial_balance(
            business_unit="US001", fiscal_year=2025, period=12)
        self.assertEqual(tb["scope_status"], "ok")
        self.assertTrue(tb["rows"])




class TreeVersionTests(unittest.TestCase):
    """A reorganised tree must not restate last year's rollup.

    The sample ships one tree version, so the second one is built here:
    same tree name, a new EFFDT inside FY2026, and a node renamed. If the
    rollup reads PSTREEDEFN at today, an FY2025 report picks up the FY2026
    structure and the prior-year comparison silently changes shape.
    """

    REORG_EFFDT = "2026-03-01"

    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE.exists():
            raise unittest.SkipTest("run scripts/seed_sample_data.py first")
        cls._dir = tempfile.TemporaryDirectory()
        root = Path(cls._dir.name)
        db_path = root / "reorg.db"
        shutil.copy(SAMPLE, db_path)
        con = sqlite3.connect(db_path)
        for table in ("PSTREEDEFN", "PSTREENODE", "PSTREELEAF"):
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
            if not cols or "EFFDT" not in cols:
                continue
            rows = con.execute(
                f"SELECT {','.join(cols)} FROM {table} "
                "WHERE TREE_NAME = 'ACCOUNT'").fetchall()
            if not rows:
                con.close()
                raise unittest.SkipTest(f"sample has no {table} rows")
            for row in rows:
                d = dict(zip(cols, row))
                d["EFFDT"] = cls.REORG_EFFDT
                con.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [d[c] for c in cols])
        con.commit()
        con.close()
        cls.engine = _engine(db_path, root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._dir.cleanup()

    def _effdt(self, fy: int, per: int) -> str:
        out = self.engine.rollup_trial_balance(
            business_unit="US001", fiscal_year=fy, period=per,
            tree_name="ACCOUNT")
        return str(out.get("tree_effdt") or "")[:10]

    def test_a_prior_year_uses_the_tree_version_in_force_then(self):
        self.assertEqual(self._effdt(2025, 12), "1900-01-01",
                         "the FY2025 rollup picked up a tree version that "
                         "did not exist until March 2026")

    def test_the_current_year_uses_the_reorganised_tree(self):
        self.assertEqual(self._effdt(2026, 6), self.REORG_EFFDT)


if __name__ == "__main__":
    unittest.main()
