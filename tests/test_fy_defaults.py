"""Data-aware fiscal-year defaulting.

The fiscal calendar answers "which year is today in" — it says nothing about
whether the ledger HOLDS that year. On a dev refresh posted only through the
prior year, defaulting to the calendar year turned every unscoped GL question
into "no ledger data found for the current fiscal year" with no next move.
A defaulted year now clamps to the latest posted year, says so in a
scope_note, and never overrides a year the caller named explicitly.

The speed contract matters as much as the behavior: the clamp may only use
the TTL-cached last_posted_period — a second call must add zero queries.
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

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402


def _engine_with_ledger_through(tmp: Path, last_fy: int):
    """Sample copy whose ACTUALS ledger is posted only through last_fy,
    while the fiscal calendar still covers today (FY 2026)."""
    dst = tmp / "s.db"
    shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
    con = sqlite3.connect(dst)
    con.execute("DELETE FROM PS_LEDGER WHERE FISCAL_YEAR > ?", (last_fy,))
    con.commit()
    con.close()
    cfg = Config.sample(ROOT)
    cfg.db.sqlite_path = str(dst)
    cfg.db.use_views = False
    db = Database(cfg)
    return db, TBEngine(db, cfg)


class ClampTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-fy-"))
        self.db, self.engine = _engine_with_ledger_through(self.tmp, 2025)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaulted_year_clamps_to_posted_data(self) -> None:
        tb = self.engine.trial_balance()
        self.assertEqual(tb["fiscal_year"], 2025)
        self.assertTrue(tb["rows"], "the clamped year must actually hold data")
        notes = tb.get("scope_notes") or []
        self.assertTrue(any("only posted through 2025" in n for n in notes),
                        f"the clamp must be visible, got {notes}")

    def test_explicit_year_is_never_overridden(self) -> None:
        tb = self.engine.trial_balance(fiscal_year=2026)
        self.assertEqual(tb["fiscal_year"], 2026)
        self.assertNotIn("scope_notes", tb)

    def test_account_balance_clamps_too(self) -> None:
        ab = self.engine.account_balance("1100")
        self.assertEqual(ab["fiscal_year"], 2025)
        self.assertTrue(any("only posted through" in n
                            for n in ab.get("scope_notes", [])))

    def test_clamp_adds_no_queries_when_cached(self) -> None:
        self.engine.trial_balance()  # warms the posted-period cache
        counted = []
        orig = self.db.query

        def spy(sql, params=None, **kw):
            counted.append(sql)
            return orig(sql, params, **kw)

        self.db.query = spy
        try:
            self.engine.trial_balance()
        finally:
            self.db.query = orig
        scope_probes = [q for q in counted
                        if "MAX(FISCAL_YEAR)" in q.upper()
                        or "last_regular" in q]
        self.assertEqual(scope_probes, [],
                         "the clamp must ride the cached last_posted_period; "
                         "a per-call probe is a hot-path regression")


class NoClampWhenCurrentYearPostedTests(unittest.TestCase):
    """An instance posted into the calendar year keeps today's scope."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-fy-"))
        self.db, self.engine = _engine_with_ledger_through(self.tmp, 2026)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_note_and_no_clamp(self) -> None:
        tb = self.engine.trial_balance()
        self.assertEqual(tb["fiscal_year"], 2026)
        self.assertNotIn("scope_notes", tb)


class GlTieDiagnosisTests(unittest.TestCase):
    """A tie that cannot run must say what exists, not just 'no data'."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-fy-"))
        dst = self.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        con = sqlite3.connect(dst)
        con.execute("DELETE FROM PS_LEDGER")  # open items, nothing posted
        con.commit()
        con.close()
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)
        from pstb.ar import ARBilling
        self.ar = ARBilling(self.engine)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tie_reports_scope_diagnosis(self) -> None:
        tie = self.ar._gl_tie("US001")
        self.assertFalse(tie["evaluated"])
        diag = tie.get("scope_diagnosis")
        self.assertIsInstance(diag, dict, "the dead-end must carry a remedy")
        self.assertIn(diag.get("scope_status"),
                      {"business_unit_not_found", "ledger_not_found",
                       "no_data_for_period"})
        self.assertNotIn("fiscal year 0", str(diag))


if __name__ == "__main__":
    unittest.main()
