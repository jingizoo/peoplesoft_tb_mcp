"""Focused tests for chat-first scope selection and provider isolation."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from pstb.gui import app as gui


CATALOG = {
    "scopes": [
        {
            "business_unit": "10000",
            "descr": "US Operations",
            "base_currency": "USD",
            "ledgers": [
                {
                    "ledger": "ACTUALS",
                    "fiscal_years": [2025, 2026],
                    "last_posted": {"fiscal_year": 2026, "period": 6},
                },
                {
                    "ledger": "BUDGET",
                    "fiscal_years": [2026, 2026],
                    "last_posted": {"fiscal_year": 2026, "period": 12},
                },
            ],
        },
        {
            "business_unit": "CA001",
            "descr": "Canada",
            "base_currency": "CAD",
            "ledgers": [
                {
                    "ledger": "ACTUALS",
                    "fiscal_years": [2025, 2026],
                    "last_posted": {"fiscal_year": 2026, "period": 3},
                }
            ],
        },
    ]
}


class DummyProvider:
    def __init__(self, name: str):
        self.name = name
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1


class ScopeValidationTests(unittest.TestCase):
    def test_multiple_business_units_require_an_explicit_choice(self):
        with self.assertRaises(gui._ScopeRequired) as caught:
            gui._validated_scope({}, CATALOG)
        self.assertEqual(len(caught.exception.options), 3)
        self.assertIn("Choose a business unit", caught.exception.detail)

    def test_selected_scope_is_canonical_and_uses_db_latest_period(self):
        result = gui._validated_scope(
            {"business_unit": "CA001", "ledger": "ACTUALS"}, CATALOG
        )
        self.assertEqual(
            result,
            {
                "business_unit": "CA001",
                "ledger": "ACTUALS",
                "fiscal_year": 2026,
                "period": 3,
                # The selected period resolved to a date, because AR,
                # Billing and AP filter on one. Without it the chip read
                # P3 and the receivables beside it read today.
                "as_of_date": "2026-03-31",
            },
        )

    def test_the_period_reaches_the_tools_that_take_a_DATE(self) -> None:
        from pstb.guards import apply_request_scope
        scope = gui._validated_scope(
            {"business_unit": "CA001", "ledger": "ACTUALS"}, CATALOG)
        for tool in ("get_ar_aging", "get_billing_workbench",
                     "get_customer_financial_360", "get_open_payables"):
            self.assertEqual(
                apply_request_scope(tool, {}, scope).get("as_of_date"),
                "2026-03-31", tool)

    def test_an_explicit_date_still_wins_over_the_chip(self) -> None:
        # Time is a default, not a cage: "what do they owe TODAY" while the
        # chip reads P3 is a legitimate question.
        from pstb.guards import apply_request_scope
        scope = gui._validated_scope(
            {"business_unit": "CA001", "ledger": "ACTUALS"}, CATALOG)
        self.assertEqual(
            apply_request_scope("get_ar_aging", {"as_of_date": "2026-08-01"},
                                scope)["as_of_date"], "2026-08-01")

    def test_a_calendar_that_cannot_answer_costs_only_the_date(self) -> None:
        from unittest.mock import patch
        with patch.object(gui.engine, "list_periods",
                          side_effect=Exception("ORA-00942")):
            scope = gui._validated_scope(
                {"business_unit": "CA001", "ledger": "ACTUALS"}, CATALOG)
        self.assertNotIn("as_of_date", scope)
        self.assertEqual(scope["period"], 3)

    def test_unknown_ledger_and_out_of_range_year_are_rejected(self):
        with self.assertRaises(HTTPException) as ledger_error:
            gui._validated_scope(
                {"business_unit": "CA001", "ledger": "BUDGET"}, CATALOG
            )
        self.assertEqual(ledger_error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as year_error:
            gui._validated_scope(
                {
                    "business_unit": "CA001",
                    "ledger": "ACTUALS",
                    "fiscal_year": 2024,
                    "period": 3,
                },
                CATALOG,
            )
        self.assertIn("outside the data range", year_error.exception.detail)

    def test_thirteen_period_calendar_is_not_rejected_by_chat_boundary(self):
        result = gui._validated_scope(
            {
                "business_unit": "CA001",
                "ledger": "ACTUALS",
                "fiscal_year": 2026,
                "period": 13,
            },
            CATALOG,
        )
        self.assertEqual(result["period"], 13)


class ProviderSessionStoreTests(unittest.TestCase):
    def test_history_is_isolated_by_session_and_validated_scope(self):
        now = [0.0]
        store = gui._ProviderSessionStore(
            ttl_seconds=60, max_entries=10, clock=lambda: now[0]
        )
        made = []

        def factory():
            provider = DummyProvider(f"provider-{len(made)}")
            made.append(provider)
            return provider

        us_key = ("session-one", "gemini", "10000", "ACTUALS", 2026, 6)
        ca_key = ("session-one", "gemini", "CA001", "ACTUALS", 2026, 3)
        other_user_key = (
            "session-two", "gemini", "10000", "ACTUALS", 2026, 6
        )

        first = store.get_or_create(us_key, factory)
        self.assertIs(first, store.get_or_create(us_key, factory))
        self.assertIsNot(first, store.get_or_create(ca_key, factory))
        self.assertIsNot(first, store.get_or_create(other_user_key, factory))
        self.assertEqual(len(made), 3)

        cleared = asyncio.run(store.reset_session("session-one"))
        self.assertEqual(cleared, 2)
        self.assertEqual(len(store), 1)
        self.assertEqual(made[0].reset_count, 1)
        self.assertEqual(made[1].reset_count, 1)
        self.assertEqual(made[2].reset_count, 0)

    def test_store_expires_idle_entries_and_enforces_bound(self):
        now = [0.0]
        store = gui._ProviderSessionStore(
            ttl_seconds=60, max_entries=2, clock=lambda: now[0]
        )
        factory = lambda: DummyProvider("test")
        first = ("session-1", "gemini", "10000", "ACTUALS", 2026, 6)
        second = ("session-2", "gemini", "10000", "ACTUALS", 2026, 6)
        third = ("session-3", "gemini", "10000", "ACTUALS", 2026, 6)
        store.get_or_create(first, factory)
        now[0] = 1.0
        store.get_or_create(second, factory)
        now[0] = 2.0
        store.get_or_create(third, factory)
        self.assertNotIn(first, store._entries)
        self.assertEqual(len(store), 2)

        now[0] = 70.0
        store.get_or_create(first, factory)
        self.assertEqual(len(store), 1)
        self.assertIn(first, store._entries)


class ProviderSessionResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_waits_for_an_active_scoped_turn(self):
        store = gui._ProviderSessionStore(ttl_seconds=60, max_entries=2)
        key = ("session-one", "ollama", "10000", "ACTUALS", 2026, 6)
        provider = DummyProvider("test")
        entry = store.get_or_create(key, lambda: provider)

        await entry.lock.acquire()
        reset_task = asyncio.create_task(store.reset_session("session-one"))
        await asyncio.sleep(0)
        self.assertFalse(reset_task.done())
        self.assertEqual(provider.reset_count, 0)

        entry.lock.release()
        self.assertEqual(await reset_task, 1)
        self.assertEqual(provider.reset_count, 1)


class ChatRoutingTests(unittest.TestCase):
    def test_only_data_and_mixed_questions_require_financial_scope(self):
        self.assertTrue(gui._question_requires_scope("Show the AR aging"))
        self.assertTrue(
            gui._question_requires_scope("Run the income statement")
        )
        self.assertTrue(gui._question_requires_scope("How much did we earn?"))
        self.assertTrue(gui._question_requires_scope("Why did sales fall?"))
        self.assertTrue(gui._question_requires_scope("What's driving profit?"))
        self.assertTrue(gui._question_requires_scope("Who owes us?"))
        self.assertTrue(
            gui._question_requires_scope("Are there unposted journals?")
        )
        self.assertTrue(
            gui._question_requires_scope(
                "Is the suspense balance within policy?"
            )
        )
        self.assertFalse(
            gui._question_requires_scope(
                "What is our capitalization threshold policy?"
            )
        )

    def test_scope_catalog_question_works_before_selection(self):
        payload = {
            "message": "What business units and ledgers exist?",
            "session_id": "session-0001",
            "scope": {},
        }
        with patch.object(gui, "_financial_scope_catalog", return_value=CATALOG):
            result = asyncio.run(gui.chat(payload))
        self.assertFalse(result.get("scope_required", False))
        self.assertEqual(result["tool_calls"][0]["tool"], "list_financial_scopes")
        self.assertEqual(len(result["scope_options"]), 3)

    def test_ledger_balance_question_is_not_misrouted_to_scope_catalog(self):
        self.assertIsNone(gui._SCOPE_DISCOVERY_RE.search("Show ledger balance"))
        self.assertFalse(
            gui._is_scope_catalog_question(
                "Which business units have overdue receivables?"
            )
        )
        self.assertFalse(
            gui._is_scope_catalog_question(
                "Which business units had activity this month?"
            )
        )

    def test_financial_question_returns_inline_scope_options(self):
        payload = {
            "message": "Does the trial balance balance?",
            "session_id": "session-0001",
            "scope": {},
        }
        with patch.object(gui, "_financial_scope_catalog", return_value=CATALOG):
            result = asyncio.run(gui.chat(payload))
        self.assertTrue(result["scope_required"])
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(len(result["scope_options"]), 3)

    def test_chat_requires_browser_session_and_scope_fields(self):
        with self.assertRaises(HTTPException) as session_error:
            asyncio.run(gui.chat({"message": "hello", "scope": {}}))
        self.assertEqual(session_error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as scope_error:
            asyncio.run(
                gui.chat({"message": "hello", "session_id": "session-0001"})
            )
        self.assertEqual(scope_error.exception.status_code, 400)


class ChatResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_catalog_io_does_not_block_the_event_loop(self):
        def slow_catalog():
            time.sleep(0.05)
            return CATALOG

        payload = {
            "message": "What business units and ledgers exist?",
            "session_id": "session-0001",
            "scope": {},
        }
        with patch.object(gui, "_financial_scope_catalog", side_effect=slow_catalog):
            turn = asyncio.create_task(gui.chat(payload))
            await asyncio.sleep(0.005)
            self.assertFalse(turn.done())
            result = await turn
        self.assertEqual(result["tool_calls"][0]["tool"], "list_financial_scopes")


if __name__ == "__main__":
    unittest.main()


class McpSessionDiagnosisTests(unittest.TestCase):
    """'Shared MCP session unavailable' must carry its own diagnosis. The
    stdio exception is TaskGroup noise; the real reason died with the
    subprocess's stderr, so the app re-imports the server capturing it and
    /api/meta tells the page, which shows a banner instead of leaving the
    cause in a terminal nobody watches."""

    def test_import_check_reports_a_clean_import(self):
        from pstb.gui import app as gapp
        detail = gapp._server_import_check()
        self.assertIn("imports cleanly", detail,
                      "on a healthy install the check must say the failure "
                      "is in the handshake, not the server")

    def test_import_check_captures_the_real_stderr(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from pstb.gui import app as gapp
        boom = SimpleNamespace(returncode=1, stdout="", stderr=(
            "Traceback (most recent call last):\n"
            "  ...\n"
            "oracledb.exceptions.DatabaseError: ORA-01017: invalid "
            "username/password; logon denied"))
        with patch("subprocess.run", return_value=boom):
            detail = gapp._server_import_check()
        self.assertIn("ORA-01017", detail,
                      "the diagnosis must surface the database's own error")

    def test_meta_reports_session_state(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
            body = client.get("/api/meta").json()
            self.assertIn("mcp_session", body)
            self.assertIn("shared", body["mcp_session"])
