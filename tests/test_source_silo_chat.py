"""Slash database workspaces are separate conversations, not a selector.

These tests execute the pure routing/state functions with Node and pin the
browser/server contract around them.  The production page intentionally has
no frontend build system, so exercising its own functions catches drift more
directly than a second JavaScript implementation in the test would.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pstb" / "gui" / "static" / "index.html"


class SourceSiloChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML.read_text()

    def _between(self, start: str, end: str) -> str:
        left = self.html.index(start)
        return self.html[left:self.html.index(end, left)]

    def _function(self, name: str, following: str) -> str:
        return self._between(f"function {name}(", following)

    def _run_node(self, source: str) -> None:
        subprocess.run(
            [shutil.which("node"), "-e", source],
            check=True, capture_output=True, text=True,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_slash_parser_routes_question_switch_alias_and_unknown(self) -> None:
        discovery = self._between(
            "function chatSourceDescriptors(", "function sourceCommand(")
        parser = self._function(
            "parseSourceCommand", "function removeSourceCommandMenu")
        script = """
let META={sources:[
  {source:'default',command:'finance',aliases:['ps'],label:'Finance'},
  {source:'p2go',command:'p2go',aliases:[],label:'P2Go'},
  {source:'warehouse',command:'warehouse',aliases:[],label:'Warehouse'}
]};
""" + discovery + parser + """
const q=parseSourceCommand('/p2go show failed jobs');
if(q.kind!=='question'||q.source!=='p2go'||q.question!=='show failed jobs')
  throw new Error('source question was not routed');
const f=parseSourceCommand('/finance');
if(f.kind!=='switch'||f.source!=='default') throw new Error('finance did not switch');
const alias=parseSourceCommand('/PS trial balance');
if(alias.source!=='default'||alias.question!=='trial balance')
  throw new Error('primary alias did not route');
if(parseSourceCommand('/').kind!=='menu') throw new Error('slash did not open menu');
const bad=parseSourceCommand('/sap show orders');
if(bad.kind!=='unknown'||bad.token!=='sap') throw new Error('unknown was not local');
if(parseSourceCommand('/warehouse inventory').source!=='warehouse')
  throw new Error('configured third source was not discovered');
if(parseSourceCommand('ordinary question').source!==undefined)
  throw new Error('ordinary question selected a source');
"""
        self._run_node(script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_source_round_trip_reuses_session_scope_and_transcript(self) -> None:
        state = self._between(
            "function chatSourceDescriptors(", "const REQ_TIMEOUT_MS")
        activate = self._function(
            "activateChatSource", "// Populate the database chooser")
        script = """
let sequence=0;
function makeSessionId(){return 'session-'+(++sequence)}
let META={sources:[
  {source:'default',command:'finance',label:'Finance'},
  {source:'p2go',command:'p2go',label:'P2Go'}
]};
let ACTIVE_CHAT_SOURCE='default',CHAT_SCOPE=null,CHAT_SESSION_ID='bootstrap',
    CHAT_RESETTING=false,VIEW='not-chat';
const CHAT_SILOS=new Map();
const $=()=>null;
function syncScopeControls(){}
function syncSourceControl(){}
function viewChat(){}
function updateWorkspaceButtons(){}
""" + state + activate + """
ensureChatSilos();
const finance=CHAT_SILOS.get('default');
finance.scope={source:'default',business_unit:'US001',ledger:'ACTUALS'};
finance.messagesNode={name:'finance transcript'};
const financeSession=finance.sessionId;
activateChatSource('p2go',false);
const p2go=CHAT_SILOS.get('p2go');
p2go.messagesNode={name:'p2go transcript'};
const p2goSession=p2go.sessionId;
activateChatSource('default',false);
activateChatSource('p2go',false);
if(CHAT_SESSION_ID!==p2goSession||p2go.sessionId!==p2goSession)
  throw new Error('p2go session rotated on switch');
if(p2go.messagesNode.name!=='p2go transcript')
  throw new Error('p2go transcript was replaced');
activateChatSource('default',false);
if(CHAT_SESSION_ID!==financeSession||CHAT_SCOPE.business_unit!=='US001'||
   finance.messagesNode.name!=='finance transcript')
  throw new Error('finance state was not restored');
