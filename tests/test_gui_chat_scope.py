"""Focused tests for chat-first scope selection and provider isolation."""
from __future__ import annotations

import asyncio
import dis
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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


class DummyRegistry:
    def names(self):
        return ["default", "p2go"]

    def resolve_name(self, source):
        value = str(source or "").strip()
        return "default" if value in ("", "default", "finance") else value

    def resolve_command(self, command):
        value = str(command or "").strip().lower().lstrip("/")
        if value in ("finance", "ps", "peoplesoft"):
            return "default"
        if value == "p2go":
            return "p2go"
        from pstb.db import DbError
        raise DbError(f"Unknown data workspace /{value}")


class ScopeValidationTests(unittest.TestCase):
    def test_secondary_source_is_a_complete_scope_without_ps_ledger(self):
        with (
            patch.object(gui.engine, "registry", DummyRegistry()),
            patch.object(gui, "_financial_scope_catalog",
                         side_effect=AssertionError("must not query PS_LEDGER")),
        ):
            self.assertEqual(
                gui._validated_scope({"source": "p2go"}),
                {"source": "p2go"},
            )

    def test_unknown_secondary_source_is_rejected_by_registry(self):
        with patch.object(gui.engine, "registry", DummyRegistry()):
            with self.assertRaises(HTTPException) as caught:
                gui._validated_scope({"source": "not-configured"}, CATALOG)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Unknown database source", caught.exception.detail)

    def test_explicit_finance_selection_is_preserved_as_a_hard_lock(self):
        with patch.object(gui.engine, "registry", DummyRegistry()):
            self.assertEqual(
                gui._validated_scope({"source": "default"}, CATALOG),
                {"source": "default"},
            )
            result = gui._validated_scope(
                {"source": "default", "business_unit": "CA001",
                 "ledger": "ACTUALS"}, CATALOG)
        self.assertEqual(result["source"], "default")
        self.assertEqual(result["business_unit"], "CA001")

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


class SourceRouteTests(unittest.TestCase):
    def test_secondary_route_is_the_database_authority(self):
        inner = AsyncMock(return_value={"answer": "ok"})
        payload = {
            "message": "show invoice relationships",
            "session_id": "session-p2go-123",
            "scope": {"source": "p2go"},
        }
        with (patch.object(gui.engine, "registry", DummyRegistry()),
              patch.object(gui, "chat", inner)):
            result = asyncio.run(gui.source_chat("p2go", payload))
        self.assertEqual(result, {"answer": "ok"})
        forwarded = inner.await_args.args[0]
        self.assertEqual(forwarded["scope"], {"source": "p2go"})

    def test_secondary_route_rejects_a_conflicting_body_source(self):
        with patch.object(gui.engine, "registry", DummyRegistry()):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(gui.source_chat("p2go", {
                    "message": "x", "session_id": "session-p2go-123",
                    "scope": {"source": "default"},
                }))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("bound to", caught.exception.detail)

    def test_route_rejects_each_conflicting_scope_alias(self):
        cases = (
            ("finance", {"source": "default", "db": "p2go"}),
            ("p2go", {"source": "p2go", "db": "default"}),
        )
        with patch.object(gui.engine, "registry", DummyRegistry()):
            for command, scope in cases:
                with self.subTest(command=command, scope=scope):
                    with self.assertRaises(HTTPException) as caught:
                        asyncio.run(gui.source_chat(command, {
                            "message": "x",
                            "session_id": f"session-{command}-123",
                            "scope": scope,
                        }))
                    self.assertEqual(caught.exception.status_code, 400)
                    self.assertIn("body scope db", caught.exception.detail)

    def test_secondary_route_rejects_stale_finance_dimensions(self):
        with patch.object(gui.engine, "registry", DummyRegistry()):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(gui.source_chat("p2go", {
                    "message": "x", "session_id": "session-p2go-123",
                    "scope": {"source": "p2go", "ledger": "ACTUALS"},
                }))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("no PeopleSoft financial scope", caught.exception.detail)

    def test_finance_alias_preserves_financial_scope_and_pins_default(self):
        inner = AsyncMock(return_value={"answer": "ok"})
        payload = {
            "message": "trial balance", "session_id": "session-fin-123",
            "scope": {"business_unit": "US001", "ledger": "ACTUALS"},
        }
        with (patch.object(gui.engine, "registry", DummyRegistry()),
              patch.object(gui, "chat", inner)):
            asyncio.run(gui.source_chat("ps", payload))
        scope = inner.await_args.args[0]["scope"]
        self.assertEqual(scope["source"], "default")
        self.assertEqual(scope["business_unit"], "US001")

    def test_unknown_workspace_is_a_404(self):
        with patch.object(gui.engine, "registry", DummyRegistry()):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(gui.source_chat("sap", {
                    "message": "x", "session_id": "session-sap-123",
                }))
        self.assertEqual(caught.exception.status_code, 404)


