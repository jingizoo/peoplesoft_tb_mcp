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

    def test_secondary_context_allows_only_source_aware_data_or_policy(self):
        self.assertEqual(
            apply_request_scope("list_tables", {}, {"source": "p2go"}),
            {"source": "p2go"},
        )
        self.assertEqual(
            apply_request_scope("wiki_lookup", {"question": "policy"},
                                {"source": "p2go"}),
            {"question": "policy"},
        )

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

    def test_chat_has_an_accessible_database_selector(self) -> None:
        builder = self._function(
            "buildChatSourceSelect", "// Database is a hard boundary")
        chat = self._function("viewChat", "async function send(")
        self.assertIn("label.append(el('span',null,'Database'))", builder)
        self.assertIn("sel.setAttribute('aria-label','Database context')",
                      builder)
        self.assertIn("s.source==='default'?'Finance':s.source", builder)
        self.assertIn("const sourceControl=buildChatSourceSelect()", chat)
        self.assertIn("if(sourceControl) context.append(sourceControl)", chat)

    def test_chat_selector_is_absent_for_one_database(self) -> None:
        builder = self._function(
            "buildChatSourceSelect", "// Database is a hard boundary")
        self.assertIn("if(list.length<2) return null;", builder)

    def test_chat_selector_uses_the_hard_scope_path(self) -> None:
        builder = self._function(
            "buildChatSourceSelect", "// Database is a hard boundary")
        switcher = self._function(
            "setChatSource", "// Populate the database chooser")
        self.assertIn("sel.onchange=()=>setChatSource(sel.value)", builder)
        self.assertIn("setChatScope({source:name},true)", switcher)

    def test_initial_finance_selection_is_also_a_hard_scope(self) -> None:
        catalog = self._function(
            "applyScopeCatalog", "async function loadScopesInBackground")
        self.assertIn(
            "setChatScope({source:'default'},false)", catalog,
        )

    def test_inflight_turn_stays_bound_to_its_original_session(self) -> None:
        send = self._function("send", "function renderFollowUps")
        self.assertIn("const sendSession=CHAT_SESSION_ID", send)
        self.assertIn("encodeURIComponent(sendSession)+'&turn='", send)
        self.assertIn("message:m,session_id:sendSession,scope:CHAT_SCOPE||{}",
                      send)
        self.assertGreaterEqual(
            send.count("if(sendSession!==CHAT_SESSION_ID)"), 3,
            "success, error, and activity outcomes must all be stale-safe",
        )
        self.assertIn(
            "if(sendSession!==CHAT_SESSION_ID){ clearInterval(poll); return; }",
            send,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_switching_round_trip_restores_the_finance_scope(self) -> None:
        """Execute the real switcher, not a Python copy of its behavior."""
        canonical = self._function("canonicalScope", "function scopeLabel")
        switcher = self._function(
            "setChatSource", "// Populate the database chooser")
        script = """
const META={sources:[{source:'default'},{source:'p2go'}]};
let CHAT_SCOPE={business_unit:'US001',ledger:'ACTUALS',
  fiscal_year:2026,period:7};
let LAST_FINANCE_SCOPE=null;
const calls=[];
""" + canonical + """
function setChatScope(value,announce){
  const next=canonicalScope(value);
  calls.push({value:{...value},next,announce});
  CHAT_SCOPE=next;
}
function dismissScopeChoosers(){}
""" + switcher + """
setChatSource('p2go');
if(calls.length!==1||calls[0].value.source!=='p2go'||!calls[0].announce)
  throw new Error('secondary source did not use setChatScope');
if(!CHAT_SCOPE||CHAT_SCOPE.source!=='p2go'||Object.keys(CHAT_SCOPE).length!==1)
  throw new Error('secondary source was not the complete hard scope');
if(!LAST_FINANCE_SCOPE||LAST_FINANCE_SCOPE.business_unit!=='US001'||
   LAST_FINANCE_SCOPE.ledger!=='ACTUALS'||
   LAST_FINANCE_SCOPE.fiscal_year!==2026||LAST_FINANCE_SCOPE.period!==7)
  throw new Error('finance scope was not retained');
setChatSource('default');
const restored=calls[1].value;
if(restored.business_unit!=='US001'||restored.ledger!=='ACTUALS'||
   restored.fiscal_year!==2026||restored.period!==7)
  throw new Error('finance scope was not restored');
"""
        subprocess.run([shutil.which("node"), "-e", script], check=True,
                       capture_output=True, text=True)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_any_database_scope_change_isolates_late_answers(self) -> None:
        setter = self._function(
            "setChatScope", "document.addEventListener('focusin'")
        script = """
let CHAT_SCOPE={source:'default',business_unit:'US001',ledger:'ACTUALS'};
let LAST_FINANCE_SCOPE={...CHAT_SCOPE};
let CHAT_SESSION_ID='session-1', sequence=1;
let PENDING_TIME={}, SYNC_TIME_SELECTS=null, SCOPE_YEARS_STALE=false;
function makeSessionId(){ return 'session-'+(++sequence); }
function canonicalScope(value){
  return value&&value.source==='p2go'?{source:'p2go'}:(value?{...value}:null);
}
function scopeLabel(value){ return JSON.stringify(value); }
function syncScopeControls(){}
function syncSourceControl(){}
function updateScopeChip(){}
function esc(value){ return String(value); }
function el(){ return {}; }
const $=()=>null;
""" + setter + """
setChatScope({source:'p2go'},false);
if(CHAT_SESSION_ID!=='session-2'||CHAT_SCOPE.source!=='p2go')
  throw new Error('entering the secondary source kept the old generation');
setChatScope({source:'default',business_unit:'US001',ledger:'ACTUALS'},false);
if(CHAT_SESSION_ID!=='session-3'||CHAT_SCOPE.source!=='default')
  throw new Error('leaving through a finance scope kept the old generation');
"""
        subprocess.run([shutil.which("node"), "-e", script], check=True,
                       capture_output=True, text=True)

    def test_finance_only_chips_hide_on_another_database(self) -> None:
        self.assertIn("const psOnly = selected==='default';", self.html)
        self.assertIn("['#scope-fy','#scope-per']", self.html)
        self.assertIn("if(el2) el2.hidden=!psOnly", self.html)
        self.assertIn("if(starters) starters.hidden=!psOnly", self.html)
        self.assertIn("chips.id='chat-starters'", self.html)
        self.assertIn("curated financial tools", self.html)
        self.assertIn("database '+value.source+' — ad-hoc only", self.html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_chat_control_does_not_depend_on_hidden_browse_toolbar(self) -> None:
        syncer = self._function("syncSourceControl", "function syncScopeControls")
        script = """
const META={sources:[{source:'default'},{source:'p2go'}]};
const nodes={
  '#chat-source':{value:'default'},
  '#scope-fy':{hidden:false},
  '#scope-per':{hidden:false}
};
const $=selector=>nodes[selector]||null;
""" + syncer + """
syncSourceControl({source:'p2go'});
if(nodes['#chat-source'].value!=='p2go'||
   !nodes['#scope-fy'].hidden||!nodes['#scope-per'].hidden)
  throw new Error('secondary chat context was not applied');
syncSourceControl({source:'default'});
if(nodes['#chat-source'].value!=='default'||
   nodes['#scope-fy'].hidden||nodes['#scope-per'].hidden)
  throw new Error('finance chat controls were not restored');
"""
        subprocess.run([shutil.which("node"), "-e", script], check=True,
                       capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
