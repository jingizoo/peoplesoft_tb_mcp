"""Answering about ONE business unit must not query every other one.

From the floor: "we saw many queuing up in ledger". /api/scope — which the
scope bar calls whenever the business unit or ledger changes, and again when
a scope is chosen from the chooser — wanted one thing: the fiscal years that
hold data for the pair the user just picked. It got them by building the
ACTIVITY catalog for the whole installation and filtering down to one row.

Activity costs two MIN/MAX queries against PS_LEDGER per pair, which the
engine itself documents as the slow query class on a real instance. At a few
hundred BU/ledger pairs that is several hundred ledger queries to answer a
question about one of them — issued on every scope change, all of it
competing for the same eight-session Oracle pool, with every dashboard and
chat query queued behind it.

These tests count queries rather than timing them, because the cost is the
ROUND TRIPS and a fast sample database hides that completely.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}


class LedgerFanoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.gapp = gapp
        cls.client = TestClient(gapp.app, **LOOP)

    def _count_queries(self, call):
        """Every SQL statement the call issues, in order."""
        seen: list = []
        original = self.gapp.db.query

        def spy(sql, params=None, max_rows=None):
            seen.append(" ".join(str(sql).split()))
            return original(sql, params, max_rows)

        self.gapp.db.query = spy
        try:
            call()
        finally:
            self.gapp.db.query = original
        return seen

    def test_one_scope_lookup_does_not_scan_the_whole_catalog(self) -> None:
        body = None

        def call():
            nonlocal body
            body = self.client.get(
                "/api/scope?business_unit=US001&ledger=ACTUALS").json()

        queries = self._count_queries(call)
        self.assertEqual(body["business_unit"], "US001")
        # The endpoint legitimately needs a handful: the BU's ledgers, the
        # last posted period, and this pair's year bounds. It must not scale
        # with the number of OTHER business units in the installation.
        self.assertLess(len(queries), 12, "\n".join(queries))

    def test_it_still_returns_the_years_that_hold_data(self) -> None:
        # Cheaper is only a fix if the answer survives: the year dropdown and
        # the fiscal-year range check in _validated_scope both read this.
        body = self.client.get(
            "/api/scope?business_unit=US001&ledger=ACTUALS").json()
        years = body["fiscal_years"]
        self.assertTrue(years, "the scope editor would offer no year at all")
        self.assertTrue(all(isinstance(y, int) for y in years))
        self.assertEqual(years, sorted(years, reverse=True))
        self.assertIn(body["fiscal_year"], years)

    def test_the_years_match_what_the_full_catalog_reports(self) -> None:
        # The cheap path must agree with the expensive one it replaced,
        # or the scope editor quietly starts offering different years.
        cheap = self.client.get(
            "/api/scope?business_unit=US001&ledger=ACTUALS").json()
        bounds, _ = self.gapp.engine._scope_period_details("US001", "ACTUALS")
        expected = set(int(y) for y in bounds) | {cheap["fiscal_year"]}
        self.assertEqual(set(cheap["fiscal_years"]), expected)

    def test_no_gui_route_builds_the_activity_catalog(self) -> None:
        # include_activity=True is the per-pair MIN/MAX walk. Nothing serving
        # a page should reach for it; the MCP tool still exposes it for a
        # question that explicitly asks.
        source = (ROOT / "pstb" / "gui" / "app.py").read_text()
        self.assertNotIn("include_activity=True", source)


if __name__ == "__main__":
    unittest.main()
