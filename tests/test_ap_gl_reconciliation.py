"""Controller-grade AP accounting activity to GL journal reconciliation."""
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
from pstb.modules import ModulePacks  # noqa: E402


class APGLReconciliationTests(unittest.TestCase):
    CONTROL = "APCTRL"
    AS_OF = "2026-06-30"
    ACTIVITY = -125_000.0

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pstb-apgl-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "apgl.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE PS_VCHR_ACCTG_LINE (
                       BUSINESS_UNIT TEXT, BUSINESS_UNIT_GL TEXT,
                       VOUCHER_ID TEXT, UNPOST_SEQ INTEGER,
                       POSTING_PROCESS TEXT, LEDGER TEXT, ACCOUNT TEXT,
                       ACCOUNTING_DT TEXT, FISCAL_YEAR INTEGER,
                       ACCOUNTING_PERIOD INTEGER, MONETARY_AMOUNT REAL,
                       CURRENCY_CD TEXT, POST_STATUS_AP TEXT,
                       GL_DISTRIB_STATUS TEXT, JOURNAL_ID TEXT,
                       JOURNAL_DATE TEXT, JOURNAL_LINE INTEGER
                   )"""
            )
            self._insert_pair(conn, "APREC2606", 1, self.ACTIVITY)

        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(self.path)
        cfg.db.use_views = False
        cfg.defaults.business_unit = "US001"
        cfg.defaults.ledger = "ACTUALS"
        self.db = Database(cfg)
        self.addCleanup(self.db.close)
        self.engine = TBEngine(self.db, cfg)
        self.m = ModulePacks(self.engine)

    def _insert_ap(
        self, conn: sqlite3.Connection, journal_id: str, journal_line: int,
        amount: float, *, account: str | None = None, currency: str = "USD",
        post_status: str = "P", distribution_status: str = "D",
        posting_process: str = "ACCR", accounting_date: str | None = None,
        fiscal_year: int = 2026, period: int = 6, ledger: str = "ACTUALS",
    ) -> None:
        day = accounting_date or self.AS_OF
        conn.execute(
            """INSERT INTO PS_VCHR_ACCTG_LINE (
                   BUSINESS_UNIT, BUSINESS_UNIT_GL, VOUCHER_ID, UNPOST_SEQ,
                   POSTING_PROCESS, LEDGER, ACCOUNT, ACCOUNTING_DT,
                   FISCAL_YEAR, ACCOUNTING_PERIOD, MONETARY_AMOUNT,
                   CURRENCY_CD, POST_STATUS_AP, GL_DISTRIB_STATUS,
                   JOURNAL_ID, JOURNAL_DATE, JOURNAL_LINE
               ) VALUES ('US001', 'US001', 'VTEST001', 0, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (posting_process, ledger, account or self.CONTROL, day,
             fiscal_year, period, amount, currency, post_status,
             distribution_status, journal_id, day, journal_line),
        )

    def _insert_gl(
        self, conn: sqlite3.Connection, journal_id: str, journal_line: int,
        amount: float, *, account: str | None = None, currency: str = "USD",
        header_status: str = "P", journal_date: str | None = None,
        fiscal_year: int = 2026, period: int = 6, unpost_seq: int = 0,
    ) -> None:
        day = journal_date or self.AS_OF
        conn.execute(
            """INSERT INTO PS_JRNL_HEADER (
                   BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE, UNPOST_SEQ,
                   JRNL_HDR_STATUS, FISCAL_YEAR, ACCOUNTING_PERIOD, SOURCE,
                   OPRID, POSTED_DATE, DESCR254_MIXED, LEDGER_GROUP, CURRENCY_CD
               ) VALUES ('US001', ?, ?, ?, ?, ?, ?, 'AP', 'APPOST', ?,
                         'AP reconciliation fixture', 'ACTUALS', ?)""",
            (journal_id, day, unpost_seq, header_status, fiscal_year, period,
             day, currency),
        )
        conn.execute(
            """INSERT INTO PS_JRNL_LN (
                   BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE, UNPOST_SEQ,
                   JOURNAL_LINE, LEDGER, ACCOUNT, DEPTID, CURRENCY_CD,
                   MONETARY_AMOUNT, FOREIGN_AMOUNT, FOREIGN_CURRENCY,
                   LINE_DESCR
               ) VALUES ('US001', ?, ?, ?, ?, 'ACTUALS', ?, '10000', ?,
                         ?, ?, ?, 'AP control activity')""",
            (journal_id, day, unpost_seq, journal_line,
             account or self.CONTROL, currency, amount, amount, currency),
        )

    def _insert_pair(
        self, conn: sqlite3.Connection, journal_id: str, journal_line: int,
        amount: float, **kwargs,
    ) -> None:
        self._insert_ap(conn, journal_id, journal_line, amount, **kwargs)
        gl_kwargs = {
            key: value for key, value in kwargs.items()
            if key in {"account", "currency", "fiscal_year", "period"}
        }
        if "accounting_date" in kwargs:
            gl_kwargs["journal_date"] = kwargs["accounting_date"]
        self._insert_gl(conn, journal_id, journal_line, amount, **gl_kwargs)

    def _run(self, **kwargs) -> dict:
        params = {
            "business_unit": "US001",
            "control_accounts": self.CONTROL,
            "as_of_date": self.AS_OF,
        }
        params.update(kwargs)
        return self.m.reconcile_ap_to_gl(**params)

    def _delete_fixture_pair(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM PS_VCHR_ACCTG_LINE")
            conn.execute("DELETE FROM PS_JRNL_LN WHERE JOURNAL_ID='APREC2606'")
            conn.execute("DELETE FROM PS_JRNL_HEADER WHERE JOURNAL_ID='APREC2606'")

    def test_exact_journal_key_activity_tie_is_evaluated(self) -> None:
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["evaluated"])
        self.assertTrue(out["ties"])
        self.assertTrue(out["aggregate_ties"])
        self.assertEqual(out["subledger_total"], self.ACTIVITY)
        self.assertEqual(out["gl_total"], self.ACTIVITY)
        self.assertEqual(out["gl_balance"], self.ACTIVITY)
        self.assertEqual(out["difference"], 0.0)
        self.assertEqual(out["currency"], "USD")
        self.assertEqual(out["accounting_source"], "PS_VCHR_ACCTG_LINE")
        self.assertEqual(
            out["accounting_source_basis"], "unique live-catalog suffix")
        self.assertIn("not an AP open-liability", out["amount_basis"])
        self.assertEqual(out["population"]["matched_journal_keys"], 1)
        self.assertEqual(
            out["population"]["journal_key"],
            ["BUSINESS_UNIT_GL", "JOURNAL_ID", "JOURNAL_DATE",
             "JOURNAL_LINE", "LEDGER", "ACCOUNT"],
        )

    def test_missing_accounting_line_source_fails_closed(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE PS_VCHR_ACCTG_LINE")
        self.db.clear_catalog()
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIsNone(out["subledger_total"])
        self.assertIn("Voucher headers", out["reason"])
        self.assertIn("APY1410/APY1420", out["reason"])

    def test_wrong_liability_account_never_enters_selected_control_tie(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE PS_VCHR_ACCTG_LINE SET ACCOUNT='OTHERCTRL'")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertFalse(out["ties"])
        self.assertEqual(out["subledger_total"], 0.0)
        self.assertEqual(out["gl_total"], self.ACTIVITY)
        self.assertEqual(out["difference"], -self.ACTIVITY)
        category = next(row for row in out["reconciling_categories"]
                        if row["category"] ==
                        "posted_gl_without_ap_accounting_key")
        self.assertEqual(category["evidence"], "observed")

    def test_missing_required_accounting_field_is_incomplete(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "ALTER TABLE PS_VCHR_ACCTG_LINE DROP COLUMN POSTING_PROCESS")
        self.db.clear_catalog()
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIn("POSTING_PROCESS", out["missing_columns"])
        self.assertIn("No voucher-header approximation", out["reason"])

    def test_null_ap_amount_fails_closed_instead_of_becoming_zero(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_VCHR_ACCTG_LINE SET MONETARY_AMOUNT=NULL")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIsNone(out["subledger_total"])
        self.assertIn("MONETARY_AMOUNT is null", out["reason"])

    def test_null_accounting_date_fails_closed_instead_of_leaving_scope(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE PS_VCHR_ACCTG_LINE SET ACCOUNTING_DT=NULL")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIn("ACCOUNTING_DT is blank or invalid", out["reason"])

    def test_null_gl_amount_fails_closed_instead_of_becoming_zero(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_JRNL_LN SET MONETARY_AMOUNT=NULL "
                "WHERE JOURNAL_ID='APREC2606'")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIsNone(out["gl_total"])
        self.assertIn("MONETARY_AMOUNT is null", out["reason"])

    def test_incomplete_journal_generator_key_cannot_coincidentally_tie(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE PS_VCHR_ACCTG_LINE SET JOURNAL_ID=''")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIsNone(out["difference"])
        self.assertIn("complete Journal Generator", out["reason"])

    def test_zero_journal_line_means_drill_key_is_unavailable(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE PS_VCHR_ACCTG_LINE SET JOURNAL_LINE=0")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIn("drill-down key", out["reason"])

    def test_distributed_ap_without_posted_gl_is_an_observed_difference(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM PS_JRNL_LN WHERE JOURNAL_ID='APREC2606'")
            conn.execute("DELETE FROM PS_JRNL_HEADER WHERE JOURNAL_ID='APREC2606'")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertFalse(out["ties"])
        self.assertEqual(out["difference"], self.ACTIVITY)
        category = next(row for row in out["reconciling_categories"]
                        if row["category"] ==
                        "distributed_ap_without_posted_gl_key")
        self.assertEqual(category["signed_amount"], self.ACTIVITY)

    def test_aggregate_offset_does_not_hide_key_exceptions(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_ap(conn, "APONLY2606", 1, -250.0)
            self._insert_gl(conn, "GLONLY2606", 1, -250.0)
        out = self._run()
        self.assertTrue(out["aggregate_ties"])
        self.assertFalse(out["ties"])
        self.assertEqual(out["difference"], 0.0)
        self.assertEqual(
            out["conclusion"], "aggregate_tie_with_key_or_status_exceptions")
        categories = {row["category"] for row in out["reconciling_categories"]}
        self.assertIn("distributed_ap_without_posted_gl_key", categories)
        self.assertIn("posted_gl_without_ap_accounting_key", categories)

    def test_nondistributed_ap_activity_prevents_a_clean_tie(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_ap(
                conn, "APPEND2606", 1, -500.0, distribution_status="N")
        out = self._run()
        self.assertTrue(out["aggregate_ties"])
        self.assertFalse(out["ties"])
        category = next(row for row in out["reconciling_categories"]
                        if row["category"] ==
                        "ap_accounting_not_distributed_to_gl")
        self.assertFalse(category["included_in_subledger_total"])
        self.assertEqual(category["status_groups"][0]["gl_distrib_status"], "N")

    def test_only_ineligible_rows_are_incomplete_not_a_zero_tie(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_VCHR_ACCTG_LINE SET POST_STATUS_AP='U', "
                "GL_DISTRIB_STATUS='N'")
            conn.execute("DELETE FROM PS_JRNL_LN WHERE JOURNAL_ID='APREC2606'")
            conn.execute("DELETE FROM PS_JRNL_HEADER WHERE JOURNAL_ID='APREC2606'")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIsNone(out["difference"])
        self.assertIn("not a reconciliation pass", out["reason"])

    def test_unposted_gl_journal_is_not_counted_as_posted_activity(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_JRNL_HEADER SET JRNL_HDR_STATUS='U' "
                "WHERE JOURNAL_ID='APREC2606'")
        out = self._run()
        self.assertFalse(out["ties"])
        self.assertEqual(out["gl_total"], 0.0)
        self.assertEqual(out["difference"], self.ACTIVITY)
        category = next(row for row in out["reconciling_categories"]
                        if row["category"] ==
                        "control_account_journals_not_gl_posted")
        self.assertEqual(category["header_statuses"], ["U"])

    def test_all_ap_posting_processes_are_included_and_disclosed(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_pair(
                conn, "APPYMN2606", 1, 25_000.0, posting_process="PYMN")
        out = self._run()
        self.assertTrue(out["ties"])
        self.assertEqual(out["subledger_total"], -100_000.0)
        processes = {
            row["posting_process"]
            for row in out["population"]["status_groups"]
            if row["post_status_ap"] == "P"
            and row["gl_distrib_status"] == "D"
        }
        self.assertEqual(processes, {"ACCR", "PYMN"})

    def test_other_ledger_rows_are_filtered_before_validation_and_cap(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_ap(
                conn, "APALT2606", 1, -999.0, ledger="ALTLEDGER")
            conn.execute(
                "UPDATE PS_VCHR_ACCTG_LINE SET MONETARY_AMOUNT=NULL "
                "WHERE LEDGER='ALTLEDGER'"
            )
        self.engine.cfg.tools.ap_reconciliation_line_cap = 1
        out = self._run()
        self.assertTrue(out["ties"])
        self.assertEqual(out["subledger_total"], self.ACTIVITY)
        self.assertEqual(out["population"]["ap_rows_read"], 1)
        self.assertNotIn(
            "ALTLEDGER",
            {row["ledger"] for row in out["population"]["status_groups"]})

    def test_blank_ledger_on_distributed_ap_line_fails_closed(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE PS_VCHR_ACCTG_LINE SET LEDGER='' ")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIn("blank ledger", out["reason"])

    def test_mixed_currency_activity_is_not_summed(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE PS_VCHR_ACCTG_LINE SET CURRENCY_CD='EUR'")
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["difference"])
        self.assertEqual(out["mixed_currencies"], ["EUR", "USD"])
        self.assertIn("not summed or translated", out["reason"])

    def test_custom_company_prefixed_accounting_table_is_resolved(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "ALTER TABLE PS_VCHR_ACCTG_LINE "
                "RENAME TO ACME_VCHR_ACCTG_LINE")
        self.db.clear_catalog()
        out = self._run()
        self.assertTrue(out["ties"])
        self.assertEqual(out["accounting_source"], "ACME_VCHR_ACCTG_LINE")

    def test_peopletools_physical_override_wins_over_readable_ps_table(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE ACME_VCHR_ACCTG_LINE AS "
                "SELECT * FROM PS_VCHR_ACCTG_LINE"
            )
            conn.execute("DELETE FROM PS_VCHR_ACCTG_LINE")
            conn.execute(
                "INSERT INTO PSRECDEFN (RECNAME, RECDESCR, RECTYPE, "
                "SQLTABLENAME) VALUES ('VCHR_ACCTG_LINE', "
                "'AP Accounting Entries', 0, 'ACME_VCHR_ACCTG_LINE')"
            )
        self.db.clear_catalog()
        out = self._run()
        self.assertTrue(out["ties"])
        self.assertEqual(out["accounting_source"], "ACME_VCHR_ACCTG_LINE")
        self.assertEqual(
            out["accounting_source_basis"], "PSRECDEFN.SQLTABLENAME")

    def test_configured_accounts_are_reachable_when_argument_omitted(self) -> None:
        self.engine.cfg.defaults.ap_control_accounts = [self.CONTROL]
        out = self.m.reconcile_ap_to_gl(
            business_unit="US001", as_of_date=self.AS_OF)
        self.assertTrue(out["ties"])
        self.assertEqual(out["control_accounts"], [self.CONTROL])
        self.assertEqual(
            out["control_accounts_source"],
            "config defaults.ap_control_accounts",
        )

    def test_missing_control_accounts_never_guesses_an_account(self) -> None:
        out = self.m.reconcile_ap_to_gl(
            business_unit="US001", as_of_date=self.AS_OF)
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertEqual(out["control_accounts"], [])
        self.assertIn("no account is assumed", out["reason"])

    def test_empty_period_is_no_data_not_a_zero_or_pass(self) -> None:
        self._delete_fixture_pair()
        out = self._run()
        self.assertEqual(out["status"], "no_data")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIn("not a reconciliation pass", out["reason"])

    def test_as_of_date_and_explicit_period_must_agree(self) -> None:
        out = self._run(fiscal_year=2026, period=5)
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIn("belongs to FY2026 period 6", out["reason"])

    def test_duplicate_posted_gl_key_fails_closed(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO PS_JRNL_LN (
                       BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE, UNPOST_SEQ,
                       JOURNAL_LINE, LEDGER, ACCOUNT, DEPTID, CURRENCY_CD,
                       MONETARY_AMOUNT, FOREIGN_AMOUNT, FOREIGN_CURRENCY,
                       LINE_DESCR
                   ) VALUES ('US001', 'APREC2606', '2026-06-30', 0, 1,
                             'ACTUALS', ?, '99999', 'USD', 0, 0, 'USD',
                             'duplicate key')""",
                (self.CONTROL,),
            )
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["ties"])
        self.assertIn("not unique", out["reason"])

    def test_line_cap_reports_partial_without_totals(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_pair(conn, "APREC2606B", 1, -10.0)
        self.engine.cfg.tools.ap_reconciliation_line_cap = 1
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["subledger_total"])
        self.assertIsNone(out["difference"])
        self.assertEqual(out["population"]["status"], "partial")

    def test_configured_line_cap_has_a_100k_runtime_ceiling(self) -> None:
        self.engine.cfg.tools.ap_reconciliation_line_cap = 250_000
        out = self._run()
        self.assertTrue(out["ties"])
        self.assertEqual(out["population"]["line_safety_cap"], 100_000)


if __name__ == "__main__":
    unittest.main()
