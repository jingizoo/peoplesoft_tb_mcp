"""Speed lock-in: the perf regression gate.

"Speed is good now — don't compromise it." This file makes that mechanical.
Each budget below is the EXACT number of database round trips a warm hot-path
call makes today. A failure here does not mean the code is wrong — it means a
change added (or removed) a round trip on a path users feel on a WAN Oracle
connection, where every query is a full network round trip. If the change is
deliberate, update the budget in the same PR and say why in the PR body.

Query counts are structural, not stopwatch-based, so this gate is exact and
never flaky in CI. The one wall-clock test uses a deliberately generous
ceiling: it exists to catch accidental quadratic blowups in the number guard,
not to measure speed.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import (misattributed_figures, payload_numbers,
                         ungrounded_figures)  # noqa: E402

BUDGET_MSG = (
    "\n\nThis is the speed lock-in gate: a warm {name} call now issues "
    "{got} queries where the budget is {budget}. Every extra query is a "
    "full WAN round trip on the real Oracle box. If the new round trip is "
    "deliberate, update the budget in this same PR and justify it there."
)


class _Meter:
    def __init__(self, db: Database):
        self.db = db
        self.calls: list[str] = []
        self._orig = db.query

    def count(self, fn) -> int:
        fn()  # warm-up pass fills every cache the path uses
        self.calls.clear()
        self.db.query = self._spy
        try:
            fn()
        finally:
            self.db.query = self._orig
        return len(self.calls)

    def _spy(self, sql, params=None, **kw):
        self.calls.append(sql)
        return self._orig(sql, params, **kw)


class QueryBudgetTests(unittest.TestCase):
    """Exact warm-call round-trip budgets for the hot tools."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pstb-perf-"))
        dst = cls.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)
        cls.ar = ARBilling(cls.engine)
        cls.meter = _Meter(cls.db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _assert_budget(self, name: str, budget: int, fn) -> None:
        got = self.meter.count(fn)
        self.assertEqual(got, budget, BUDGET_MSG.format(
            name=name, got=got, budget=budget))

    def test_trial_balance_budget(self) -> None:
        self._assert_budget("get_trial_balance", 2,
                            lambda: self.engine.trial_balance())

    def test_account_balance_budget(self) -> None:
        self._assert_budget("get_account_balance", 3,
                            lambda: self.engine.account_balance("1100"))

    def test_ar_aging_budget(self) -> None:
        self._assert_budget("get_ar_aging", 4, lambda: self.ar.aging())

    def test_cross_bu_top_billing_budget(self) -> None:
        self._assert_budget(
            "get_top_billing_customers (ALL)", 1,
            lambda: self.ar.top_billing_customers(
                business_unit="ALL", display_currency="USD"))

    def test_integrity_check_budget(self) -> None:
        self._assert_budget("tb_integrity_check", 8,
                            lambda: self.engine.tb_integrity_check())

    def test_next_wave_tool_budgets(self) -> None:
        # The review that added these found the wave had shipped with no
        # budgets at all — the exact drift the speed gate exists to stop.
        from pstb.ar import ARBilling
        from pstb.modules import ModulePacks
        ar = ARBilling(self.engine)
        modules = ModulePacks(self.engine)
        for name, budget, fn in [
            ("get_invoice_lifecycle", 4,
             lambda: ar.invoice_lifecycle(business_unit="US001",
                                          as_of_date="2026-08-06")),
            ("get_dso_trend", 2,
             lambda: ar.dso_trend(business_unit="US001")),
            ("get_cash_outlook", 2,
             lambda: ar.cash_outlook(business_unit="US001",
                                     as_of_date="2026-08-06")),
            ("get_vendor_intelligence", 2,
             lambda: modules.vendor_intelligence(business_unit="US001",
                                                 as_of_date="2026-08-06")),
            ("get_duplicate_payments", 3,
             lambda: modules.duplicate_payments(business_unit="US001",
                                                as_of_date="2026-08-06")),
            ("get_invoice_totals", 1,
             lambda: ar.invoice_totals(business_unit="US001")),
            # Two entries, because the branches genuinely differ and one
            # cannot cover both. Splitting by a chartfield is FREE — extras
            # only widen the SELECT and GROUP BY of a fetch that happens
            # anyway — so a bridge costs exactly what compare_trial_balance
            # costs. The prior-year path pays one more for the calendar's
            # last regular period, and `and` short-circuits so the same-year
            # path never does. Reorder that condition and this fails.
            ("explain_balance_change (same-year, chartfield)", 2,
             lambda: self.engine.explain_balance_change(
                 account="6000-6999", by="DEPTID", business_unit="US001",
                 fiscal_year=2026, period=6, vs_period=5)),
            ("explain_balance_change (prior-year)", 3,
             lambda: self.engine.explain_balance_change(
                 account="1000-1999", by="ACCOUNT", business_unit="US001",
                 fiscal_year=2026, period=6, vs_fiscal_year=2025,
                 vs_period=12)),
        ]:
            self._assert_budget(name, budget, fn)

    def test_warm_scope_subroutines_are_free(self) -> None:
        # The subroutines every tool leans on must serve from cache when
        # warm: a per-call probe here multiplies across every tool call.
        for name, fn in [
            ("last_posted_period", lambda: self.engine.last_posted_period()),
            ("resolve_ledger_for",
             lambda: self.engine.resolve_ledger_for("US001")),
            ("base_currency_for",
             lambda: self.engine.base_currency_for("US001")),
        ]:
            self._assert_budget(name + " (warm)", 0, fn)


class GuardCeilingTests(unittest.TestCase):
    """The number guard runs on EVERY answer. A generous wall-clock ceiling
    catches an accidental O(figures x payload) blowup without ever flaking
    on a slow CI runner."""

    def test_grounding_large_payloads_stays_linear(self) -> None:
        payloads = [json.dumps({"rows": [
            {"acct": f"{a}", "amount": a * 1013.17, "bal": a * -7.5}
            for a in range(i * 500, i * 500 + 500)]}) for i in range(8)]
        answer = " ".join(f"{a * 1013.17:,.2f}" for a in range(40)) + \
            " plus an invented 987,654,321.99"
        t0 = time.perf_counter()
        found = payload_numbers(payloads)
        invented = ungrounded_figures(answer, payloads)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0,
                        f"guard took {elapsed:.2f}s on 4k rows — the number "
                        "guard runs on every answer; something went "
                        "super-linear")
        self.assertTrue(found)
        self.assertTrue(any("987" in f for f in invented),
                        "the ceiling test must still exercise a real "
                        "detection, not just timing")

    def test_attribution_adds_one_walk_not_a_second_pass_per_figure(self):
        """The attribution guard also runs on every answer, and it reads
        the answer sentence by sentence. Naive nesting — every figure
        rescanned against every payload — would be O(figures x rows) and
        would only show up on a real trial balance."""
        payloads = [("get_trial_balance", json.dumps({"rows": [
            {"acct": f"{a}", "amount": a * 1013.17}
            for a in range(i * 500, i * 500 + 500)]})) for i in range(8)]
        answer = ". ".join(
            f"The general ledger shows {a * 1013.17:,.2f}"
            for a in range(40)) + "."
        t0 = time.perf_counter()
        findings = misattributed_figures(answer, payloads, "data")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0,
                        f"attribution took {elapsed:.2f}s on 4k rows")
        self.assertEqual(findings, [],
                        "correctly attributed ledger figures must stay "
                        "silent — a timing test that flags everything is "
                        "not measuring the real path")


if __name__ == "__main__":
    unittest.main()
