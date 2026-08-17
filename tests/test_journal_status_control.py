"""Focused regressions for exact, fail-closed journal-status evidence."""
from __future__ import annotations

import datetime as dt
import json
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
from pstb.guards import (  # noqa: E402
    financial_result_domains,
    question_financial_domains,
    tool_result_status,
)
from pstb.journal_controls import (  # noqa: E402
    HEADER_STATUS,
    JournalStatusControl,
)


class JournalStatusControlTests(unittest.TestCase):
    THROUGH = "2026-06-30"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pstb-journal-control-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journals.db"
        with sqlite3.connect(self.path) as conn:
            self._create_schema(conn)
            self._insert_journal(conn, "JPOST001", status="P")
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(self.path)
        cfg.db.use_views = False
        self.db = Database(cfg)
        self.addCleanup(self.db.close)
        self.control = JournalStatusControl(TBEngine(self.db, cfg))

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE PS_JRNL_HEADER (
              BUSINESS_UNIT TEXT NOT NULL,
              JOURNAL_ID TEXT NOT NULL,
              JOURNAL_DATE TEXT NOT NULL,
              UNPOST_SEQ INTEGER NOT NULL,
              JRNL_HDR_STATUS TEXT,
              FISCAL_YEAR INTEGER NOT NULL,
              ACCOUNTING_PERIOD INTEGER NOT NULL,
              SOURCE TEXT NOT NULL,
              OPRID TEXT,
              JRNL_PROCESS_REQST TEXT,
              PROCESS_INSTANCE INTEGER,
              POSTED_DATE TEXT,
              LEDGER_GROUP TEXT
            );
            CREATE TABLE PS_JRNL_LN (
              BUSINESS_UNIT TEXT NOT NULL,
              JOURNAL_ID TEXT NOT NULL,
              JOURNAL_DATE TEXT NOT NULL,
              UNPOST_SEQ INTEGER NOT NULL,
              JOURNAL_LINE INTEGER NOT NULL,
              LEDGER TEXT NOT NULL,
              MONETARY_AMOUNT REAL,
              CURRENCY_CD TEXT
            );
            CREATE INDEX H_SCOPE ON PS_JRNL_HEADER
              (BUSINESS_UNIT, FISCAL_YEAR, ACCOUNTING_PERIOD, JOURNAL_DATE);
            CREATE INDEX L_KEY ON PS_JRNL_LN
              (BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE, UNPOST_SEQ, LEDGER);
            CREATE TABLE PS_CAL_DETP_TBL (
              SETID TEXT, CALENDAR_ID TEXT, FISCAL_YEAR INTEGER,
              ACCOUNTING_PERIOD INTEGER, BEGIN_DT TEXT, END_DT TEXT
            );
            INSERT INTO PS_CAL_DETP_TBL VALUES
              ('SHARE', '01', 2026, 6, '2026-06-01', '2026-06-30');
            CREATE TABLE PS_LED_GRP_TBL (
              LEDGER_GROUP TEXT NOT NULL, LEDGER TEXT NOT NULL
            );
            INSERT INTO PS_LED_GRP_TBL VALUES ('ACTUALS', 'ACTUALS');
            """
        )

    @staticmethod
    def _insert_journal(
        conn: sqlite3.Connection,
        journal_id: str,
        *,
        status: str | None = "P",
        business_unit: str = "US001",
        ledger: str = "ACTUALS",
        journal_date: str = "2026-06-20",
        unpost_seq: int = 0,
        source: str = "ONL",
        ledger_group: str | None = "ACTUALS",
        posted_date: str | None = "2026-06-21",
        amounts: tuple[float, ...] = (100.0, -100.0),
        currencies: tuple[str | None, ...] | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO PS_JRNL_HEADER (
                   BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE, UNPOST_SEQ,
                   JRNL_HDR_STATUS, FISCAL_YEAR, ACCOUNTING_PERIOD, SOURCE,
                   OPRID, JRNL_PROCESS_REQST, PROCESS_INSTANCE, POSTED_DATE,
                   LEDGER_GROUP
               ) VALUES (?, ?, ?, ?, ?, 2026, 6, ?, 'GLUSER', 'P', 42,
                         ?, ?)""",
            (business_unit, journal_id, journal_date, unpost_seq, status,
             source, posted_date, ledger_group),
        )
        currencies = currencies or tuple("USD" for _ in amounts)
        for line_no, (amount, currency) in enumerate(
                zip(amounts, currencies), start=1):
            conn.execute(
                """INSERT INTO PS_JRNL_LN (
                       BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE, UNPOST_SEQ,
                       JOURNAL_LINE, LEDGER, MONETARY_AMOUNT, CURRENCY_CD
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (business_unit, journal_id, journal_date, unpost_seq,
                 line_no, ledger, amount, currency),
            )

    def _run(self, **overrides) -> dict:
        params = {
            "business_unit": "US001",
            "ledger": "ACTUALS",
            "fiscal_year": 2026,
            "period": 6,
            "through_date": self.THROUGH,
        }
        params.update(overrides)
        return self.control.evaluate(**params)

    def test_exact_posted_journal_is_complete_and_nets(self) -> None:
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["evaluated"])
        self.assertTrue(out["control_passed"])
        self.assertFalse(out["truncated"])
        self.assertTrue(out["evidence_completeness"]["complete"])
        self.assertTrue(
            out["evidence_completeness"]["population_complete"])
        self.assertEqual(out["population"]["returned_journals"], 1)
        self.assertTrue(out["population"]["population_complete"])

        journal = out["journals"][0]
        self.assertEqual(journal["journal_key"], {
            "business_unit": "US001",
            "journal_id": "JPOST001",
            "journal_date": "2026-06-20",
            "unpost_seq": 0,
        })
        self.assertEqual(journal["header_status_code"], "P")
        self.assertEqual(journal["header_status_disposition"], "posted")
        self.assertEqual(journal["source"], "ONL")
        self.assertEqual(journal["debit_total"], 100.0)
        self.assertEqual(journal["credit_total"], 100.0)
        self.assertEqual(journal["signed_net"], 0.0)
        self.assertEqual(journal["currency"], "USD")
        self.assertTrue(journal["netting"])
        self.assertEqual(journal["operator_fields"]["oprid"], "GLUSER")
        self.assertEqual(
            journal["process_fields"]["posted_date"], "2026-06-21")
        self.assertFalse(out["cutoff"]["historical_status_reconstructed"])
        self.assertEqual(
            out["cutoff"]["journal_date_through"], self.THROUGH)

    def test_actual_evaluator_payload_satisfies_strict_evidence_contract(self) -> None:
        out = self._run(journal_id="JPOST001")
        ok, reason = tool_result_status("get_journal_status", json.dumps(out))
        self.assertTrue(ok, reason)
        self.assertTrue(out["evidence_completeness"]["statuses_classified"])

    def test_nonexistent_exact_journal_is_no_data_not_a_pass(self) -> None:
        out = self._run(journal_id="DOESNOTEXIST")
        self.assertEqual(out["status"], "no_data")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["control_passed"])
        self.assertFalse(out["evidence_completeness"]["complete"])
        self.assertEqual(out["population"]["returned_journals"], 0)
        self.assertIn("not a zero, pass, or clean", out["reason"])

    def test_header_without_lines_is_observed_but_netting_is_incomplete(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_journal(
                conn, "JHEADERONLY", status="N", posted_date=None,
                amounts=(), currencies=()
            )
        out = self._run(journal_id="JHEADERONLY")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["evaluated"])
        self.assertTrue(out["status_evaluated"])
        self.assertFalse(out["status_control_passed"])
        self.assertFalse(out["control_passed"])
        self.assertFalse(out["netting_evaluated"])
        self.assertIsNone(out["netting_passed"])
        self.assertTrue(out["evidence_completeness"]["status_complete"])
        self.assertFalse(out["evidence_completeness"]["netting_complete"])
        ok, reason = tool_result_status(
            "get_journal_status", json.dumps(out)
        )
        self.assertTrue(ok, reason)
        grounded = financial_result_domains(
            "get_journal_status", json.dumps(out))
        self.assertEqual(grounded, {"journal"})
        for question in (
            "Does journal JHEADERONLY net to zero?",
            "Was journal JHEADERONLY posted by June 30?",
            "Was journal JHEADERONLY valid at June 30?",
        ):
            self.assertFalse(
                question_financial_domains(question).issubset(grounded),
                question,
            )
        self.assertEqual(out["population"]["returned_journals"], 1)
        self.assertEqual(len(out["journals"]), 1)
        journal = out["journals"][0]
        self.assertEqual(journal["header_status_code"], "N")
        self.assertEqual(journal["action_class"], "edit_required")
        self.assertEqual(journal["line_count"], 0)
        self.assertIsNone(journal["signed_net"])
        self.assertIsNone(journal["netting"])
        self.assertEqual(
            journal["ledger_scope_basis"],
            "LED_GRP_TBL ledger-group membership")
        self.assertIn("has a journal header but no line", out["reason"])
        self.assertNotIn("No journal with ID", out["reason"])

    def test_ledger_group_name_can_differ_from_selected_ledger(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_JRNL_HEADER SET LEDGER_GROUP='LOCAL_GAAP' "
                "WHERE JOURNAL_ID='JPOST001'"
            )
            conn.execute(
                "INSERT INTO PS_LED_GRP_TBL VALUES ('LOCAL_GAAP', 'ACTUALS')"
            )
            conn.execute("DELETE FROM PS_JRNL_LN WHERE JOURNAL_ID='JPOST001'")
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["status_control_passed"])
        self.assertFalse(out["netting_evaluated"])
        journal = out["journals"][0]
        self.assertTrue(journal["ledger_scope_confirmed"])
        self.assertEqual(
            journal["ledger_scope_basis"],
            "LED_GRP_TBL ledger-group membership",
        )

    def test_missing_line_table_does_not_erase_exact_header_status(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE PS_JRNL_LN")
        self.db.clear_catalog()
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["status_evaluated"])
        self.assertTrue(out["status_control_passed"])
        self.assertFalse(out["netting_evaluated"])
        self.assertIsNone(out["netting_passed"])
        self.assertEqual(out["journals"][0]["header_status_code"], "P")
        self.assertFalse(out["records"]["line"]["available"])

    def test_unreadable_group_membership_and_no_lines_keeps_scope_incomplete(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE PS_LED_GRP_TBL")
            conn.execute("DELETE FROM PS_JRNL_LN WHERE JOURNAL_ID='JPOST001'")
        self.db.clear_catalog()
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["status_control_passed"])
        self.assertEqual(out["journals"][0]["header_status_code"], "P")
        self.assertFalse(out["journals"][0]["ledger_scope_confirmed"])
        self.assertIn("cannot be confirmed", out["reason"])

    def test_missing_line_currency_only_disables_netting_leg(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE PS_JRNL_LN DROP COLUMN CURRENCY_CD")
        self.db.clear_catalog()
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["status_control_passed"])
        self.assertFalse(out["netting_evaluated"])
        self.assertIn(
            "CURRENCY_CD",
            out["evidence_completeness"]["missing_line_netting_fields"],
        )

    def test_posted_and_unposted_are_not_collapsed_to_pending(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_journal(conn, "JUNPOST", status="U")
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertFalse(out["control_passed"])
        statuses = {
            row["journal_key"]["journal_id"]: row
            for row in out["journals"]
        }
        self.assertEqual(statuses["JPOST001"]["header_status_disposition"],
                         "posted")
        self.assertEqual(statuses["JUNPOST"]["header_status_disposition"],
                         "unposted")
        exception = next(
            row for row in out["exceptions"]
            if row["journal_key"]["journal_id"] == "JUNPOST"
        )
        self.assertEqual(exception["category"], "review_unpost")
        self.assertEqual(exception["observed_status"], "U")

    def test_all_delivered_header_codes_have_exact_distinct_meanings(self) -> None:
        self.assertEqual(set(HEADER_STATUS), set("DIMENTUPVZ"))
        self.assertEqual(
            HEADER_STATUS["T"]["label"], "Journal entry incomplete")
        self.assertEqual(
            HEADER_STATUS["M"]["disposition"], "sje_model")
        self.assertEqual(
            HEADER_STATUS["Z"]["disposition"], "upgrade_cannot_unpost")

    def test_deleted_model_and_upgrade_codes_are_informational(self) -> None:
        for code in ("D", "M", "Z"):
            with sqlite3.connect(self.path) as conn:
                self._insert_journal(conn, f"JINFO{code}", status=code)
        out = self._run()
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["control_passed"])
        self.assertEqual(out["exceptions"], [])
        info = {
            row["header_status_code"]: row
            for row in out["journals"]
            if row["header_status_code"] in {"D", "M", "Z"}
        }
        self.assertEqual(set(info), {"D", "M", "Z"})
        self.assertTrue(all(
            row["action_class"] == "informational"
            and row["requires_close_action"] is False
            for row in info.values()
        ))

    def test_non_netting_posted_journal_is_an_observed_exception(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_JRNL_LN SET MONETARY_AMOUNT=-90 "
                "WHERE JOURNAL_ID='JPOST001' AND JOURNAL_LINE=2"
            )
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["status_control_passed"])
        self.assertTrue(out["control_passed"])
        self.assertTrue(out["netting_evaluated"])
        self.assertFalse(out["netting_passed"])
        journal = out["journals"][0]
        self.assertEqual(journal["signed_net"], 10.0)
        self.assertFalse(journal["netting"])
        exception = next(
            row for row in out["exceptions"]
            if row["category"] == "journal_not_netting_in_selected_ledger"
        )
        self.assertEqual(exception["observed_signed_net"], 10.0)
        self.assertEqual(exception["currency"], "USD")

    def test_missing_optional_approval_fields_remains_honest_and_complete(self) -> None:
        # This fixture has no APPROVAL_STATUS/APPR_STATUS/WF_STATUS columns.
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["evidence_completeness"]["complete"])
        journal = out["journals"][0]
        self.assertNotIn("approval_fields", journal)
        unavailable = out["evidence_completeness"][
            "optional_header_fields_unavailable"]
        self.assertIn("APPROVAL_STATUS", unavailable)
        self.assertIn("APPR_STATUS", unavailable)
        self.assertIn("WF_STATUS", unavailable)

    def test_missing_source_label_does_not_remove_core_status_evidence(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE PS_JRNL_HEADER DROP COLUMN SOURCE")
        self.db.clear_catalog()
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["control_passed"])
        self.assertNotIn("source", out["journals"][0])
        self.assertIn(
            "SOURCE",
            out["evidence_completeness"][
                "optional_header_fields_unavailable"],
        )

    def test_population_cap_returns_partial_status_but_no_clean_conclusion(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_journal(conn, "JSECOND", status="P")
        out = self._run(limit=1)
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["control_passed"])
        self.assertTrue(out["truncated"])
        self.assertFalse(out["population"]["population_complete"])
        self.assertFalse(
            out["evidence_completeness"]["population_complete"])
        self.assertEqual(len(out["journals"]), 1)
        self.assertNotIn("signed_net", out["journals"][0])
        self.assertIn("At least 2 journals", out["reason"])

    def test_wrong_business_unit_or_ledger_never_widens_scope(self) -> None:
        for kwargs in (
            {"business_unit": "US999"},
            {"ledger": "BUDGETS"},
        ):
            with self.subTest(kwargs=kwargs):
                out = self._run(**kwargs)
                self.assertEqual(out["status"], "no_data")
                self.assertFalse(out["evaluated"])
                self.assertIsNone(out["control_passed"])
                self.assertEqual(out["population"]["returned_journals"], 0)

    def test_unknown_or_null_header_status_is_incomplete(self) -> None:
        for status in ("X", None):
            with self.subTest(status=status):
                with sqlite3.connect(self.path) as conn:
                    conn.execute(
                        "UPDATE PS_JRNL_HEADER SET JRNL_HDR_STATUS=? "
                        "WHERE JOURNAL_ID='JPOST001'", (status,)
                    )
                out = self._run(journal_id="JPOST001")
                self.assertEqual(out["status"], "incomplete")
                self.assertFalse(out["evaluated"])
                self.assertIsNone(out["control_passed"])
                self.assertFalse(out["evidence_completeness"]["complete"])

    def test_missing_required_status_column_fails_closed(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE PS_JRNL_HEADER DROP COLUMN JRNL_HDR_STATUS")
        self.db.clear_catalog()
        out = self._run()
        self.assertEqual(out["status"], "incomplete")
        self.assertFalse(out["evaluated"])
        self.assertIn("JRNL_HDR_STATUS", out["reason"])
        self.assertEqual(out["journals"], [])

    def test_full_key_keeps_unpost_sequence_and_date_versions_separate(self) -> None:
        with sqlite3.connect(self.path) as conn:
            self._insert_journal(
                conn, "JPOST001", status="U", journal_date="2026-06-20",
                unpost_seq=1,
            )
            self._insert_journal(
                conn, "JPOST001", status="P", journal_date="2026-06-25",
                unpost_seq=0,
            )
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        keys = {
            (row["journal_date"], row["unpost_sequence"])
            for row in out["journals"]
        }
        self.assertEqual(keys, {
            ("2026-06-20", 0),
            ("2026-06-20", 1),
            ("2026-06-25", 0),
        })

    def test_psrecdefn_custom_physical_names_win_without_prefix_assumption(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                ALTER TABLE PS_JRNL_HEADER RENAME TO ACME_JRNL_HEADER;
                ALTER TABLE PS_JRNL_LN RENAME TO ACME_JRNL_LN;
                CREATE TABLE PSRECDEFN (RECNAME TEXT, SQLTABLENAME TEXT);
                INSERT INTO PSRECDEFN VALUES
                  ('JRNL_HEADER', 'ACME_JRNL_HEADER'),
                  ('JRNL_LN', 'ACME_JRNL_LN');
                """
            )
        self.db.clear_catalog()
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["records"]["header"]["name"],
                         "ACME_JRNL_HEADER")
        self.assertEqual(out["records"]["line"]["name"], "ACME_JRNL_LN")
        self.assertEqual(out["records"]["header"]["resolution_basis"],
                         "PSRECDEFN.SQLTABLENAME")

    def test_mixed_currencies_are_separated_and_inconclusive(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE PS_JRNL_LN SET CURRENCY_CD='EUR' "
                "WHERE JOURNAL_ID='JPOST001' AND JOURNAL_LINE=2"
            )
        out = self._run(journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertTrue(out["evaluated"])
        self.assertTrue(out["status_evaluated"])
        self.assertTrue(out["status_control_passed"])
        self.assertFalse(out["netting_evaluated"])
        self.assertIsNone(out["netting_passed"])
        journal = out["journals"][0]
        self.assertIsNone(journal["signed_net"])
        self.assertIsNone(journal["currency"])
        self.assertEqual(
            {row["currency"] for row in journal["currency_totals"]},
            {"USD", "EUR"},
        )
        self.assertIn("not silently combined", out["reason"])

    def test_future_cutoff_and_missing_explicit_scope_fail_closed(self) -> None:
        future = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        for kwargs in (
            {"business_unit": ""},
            {"ledger": ""},
            {"through_date": future},
            {"through_date": "2026-07-01"},
        ):
            with self.subTest(kwargs=kwargs):
                out = self._run(**kwargs)
                self.assertEqual(out["status"], "incomplete")
                self.assertFalse(out["evaluated"])
                self.assertIsNone(out["control_passed"])

    def test_blank_cutoff_resolves_to_selected_fiscal_period_end(self) -> None:
        out = self._run(through_date="", journal_id="JPOST001")
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["cutoff"]["journal_date_through"], "2026-06-30")
        self.assertEqual(out["cutoff"]["resolved_from"],
                         "fiscal calendar period end")


if __name__ == "__main__":
    unittest.main()
