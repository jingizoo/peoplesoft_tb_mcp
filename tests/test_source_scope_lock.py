"""The database selector is a guard, not a label.

The badge says which database answered. This says which database the model
is ALLOWED to reach: a source the person did not select is refused, the same
way a business unit they did not select already was.

Why hard rather than soft. fiscal_year and period are defaults the question
may override — "show me period 3" while the chip reads P6 is a legitimate
question. A database is not that. Answering from a warehouse when the reader
selected the finance system is not a narrower answer to their question, it
is an answer to a different one, and the figures carry the same column names
either way.

Why the primary is conditional. A one-database deployment sends no source
argument and behaves exactly as before. Once the page has a real database
choice, selecting Finance sends the explicit ``default`` sentinel so the
same guard refuses a model attempt to reach an unselected secondary source.
"""
from __future__ import annotations

import sys
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.guards import (ScopeConflict, _SOURCE_SCOPED_TOOLS,  # noqa: E402
                         _PRIMARY_ONLY_STRUCTURAL_TOOLS,
                         _TOOL_SCOPE_ARGS, apply_request_scope,
                         normalize_request_scope)

MART = {"source": "p2go", "business_unit": "US001", "ledger": "ACTUALS"}
PRIMARY = {"business_unit": "US001", "ledger": "ACTUALS"}


