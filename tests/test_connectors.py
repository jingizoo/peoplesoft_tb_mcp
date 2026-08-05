"""Connector framework + Coupa pack, entirely on recorded fixtures.

The rules under test are the ones that keep external sources as
trustworthy as the ledger: the transport physically cannot write, secrets
never surface in errors, caching keeps warm paths off the network, mixed
currencies never sum, and the AP tie-out reports matches, amount breaks
and misses with its match basis disclosed. Fixture dates are pinned, so
every test passes `today` explicitly — a test that depends on the wall
clock rots silently.
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.connectors import (ConnectorError, FIXTURE_DIR,  # noqa: E402
                             FixtureTransport, RestConnector)
from pstb.connectors.coupa import CoupaConnector, _norm_name  # noqa: E402
from pstb.db import Database  # noqa: E402

TODAY = dt.date(2026, 8, 5)


def _coupa() -> CoupaConnector:
    return CoupaConnector(
        transport=FixtureTransport(FIXTURE_DIR / "coupa.json"))


class FrameworkTests(unittest.TestCase):
    def test_transport_refuses_anything_but_get(self) -> None:
        c = _coupa()
        with self.assertRaises(ConnectorError) as ctx:
            c._request("POST", "https://x.example/api/invoices")
        self.assertIn("read-only", str(ctx.exception))

    def test_warm_calls_never_touch_the_network(self) -> None:
        c = _coupa()
        c.invoices(today=TODAY)
        before = len(c.transport.calls)
        c.invoices(today=TODAY)
        self.assertEqual(len(c.transport.calls), before,
                         "second warm call must serve from cache")

    def test_missing_fixture_names_the_remedy(self) -> None:
        c = _coupa()
        with self.assertRaises(ConnectorError) as ctx:
            c.get("/api/does_not_exist")
        self.assertIn("Record the live response", str(ctx.exception))

    def test_credential_errors_never_echo_secrets(self) -> None:
        secret = "sup3r-s3cret-value"
        c = RestConnector("https://x.example", api_key=secret,
                          transport=lambda m, u, h, b=None: (401, ""))
        c.name = "testsrc"
        with self.assertRaises(ConnectorError) as ctx:
            c.get("/api/x")
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("credentials", str(ctx.exception))

    def test_health_reports_fixture_mode_honestly(self) -> None:
        h = _coupa().health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["mode"], "fixtures")
        self.assertIn("COUPA_BASE_URL", h["note"])


class CoupaViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _coupa()

    def test_invoices_window_filters_and_totals_per_currency(self) -> None:
        out = self.c.invoices(days=30, today=TODAY)
        numbers = [i["number"] for i in out["invoices"]]
        self.assertIn("INV-9003", numbers)
        self.assertNotIn("INV-9002", numbers, "outside the 30-day window")
        self.assertEqual(set(out["totals_by_currency"]), {"USD"})

    def test_status_filter_is_exact(self) -> None:
        out = self.c.invoices(status="paid", days=60, today=TODAY)
        self.assertEqual([i["number"] for i in out["invoices"]],
                         ["INV-9001"])

    def test_stuck_approvals_respects_threshold_and_orders(self) -> None:
        out = self.c.stuck_approvals(days_pending=3, today=TODAY)
        self.assertEqual([r["number"] for r in out["stuck"]], ["CINV-2101"],
                         "2 days pending must not count as stuck at 3")
        self.assertEqual(out["stuck"][0]["days_pending"], 12)
        self.assertEqual(out["stuck"][0]["approver"], "D. Okafor")

    def test_rni_excludes_settled_and_negative_lines(self) -> None:
        out = self.c.received_not_invoiced()
        pos = {(r["po"], r["rni_amt"]) for r in out["lines"]}
        self.assertIn(("PO-7001", 3000.0), pos)
        self.assertIn(("PO-7002", 7500.0), pos)
        self.assertNotIn(("PO-7003", 0.0), pos)
        self.assertFalse(any(r["po"] == "PO-7004" for r in out["lines"]),
                         "over-invoiced lines are not accrual candidates")

    def test_rni_totals_never_cross_currencies(self) -> None:
        out = self.c.received_not_invoiced()
        self.assertEqual(out["rni_totals_by_currency"],
                         {"USD": 10500.0, "EUR": 3000.0})

    def test_supplier_name_normalization(self) -> None:
        self.assertEqual(_norm_name("Ridgeline Supply Co."),
                         _norm_name("RIDGELINE SUPPLY"))
        self.assertNotEqual(_norm_name("Cobalt IT"),
                            _norm_name("Meridian Analytics LLC"))


class ApTieTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(Config.sample(ROOT))
        cls.c = _coupa()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_tie_reports_matches_breaks_and_misses(self) -> None:
        out = self.c.ap_tie(self.db, days=90, today=TODAY)
        self.assertTrue(out["evaluated"])
        self.assertFalse(out["ties"])
        self.assertEqual(out["matched"], 3)
        self.assertEqual([b["invoice"] for b in out["amount_breaks"]],
                         ["INV-9002"])
        self.assertEqual(out["amount_breaks"][0]["difference"], 300.0)
        self.assertEqual([m["number"] for m in out["missing_in_ap"]],
                         ["CINV-2088"])
        self.assertIn("invoice number", out["match_basis"])

    def test_every_break_figure_is_in_the_payload(self) -> None:
        # The number guard grounds answers against payload text — the
        # difference must be a figure the tool computed, present verbatim.
        import json
        out = self.c.ap_tie(self.db, days=90, today=TODAY)
        text = json.dumps(out)
        self.assertIn("300.0", text)
        self.assertIn("12750", text)


class ServerRegistrationTests(unittest.TestCase):
    def test_coupa_tools_are_registered(self) -> None:
        import pstb.server as srv
        out = srv.coupa_health()
        self.assertEqual(out.get("mode"), "fixtures")
        tie = srv.coupa_to_ap_tie(days=3650)
        self.assertTrue(tie.get("evaluated"))


if __name__ == "__main__":
    unittest.main()
