"""AP / AM / PC module packs — the consultant's questions, one call each.

The behaviors that must hold, per module:

  AP   open vs paid separated; overdue and due-soon computed against as_of;
       vouchers stuck in recycle/unposted surfaced, not silently included;
       payments rankable and filterable by vendor name.
  AM   register netted by category (a retirement's negative row reduces
       it); additions and retirements isolated to the window; NBV never
       approximated.
  PC   BUD rows split from ACT rows; over-budget and stale flagged with
       evidence; a project with no budget row reads as unbudgeted (null),
       never 0% or 100%.

Plus the shape rule every pack inherits: optional columns degrade with a
record_note; required ones fail with the diagnose pointer.
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
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.modules import ModuleError, ModulePacks  # noqa: E402


class _SampleCase(unittest.TestCase):
    def setUp(self) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.m = ModulePacks(TBEngine(self.db, cfg))

    def tearDown(self) -> None:
        self.db.close()


class OpenPayablesTests(_SampleCase):
    def test_paid_vouchers_are_not_owed(self) -> None:
        out = self.m.open_payables(as_of_date="2026-08-04")
        # Eleven open: four for the original three suppliers, four added
        # with the supplier family and the shared-bank pair, and three from
        # the purchase-to-pay chain (VCHR2002 5,750 + VCHR2003 6,000 +
        # VCHR2004 12,000 = 23,750, all stuck on match, none yet due).
        self.assertEqual(out["voucher_count"], 11)
        self.assertEqual(out["open_total"], 216500.0)

    def test_overdue_is_judged_against_as_of(self) -> None:
        out = self.m.open_payables(as_of_date="2026-08-04")
        # Cobalt's 27,650 plus Ridgeline Fasteners' 22,300.
        self.assertEqual(out["overdue_total"], 49950.0)
        cobalt = next(v for v in out["by_vendor"]
                      if v["vendor"] == "Cobalt IT Services")
        self.assertEqual(cobalt["overdue_amount"], 27650.0)
        self.assertEqual(cobalt["oldest_due_dt"], "2026-07-05")

    def test_stuck_vouchers_are_surfaced_not_buried(self) -> None:
        out = self.m.open_payables(as_of_date="2026-08-04")
        stuck = out["pipeline_exceptions"]
        self.assertEqual([x["voucher_id"] for x in stuck], ["VCHR90004"])
        self.assertIn("recycle", stuck[0]["why"])
        # It still counts as owed — stuck money is money.
        self.assertIn(stuck[0]["amount"],
                      [v["open_amount"] for v in out["by_vendor"]
                       if v["vendor_id"] == "V1003"])

    def test_due_soon_window_is_seven_days(self) -> None:
        out = self.m.open_payables(as_of_date="2026-08-04")
        # VCHR90001 (18,400 due 08-09) and VCHR90008 (14,800 due 08-11).
        self.assertEqual(out["due_within_7_days"], 33200.0)


class VendorPaymentsTests(_SampleCase):
    def test_the_once_refused_question_answers(self) -> None:
        out = self.m.vendor_payments(vendor="Cobalt", months=12,
                                     as_of_date="2026-08-04")
        self.assertEqual(len(out["vendors"]), 1)
        v = out["vendors"][0]
        self.assertEqual(v["payments"], 2)
        self.assertEqual(v["paid"], 51900.0)
        self.assertEqual(v["last_payment_dt"], "2026-06-30")

    def test_empty_vendor_ranks_everyone(self) -> None:
        out = self.m.vendor_payments(months=12, as_of_date="2026-08-04")
        self.assertGreaterEqual(out["vendor_count"], 3)
        paid = [v["paid"] for v in out["vendors"]]
        self.assertEqual(paid, sorted(paid, reverse=True))

    def test_no_match_says_so_with_a_next_step(self) -> None:
        out = self.m.vendor_payments(vendor="Nonexistent Corp")
        self.assertEqual(out["vendors"], [])
        self.assertIn("No payments", out["note"])


class AssetRegisterTests(_SampleCase):
    def test_a_retirement_nets_its_category_to_zero(self) -> None:
        out = self.m.asset_register(as_of_date="2026-08-04")
        auto = next(c for c in out["by_category"] if c["category"] == "AUTO")
        self.assertEqual(auto["cost"], 0.0)

    def test_window_isolates_additions_and_retirements(self) -> None:
        out = self.m.asset_register(months=8, as_of_date="2026-08-04")
        added = {a["asset_id"] for a in out["additions_in_window"]}
        self.assertEqual(added, {"A-0002", "A-0003"})  # ADJ counts as add
        self.assertEqual([r["asset_id"] for r in out["retirements_in_window"]],
                         ["A-0004"])

    def test_nbv_is_never_approximated(self) -> None:
        out = self.m.asset_register(as_of_date="2026-08-04")
        self.assertIn("COST basis only", out["note"])
        self.assertNotIn("nbv", str(out).lower().replace("nbv needs", ""))


class ProjectCostTests(_SampleCase):
    def test_budget_rows_are_split_from_actuals(self) -> None:
        out = self.m.project_costs(as_of_date="2026-08-04")
        prj = {p["project_id"]: p for p in out["projects"]}
        self.assertEqual(prj["PRJ-100"]["actual"], 215000.0)
        self.assertEqual(prj["PRJ-100"]["budget"], 250000.0)

    def test_over_budget_is_flagged_with_the_numbers(self) -> None:
        out = self.m.project_costs(as_of_date="2026-08-04")
        self.assertEqual(out["over_budget"], ["PRJ-200"])
        p200 = next(p for p in out["projects"]
                    if p["project_id"] == "PRJ-200")
        self.assertTrue(p200["over_budget"])
        self.assertLess(p200["remaining"], 0)

    def test_stale_needs_budget_remaining_and_no_recent_activity(self) -> None:
        out = self.m.project_costs(as_of_date="2026-08-04",
                                   stale_after_months=3)
        self.assertEqual(out["stale"], ["PRJ-300"])
        p300 = next(p for p in out["projects"]
                    if p["project_id"] == "PRJ-300")
        self.assertEqual(p300["last_activity"], "2026-02-28")

    def test_a_project_without_a_budget_is_unbudgeted_not_percent(self) -> None:
        # Wrongly reporting 0% or 100% here is a fabricated fact.
        tmp = Path(tempfile.mkdtemp(prefix="pstb-pc-"))
        try:
            dst = tmp / "s.db"
            shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
            con = sqlite3.connect(dst)
            con.execute(
                "INSERT INTO PS_PROJ_RESOURCE VALUES "
                "('US001','PRJ-400','GEN','ACT','2026-07-01',5000.0,'USD')")
            con.commit()
            con.close()
            cfg = Config.sample(ROOT)
            cfg.db.sqlite_path = str(dst)
            cfg.db.use_views = False
            db = Database(cfg)
            try:
                out = ModulePacks(TBEngine(db, cfg)).project_costs(
                    as_of_date="2026-08-04")
                p400 = next(p for p in out["projects"]
                            if p["project_id"] == "PRJ-400")
                self.assertIsNone(p400["pct_used"])
                self.assertFalse(p400["over_budget"])
            finally:
                db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ShapeFallbackTests(unittest.TestCase):
    """The packs inherit the site-shape rules the rest of the product obeys."""

    def _mutated(self, drops: list) -> tuple:
        tmp = Path(tempfile.mkdtemp(prefix="pstb-modshape-"))
        dst = tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        con = sqlite3.connect(dst)
        for (v,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            con.execute(f"DROP VIEW {v}")
        for table, col in drops:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        con.commit()
        con.close()
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        db = Database(cfg)
        return tmp, db, ModulePacks(TBEngine(db, cfg))

    def test_no_due_date_degrades_with_a_note(self) -> None:
        tmp, db, m = self._mutated([("PS_VOUCHER", "DUE_DT")])
        try:
            out = m.open_payables(as_of_date="2026-08-04")
            self.assertEqual(out["open_total"], 216500.0)
            self.assertEqual(out["overdue_total"], 0.0)
            self.assertTrue(any("DUE_DT" in n
                                for n in out["record_notes"]))
        finally:
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_close_status_falls_back_to_payment_xref(self) -> None:
        tmp, db, m = self._mutated([("PS_VOUCHER", "CLOSE_STATUS")])
        try:
            out = m.open_payables(as_of_date="2026-08-04")
            # The open vouchers have no payment xref, so the fallback
            # finds the same set — the three chain vouchers included, since
            # only the PAID chain voucher carries an xref — and says how it
            # decided.
            self.assertEqual(out["voucher_count"], 11)
            self.assertTrue(any("CLOSE_STATUS" in n
                                for n in out["record_notes"]))
        finally:
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_missing_required_record_fails_with_the_pointer(self) -> None:
        tmp, db, m = self._mutated([])
        try:
            con = sqlite3.connect(tmp / "s.db")
            con.execute("ALTER TABLE PS_PROJ_RESOURCE RENAME TO GONE")
            con.commit()
            con.close()
            db.clear_catalog()
            with self.assertRaises(ModuleError) as ctx:
                m.project_costs()
            self.assertIn("search_records", str(ctx.exception))
        finally:
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
