from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402


class RecordingDatabase(Database):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.statements: list[str] = []

    def query(self, sql: str, params=None, max_rows=None):
        self.statements.append(sql)
        return super().query(sql, params, max_rows)


class ScopeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "scope.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE PS_LEDGER (
                    BUSINESS_UNIT TEXT,
                    LEDGER TEXT,
                    FISCAL_YEAR INTEGER,
                    ACCOUNTING_PERIOD INTEGER,
                    BASE_CURRENCY TEXT
                );
                CREATE INDEX IX_LEDGER_SCOPE
                    ON PS_LEDGER (
                        BUSINESS_UNIT, LEDGER, FISCAL_YEAR, ACCOUNTING_PERIOD
                    );
                """
            )
            conn.executemany(
                "INSERT INTO PS_LEDGER VALUES (?, ?, ?, ?, ?)",
                [
                    ("10000", "ACTUALS", 2025, 12, "USD"),
                    ("10000", "ACTUALS", 2026, 6, "USD"),
                    # Adjustment-only data advances the available-year range,
                    # but must not become the latest regular posted period.
                    ("10000", "ACTUALS", 2027, 998, "USD"),
                    ("10000", "BUDGET", 2026, 12, "USD"),
                    ("CA001", "ACTUALS", 2025, 12, "CAD"),
                    ("CA001", "ACTUALS", 2026, 3, "CAD"),
                    # A duplicate balance row must not duplicate the scope.
                    ("CA001", "ACTUALS", 2026, 3, "CAD"),
                ],
            )

        self.cfg = Config.sample(self.tempdir.name)
        self.cfg.db.sqlite_path = str(self.db_path)
        self.cfg.defaults.business_unit = "ZZ999"
        self.cfg.defaults.ledger = "NOLEDGER"
        self.db = RecordingDatabase(self.cfg)
        self.addCleanup(self.db.close)
        self.engine = TBEngine(self.db, self.cfg)

    def _add_setup_metadata(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE PS_BUS_UNIT_TBL_GL "
                "(BUSINESS_UNIT TEXT, DESCR TEXT, BASE_CURRENCY TEXT)"
            )
            conn.executemany(
                "INSERT INTO PS_BUS_UNIT_TBL_GL VALUES (?, ?, ?)",
                [
                    ("10000", "US Operations", "USD"),
                    ("CA001", "Canada Operations", "CAD"),
                    ("ORPHAN", "Setup only, no ledger data", "EUR"),
                ],
            )

    def test_catalog_survives_missing_setup_grant_and_returns_every_scope(self) -> None:
        result = self.engine.list_financial_scopes(include_activity=True)

        by_bu = {scope["business_unit"]: scope for scope in result["scopes"]}
        self.assertEqual(set(by_bu), {"10000", "CA001"})
        self.assertIsNone(by_bu["10000"]["descr"])
        self.assertEqual(by_bu["10000"]["base_currency"], "USD")
        self.assertEqual(by_bu["CA001"]["base_currency"], "CAD")

        ledgers = {
            row["ledger"]: row for row in by_bu["10000"]["ledgers"]
        }
        self.assertEqual(set(ledgers), {"ACTUALS", "BUDGET"})
        self.assertEqual(ledgers["ACTUALS"]["fiscal_years"], [2025, 2027])
        self.assertEqual(
            ledgers["ACTUALS"]["last_posted"],
            {"fiscal_year": 2026, "period": 6},
        )
        self.assertIsNone(ledgers["ACTUALS"]["row_count"])

        # Invalid configured defaults are never returned as the suggested
        # catalog choice when accessible ledger-derived values exist.
        self.assertEqual(
            result["default"],
            {"business_unit": "10000", "ledger": "ACTUALS"},
        )
        self.assertFalse(result["truncated"])

        discovery_sql = "\n".join(self.db.statements)
        self.assertIsNone(
            re.search(r"\bCOUNT\s*\(", discovery_sql, re.IGNORECASE),
            discovery_sql,
        )

    def test_fast_catalog_defers_per_scope_activity_queries(self) -> None:
        result = self.engine.list_financial_scopes(include_activity=False)
        ledgers = result["scopes"][0]["ledgers"]
        self.assertTrue(all(row["fiscal_years"] == [] for row in ledgers))
        self.assertTrue(all(row["last_posted"] is None for row in ledgers))
        discovery_sql = "\n".join(self.db.statements)
        self.assertNotIn("MIN(FISCAL_YEAR)", discovery_sql)

    def test_setup_table_only_enriches_ledger_derived_business_units(self) -> None:
        self._add_setup_metadata()

        result = self.engine.list_business_units()
        by_bu = {
            row["business_unit"]: row for row in result["business_units"]
        }

        self.assertEqual(set(by_bu), {"10000", "CA001"})
        self.assertEqual(by_bu["10000"]["descr"], "US Operations")
        self.assertEqual(by_bu["CA001"]["descr"], "Canada Operations")
        self.assertNotIn("ORPHAN", by_bu)

    def test_base_currency_falls_back_to_ledger_before_config(self) -> None:
        # PS_BUS_UNIT_TBL_GL deliberately does not exist in this fixture. The
        # global configured currency is USD, but CA001's ledger is CAD.
        self.assertEqual(self.cfg.defaults.base_currency, "USD")
        self.assertEqual(self.engine.base_currency_for("CA001"), "CAD")

    def test_effective_defaults_refresh_after_catalog_change(self) -> None:
        first = self.engine.effective_defaults()
        self.assertEqual(first["business_unit"], "10000")
        self.assertEqual(first["ledger"], "ACTUALS")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO PS_LEDGER VALUES (?, ?, ?, ?, ?)",
                ("ZZ999", "ACTUALS", 2026, 7, "USD"),
            )

        # A short TTL protects the database from repeated catalog discovery.
        self.assertEqual(
            self.engine.effective_defaults()["business_unit"], "10000"
        )

        # Services may also invalidate immediately after a grant/onboarding
        # event; the next call must re-read PS_LEDGER rather than setup data.
        self.engine.invalidate_scope_cache()
        refreshed = self.engine.effective_defaults()
        self.assertEqual(refreshed["business_unit"], "ZZ999")
        self.assertEqual(refreshed["ledger"], "ACTUALS")

    def test_explicit_bu_with_blank_ledger_uses_a_ledger_valid_for_that_bu(self) -> None:
        self.cfg.defaults.business_unit = "10000"
        self.cfg.defaults.ledger = "BUDGET"
        self.engine.invalidate_scope_cache()

        # BUDGET is valid for the default BU, but CA001 only has ACTUALS.
        self.assertEqual(self.engine.resolve_ledger_for("CA001"), "ACTUALS")
        self.assertEqual(
            self.engine._defaults("CA001", 2026, 3, "")[-1],
            "ACTUALS",
        )
        self.assertEqual(
            self.engine.last_posted_period("CA001", ""),
            (2026, 3),
        )

    def test_thirteen_period_calendar_uses_period_thirteen_as_latest(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO PS_LEDGER VALUES (?, ?, ?, ?, ?)",
                ("CA001", "ACTUALS", 2026, 13, "CAD"),
            )

        self.assertEqual(
            self.engine.last_posted_period("CA001", "ACTUALS"),
            (2026, 13),
        )
        # The ledger fallback keeps account-balance/AR reconciliation on P13
        # even when the fiscal-calendar setup table is not granted.
        self.assertEqual(
            self.engine._max_regular_period(2026, "CA001", "ACTUALS"),
            13,
        )

    def test_ar_reconciliation_uses_period_thirteen_and_bu_valid_ledger(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO PS_LEDGER VALUES (?, ?, ?, ?, ?)",
                ("CA001", "ACTUALS", 2026, 13, "CAD"),
            )
        self.cfg.defaults.business_unit = "10000"
        self.cfg.defaults.ledger = "BUDGET"
        self.engine.invalidate_scope_cache()

        self.assertEqual(
            ARBilling(self.engine)._latest_posted_period("CA001"),
            (2026, 13, None),
        )


if __name__ == "__main__":
    unittest.main()
