"""CPA-safe received-not-invoiced review-candidate control."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.grni import GRNIControl  # noqa: E402
from pstb.guards import tool_result_status  # noqa: E402
from pstb.modules import ModuleError, ModulePacks  # noqa: E402


class _FixedDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 14)


class _JuneCloseDate(dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 6, 30)


class GRNIControlTests(unittest.TestCase):
    BU = "US001"
    AS_OF = "2026-08-14"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pstb-grni-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "grni.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", self.path)
        # Historical candidate reconstruction needs a separate availability
        # date so a voucher entered after close but backdated into the period
        # cannot suppress the cutoff candidate. The bundled demo omits it;
        # this focused fixture adds the delivered-shape guard explicitly.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("ALTER TABLE PS_VOUCHER ADD COLUMN ENTERED_DT TEXT")
            conn.execute("UPDATE PS_VOUCHER SET ENTERED_DT=INVOICE_DT")
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(self.path)
        cfg.db.use_views = False
        cfg.defaults.business_unit = self.BU
        self.db = Database(cfg)
        self.addCleanup(self.db.close)
        self.engine = TBEngine(self.db, cfg)
        self.control = GRNIControl(ModulePacks(self.engine))
        date_patch = patch("pstb.grni.dt.date", _FixedDate)
        date_patch.start()
        self.addCleanup(date_patch.stop)

    def _sql(self, statement: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(statement, params)
        self.db.clear_catalog()

    def _run(self, **kwargs) -> dict:
        params = {"business_unit": self.BU, "as_of_date": self.AS_OF}
        params.update(kwargs)
        return self.control.period_end_accrual(**params)

    def _insert_po_receipt(
        self, *, po: str, receiver: str, amount: float,
        currency: str = "USD", receipt_date: str = "2026-08-01",
        line: int = 1, schedule: int = 1, bu: str = "US001",
        status: str = "O",
    ) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO PS_PO_HDR VALUES (?, ?, 'V1009', 'D', ?, ?)",
                (bu, po, receipt_date, currency),
            )
            conn.execute(
                "INSERT INTO PS_RECV_HDR VALUES (?, ?, 'V1009', ?, ?)",
                (bu, receiver, receipt_date, status),
            )
            conn.execute(
                """INSERT INTO PS_RECV_LN_SHIP
                   (BUSINESS_UNIT, RECEIVER_ID, RECV_LN_NBR,
                    RECV_SHIP_SEQ_NBR, BUSINESS_UNIT_PO, PO_ID, LINE_NBR,
                    SCHED_NBR, QTY_SH_ACCPT_VUOM, MERCHANDISE_AMT)
                   VALUES (?, ?, 1, 1, ?, ?, ?, ?, 1, ?)""",
                (bu, receiver, bu, po, line, schedule, amount),
            )
        self.db.clear_catalog()

    def _insert_voucher(
        self, *, po: str, voucher: str, amount: float,
        currency: str = "USD", invoice_date: str | None = "2026-08-05",
        line: int = 1, schedule: int = 1, receiver: str = "",
        recv_line: int = 0, bu: str = "US001", entry_status: str = "P",
        post_status: str = "P", match_status: str = "N",
    ) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                """INSERT INTO PS_VOUCHER
                   (BUSINESS_UNIT, VOUCHER_ID, VENDOR_ID, INVOICE_ID,
                    INVOICE_DT, DUE_DT, GROSS_AMT, ENTRY_STATUS,
                    CLOSE_STATUS, POST_STATUS, CURRENCY_CD,
                    MATCH_STATUS_VCHR)
                   VALUES (?, ?, 'V1009', ?, ?, ?, ?, ?, 'O', ?, ?, ?)""",
                (bu, voucher, "INV-" + voucher, invoice_date, invoice_date,
                 amount, entry_status, post_status, currency, match_status),
            )
            conn.execute(
                "UPDATE PS_VOUCHER SET ENTERED_DT=? "
                "WHERE BUSINESS_UNIT=? AND VOUCHER_ID=?",
                (invoice_date, bu, voucher),
            )
            conn.execute(
                """INSERT INTO PS_VOUCHER_LINE
                   (BUSINESS_UNIT, VOUCHER_ID, VOUCHER_LINE_NUM,
                    BUSINESS_UNIT_PO, PO_ID, LINE_NBR, SCHED_NBR,
                    RECEIVER_ID, RECV_LN_NBR, QTY_VCHR, UNIT_PRICE,
                    MERCHANDISE_AMT, DESCR)
                   VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'test')""",
                (bu, voucher, bu, po, line, schedule, receiver, recv_line,
                 amount, amount),
            )
        self.db.clear_catalog()

    def test_sample_reconstructs_schedule_candidates_without_netting_breaks(
            self) -> None:
        out = self._run(materiality=1_000)
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["evaluated"])
        self.assertTrue(out["population"]["complete"])
        self.assertEqual(out["population"]["candidate_count"], 1)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["conclusion"], "po_linked_candidates_present")
        self.assertEqual(out["coverage"]["classification"],
                         "po_linked_document_review_only")
        self.assertFalse(out["coverage"]["all_grni_complete"])
        self.assertEqual(out["rni_totals_by_currency"], {"USD": 2400.0})
        self.assertEqual([(row["po_id"], row["sched_nbr"],
                           row["rni_candidate_amount"])
                          for row in out["lines"]],
                         [("PO2005", 1, 2400.0)])
        # Closed/complete receipt headers are not current accrual-eligible;
        # they are disclosed instead of being netted into this candidate.
        self.assertEqual(out["exceptions"]["over_invoiced"], [])
        self.assertEqual(
            {row["receipt_status"]
             for row in out["exceptions"]["excluded_receipt_statuses"]},
            {"C"},
        )

    def test_result_never_claims_a_booked_accrual_or_journal(self) -> None:
        out = self._run()
        self.assertEqual(out["booked_status"], "not_evaluated")
        self.assertEqual(out["candidate_basis"]["classification"],
                         "review_candidate_only")
        self.assertIn("Journal Generator", out["booked_basis"])
        self.assertIn("not evidence that an accrual is booked", out["note"])

    def test_actual_complete_payload_satisfies_only_the_strict_ap_gate(
            self) -> None:
        out = self._run()
        accepted, reason = tool_result_status(
            "get_po_grni_candidates", json.dumps(out))
        self.assertTrue(accepted, reason)

        capped = self._run(max_rows=1)
        accepted, _ = tool_result_status(
            "get_po_grni_candidates", json.dumps(capped))
        self.assertFalse(accepted)

    def test_complete_zero_candidate_count_is_explicit_not_inferred(self) -> None:
        self._insert_voucher(po="PO2005", voucher="VFULL", amount=2400,
                             receiver="RECV3004", recv_line=1)
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["lines"], [])
        self.assertEqual(out["population"]["candidate_count"], 0)
        self.assertTrue(out["population"]["complete"])
        accepted, reason = tool_result_status(
            "get_po_grni_candidates", json.dumps(out))
        self.assertTrue(accepted, reason)

    def test_historical_cutoff_is_incomplete_even_with_availability_date(
            self) -> None:
        out = self._run(as_of_date="2026-06-30")
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertFalse(out["population"]["complete"])
        self.assertIsNone(out["population"]["candidate_count"])
        self.assertIn("current state without effective-dated history",
                      out["reason"])

    def test_current_cutoff_can_run_without_separate_availability_date(
            self) -> None:
        self._sql("ALTER TABLE PS_VOUCHER DROP COLUMN ENTERED_DT")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["coverage"]["point_in_time_complete"])

    def test_cutoff_includes_prior_receipts_and_excludes_later_receipts(
            self) -> None:
        with patch("pstb.grni.dt.date", _JuneCloseDate):
            out = self._run(as_of_date="2026-06-30")
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["population"]["receipt_rows_returned"], 2)
        self.assertEqual(out["rni_totals_by_currency"], {"USD": 2400.0})
        self.assertEqual([row["po_id"] for row in out["lines"]], ["PO2005"])
        self.assertNotIn("PO2002", str(out))

    def test_voucher_after_cutoff_is_not_subtracted(self) -> None:
        self._insert_voucher(po="PO2005", voucher="VLATE", amount=1000,
                             invoice_date="2026-07-02")
        with patch("pstb.grni.dt.date", _JuneCloseDate):
            june = self._run(as_of_date="2026-06-30")
        august = self._run()
        self.assertEqual(june["rni_totals_by_currency"], {"USD": 2400.0})
        self.assertEqual(august["rni_totals_by_currency"], {"USD": 1400.0})

    def test_partial_invoice_reduces_only_its_schedule(self) -> None:
        self._insert_po_receipt(po="PO-SCHED", receiver="RCV-S1",
                                amount=1000, schedule=1)
        # Same PO line, a different schedule.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO PS_RECV_HDR VALUES "
                "('US001','RCV-S2','V1009','2026-08-02','O')")
            conn.execute(
                """INSERT INTO PS_RECV_LN_SHIP VALUES
                   ('US001','RCV-S2',1,1,'US001','PO-SCHED',1,2,1,2000)"""
            )
        self.db.clear_catalog()
        self._insert_voucher(po="PO-SCHED", voucher="VSCHED", amount=600,
                             schedule=1, receiver="RCV-S1", recv_line=1)
        out = self._run()
        by_schedule = {row["sched_nbr"]: row["rni_candidate_amount"]
                       for row in out["lines"]
                       if row["po_id"] == "PO-SCHED"}
        self.assertEqual(by_schedule, {1: 400.0, 2: 2000.0})
        self.assertEqual(out["matching_basis"]["precision"],
                         "po_line_schedule")

    def test_blank_receipt_reference_uses_disclosed_schedule_fallback(
            self) -> None:
        self._insert_voucher(po="PO2005", voucher="VFALL", amount=500)
        out = self._run()
        candidate = next(row for row in out["lines"]
                         if row["po_id"] == "PO2005")
        self.assertEqual(candidate["rni_candidate_amount"], 1900.0)
        self.assertEqual(candidate["schedule_fallback_references"], 1)
        self.assertEqual(out["population"]["schedule_fallback_references"], 1)

    def test_conflicting_explicit_receipt_reference_fails_closed(self) -> None:
        self._insert_voucher(po="PO2005", voucher="VBADREF", amount=500,
                             receiver="NOT-A-RECEIPT", recv_line=1)
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertFalse(out["population"]["complete"])
        self.assertIsNone(out["totals_by_currency"])
        refs = out["exceptions"]["unmatched_voucher_references"]
        self.assertEqual(refs[0]["voucher_id"], "VBADREF")

    def test_multi_currency_populations_are_kept_separate(self) -> None:
        self._insert_po_receipt(po="PO-EUR", receiver="RCV-EUR",
                                amount=700, currency="EUR")
        out = self._run(materiality=500)
        self.assertEqual(out["rni_totals_by_currency"],
                         {"EUR": 700.0, "USD": 2400.0})
        self.assertNotIn("grand_total", out)
        self.assertIn("no cross-currency total",
                      out["candidate_basis"]["amount_basis"])

    def test_voucher_currency_mismatch_never_subtracts_across_currency(
            self) -> None:
        self._insert_voucher(po="PO2005", voucher="VEUR", amount=500,
                             currency="EUR")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIsNone(out["rni_totals_by_currency"])
        self.assertIn("cross-currency subtraction", out["reason"])

    def test_recycled_or_unposted_voucher_does_not_suppress_candidate(
            self) -> None:
        self._insert_voucher(po="PO2005", voucher="VRECYCLE", amount=1000,
                             entry_status="R", post_status="U")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["rni_totals_by_currency"], {"USD": 2400.0})
        excluded = out["exceptions"]["excluded_voucher_statuses"]
        self.assertEqual(excluded[0]["voucher_id"], "VRECYCLE")
        self.assertEqual(out["population"]["voucher_rows_excluded_by_status"],
                         1)

    def test_unknown_voucher_status_is_incomplete(self) -> None:
        self._insert_voucher(po="PO2005", voucher="VUNKNOWN", amount=100,
                             entry_status="Z", post_status="P")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["population"]["complete"])
        self.assertIn("not a governed", out["reason"])

    def test_materiality_and_age_are_line_level_and_explainable(self) -> None:
        out = self._run(materiality=3000, aging_days=30)
        row = out["lines"][0]
        self.assertEqual(row["age_days"], 47)
        self.assertEqual(row["age_bucket"], "31_60")
        self.assertFalse(row["material"])
        self.assertTrue(row["aged"])
        self.assertEqual(len(out["exceptions"]["material_or_aged"]), 1)
        self.assertIn("transaction-currency",
                      out["materiality"]["application"])

    def test_receipt_row_cap_is_incomplete_not_a_partial_total(self) -> None:
        out = self._run(max_rows=2)
        self.assertEqual(out["status"], "incomplete")
        self.assertTrue(out["truncated"])
        self.assertTrue(out["population"]["truncated"])
        self.assertFalse(out["population"]["complete"])
        self.assertIsNone(out["population"]["candidate_count"])
        self.assertIsNone(out["totals_by_currency"])

    def test_voucher_row_cap_is_incomplete_not_a_partial_total(self) -> None:
        # Leave one eligible receipt key and place two vouchers on it, so the
        # cap is reached on vouchers rather than receipts.
        self._sql("DELETE FROM PS_RECV_LN_SHIP WHERE PO_ID <> 'PO2005'")
        self._sql("DELETE FROM PS_RECV_HDR WHERE RECEIVER_ID <> 'RECV3004'")
        self._insert_voucher(po="PO2005", voucher="VCAP1", amount=100)
        self._insert_voucher(po="PO2005", voucher="VCAP2", amount=100)
        out = self._run(max_rows=1)
        self.assertEqual(out["status"], "incomplete")
        self.assertTrue(out["truncated"])
        self.assertFalse(out["population"]["complete"])
        self.assertIsNone(out["population"]["candidate_count"])
        self.assertIsNone(out["rni_totals_by_currency"])

    def test_missing_or_null_source_dates_fail_closed(self) -> None:
        self._sql("UPDATE PS_RECV_HDR SET RECEIPT_DT=NULL "
                  "WHERE RECEIVER_ID='RECV3004'")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("RECEIPT_DT is blank", out["reason"])
        self.assertIsNone(out["totals_by_currency"])

        # Restore the receipt date, then make an attributable voucher date
        # blank. The query intentionally brings null dates back for refusal.
        self._sql("UPDATE PS_RECV_HDR SET RECEIPT_DT='2026-06-28' "
                  "WHERE RECEIVER_ID='RECV3004'")
        self._insert_voucher(po="PO2005", voucher="VNODATE", amount=100,
                             invoice_date=None)
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("INVOICE_DT is blank", out["reason"])

    def test_null_amount_key_or_status_cannot_become_zero(self) -> None:
        for statement, expected in (
            ("UPDATE PS_RECV_LN_SHIP SET MERCHANDISE_AMT=NULL "
             "WHERE RECEIVER_ID='RECV3004'", "MERCHANDISE_AMT is blank"),
            ("UPDATE PS_RECV_HDR SET RECV_STATUS='' "
             "WHERE RECEIVER_ID='RECV3004'", "RECV_STATUS is blank"),
        ):
            with self.subTest(expected=expected):
                # Each subcase gets the pristine row back first.
                self._sql("UPDATE PS_RECV_LN_SHIP SET MERCHANDISE_AMT=2400 "
                          "WHERE RECEIVER_ID='RECV3004'")
                self._sql("UPDATE PS_RECV_HDR SET RECV_STATUS='O' "
                          "WHERE RECEIVER_ID='RECV3004'")
                self._sql(statement)
                out = self._run()
                self.assertEqual(out["status"], "incomplete")
                self.assertIn(expected, out["reason"])
                self.assertIsNone(out["totals_by_currency"])

        self._sql("UPDATE PS_RECV_HDR SET RECV_STATUS='O' "
                  "WHERE RECEIVER_ID='RECV3004'")
        self._insert_voucher(po="PO2005", voucher="VNOSTATUS", amount=100,
                             entry_status="")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("ENTRY_STATUS is blank", out["reason"])

    def test_duplicate_receipt_event_key_is_incomplete(self) -> None:
        self._sql(
            """INSERT INTO PS_RECV_LN_SHIP
               SELECT * FROM PS_RECV_LN_SHIP WHERE RECEIVER_ID='RECV3004'"""
        )
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("duplicate receipt shipment key", out["reason"])

    def test_hold_closed_and_canceled_receipts_are_excluded(self) -> None:
        self._insert_po_receipt(po="PO-HOLD", receiver="RCV-HOLD",
                                amount=100, status="H")
        self._insert_po_receipt(po="PO-CLOSED", receiver="RCV-CLOSED",
                                amount=200, status="C")
        self._insert_po_receipt(po="PO-CANCEL", receiver="RCV-CANCEL",
                                amount=300, status="X")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertNotIn("PO-HOLD", [row["po_id"] for row in out["lines"]])
        self.assertNotIn("PO-CLOSED", [row["po_id"] for row in out["lines"]])
        self.assertNotIn("PO-CANCEL", [row["po_id"] for row in out["lines"]])
        statuses = {row["receipt_status"]
                    for row in out["exceptions"]["excluded_receipt_statuses"]}
        self.assertEqual(statuses, {"H", "C", "X"})

    def test_line_level_fallback_is_disclosed_when_schedule_is_unavailable(
            self) -> None:
        self._sql("ALTER TABLE PS_VOUCHER_LINE DROP COLUMN SCHED_NBR")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["matching_basis"]["precision"], "po_line")
        self.assertTrue(out["matching_basis"]["reduced_precision"])
        self.assertTrue(all(row["sched_nbr"] is None for row in out["lines"]))

    def test_peopletools_mapping_prefers_custom_physical_record(self) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("ALTER TABLE PS_RECV_LN_SHIP "
                         "RENAME TO ACME_RECV_LN_SHIP")
            conn.execute("UPDATE PSRECDEFN SET "
                         "SQLTABLENAME='ACME_RECV_LN_SHIP' "
                         "WHERE RECNAME='RECV_LN_SHIP'")
        self.db.clear_catalog()
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(
            out["source_records"]["RECV_LN_SHIP"],
            {"physical": "ACME_RECV_LN_SHIP",
             "resolution_basis": "PSRECDEFN.SQLTABLENAME"},
        )

    def test_ambiguous_company_and_delivered_records_never_guess(self) -> None:
        self._sql("CREATE TABLE ACME_RECV_LN_SHIP AS "
                  "SELECT * FROM PS_RECV_LN_SHIP")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("More than one physical object", out["reason"])
        self.assertIsNone(out["totals_by_currency"])

    def test_exact_business_unit_scope_does_not_leak_other_unit(self) -> None:
        self._insert_po_receipt(po="PO-OTHER", receiver="RCV-OTHER",
                                amount=999999, bu="US002")
        out = self._run()
        self.assertEqual(out["business_unit"], "US001")
        self.assertEqual(out["rni_totals_by_currency"], {"USD": 2400.0})
        self.assertNotIn("PO-OTHER", str(out))

    def test_non_po_receipts_are_counted_as_out_of_scope_not_silently_lost(
            self) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO PS_RECV_HDR VALUES "
                         "('US001','RCV-NONPO','V1009','2026-08-10','O')")
            conn.execute(
                """INSERT INTO PS_RECV_LN_SHIP VALUES
                   ('US001','RCV-NONPO',1,1,'US001','',1,1,1,999)"""
            )
        self.db.clear_catalog()
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["population"]["non_po_receipt_rows_excluded"], 1)
        self.assertFalse(out["coverage"]["all_grni_complete"])
        self.assertIn("non-PO receipts", out["coverage"]["excluded"])

    def test_cross_business_unit_po_link_fails_without_widening_scope(
            self) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO PS_PO_HDR VALUES "
                "('US002','PO-CROSS','V1009','D','2026-08-01','USD')")
            conn.execute("INSERT INTO PS_RECV_HDR VALUES "
                         "('US001','RCV-CROSS','V1009','2026-08-10','O')")
            conn.execute(
                """INSERT INTO PS_RECV_LN_SHIP VALUES
                   ('US001','RCV-CROSS',1,1,'US002','PO-CROSS',1,1,1,999)"""
            )
        self.db.clear_catalog()
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["population"]["complete"])
        self.assertIsNone(out["rni_totals_by_currency"])
        self.assertIn("will not widen caller scope", out["reason"])

    def test_unknown_receipt_status_fails_closed(self) -> None:
        self._sql("UPDATE PS_RECV_HDR SET RECV_STATUS='Z' "
                  "WHERE RECEIVER_ID='RECV3004'")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("not a governed eligible/excluded value", out["reason"])

    def test_truncated_catalog_resolution_never_uses_delivered_fallback(
            self) -> None:
        with patch.object(self.engine, "list_tables", return_value={
                "tables": [{"table_name": "PS_PO_HDR"}],
                "truncated": True}):
            out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertIn("live-catalog search", out["reason"])
        self.assertIn("truncated", out["reason"])
        self.assertIsNone(out["totals_by_currency"])

    def test_empty_population_is_no_data_not_a_zero_booked_accrual(self) -> None:
        self._sql("DELETE FROM PS_RECV_LN_SHIP")
        out = self._run()
        self.assertEqual(out["status"], "no_data")
        self.assertFalse(out["evaluated"])
        self.assertTrue(out["population"]["complete"])
        self.assertEqual(out["population"]["candidate_count"], 0)
        self.assertIsNone(out["rni_totals_by_currency"])
        self.assertIn("not proof of a zero booked accrual", out["reason"])

    def test_invalid_parameters_are_refused(self) -> None:
        for kwargs in (
            {"as_of_date": "not-a-date"},
            {"as_of_date": "2099-12-31"},
            {"materiality": -1},
            {"aging_days": -1},
            {"max_rows": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ModuleError):
                    self._run(**kwargs)

    def test_row_limit_is_capped_at_memory_safety_ceiling(self) -> None:
        # A no-data scope lets us inspect the effective limit without needing
        # to manufacture 100,001 rows.
        out = self.control.period_end_accrual(
            business_unit="NO_SUCH_BU", as_of_date=self.AS_OF,
            max_rows=999_999,
        )
        self.assertEqual(out["status"], "no_data")
        self.assertEqual(out["population"]["effective_row_cap"], 100_000)
        self.assertEqual(out["population"]["hard_row_cap"], 100_000)


if __name__ == "__main__":
    unittest.main()
