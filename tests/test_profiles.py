"""Record profiling, and the masking that makes it safe to send anywhere.

Sample rows leave this process and reach the configured model. On the Gemini
path that means they leave the company's network, so the masking rules here are
a data-protection control, not a nicety — these tests are the control's
evidence.

The second half covers the selection signal itself: an agent choosing between
PS_ITEM, its history shell and a custom staging record cannot do it on names,
and these assertions pin the evidence that lets it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.profiles import is_sensitive, mask  # noqa: E402


class MaskingRuleTests(unittest.TestCase):
    def test_personal_columns_are_sensitive(self) -> None:
        # PeopleSoft numbers its repeating columns, so the numbered variants
        # are listed deliberately: an earlier rule masked NAME1 (explicitly
        # listed) while letting ADDRESS1 through.
        for col in ("NAME1", "NAME2", "VENDOR_NAME1", "CONTACT_NAME",
                    "ADDRESS1", "ADDRESS4", "ADDR2", "CITY", "POSTAL",
                    "PHONE1", "FAX", "EMAILID", "BILL_TO_EMAIL_ADDR",
                    "SSN", "NATIONAL_ID", "TAX_ID", "BIRTHDATE", "IBAN",
                    "BANK_ACCOUNT_NUM", "CREDIT_CARD_NBR", "PASSWORD",
                    "ANNUAL_RT", "COMPRATE"):
            self.assertTrue(is_sensitive(col), f"{col} should be masked")

    def test_identifiers_and_codes_are_not_masked(self) -> None:
        # Masking these would defeat the purpose: the model needs them to
        # build a predicate, and none of them identify a person.
        for col in ("BUSINESS_UNIT", "SETID", "LEDGER", "ACCOUNT", "CUST_ID",
                    "VENDOR_ID", "ITEM_STATUS", "CURRENCY_CD", "FISCAL_YEAR",
                    "ACCOUNTING_PERIOD", "JOURNAL_ID", "POSTED_TOTAL_AMT",
                    "BAL_AMT", "DEPTID", "BANK_CD", "ACCOUNT_TYPE", "EFFDT",
                    "DESCR", "INVOICE_AMOUNT", "ITEM"):
            self.assertFalse(is_sensitive(col), f"{col} must not be masked")

    def test_mask_hides_the_value_but_keeps_the_shape(self) -> None:
        out = mask("ACME Industrial", "NAME1")
        self.assertNotIn("ACME", out)
        self.assertNotIn("Industrial", out)
        self.assertIn("15", out)          # length is the retained signal
        self.assertTrue(out.startswith("A"))

    def test_mask_handles_numbers_and_nulls(self) -> None:
        self.assertIsNone(mask(None, "SSN"))
        self.assertEqual(mask(123456789, "SSN"), "###")
        self.assertEqual(mask("   ", "NAME1"), "   ")

    def test_no_full_sensitive_value_survives_masking(self) -> None:
        for secret in ("Jonathan Q Public", "12 Rose Lane, Springfield",
                       "078-05-1120", "user@example.com"):
            self.assertNotIn(secret, str(mask(secret, "NAME1")))


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(self.cfg)
        self.engine = TBEngine(self.db, self.cfg)

    def tearDown(self) -> None:
        self.db.close()

    def test_status_codes_are_reported(self) -> None:
        # The strongest fit signal in the whole feature: knowing ITEM_STATUS
        # actually holds 'O' and 'C' turns a guessed predicate into a right one.
        prof = self.engine.profile_record("PS_ITEM")
        self.assertIn("ITEM_STATUS", prof["value_counts"])
        self.assertIn("O", prof["value_counts"]["ITEM_STATUS"])

    def test_columns_this_site_never_populates_are_named(self) -> None:
        prof = self.engine.profile_record("PS_ITEM")
        self.assertIn("PO_REF", prof["always_null"])

    def test_sample_rows_are_masked_in_the_payload(self) -> None:
        prof = self.engine.profile_record("PS_CUSTOMER")
        self.assertIn("NAME1", prof["masked_columns"])
        blob = str(prof["sample"])
        # The real name is in the sample database; it must not be in what we
        # would hand to a model.
        self.assertNotIn("ACME Industrial", blob)
        self.assertIn("C1001", blob, "the customer ID should survive masking")

    def test_sample_rows_can_be_switched_off_entirely(self) -> None:
        self.cfg.tools.sample_rows = 0
        try:
            prof = self.engine.profile_record("PS_CUSTOMER")
        finally:
            self.cfg.tools.sample_rows = 3
        self.assertNotIn("sample", prof)
        # Everything that is not row-level data still comes back, so a site
        # that forbids row egress keeps the whole selection signal.
        self.assertIn("value_counts", prof)
        self.assertIn("fill_percent", prof)

    def test_a_missing_record_is_reported_not_raised(self) -> None:
        prof = self.engine.profile_record("PS_DOES_NOT_EXIST_XX")
        self.assertFalse(prof["readable"])
        self.assertIn("search_records", prof["note"])

    def test_an_injected_name_is_refused(self) -> None:
        with self.assertRaises(EngineError):
            self.engine.profile_record("PS_ITEM; DROP TABLE PS_ITEM")

    def test_profiling_never_scans_the_whole_record(self) -> None:
        from tests.test_query_caching import QueryCountRecorder

        with QueryCountRecorder(self.db) as rec:
            self.engine.profile_record("PS_ITEM")
        reads = [s for s in rec.data]
        self.assertTrue(reads)
        for sql in reads:
            self.assertTrue(
                any(cap in sql.upper() for cap in ("LIMIT", "ROWNUM", "TOP")),
                f"profiling issued an uncapped read: {sql[:90]}")


class CompareTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()

    def test_empty_and_unreadable_candidates_are_separated(self) -> None:
        out = self.engine.compare_records(
            ["PS_ITEM", "PS_BI_HDR", "PS_NOT_A_RECORD_XX"])
        self.assertIn("PS_ITEM", out["readable_and_populated"])
        self.assertIn("PS_NOT_A_RECORD_XX", out["empty_or_unreadable"])

    def test_comparison_is_bounded(self) -> None:
        out = self.engine.compare_records(["PS_ITEM"] * 20)
        self.assertLessEqual(len(out["candidates"]), 6)

    def test_no_candidates_is_an_error_not_an_empty_answer(self) -> None:
        with self.assertRaises(EngineError):
            self.engine.compare_records([])


if __name__ == "__main__":
    unittest.main()