class LockTests(unittest.TestCase):
    def test_an_unselected_database_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            apply_request_scope("run_sql", {"source": "default"}, MART)
        self.assertIn("source", str(ctx.exception))

    def test_the_selected_database_is_injected_when_omitted(self) -> None:
        out = apply_request_scope("list_tables", {}, MART)
        self.assertEqual(out["source"], "p2go")

    def test_a_matching_choice_passes(self) -> None:
        out = apply_request_scope("list_tables", {"source": "p2go"}, MART)
        self.assertEqual(out["source"], "p2go")

    def test_case_and_primary_aliases_are_one_database(self) -> None:
        # "peoplesoft" / "main" / "" all mean the primary; comparing raw
        # strings made a matching selection look like a conflict.
        self.assertEqual(
            apply_request_scope("list_tables", {"source": "P2GO"}, MART)
            ["source"], "p2go")
        for alias in ("peoplesoft", "main", "PS", "default"):
            with self.subTest(alias):
                out = apply_request_scope(
                    "list_tables", {"source": alias},
                    {"source": "default", **PRIMARY})
                self.assertEqual(out["source"], "default")

    def test_configured_source_named_like_a_primary_alias_stays_distinct(self):
        selected = {"source": "finance"}
        with self.assertRaises(ScopeConflict):
            apply_request_scope(
                "list_tables", {"source": "default"}, selected,
            )
        with self.assertRaises(ScopeConflict):
            apply_request_scope("describe_record", {}, selected)

    def test_primary_alias_cannot_escape_to_a_same_named_secondary(self):
        out = apply_request_scope(
            "list_tables", {"source": "finance"},
            {"source": "default", **PRIMARY},
        )
        self.assertEqual(out["source"], "default")

    def test_reaching_elsewhere_from_the_primary_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict):
            apply_request_scope("list_tables", {"source": "p2go"},
                                {"source": "default", **PRIMARY})

    def test_every_source_taking_tool_is_locked(self) -> None:
        import inspect

        from pstb import server as srv
        for name in _SOURCE_SCOPED_TOOLS:
            with self.subTest(name):
                self.assertEqual(_TOOL_SCOPE_ARGS[name].get("source"),
                                 "source")
                fn = getattr(srv, name, None)
                if fn is not None:
                    self.assertIn("source",
                                  inspect.signature(fn).parameters,
                                  "a locked tool that cannot accept the "
                                  "argument is the blank-card bug again")

    def test_curated_financial_tools_require_finance_context(self) -> None:
        # They answer from the primary by construction and take no source
        # argument. A secondary selection therefore refuses them rather than
        # placing a Finance answer under a P2Go context label.
        for name in ("get_trial_balance", "get_ar_aging",
                     "get_customer_financial_360"):
            with self.subTest(name):
                self.assertNotIn("source", _TOOL_SCOPE_ARGS.get(name, {}))
                with self.assertRaises(ScopeConflict):
                    apply_request_scope(name, {}, MART)
                out = apply_request_scope(
                    name, {}, {"source": "default", **PRIMARY})
                self.assertNotIn("source", out)

    def test_primary_only_structural_tools_refuse_secondary_context(self):
        for name in _PRIMARY_ONLY_STRUCTURAL_TOOLS:
            with self.subTest(name=name):
                with self.assertRaises(ScopeConflict):
                    apply_request_scope(name, {}, MART)
                # The same tool remains available when Finance is the
                # explicit selection; only cross-database mixing is blocked.
                self.assertEqual(
                    apply_request_scope(name, {},
                                        {"source": "default", **PRIMARY}),
                    {},
                )

    def test_secondary_context_is_closed_by_default(self) -> None:
        # Representative primary-backed tools that are not all financial
        # evidence tools. Future source-unaware tools are refused too.
        for name in (
            "get_tree_node_accounts", "list_business_units", "list_trees",
            "resolve_period", "describe_entity_graph",
            "list_integration_endpoints", "coupa_to_ap_tie",
            "a_future_source_unaware_tool",
        ):
            with self.subTest(name=name), self.assertRaises(ScopeConflict):
                apply_request_scope(name, {}, {"source": "p2go"})

    def test_secondary_sql_cannot_import_finance_policy_or_scope(self) -> None:
        for args in (
            {"sql": "SELECT 1", "policy_binds": {"limit": "close_limit"}},
            {"sql": "SELECT 1", "business_unit": "US001"},
        ):
            with self.subTest(args=args), self.assertRaises(ScopeConflict):
                apply_request_scope("run_sql", args, {"source": "p2go"})

    def test_secondary_context_allows_only_source_aware_data(self):
        self.assertEqual(
            apply_request_scope("list_tables", {}, {"source": "p2go"}),
            {"source": "p2go"},
        )
        with self.assertRaises(ScopeConflict):
            apply_request_scope("wiki_lookup", {"question": "policy"},
                                {"source": "p2go"})

    def test_finance_database_without_bu_cannot_run_a_curated_control(self):
        # This is deliberately a direct tool-call probe: even a technical
        # question that routing did not classify as financial must not make
        # get_trial_balance fall through to configured default scope.
        with self.assertRaises(ScopeConflict):
            apply_request_scope(
                "get_trial_balance", {}, {"source": "default"},
            )
        self.assertEqual(
            apply_request_scope("list_tables", {}, {"source": "default"}),
            {"source": "default"},
        )
        self.assertEqual(
            apply_request_scope(
                "list_financial_scopes", {}, {"source": "default"},
            ),
            {},
        )


