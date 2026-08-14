"""Truthfulness regressions for AP duplicate-payment findings.

The bundled duplicate fixture is deliberately a *voucher* duplicate: its
cross-reference rows point at payment IDs that do not exist in
PS_PAYMENT_TBL.  That makes it a useful boundary test.  A voucher candidate
must remain visible for review, but the tool may call it a confirmed duplicate
payment only after two distinct vouchers lead to two distinct, non-void
payment headers.

The legacy result keys are part of the public tool contract.  These tests keep
them while pinning the new payment-evidence distinction.
"""
from __future__ import annotations

import re
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
from pstb.modules import ModulePacks  # noqa: E402

BU = "US001"
AS_OF = "2026-06-30"
INVOICE = "INV-DUP01"


class DuplicatePaymentTruthTests(unittest.TestCase):
    def _result(self, payment_headers=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "duplicate-payment.db"
        path.write_bytes((ROOT / "sample_data" / "ps_sample.db").read_bytes())

        with sqlite3.connect(path) as con:
            con.executemany(
                "INSERT INTO PS_PAYMENT_TBL "
                "(BANK_SETID, PYMNT_ID, VENDOR_ID, PYMNT_DT, PYMNT_AMT, "
                "CURRENCY_CD, PYMNT_STATUS) VALUES (?,?,?,?,?,?,?)",
                payment_headers,
            )

        cfg = load_config(str(ROOT / "config.yaml"))
        cfg.db.sqlite_path = str(path)
        packs = ModulePacks(TBEngine(Database(cfg), cfg))
        self.addCleanup(packs.db.close)
        return packs.duplicate_payments(
            business_unit=BU, months=12, as_of_date=AS_OF)

    def _candidate(self, out):
        matches = [row for row in out["exact_invoice_duplicates"]
                   if row["invoice_id"] == INVOICE]
        self.assertEqual(len(matches), 1, "the fixture candidate disappeared")
        return matches[0]

    def _confirmed(self, out):
        return [row for row in out["confirmed_duplicate_payments"]
                if row["invoice_id"] == INVOICE]

    def test_missing_payment_headers_stay_a_voucher_candidate_only(self):
        out = self._result()

        # Existing clients and cards still consume these three keys.
        self.assertIn("exact_invoice_duplicates", out)
        self.assertIn("same_amount_pairs", out)
        self.assertIn("exact_total", out)
        self.assertEqual(out["exact_total"], 15_600.0)
        self.assertEqual(out["duplicate_voucher_candidates"],
                         out["exact_invoice_duplicates"])

        candidate = self._candidate(out)
        self.assertEqual(candidate["finding_type"],
                         "duplicate_voucher_candidate")
        evidence = candidate["payment_evidence"]
        self.assertTrue(evidence["evaluated"])
        self.assertFalse(evidence["confirmed"])
        self.assertEqual(evidence["payment_count"], 0)
        self.assertEqual(evidence["payment_ids"], [])
        self.assertEqual(evidence["paid_total"], 0.0)
        self.assertEqual(self._confirmed(out), [])
        self.assertEqual(out["confirmed_duplicate_payment_total"], 0.0)

        # A reader must not have to infer this distinction from empty rows.
        population = " ".join(str(out.get(key, "")) for key in
                              ("note", "population", "population_note"))
        population = re.sub(r"[_-]+", " ", population.lower())
        self.assertRegex(
            population,
            r"duplicate voucher.*not proof.*paid twice",
        )

    def test_a_void_header_does_not_complete_the_payment_pair(self):
        out = self._result([
            ("SHARE", "PMT80001", "V1001", "2026-03-20", 7_800.0,
             "USD", "P"),
            ("SHARE", "PMT80002", "V1001", "2026-03-21", 7_800.0,
             "USD", "V"),
        ])

        evidence = self._candidate(out)["payment_evidence"]
        self.assertTrue(evidence["evaluated"])
        self.assertFalse(evidence["confirmed"])
        self.assertEqual(evidence["payment_count"], 1)
        self.assertEqual(evidence["payment_ids"], ["PMT80001"])
        self.assertEqual(evidence["paid_total"], 7_800.0)
        self.assertEqual(self._confirmed(out), [])
        self.assertEqual(out["confirmed_duplicate_payment_total"], 0.0)

    def test_two_distinct_nonvoid_headers_confirm_the_duplicate_payment(self):
        out = self._result([
            ("SHARE", "PMT80001", "V1001", "2026-03-20", 7_800.0,
             "USD", "P"),
            ("SHARE", "PMT80002", "V1001", "2026-03-21", 7_800.0,
             "USD", "P"),
        ])

        candidate = self._candidate(out)
        evidence = candidate["payment_evidence"]
        self.assertTrue(evidence["evaluated"])
        self.assertTrue(evidence["confirmed"])
        self.assertEqual(evidence["payment_count"], 2)
        self.assertEqual(evidence["payment_ids"],
                         ["PMT80001", "PMT80002"])
        self.assertEqual(evidence["paid_total"], 15_600.0)
        self.assertEqual(evidence["duplicate_exposure"], 7_800.0)
        confirmed = self._confirmed(out)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["finding_type"],
                         "confirmed_duplicate_payment")
        self.assertTrue(confirmed[0]["payment_evidence"]["confirmed"])
        self.assertEqual(out["confirmed_duplicate_payment_total"], 15_600.0)
        self.assertEqual(out["confirmed_duplicate_exposure"], 7_800.0)


if __name__ == "__main__":
    unittest.main()
