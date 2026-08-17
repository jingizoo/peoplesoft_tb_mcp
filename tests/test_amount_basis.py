"""The currency and amount-basis contract, stated the same way everywhere.

P0 #3 of the external review. The engine layer was already right —
queries.basis_clause has applied the correct predicates for a long time,
including the deliberate decision NOT to filter CURRENCY_CD on the base
basis. Two things were not right:

1. The trial balance reported ``currency_filter: "base currency only"``.
   No such filter is applied, and applying one would be a bug: a journal
   entered in EUR posts a CURRENCY_CD='EUR' row whose POSTED_TOTAL_AMT is
   already the base amount, so filtering to the base code drops real
   activity while the TB still balances (both sides of the FX journal go).
   The label described the opposite of the query.

2. The transaction basis was implemented and unreachable. No tool took
   amount_basis, so "what did we actually spend in euros" could not be
   asked, and the only way to get a per-currency answer was currency=
   "detail", which still reported base amounts.

The fixture below is the shape that catches both: one account carrying a
USD row and a EUR-entered row.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pstb.config import load_config
from pstb.db import Database
from pstb.engine import EngineError, TBEngine, normalize_amount_basis
from pstb.guards import tool_result_status

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data" / "ps_sample.db"

ACCOUNT = "6100"
EUR_BASE = 25000.0        # what the EUR journal is worth in base currency
EUR_TRAN = 21000.0        # what it was entered as


class AmountBasisTests(unittest.TestCase):
    """One account, two currencies, and the numbers each basis must report."""

    @classmethod
    def setUpClass(cls) -> None:
        if not SAMPLE.exists():
            raise unittest.SkipTest("run scripts/seed_sample_data.py first")
        cls._dir = tempfile.TemporaryDirectory()
        root = Path(cls._dir.name)
        db_path = root / "fx.db"
        shutil.copy(SAMPLE, db_path)

        (root / "config.yaml").write_text(
            "db:\n  backend: sqlite\n"
            f"  sqlite_path: {db_path}\n"
            "defaults:\n  business_unit: US001\n  ledger: ACTUALS\n"
            "  base_currency: USD\n")
        cls.cfg = load_config(str(root / "config.yaml"))

        # Measure the untouched account BEFORE adding the EUR row. The
        # ending balance is cumulative through the period, not the period's
        # own row amount, so it has to be observed rather than assumed.
        pristine = TBEngine(Database(cls.cfg), cls.cfg)
        baseline = pristine.trial_balance(
            business_unit="US001", fiscal_year=2026, period=6, account=ACCOUNT)
        usd_row = next((r for r in baseline["rows"]
                        if r["account"] == ACCOUNT), None)
        if usd_row is None:
            raise unittest.SkipTest(f"sample has no {ACCOUNT} activity")
        cls.usd_ending = float(usd_row["ending"])
        cls.usd_activity = float(usd_row["period_activity"])

        con = sqlite3.connect(db_path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(PS_LEDGER)")]
        base_row = con.execute(
            "SELECT * FROM PS_LEDGER WHERE ACCOUNT = ? AND FISCAL_YEAR = 2026"
            "  AND ACCOUNTING_PERIOD = 6 LIMIT 1", (ACCOUNT,)).fetchone()
        if base_row is None:
            con.close()
            raise unittest.SkipTest(f"sample has no {ACCOUNT} period-6 row")
        row = dict(zip(cols, base_row))
        cls.usd_tran_activity = float(row.get("POSTED_TRAN_AMT")
                                      or row["POSTED_TOTAL_AMT"])
        row["CURRENCY_CD"] = "EUR"
        row["POSTED_TOTAL_AMT"] = EUR_BASE
        if "POSTED_TRAN_AMT" in row:
            row["POSTED_TRAN_AMT"] = EUR_TRAN
        con.execute(
            f"INSERT INTO PS_LEDGER ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        con.commit()
        con.close()

        cls.engine = TBEngine(Database(cls.cfg), cls.cfg)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._dir.cleanup()

    def _tb(self, **kw):
        return self.engine.trial_balance(
            business_unit="US001", fiscal_year=2026, period=6,
            account=ACCOUNT, **kw)

    # ------------------------------------------------------------- base
    def test_base_includes_foreign_entered_activity(self):
        """The EUR row's BASE amount belongs in the base total."""
        tb = self._tb()
        row = next(r for r in tb["rows"] if r["account"] == ACCOUNT)
        self.assertAlmostEqual(row["ending"], self.usd_ending + EUR_BASE, 2)

    def test_base_no_longer_claims_a_filter_it_does_not_apply(self):
        tb = self._tb()
        self.assertNotIn(
            "currency_filter", tb,
            "the top-level currency_filter said 'base currency only' while "
            "basis_clause applies no currency predicate at all")
        note = tb["currency"]
        self.assertEqual(note["amount_basis"], "base")
        self.assertEqual(note["amount_column"], "POSTED_TOTAL_AMT")
        self.assertEqual(note["base_currency"], "USD")
        self.assertTrue(note["includes_foreign_entered_activity"])
        self.assertTrue(note["totals_are_summable"])
        self.assertIn("NOT filtered", note["reads"])

    def test_base_reports_one_summable_total(self):
        tb = self._tb()
        self.assertIsNotNone(tb["totals"]["ending"])
        self.assertNotIn("by_currency", tb["totals"],
                         "currency is not a dimension of a base-basis result; "
                         "an 'UNSPECIFIED' bucket would invent one")

    # ------------------------------------------------ transaction basis
    def test_transaction_keys_by_currency_without_being_asked(self):
        tb = self._tb(amount_basis="transaction")
        self.assertIn("CURRENCY_CD", tb["group_by"])
        by = {r["currency_cd"]: r["ending"] for r in tb["rows"]}
        # The EUR row is period-6 activity only, so its ending equals it.
        self.assertAlmostEqual(by["EUR"], EUR_TRAN, 2)
        self.assertAlmostEqual(by["USD"], self.usd_ending, 2)

    def test_transaction_withholds_the_meaningless_grand_total(self):
        tb = self._tb(amount_basis="transaction")
        totals = tb["totals"]
        for field in ("beginning", "period_activity", "ending",
                      "ending_dr", "ending_cr", "in_balance"):
            self.assertIsNone(totals[field], field)
        self.assertIn("cannot be added together", totals["withheld_reason"])
        self.assertAlmostEqual(totals["by_currency"]["EUR"]["ending"],
                               EUR_TRAN, 2)
        self.assertAlmostEqual(totals["by_currency"]["USD"]["ending"],
                               self.usd_ending, 2)

    def test_a_single_currency_transaction_result_stays_summable(self):
        """Withholding is about MIXING currencies, not about the basis."""
        tb = self._tb(amount_basis="transaction", currency="USD")
        self.assertEqual(tb["currency"]["currencies_present"], ["USD"])
        self.assertIsNotNone(tb["totals"]["ending"])

    def test_the_two_bases_do_not_report_the_same_number(self):
        """If they did, one of them would be wrong."""
        base = self._tb()["totals"]["ending"]
        tran = self._tb(amount_basis="transaction")["totals"]["by_currency"]
        self.assertNotAlmostEqual(base, sum(v["ending"] for v in tran.values()))

    # ------------------------------------------------------------ input
    def test_an_unknown_basis_is_refused_not_quietly_downgraded(self):
        with self.assertRaises(EngineError) as caught:
            self._tb(amount_basis="reporting_currency_v2")
        self.assertIn("amount_basis", str(caught.exception))
        self.assertIn("transaction", str(caught.exception))

    def test_accepted_spellings(self):
        for value, expected in (("", "base"), ("default", "base"),
                                ("BASE", "base"), ("Transaction", "transaction"),
                                ("tran", "transaction"), ("entered", "transaction"),
                                ("reporting", "base")):
            with self.subTest(value=value):
                self.assertEqual(normalize_amount_basis(value), expected)

    # ------------------------------------------------------------ guard
    def test_the_gate_refuses_a_summed_multi_currency_total(self):
        """Belt and braces: the engine withholds it, the guard catches it."""
        tampered = self._tb(amount_basis="transaction")
        tampered["totals"]["ending"] = 148680.0        # what summing would give
        ok, reason = tool_result_status(
            "get_trial_balance", json.dumps(tampered, default=str))
        self.assertFalse(ok)
        self.assertIn("cannot be added", reason)

    def test_an_honest_transaction_payload_is_still_evidence(self):
        ok, reason = tool_result_status(
            "get_trial_balance",
            json.dumps(self._tb(amount_basis="transaction"), default=str))
        self.assertTrue(ok, reason)

    def test_a_base_payload_is_still_evidence(self):
        ok, reason = tool_result_status(
            "get_trial_balance", json.dumps(self._tb(), default=str))
        self.assertTrue(ok, reason)


class ToolSurfaceTests(unittest.TestCase):
    """A basis the engine supports but no tool exposes is not a capability."""

    def test_the_tool_takes_amount_basis_and_says_what_it_means(self):
        import inspect

        from pstb import server
        sig = inspect.signature(server.get_trial_balance)
        self.assertIn("amount_basis", sig.parameters)
        self.assertEqual(sig.parameters["amount_basis"].default, "base")
        doc = server.get_trial_balance.__doc__ or ""
        self.assertIn("POSTED_TRAN_AMT", doc)
        self.assertIn("cannot be added", doc)

    def test_the_prompt_routes_to_it(self):
        import pstb.client.prompt as prompt_mod
        source = Path(prompt_mod.__file__).read_text()
        self.assertIn('amount_basis="transaction"', source,
                      "nothing tells the model the basis exists, so nothing "
                      "will ever fire it")
        self.assertIn("never add them", source,
                      "the routing note must carry the rule, not just the "
                      "argument name")


if __name__ == "__main__":
    unittest.main()