class ProviderSessionStoreTests(unittest.TestCase):
    def test_secondary_provider_receives_only_the_closed_generic_profile(self):
        from pstb.guards import (
            SOURCE_SILO_CHAT_TOOLS,
            SOURCE_SILO_PROPOSAL_TOOLS,
            SOURCE_SILO_TOOLS,
        )

        offered = [SimpleNamespace(name=name) for name in (
            sorted(SOURCE_SILO_CHAT_TOOLS)
            + ["get_trial_balance", "wiki_lookup", "remember_record_fact",
               "a_future_tool"]
        )]
        p2go = gui._provider_tools_for_scope(offered, {"source": "p2go"})
        self.assertEqual(
            {tool.name for tool in p2go}, SOURCE_SILO_CHAT_TOOLS)
        self.assertEqual(
            SOURCE_SILO_PROPOSAL_TOOLS, {"propose_metadata_meaning"})
        self.assertNotIn(
            "propose_metadata_meaning", SOURCE_SILO_TOOLS,
            "the read/export allowlist must not acquire a local write merely "
            "because chat can submit a pending proposal",
        )
        finance = gui._provider_tools_for_scope(
            offered, {"source": "default", "business_unit": "US001"})
        self.assertEqual(finance, offered)

    def test_history_is_isolated_by_database_source(self):
        finance = gui._provider_key(
            "session-one", "gemini",
            {"business_unit": "10000", "ledger": "ACTUALS",
             "fiscal_year": 2026, "period": 6},
        )
        p2go = gui._provider_key(
            "session-one", "gemini",
            {"source": "p2go", "business_unit": "10000",
             "ledger": "ACTUALS", "fiscal_year": 2026, "period": 6},
        )
        self.assertNotEqual(finance, p2go)
        self.assertEqual(finance[2], "default")
        self.assertEqual(p2go[2], "p2go")

    def test_source_only_context_has_a_stable_provider_key(self):
        key = gui._provider_key(
            "session-one", "gemini", {"source": "p2go"})
        self.assertEqual(
            key,
            ("session-one", "gemini", "p2go", "", "", 0, 0),
        )

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

    def test_finance_database_lock_without_bu_still_requires_financial_scope(self):
        payload = {
            "message": "Does the trial balance balance?",
            "session_id": "session-0001",
            "scope": {"source": "default"},
        }
        with (
            patch.object(gui.engine, "registry", DummyRegistry()),
            patch.object(gui, "_financial_scope_catalog",
                         return_value=CATALOG),
        ):
            result = asyncio.run(gui.chat(payload))
        self.assertTrue(result["scope_required"])
        self.assertEqual(result["tool_calls"], [])
        self.assertIn("business unit and ledger", result["answer"])

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


