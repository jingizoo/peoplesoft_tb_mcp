"""Three AP answers that were wrong in ways the sample could never show.

All three were found by reading, not by a failing test, and none of them
could fail on the bundled data — which is the point. The sample has one
SETID, one business unit, and every record present. A real instance has
several of each, and that is where these fire.

  1. FAN-OUT. PS_VENDOR is keyed (SETID, VENDOR_ID). Joining it on
     VENDOR_ID alone multiplies every voucher row by the number of SETIDs
     the supplier is set up in. In duplicate_payments that turns COUNT(*)
     into a duplicate accusation against a single voucher; in
     vendor_intelligence it multiplies the spend. Both are wrong figures
     presented with the same confidence as right ones.
  2. AN UNAPPLIED SCOPE. vendor_payments printed a business unit beside a
     total that covered the whole installation. PS_PAYMENT_TBL has no
     business unit — a payment belongs to a pay cycle — so the filter has
     to run through the voucher cross-reference or be disclosed as absent.
  3. AN UNGUARDED READ. vendor_intelligence selected DUE_DT, NAME1 and
     CURRENCY_CD and joined two records it never checked, while every
     sibling tool in the same file names a missing record and degrades.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.modules import ModuleError, ModulePacks  # noqa: E402

BU = "US001"


def _packs(db_cls=Database):
    cfg = load_config(str(ROOT / "config.yaml"))
    return ModulePacks(TBEngine(db_cls(cfg), cfg))


class _Fixture:
    """A copy of the sample with rows the real world has and it does not."""

    def __init__(self, *statements: str):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "ap.db"
        src = ROOT / "sample_data" / "ps_sample.db"
        self.path.write_bytes(src.read_bytes())
        con = sqlite3.connect(self.path)
        for sql in statements:
            con.execute(sql)
        con.commit()
        con.close()

    def packs(self):
        cfg = load_config(str(ROOT / "config.yaml"))
        cfg.db.sqlite_path = str(self.path)
        return ModulePacks(TBEngine(Database(cfg), cfg))


class FanOutTests(unittest.TestCase):
    """One supplier, three SETIDs, and every AP figure multiplied."""

    @classmethod
    def setUpClass(cls) -> None:
        # V1001 already exists under one SETID. Real installations carry the
        # same supplier under SHARE plus a business SETID or two.
        cls.fx = _Fixture(
            "INSERT INTO PS_VENDOR (SETID,VENDOR_ID,NAME1,VENDOR_STATUS) "
            "VALUES ('US01','V1001','Ridgeline Supply Co','A')",
            "INSERT INTO PS_VENDOR (SETID,VENDOR_ID,NAME1,VENDOR_STATUS) "
            "VALUES ('CA01','V1001','Ridgeline Supply Co','A')")
        cls.packs = cls.fx.packs()
        cls.clean = _packs()

    def test_a_vendor_in_three_setids_is_still_one_vendor(self) -> None:
        rows, _ = self.packs.db.query(
            "SELECT COUNT(*) AS n FROM PS_VENDOR WHERE VENDOR_ID='V1001'",
            {}, max_rows=1)
        self.assertEqual(int(rows[0]["n"]), 3, "fixture did not take")

    def test_one_voucher_is_never_reported_as_a_duplicate(self) -> None:
        # The failure: COUNT(*) > 1 over a fanned-out join accuses a single
        # voucher of being paid twice. An AP team acting on that stops a
        # legitimate payment.
        before = {d["invoice_id"]: d["vouchers"] for d in
                  self.clean.duplicate_payments(business_unit=BU)[
                      "exact_invoice_duplicates"]}
        after = {d["invoice_id"]: d["vouchers"] for d in
                 self.packs.duplicate_payments(business_unit=BU)[
                     "exact_invoice_duplicates"]}
        self.assertEqual(before, after,
                         "duplicating a vendor's master row changed the "
                         "duplicate-voucher finding")

    def test_the_duplicate_total_is_not_multiplied(self) -> None:
        self.assertEqual(
            self.packs.duplicate_payments(business_unit=BU)["exact_total"],
            self.clean.duplicate_payments(business_unit=BU)["exact_total"])

    def test_vendor_spend_is_not_multiplied(self) -> None:
        # The worse half: this one is money. A supplier in three SETIDs
        # reported three times the spend, and share_pct with it.
        clean = {v["vendor_id"]: (v["paid_total"], v["payments"])
                 for v in self.clean.vendor_intelligence(
                     business_unit=BU)["vendors"]}
        fanned = {v["vendor_id"]: (v["paid_total"], v["payments"])
                  for v in self.packs.vendor_intelligence(
                      business_unit=BU)["vendors"]}
        self.assertEqual(clean, fanned)

    def test_open_payables_and_vendor_payments_hold_too(self) -> None:
        # They already deduplicated; this pins it so the shared helper is
        # not "simplified" back into a direct join in one of them.
        self.assertEqual(
            self.packs.open_payables(business_unit=BU)["open_total"],
            self.clean.open_payables(business_unit=BU)["open_total"])
        self.assertEqual(
            self.packs.vendor_payments(business_unit=BU)["total_paid"],
            self.clean.vendor_payments(business_unit=BU)["total_paid"])

    def test_every_vendor_join_goes_through_the_one_helper(self) -> None:
        # A second spelling of this rule is how three of the five tools
        # ended up right and two wrong.
        source = (ROOT / "pstb" / "modules.py").read_text()
        self.assertNotIn("PS_VENDOR N ON", source,
                         "a direct join to the SETID-keyed vendor record")
        self.assertGreaterEqual(source.count("_vendor_names()"), 5)


class PaymentScopeTests(unittest.TestCase):
    """A business unit printed beside a figure it did not bound."""

    @classmethod
    def setUpClass(cls) -> None:
        # A payment belonging to another unit's voucher. Before the fix it
        # was counted into US001's total.
        cls.fx = _Fixture(
            "INSERT INTO PS_PAYMENT_TBL VALUES "
            "('SHARE','PAY-CA-1','V1001','2026-06-15',777777.0,'USD','P')",
            "INSERT INTO PS_PYMNT_VCHR_XREF VALUES "
            "('CA001','VCH-CA-1','PAY-CA-1',777777.0)")
        cls.packs = cls.fx.packs()

    def test_another_units_payment_is_not_in_this_units_total(self) -> None:
        out = self.packs.vendor_payments(business_unit=BU)
        self.assertTrue(out["scoped_to_business_unit"])
        self.assertNotIn(777777.0, [v["paid"] for v in out["vendors"]])
        self.assertLess(out["total_paid"], 777777.0)

    def test_the_other_unit_can_see_its_own(self) -> None:
        out = self.packs.vendor_payments(business_unit="CA001")
        self.assertEqual(out["total_paid"], 777777.0)

    def test_a_refused_cross_reference_widens_the_answer_OUT_LOUD(self):
        # What a missing grant actually looks like: the read raises. The
        # tool must still answer — and must not keep claiming the unit.
        from pstb.db import DbError

        class NoGrant(Database):
            def query(self, sql, params=None, **kw):
                if "PS_PYMNT_VCHR_XREF" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        out = _packs(NoGrant).vendor_payments(business_unit=BU)
        self.assertFalse(out["scoped_to_business_unit"])
        self.assertIn("WHOLE INSTALLATION", " ".join(out["record_notes"]))
        self.assertIn("NOT scoped", out["note"])
        self.assertTrue(out["vendors"], "it still answers, it just says what "
                                        "the answer covers")

    def test_an_introspection_gap_does_not_silently_drop_the_scope(self):
        # An empty column list means "could not read", not "not there".
        # Assuming absence would widen the population on a site whose
        # record is perfectly fine, so the scoped read is still attempted.
        class BlindCatalog(Database):
            def columns(self, table):
                return (set() if table == "PS_PYMNT_VCHR_XREF"
                        else super().columns(table))

        out = _packs(BlindCatalog).vendor_payments(business_unit=BU)
        self.assertTrue(out["scoped_to_business_unit"])
        self.assertEqual(out["total_paid"],
                         _packs().vendor_payments(
                             business_unit=BU)["total_paid"])

    def test_a_cross_reference_without_the_pair_is_disclosed(self) -> None:
        class NoUnitOnXref(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "BUSINESS_UNIT"}
                        if table == "PS_PYMNT_VCHR_XREF" else cols)

        out = _packs(NoUnitOnXref).vendor_payments(business_unit=BU)
        self.assertFalse(out["scoped_to_business_unit"])
        self.assertIn("WHOLE INSTALLATION", " ".join(out["record_notes"]))


class ShapeGuardTests(unittest.TestCase):
    """A missing record must be named, not raised as ORA-00942."""

    def _without(self, *tables, **cols):
        missing_tables, missing_cols = set(tables), cols

        class Shaped(Database):
            def columns(self, table):
                if table in missing_tables:
                    return set()
                found = super().columns(table)
                drop = missing_cols.get(table)
                return {c for c in found if c != drop} if drop else found

        return _packs(Shaped)

    def test_a_missing_cross_reference_is_refused_by_name(self) -> None:
        with self.assertRaises(ModuleError) as ctx:
            self._without("PS_PYMNT_VCHR_XREF").vendor_intelligence(
                business_unit=BU)
        self.assertIn("PS_PYMNT_VCHR_XREF", str(ctx.exception))
        self.assertIn("search_records", str(ctx.exception))

    def test_a_missing_payment_record_is_refused_by_name(self) -> None:
        with self.assertRaises(ModuleError) as ctx:
            self._without("PS_PAYMENT_TBL").vendor_intelligence(
                business_unit=BU)
        self.assertIn("PS_PAYMENT_TBL", str(ctx.exception))

    def test_no_due_date_withholds_timing_and_says_why(self) -> None:
        # The trap this guards: with no DUE_DT every vendor's
        # avg_days_vs_due is null, which reads as "nobody pays late".
        out = self._without(PS_VOUCHER="DUE_DT").vendor_intelligence(
            business_unit=BU)
        self.assertTrue(all(v["avg_days_vs_due"] is None
                            for v in out["vendors"]))
        self.assertEqual([o for o in out["observations"]
                          if o["kind"] in ("early_payment", "late_payment")],
                         [])
        note = " ".join(out["record_notes"])
        self.assertIn("DUE_DT", note)
        self.assertIn("not evidence", note)

    def test_no_vendor_name_still_ranks_by_id(self) -> None:
        out = self._without(PS_VENDOR="NAME1").vendor_intelligence(
            business_unit=BU)
        self.assertTrue(out["vendors"])
        self.assertIn("NAME1", " ".join(out["record_notes"]))

    def test_no_currency_column_is_disclosed(self) -> None:
        out = self._without(PS_PAYMENT_TBL="CURRENCY_CD").vendor_intelligence(
            business_unit=BU)
        self.assertIn("CURRENCY_CD", " ".join(out["record_notes"]))

    def test_the_geography_note_did_not_eat_the_others(self) -> None:
        # A later `notes = []` rebound the list and threw away every shape
        # note gathered before the query. PS_VENDOR_ADDR now exists in the
        # sample, so the geography note is gone — drop BOTH and the list
        # must still carry both absences.
        out = self._without("PS_VENDOR_ADDR",
                            PS_VOUCHER="DUE_DT").vendor_intelligence(
            business_unit=BU)
        notes = " ".join(out["record_notes"])
        self.assertIn("DUE_DT", notes)
        self.assertIn("PS_VENDOR_ADDR", notes)


if __name__ == "__main__":
    unittest.main()