"""
        self._run_node(script)

    def test_unknown_command_returns_before_any_network_call(self) -> None:
        send = self._function("send", "function renderFollowUps")
        unknown = send.index("if(parsed.kind==='unknown')")
        route = send.index("fetch(route")
        self.assertLess(unknown, route)
        self.assertIn("showUnknownSourceCommand(parsed.token);return", send)

    def test_chat_uses_canonical_source_route_and_captured_state(self) -> None:
        send = self._function("send", "function renderFollowUps")
        self.assertIn(
            "'/api/source/'+encodeURIComponent(silo.descriptor.command)+'/chat'",
            send,
        )
        self.assertIn("const sendSession=silo.sessionId", send)
        self.assertIn("const sendGeneration=silo.generation", send)
        self.assertIn("const sendScope=silo.scope?{...silo.scope}:{}", send)
        self.assertIn("const msgs=ensureSiloMessages(silo)", send)
        self.assertNotIn("scope:CHAT_SCOPE", send)

    def test_csv_uses_the_card_silos_source_authoritative_route(self) -> None:
        export = self._between(
            "async function downloadExport(", "const CSV_HINT=")
        self.assertIn(
            "'/api/source/'+encodeURIComponent(descriptor.command)+'/export'",
            export,
        )
        self.assertNotIn("fetch('/api/export'", export)

    def test_question_log_ids_and_provenance_are_silo_specific(self) -> None:
        from pstb.qlog import QuestionLog

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = QuestionLog("questions.jsonl", root)

            def record(index: int) -> str:
                source = "default" if index % 2 == 0 else "p2go"
                return log.log_turn(
                    surface="gui", provider="test",
                    question="show invoice status", calls=[], rounds=1,
                    answer="ok", scope={"source": source},
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                turn_ids = list(pool.map(record, range(40)))

            self.assertEqual(len(set(turn_ids)), 40)
            rows = [json.loads(line) for line in
                    (root / "questions.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 40)
            self.assertEqual(
                {row["source_database"] for row in rows},
                {"default", "p2go"},
            )
            self.assertTrue(all(
                row["scope"]["source"] == row["source_database"]
                for row in rows
            ))

    def test_clear_rotates_only_the_active_silo(self) -> None:
        clear = self._between(
            "async function clearActiveSilo(", "function viewChat(")
        self.assertIn("const silo=activeChatSilo()", clear)
        self.assertIn("silo.sessionId=makeSessionId()", clear)
        self.assertIn("silo.generation+=1", clear)
        self.assertNotIn("CHAT_SILOS.forEach", clear)
        self.assertNotIn("for(const", clear)
        self.assertIn("body:JSON.stringify({session_id:oldSessionId})", clear)

    def test_secondary_has_fixed_badge_and_no_finance_controls_or_starters(self) -> None:
        chat = self._function("viewChat", "async function send(")
        intro = self._function("populateSiloIntro", "function ensureSiloMessages")
        self.assertIn("sourceBadgeHtml(silo.source)", chat)
        self.assertIn("if(silo.source==='default')", chat)
        self.assertIn("Read-only semantic and relationship queries only", chat)
        self.assertIn("Finance scopes and curated controls are not available", intro)
        finance_branch = chat.index("if(silo.source==='default')")
        self.assertGreater(chat.index("chips=el('div','starter-groups')"),
                           finance_branch)
        self.assertGreater(chat.index("buildTimeSelects()"), finance_branch)

    def test_every_visible_turn_is_source_badged(self) -> None:
        send = self._function("send", "function renderFollowUps")
        self.assertGreaterEqual(send.count("sourceBadgeHtml(silo.source)"), 6)
        self.assertIn("sourceBadgeHtml(silo.source)+'<br>'+esc(m2)", send)
        self.assertIn("Database: '+sourceCommand(silo.source)", self.html)

    def test_secondary_prompt_is_source_bound_and_has_no_finance_doctrine(self) -> None:
        from pstb.client.prompt import source_silo_prompt

        prompt = source_silo_prompt("p2go")
        self.assertIn("p2go", prompt)
        self.assertIn("search_metadata", prompt)
        self.assertIn("relationship", prompt.lower())
        self.assertIn("read-only", prompt.lower())
        for forbidden in ("wiki_lookup", "get_trial_balance", "PS_LEDGER",
                          "FISCAL_YEAR", "BUSINESS_UNIT"):
            self.assertNotIn(forbidden, prompt)

    def test_signed_in_user_with_no_finance_units_reaches_secondary_route(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        class Registry:
            @staticmethod
            def resolve_command(command):
                return "p2go" if command == "p2go" else "default"

            @staticmethod
            def resolve_name(source):
                return str(source or "default")

        access = SimpleNamespace(
            all_units=False, units=frozenset(), oprid="P2GO_ONLY")
        inner = AsyncMock(return_value={"answer": "p2go structure"})
        with (patch.object(gapp.engine, "registry", Registry()),
              patch.object(gapp.cfg.security, "enabled", True),
              patch.object(gapp, "access_for_request", return_value=access),
              patch.object(gapp, "chat", inner),
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            response = client.post("/api/source/p2go/chat", json={
                "message": "How are order headers related to lines?",
                "session_id": "p2go-only-session",
                "scope": {"source": "p2go"},
            })
        self.assertEqual(response.status_code, 200, response.text)
        inner.assert_awaited_once()
        forwarded = inner.await_args.args[0]
        self.assertEqual(forwarded["scope"], {"source": "p2go"})

    def test_legacy_chat_rejects_conflicting_source_aliases(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        class Registry:
            @staticmethod
            def names():
                return ["default", "p2go"]

            @staticmethod
            def resolve_name(source):
                return str(source or "default")

        with (patch.object(gapp.engine, "registry", Registry()),
              patch.object(gapp, "access_for_request", return_value=None),
              TestClient(gapp.app, base_url="http://127.0.0.1:8000",
                         client=("127.0.0.1", 50000)) as client):
            for scope in (
                {"source": "default", "db": "p2go"},
                {"source": "p2go", "db": "default"},
            ):
                with self.subTest(scope=scope):
                    response = client.post("/api/chat", json={
                        "message": "Describe the available structure",
                        "session_id": "legacy-conflict-session",
                        "scope": scope,
                    })
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("scope.source", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