class EndpointWiringTests(unittest.TestCase):
    """Every GUI handler must reference names the module actually defines.

    /api/vendors called `modules.search_vendors`, and `modules` was never
    bound in pstb/gui/app.py — so browser vendor search raised NameError and
    returned 500 to every caller, with no test noticing because nothing
    exercised the route. The sweep below is the general form: a handler that
    reads an undefined global is dead, and dead is indistinguishable from
    working until somebody clicks it.
    """

    def test_every_handler_global_actually_exists(self):
        """A general sweep, not just the one regression.

        Reads each handler's global references straight out of its code
        object and checks the module can resolve them. co_names also holds
        attribute names, so a miss is only reported when the name is not a
        module global, not a builtin, and not an attribute reached on some
        local — hence the allowlist of known attribute-only names below.
        """
        import builtins
        import inspect
        import pstb.gui.app as app

        known = set(dir(app)) | set(dir(builtins))
        dangling = []
        for name, handler in vars(app).items():
            if not inspect.isfunction(handler):
                continue
            # functools.wraps copies __module__ onto a decorator's wrapper,
            # so @asynccontextmanager makes contextlib's helper look like it
            # lives here. The code object's filename does not lie.
            if handler.__code__.co_filename != app.__file__:
                continue
            for referenced in handler.__code__.co_names:
                if referenced in known:
                    continue
                # An attribute on a local object, not a global read. Only a
                # LOAD_GLOBAL would be a real dangling reference, and the
                # cheap way to tell them apart is the opcode.
                loads_global = any(
                    instruction.opname == "LOAD_GLOBAL"
                    and instruction.argval == referenced
                    for instruction in dis.get_instructions(handler))
                if loads_global:
                    dangling.append(f"{name}() -> {referenced}")
        self.assertEqual(
            sorted(set(dangling)), [],
            "these handlers read globals the module never binds, so they "
            "raise NameError and 500 for every caller: "
            + ", ".join(sorted(set(dangling))))

    def test_the_vendor_pack_is_rebuilt_by_a_reload(self):
        """Left behind, it answers from the PREVIOUS database after a reload."""
        import inspect
        import pstb.gui.app as app
        source = inspect.getsource(app._console_reload)
        for name in ("modules", "vendor_network", "procurement"):
            with self.subTest(name=name):
                self.assertIn(name, source,
                              f"{name} survives a reload pointing at the old "
                              "database")

    def test_guard_preserves_an_intentional_retryable_http_status(self):
        import pstb.gui.app as app

        def retryable():
            raise HTTPException(status_code=503, detail="retry scope load")

        with self.assertRaises(HTTPException) as ctx:
            app._guard(retryable)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "retry scope load")

    def test_reload_rebuilds_row_security_with_fresh_identity_and_caches(self):
        """A reload must not carry old grants or discovery into the new DB."""
        import pstb.gui.app as app
        from pstb.security import RowSecurity

        root = app.cfg.root
        old_cfg = SimpleNamespace(
            root=root,
            security=SimpleNamespace(
                enabled=True, privileged_users=["OLDADMIN"],
                on_unavailable="refuse"),
        )
        fresh_cfg = SimpleNamespace(
            root=root,
            security=SimpleNamespace(
                enabled=True, privileged_users=["NEWADMIN"],
                on_unavailable="refuse"),
        )
        old_db = SimpleNamespace(close=Mock())
        new_db = SimpleNamespace(close=Mock())
        stale_catalog = {"scopes": [{"business_unit": "OLD01"}]}
        old_engine = SimpleNamespace(
            list_financial_scopes=Mock(return_value=stale_catalog))
        new_engine = object()
        old_security = RowSecurity(old_db, old_cfg)
        old_security._cache["OLDUSER"] = (time.monotonic() + 600, object())
        old_source_expiry = time.monotonic() + 600
        old_security._source_cache = (
            old_source_expiry, ("PS_OLD_SEC", "OPRID", "user"))
        old_objects = {
            "ar": object(),
            "report_runner": object(),
            "relationships": object(),
            "modules": object(),
            "vendor_network": object(),
            "procurement": object(),
        }
        new_objects = {name: object() for name in old_objects}
        scope_cache = {
            "value": object(), "expires": 123.0,
            "refreshing": True, "generation": 7,
        }

        with (
            patch.multiple(
                app, cfg=old_cfg, db=old_db, engine=old_engine,
                row_security=old_security, _scope_cache=scope_cache,
                **old_objects),
            patch.object(app, "load_config", return_value=fresh_cfg),
            patch.object(app, "Database", return_value=new_db),
            patch.object(app, "TBEngine", return_value=new_engine),
            patch.object(app, "ARBilling", return_value=new_objects["ar"]),
            patch.object(
                app, "Relationships",
                return_value=new_objects["relationships"]),
            patch.object(
                app, "ReportRunner",
                return_value=new_objects["report_runner"]),
            patch.object(app, "_MP", return_value=new_objects["modules"]),
            patch.object(
                app, "VendorNetwork",
                return_value=new_objects["vendor_network"]),
            patch.object(
                app, "_Proc", return_value=new_objects["procurement"]),
            patch.object(app.threading, "Thread") as thread_ctor,
            patch.object(app, "_persist_scope_catalog") as persist,
            patch("dotenv.dotenv_values", return_value={}),
        ):
            # Schedule against the old engine but hold the worker until the
            # new configuration has invalidated its cache generation.
            app._refresh_scope_catalog_async()
            stale_worker = thread_ctor.call_args.kwargs["target"]
            result = app._console_reload()
            stale_worker()
            current = app.row_security

            self.assertIs(app.cfg, fresh_cfg)
            self.assertIs(app.db, new_db)
            self.assertIs(app.engine, new_engine)
            self.assertIsNot(current, old_security)
            self.assertIs(current.db, new_db)
            self.assertIs(current.cfg, fresh_cfg)
            self.assertEqual(current.privileged_users, frozenset({"NEWADMIN"}))
            self.assertEqual(current._cache, {})
            self.assertIsNone(current._source_cache)
            self.assertIn("OLDUSER", old_security._cache)
            self.assertEqual(
                old_security._source_cache,
                (old_source_expiry, ("PS_OLD_SEC", "OPRID", "user")))
            self.assertIn("business-unit security", result["reloaded"])
            self.assertEqual(
                scope_cache,
                {"value": None, "expires": 0.0,
                 "refreshing": False, "generation": 8})
            old_engine.list_financial_scopes.assert_called_once_with(
                include_activity=False)
            persist.assert_not_called()
            old_db.close.assert_not_called()
            new_db.close.assert_not_called()

    def test_force_scope_build_retries_after_reload_generation_changes(self):
        """An in-flight old catalog is discarded, not returned as current."""
        import threading
        import pstb.gui.app as app

        started = threading.Event()
        release = threading.Event()
        old_catalog = {"scopes": [{"business_unit": "OLD01"}]}
        new_catalog = {"scopes": [{"business_unit": "NEW01"}]}

        def old_build(**_kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test did not release old scope build")
            return old_catalog

        old_engine = SimpleNamespace(
            list_financial_scopes=Mock(side_effect=old_build))
        new_engine = SimpleNamespace(
            list_financial_scopes=Mock(return_value=new_catalog))
        scope_cache = {
            "value": None, "expires": 0.0,
            "refreshing": False, "generation": 9,
        }
        results, errors = [], []

        def build() -> None:
            try:
                results.append(app._financial_scope_catalog(force=True))
            except BaseException as exc:  # captured for the test thread
                errors.append(exc)

        with (
            patch.multiple(app, engine=old_engine, _scope_cache=scope_cache),
            patch.object(app, "_persist_scope_catalog") as persist,
        ):
            worker = threading.Thread(target=build, daemon=True)
            worker.start()
            did_start = started.wait(3)
            if did_start:
                with app._scope_cache_lock:
                    app.engine = new_engine
                    scope_cache.update({
                        "value": None, "expires": 0.0,
                        "refreshing": False, "generation": 10,
                    })
            release.set()
            worker.join(3)

            self.assertTrue(did_start, "old scope build never reached barrier")
            self.assertFalse(worker.is_alive(), "scope build did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(results, [new_catalog])
            self.assertIs(scope_cache["value"], new_catalog)
            self.assertEqual(scope_cache["generation"], 10)
            old_engine.list_financial_scopes.assert_called_once_with(
                include_activity=False)
            new_engine.list_financial_scopes.assert_called_once_with(
                include_activity=False)
            persist.assert_called_once_with(new_catalog)

    def test_row_security_construction_failure_preserves_every_old_object(self):
        """A bad replacement cannot split security from the serving graph."""
        import pstb.gui.app as app

        root = app.cfg.root
        old_cfg = SimpleNamespace(root=root)
        fresh_cfg = SimpleNamespace(root=root)
        old_db = SimpleNamespace(close=Mock())
        old_objects = {
            "cfg": old_cfg,
            "db": old_db,
            "engine": object(),
            "row_security": object(),
            "ar": object(),
            "report_runner": object(),
            "relationships": object(),
            "modules": object(),
            "vendor_network": object(),
            "procurement": object(),
        }
        new_db = SimpleNamespace(close=Mock())
        new_engine = object()
        scope_value = object()
        scope_cache = {"value": scope_value, "expires": 123.0}

        with (
            patch.multiple(app, _scope_cache=scope_cache, **old_objects),
            patch.object(app, "load_config", return_value=fresh_cfg),
            patch.object(app, "Database", return_value=new_db),
            patch.object(app, "TBEngine", return_value=new_engine),
            patch.object(
                app, "RowSecurity",
                side_effect=RuntimeError("security policy is invalid")) as ctor,
            patch("dotenv.dotenv_values", return_value={}),
        ):
            result = app._console_reload()

            ctor.assert_called_once_with(new_db, fresh_cfg)
            for name, old in old_objects.items():
                with self.subTest(name=name):
                    self.assertIs(getattr(app, name), old)
            self.assertEqual(result["reloaded"], [])
            self.assertIn("kept the running configuration", result["error"])
            self.assertIs(scope_cache["value"], scope_value)
            self.assertEqual(scope_cache["expires"], 123.0)
            new_db.close.assert_called_once_with()
            old_db.close.assert_not_called()