class NoSelectionTests(unittest.TestCase):
    """The single-database deployment must not notice any of this."""

    def test_no_source_in_scope_leaves_the_argument_alone(self) -> None:
        self.assertEqual(
            apply_request_scope("list_tables", {"source": "p2go"}, PRIMARY),
            {"source": "p2go"})
        self.assertEqual(apply_request_scope("list_tables", {}, PRIMARY), {})

    def test_a_blank_source_is_not_a_constraint(self) -> None:
        self.assertEqual(normalize_request_scope({"source": ""}), {})
        self.assertEqual(normalize_request_scope({"source": "   "}), {})


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (
            ROOT / "pstb" / "gui" / "static" / "index.html"
        ).read_text()

    def _function(self, name: str, following: str) -> str:
        start = self.html.index(f"function {name}(")
        end = self.html.index(following, start)
        return self.html[start:end]

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_finance_is_pinned_only_when_there_is_a_real_choice(self) -> None:
        canonical = self._function("canonicalScope", "function scopeLabel")
        script = """
let META={sources:[{source:'default'}]};
""" + canonical + """
const finance={business_unit:'US001',ledger:'ACTUALS',
  fiscal_year:2026,period:7};
let scope=canonicalScope(finance);
if(Object.prototype.hasOwnProperty.call(scope,'source'))
  throw new Error('single-source deployment was pinned');
META={sources:[{source:'default'},{source:'p2go'}]};
scope=canonicalScope(finance);
if(scope.source!=='default')
  throw new Error('explicit Finance selection was not pinned');
scope=canonicalScope({source:'default'});
if(!scope||scope.source!=='default'||Object.keys(scope).length!==1)
  throw new Error('Finance without a BU lost the database hard lock');
"""
        subprocess.run([shutil.which("node"), "-e", script], check=True,
                       capture_output=True, text=True)

    def test_chat_has_accessible_database_workspaces(self) -> None:
        builder = self._function(
            "buildChatWorkspaceBar", "function activateChatSource")
        chat = self._function("viewChat", "async function send(")
        self.assertIn("setAttribute('aria-label','Database workspaces')",
                      builder)
        self.assertIn("button.dataset.source=d.source", builder)
        self.assertIn("activateChatSource(d.source,true)", builder)
        self.assertIn("const workspaceBar=buildChatWorkspaceBar()", chat)

    def test_finance_workspace_exists_even_with_one_database(self) -> None:
        descriptors = self._function(
            "chatSourceDescriptors", "function descriptorForSource")
        self.assertIn("[{source:'default'}]", descriptors)
        self.assertIn("primary?'finance':source", descriptors)

    def test_chat_posts_to_source_derived_route(self) -> None:
        send = self._function("send", "function renderFollowUps")
        self.assertIn("'/api/source/'+encodeURIComponent(", send)
        self.assertIn("silo.descriptor.command", send)
        self.assertIn("scope:sendScope", send)

    def test_initial_finance_selection_is_also_a_hard_scope(self) -> None:
        catalog = self._function(
            "applyScopeCatalog", "async function loadScopesInBackground")
        self.assertIn(
            "setChatScope({source:'default'},false)", catalog,
        )

    def test_inflight_turn_stays_bound_to_its_original_session(self) -> None:
        send = self._function("send", "function renderFollowUps")
        self.assertIn("const sendSession=silo.sessionId", send)
        self.assertIn("const sendGeneration=silo.generation", send)
        self.assertIn("const sendScope=silo.scope?{...silo.scope}:{}", send)
        self.assertIn("encodeURIComponent(sendSession)+'&turn='", send)
        self.assertIn("message:m,session_id:sendSession,scope:sendScope",
                      send)
        self.assertGreaterEqual(
            send.count("isLiveSiloTurn(silo,sendSession,sendGeneration)"), 4,
            "success, error, and activity outcomes must all be stale-safe",
        )

    def test_source_switch_keeps_each_silos_session_and_transcript(self) -> None:
        switcher = self._function(
            "activateChatSource", "// Populate the database chooser")
        self.assertNotIn("makeSessionId", switcher)
        self.assertIn("prior.scope=CHAT_SCOPE", switcher)
        self.assertIn("CHAT_SESSION_ID=next.sessionId", switcher)
        self.assertIn("CHAT_SCOPE=next.scope", switcher)

    def test_finance_only_chips_hide_on_another_database(self) -> None:
        self.assertIn("const psOnly = selected==='default';", self.html)
        self.assertIn("['#scope-fy','#scope-per']", self.html)
        self.assertIn("if(el2) el2.hidden=!psOnly", self.html)
        self.assertIn("if(starters) starters.hidden=!psOnly", self.html)
        self.assertIn("chips.id='chat-starters'", self.html)
        self.assertIn("Finance scopes and curated controls are not available", self.html)
        self.assertIn("read-only semantic & relationship queries", self.html)

    def test_chat_workspaces_do_not_depend_on_hidden_browse_toolbar(self) -> None:
        builder = self._function(
            "buildChatWorkspaceBar", "function activateChatSource")
        self.assertIn("chatSourceDescriptors().forEach", builder)
        self.assertIn("button.dataset.source=d.source", builder)
        self.assertNotIn("#f-source", builder)
        self.assertNotIn("#chat-source", self.html)


if __name__ == "__main__":
    unittest.main()
