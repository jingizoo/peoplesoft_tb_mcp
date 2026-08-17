"""Web UI for the trial-balance agent.

Financial figures are served straight from the engine and rendered by the
browser — the model never produces a number that reaches the screen. The chat
panel is an assistant over already-verified data, not the source of it.

Run:  python -m pstb.gui            (then open http://127.0.0.1:8000)
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..client.llm_base import PROVIDERS, provider_model
from ..config import load_config
from ..version import build_info as _build_info
from ..db import Database, DbError
from ..engine import EngineError, TBEngine
from .. import queries as query_sql
from ..ar import ARBilling, ARError
from ..relationships import Relationships
from ..vendors import VendorNetwork
from ..qlog import QuestionLog
from ..export import ExportError
from ..report import ReportError, ReportRunner
from ..security import RowSecurity, SecurityError, access_scope
from ..wiki import WikiError, make_wiki
from . import console, localguard, progress

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, Response
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "The web UI needs FastAPI. Install it with:\n"
        "  python scripts/bootstrap.py --gui\n"
        "or: pip install -e '.[gui]'"
    ) from e

STATIC = Path(__file__).parent / "static"

cfg = load_config(os.environ.get("PSTB_CONFIG"))
db = Database(cfg)
engine = TBEngine(db, cfg)
# The GUI builds its own engine, and TBEngine.registry defaults to None — so
# without this the database chooser saw no sources however many were
# configured, and _validated_scope had nothing to validate a selection
# against. pstb/server.py does the same on its side.
from ..sources import SourceRegistry as _SourceRegistry
engine.registry = _SourceRegistry(cfg, db)
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
relationships = Relationships(ar)
from ..modules import ModulePacks as _MP
vendor_network = VendorNetwork(_MP(engine))
from ..entitygraph import EntityGraph as _EG, graph_path as _eg_path
from ..procgraph import ProcessGraph as _PG, graph_path as _pg_path
from ..procurement import Procurement as _Proc
# Opened per call against a local file, so a graph rebuilt by an
# administrator is picked up without restarting the server.
process_graph = _PG(_pg_path(cfg))
entity_graph = _EG(_eg_path(cfg))
procurement = _Proc(_MP(engine))
qlog = QuestionLog(getattr(cfg.tools, "question_log", ""), cfg.root)
try:
    wiki = make_wiki(cfg)
except WikiError:
    wiki = None

# ---------------------------------------------------------------- MCP session
# One server subprocess for the LIFETIME OF THE PROCESS, not one per chat turn.
# Spawning per turn cost a fresh Python start, MCP handshake, Oracle logon and
# cold caches on every question (~390ms of pure overhead locally, worse over a
# corporate network) with zero cache reuse between questions.
#
# The session is owned by a task that lives as long as the app, not by a
# request. MCP's stdio client uses anyio cancel scopes, and a cancel scope
# must be exited by the same task that entered it — holding the stack inside
# a request handler and closing it from another raises "attempted to exit
# cancel scope in a different task". One background task enters the stack,
# parks on a shutdown event and closes the stack itself, so both ends stay in
# the same task; individual tool calls are safe from request tasks because
# they only move messages over memory streams.
#
# That task is STARTED, not awaited, by the lifespan. Uvicorn does not accept
# a single connection until lifespan startup returns, so awaiting it here put
# a Python start, an MCP handshake and an Oracle logon in front of the first
# byte the browser could receive: the terminal printed a URL that then
# refused to load for as long as the database took, which is the worst
# possible place to spend that time. Chat degrades to a per-turn server while
# the shared one is still coming up, which is exactly what it already does
# when the shared one fails outright.
_MCP: dict = {"session": None, "tools": None, "error": None,
              "state": "starting"}


def _server_import_check() -> str:
    """Why did `python -m pstb.server` die? Import it in a subprocess that
    captures stderr and return the tail. The server connects to the database
    at import time, so the dominant failures (Oracle logon, config, a broken
    partial pull) all surface here with their real message."""
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = (str(Path(__file__).resolve().parents[2])
                         + os.pathsep + env.get("PYTHONPATH", ""))
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import pstb.server"],
            capture_output=True, text=True, timeout=90, env=env)
    except subprocess.TimeoutExpired:
        return ("server import timed out after 90s — usually a database "
                "connection hanging; check VPN/listener reachability")
    except Exception as probe_err:
        return f"import check could not run: {probe_err}"
    if proc.returncode == 0:
        return ("the server imports cleanly, so the failure is in the MCP "
                "handshake itself — check that the venv's mcp package "
                "matches (pip show mcp) and retry")
    tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
    return f"server failed to start: {tail[-600:]}"


# How long to wait before trying the shared engine again after a failed
# connect, growing to a five-minute ceiling. Retrying at all is the point:
# a transient boot-time blip — a VPN mid-reconnect, a listener restarting —
# used to pin the process on the per-turn fallback FOREVER, every question
# paying a fresh Python start and database logon with nothing on screen
# saying why the app had become slow.
_MCP_RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0)


async def _mcp_worker(stop: asyncio.Event) -> None:
    """Own the shared MCP session for the life of the process.

    Enter, publish, park, close — all in this one task, because anyio
    requires the cancel scopes inside stdio_client to be exited by the task
    that entered them. Each attempt owns its own exit stack, closed in this
    task before the next attempt begins.
    """
    # Not just the initial value: a process that has already run a lifespan
    # (a test, a reload) left 'stopped' behind, and a stale terminal state
    # would tell the page the engine had given up before it had tried.
    _MCP.update({"state": "starting", "error": None})
    attempt = 0
    while not stop.is_set():
        stack = contextlib.AsyncExitStack()
        try:
            try:
                # Only the FIRST attempt narrates the boot bar: later
                # retries happen long after the page painted, and their
                # signal is _MCP["state"], which the page watches.
                ctx = (progress.step("engine") if attempt == 0
                       else contextlib.nullcontext())
                with ctx:
                    from mcp import ClientSession, StdioServerParameters
                    from mcp.client.stdio import stdio_client
                    from ..client.chat import tool_specs

                    env = dict(os.environ)
                    env["PYTHONPATH"] = (
                        str(Path(__file__).resolve().parents[2])
                        + os.pathsep + env.get("PYTHONPATH", ""))
                    params = StdioServerParameters(
                        command=sys.executable, args=["-m", "pstb.server"],
                        env=env)
                    read, write = await stack.enter_async_context(
                        stdio_client(params))
                    session = await stack.enter_async_context(
                        ClientSession(read, write))
                    await session.initialize()
                    # Tools BEFORE session: /api/chat treats a non-None
                    # session as "the shared engine is up" and reads both,
                    # so publishing the session first opened a window where
                    # a turn got tools=None and died on it.
                    _MCP["tools"] = tool_specs(await session.list_tools())
                    _MCP["session"] = session
                    _MCP["state"] = "ready"
                    _MCP["error"] = None
                if attempt:
                    progress.end("engine", ok=True,
                                 note=f"recovered on retry {attempt}")
                    print("[pstb] shared MCP session recovered",
                          file=sys.stderr)
                await stop.wait()
                return
            except Exception as e:              # degrade, never fail to boot
                # Degraded NOW, not after the diagnosis. The import check
                # below runs a whole Python start with a 90-second ceiling,
                # and while it ran the state still said 'starting' — so the
                # page's one look at it landed in that window and the
                # degradation was never shown to anyone.
                _MCP["error"] = str(e)
                _MCP["state"] = "degraded"
                # The exception from a dead stdio subprocess is usually
                # noise ("unhandled errors in a TaskGroup") while the REAL
                # reason — an Oracle logon rejection, a config error, a
                # broken pull — died with the subprocess's stderr. Re-run
                # the import in a subprocess that captures stderr, so the
                # user sees ORA-01017 instead of a shrug. Off the event
                # loop: every request would queue behind it here.
                detail = await asyncio.to_thread(_server_import_check)
                _MCP["error"] = str(e) + (f" | {detail}" if detail else "")
                # A second end() on the already-failed step upgrades its
                # note from the raw exception to the diagnosed cause.
                progress.end("engine", ok=False, note=_MCP["error"][:300])
                print(f"[pstb] shared MCP session unavailable "
                      f"({_MCP['error']}); falling back to one server per "
                      "turn", file=sys.stderr)
        finally:
            # One finally over BOTH the startup and the park, because a
            # cancel during startup is not an Exception and would otherwise
            # skip the close and leak the half-started subprocess.
            with contextlib.suppress(Exception):
                await stack.aclose()
            if _MCP.get("state") == "ready":
                _MCP.update({"session": None, "tools": None,
                             "state": "stopped"})
            else:
                _MCP.update({"session": None, "tools": None})
        delay = _MCP_RETRY_SECONDS[min(attempt, len(_MCP_RETRY_SECONDS) - 1)]
        attempt += 1
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
    _MCP.update({"session": None, "tools": None, "state": "stopped"})


@contextlib.asynccontextmanager
async def _lifespan(_app):
    progress.end("server")
    stop = asyncio.Event()
    worker = asyncio.create_task(_mcp_worker(stop), name="pstb-mcp-session")
    # Discovery is the longest step on a real instance and nothing about the
    # first paint depends on it, so start it at boot rather than on the first
    # visitor's request. Serves from the persisted catalog meanwhile.
    _prime_scope_catalog()
    try:
        yield
    finally:
        stop.set()
        # A live subprocess is worth a graceful close. One that never
        # finished starting has nothing to close gracefully, and waiting on
        # it would hold Ctrl+C hostage to the same hang that made the
        # startup asynchronous in the first place.
        if _MCP.get("state") == "ready":
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        if not worker.done():
            worker.cancel()             # cancelling exits the scopes in-task
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await worker
        _MCP.update({"session": None, "tools": None})


app = FastAPI(title="PeopleSoft Trial Balance", docs_url=None,
              redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _access_guard(request, call_next):
    """Every route, not just the sensitive-looking ones.

    A DNS-rebound page reads the general ledger from /api/trial-balance as
    readily as it would read an admin page, so the check cannot be scoped
    to one prefix. See pstb/gui/localguard.py for what each rule stops.
    """
    status, reason = localguard.rejection(request.scope)
    if status:
        response = JSONResponse(status_code=status,
                                content={"error": reason})
    else:
        response = await call_next(request)
    # A token that arrived in the URL becomes a cookie, so the page's own
    # fetches — which carry no query string — are authenticated too. Set only
    # after the request was ACCEPTED, so a wrong token never gets stored and
    # then silently re-sent forever.
    #
    # (Business-unit security is enforced in _row_security_guard below, not
    # here, because it has to read the query string and answer 401/403 with
    # a message the page can act on.)
    if not status and localguard.POLICY.token and localguard.token_in_query(
            request.scope):
        response.set_cookie(
            localguard.TOKEN_COOKIE, localguard.POLICY.token,
            httponly=True, samesite="strict", path="/")
    localguard.apply_security_headers(response.headers)
    return response


# Routes that must answer before anyone has signed in, or the page cannot
# render the sign-in form to sign in with.
_OPEN_PATHS = frozenset({
    "/", "/api/meta", "/api/boot", "/api/session", "/api/signin",
    "/api/signout", "/console",
})


# Routes that read something OTHER than the unit-keyed PeopleSoft ledger.
# They still need a signed-in session, but a business unit means nothing to
# them — and a user with no ledger grants must still be able to ask a policy
# question, or "no access to the numbers" silently becomes "no access to the
# handbook either".
_UNIT_FREE_PREFIXES = ("/api/wiki", "/api/activity", "/api/feedback",
                       "/api/chat/reset", "/api/question-report")


def _needs_unit_check(path: str) -> bool:
    if path in _OPEN_PATHS or not path.startswith("/api/"):
        return False
    if path.startswith("/api/console"):
        return False
    return not path.startswith(_UNIT_FREE_PREFIXES)


def _default_unit_for(access) -> str:
    """The unit an unqualified request should read, for THIS person.

    The site default when they hold it — so a privileged-adjacent user's
    experience is unchanged — and otherwise the first unit they do hold.
    Alphabetical rather than clever: any rule here is arbitrary, and an
    arbitrary rule that is stable beats one that moves.
    """
    mine = sorted(getattr(access, "units", ()) or ())
    if not mine:
        raise SecurityError(
            f"{access.oprid} is granted no business units, so there is no "
            f"data to show. {access.detail}")
    try:
        discovered = engine.warm_effective_defaults()
        if discovered and access.allows(discovered["business_unit"]):
            return discovered["business_unit"]
    except Exception:
        pass
    default = (cfg.defaults.business_unit or "").strip().upper()
    return default if default in access.units else mine[0]


def _with_unit(query_string: bytes, unit: str) -> bytes:
    from urllib.parse import parse_qsl, urlencode

    pairs = [(k, v) for k, v in parse_qsl(
        query_string.decode("latin-1"), keep_blank_values=True)
        if k != "business_unit"]
    pairs.append(("business_unit", unit))
    return urlencode(pairs).encode("latin-1")


@app.middleware("http")
async def _row_security_guard(request, call_next):
    """One gate for every data route, present and future.

    Checking business units inside each handler would work until somebody
    adds the seventeenth endpoint and forgets — and the failure mode of
    forgetting is silently serving another unit's ledger, which nobody
    notices because it looks exactly like a correct answer. So the check
    lives in front of all of them and reads the request rather than the
    signature: any `business_unit` the caller names must be one their
    PeopleSoft user ID grants.

    Wiki and diagnostics routes pass through: they carry no unit, and a
    guard that refuses a policy question because the person has no ledger
    access would be answering a question nobody asked.
    """
    path = request.url.path
    if not row_security.enabled or path in _OPEN_PATHS \
            or not path.startswith("/api/") \
            or path.startswith("/api/console"):
        return await call_next(request)
    if not _needs_unit_check(path):
        # Signed in, but no unit resolution: see _UNIT_FREE_PREFIXES.
        try:
            access_for_request(request)
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"error": e.detail,
                         "signin_required": e.status_code == 401})
        return await call_next(request)
    try:
        access = access_for_request(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code,
                            content={"error": e.detail,
                                     "signin_required": e.status_code == 401})
    if access is not None and not access.all_units:
        wanted = [v for v in request.query_params.getlist("business_unit")
                  if str(v).strip()]
        for bu in wanted:
            # "ALL" is a superset request, and for a restricted user the
            # superset it means is *their* units — the tools that accept it
            # label every row with its own unit, so narrowing is honest.
            if str(bu).strip().upper() in {"ALL", "*"}:
                continue
            if not access.allows(bu):
                return JSONResponse(status_code=403,
                                    content={"error": access.refusal(bu)})
        if not wanted:
            # THE OMITTED PARAMETER IS THE DANGEROUS CASE. A request that
            # names no unit does not read nothing — it falls through to the
            # site's discovered default, which is chosen from the whole
            # installation and is very often a unit this person was never
            # granted. Measured before this existed: a CA001-only user
            # asking /api/trial-balance with no arguments got US001's
            # complete trial balance, 200 OK, no warning.
            #
            # So the default is resolved HERE, inside their reach, and
            # written into the request. Every handler then sees an explicit
            # unit and the payload names it, which is what makes the
            # narrowing visible rather than silent.
            try:
                mine = _default_unit_for(access)
            except SecurityError as e:
                return JSONResponse(status_code=403, content={"error": str(e)})
            request.scope["query_string"] = _with_unit(
                request.scope.get("query_string") or b"", mine)
    # Bind the caller for the handlers downstream. The unit CHECKS above
    # cover an argument the request names; this covers the answers that span
    # units, where there is no single argument to check — "ALL" above is
    # allowed through precisely because the tools were supposed to narrow it
    # to this person's units, and until now none of them did.
    #
    # A context variable rather than a parameter: on the chat path the tool
    # arguments are written by the MODEL, so a grant passed as an argument is
    # one the model can widen by typing a different value.
    with access_scope(access):
        return await call_next(request)


# How long a turn may hold its conversation before a NEW question stops
# queueing behind it and says so instead. The browser gives up on its own
# request at 180s; a turn still holding the lock past that has no client left
# to answer, so queueing behind it only spends the next question's 180s too.
# That is the cascade in the report: three questions in a row, each dying at
# 180s, because the first one's query was still running against the database.
_ABANDONED_AFTER = float(os.environ.get("PSTB_TURN_ABANDONED_SECONDS", "180"))
# A queued question waits this long for a turn that is still within its
# budget. Bounded so the wait plus the answer still fits inside the browser's
# patience rather than consuming all of it before work starts.
_QUEUE_WAIT = float(os.environ.get("PSTB_QUEUE_WAIT_SECONDS", "45"))


USER_COOKIE = "pstb_user"
row_security = RowSecurity(db, cfg)


def resolve_operator(request) -> str:
    """WHO is asking. The seam SSO replaces, and the only one.

    Today: the user ID this browser typed on the sign-in page, carried in
    a session cookie. Tomorrow: the subject the identity provider vouched
    for. Everything downstream asks row_security about the string this
    returns, so the swap is this function and the sign-in page — nothing
    that enforces anything has to move.

    It is worth being blunt about what today's version is: a typed user ID
    with no password identifies nobody. It scopes an honest session to the
    units PeopleSoft grants that user, which is what stops a wrong-unit
    answer reaching a screen by accident and stops the model wandering
    across units. It stops nobody who types someone else's ID.
    """
    return str(request.cookies.get(USER_COOKIE) or "").strip().upper()


def access_for_request(request):
    """The caller's business-unit reach, or None when security is off."""
    if not row_security.enabled:
        return None
    if request is None:  # noqa: SIM108
        # A direct in-process call (a test, a script) carries no browser
        # and therefore no identity. Refuse rather than assume: an
        # unidentified caller is exactly the case this feature exists for.
        raise HTTPException(
            status_code=401,
            detail="Sign in with your PeopleSoft user ID to see ledger data.")
    who = resolve_operator(request)
    if not who:
        raise HTTPException(
            status_code=401,
            detail="Sign in with your PeopleSoft user ID to see ledger data.")
    try:
        return row_security.access_for(who)
    except SecurityError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@dataclass
class _ProviderSession:
    """One provider history, scoped to one browser session and DB scope."""

    provider: object
    touched: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # What the turn holding this conversation is doing, so a question that
    # cannot get in can say WHY instead of timing out silently. busy_since
    # is per-TOOL (it names the query currently running); turn_since is the
    # whole turn, and it is what the abandoned-turn refusal measures — the
    # first cut measured busy_since, which every tool start reset, so a
    # multi-tool turn could hold the conversation for twenty minutes
    # without ever looking abandoned and the cascade the refusal exists to
    # stop simply came back.
    busy_since: float = 0.0
    busy_tool: str = ""
    turn_since: float = 0.0
    # Recent tool payloads as (tool_name, payload) pairs, so a follow-up
    # turn's restated figures ground against what this conversation already
    # fetched — and against the SYSTEM that produced them. The guard walk
    # also accepts a bare payload, which is what a worker replaced
    # mid-deploy will read from its predecessor's 1800s-TTL session.
    payloads: list = field(default_factory=list)

    def busy_for(self) -> float:
        return (time.monotonic() - self.busy_since) if self.busy_since else 0.0

    def turn_for(self) -> float:
        return (time.monotonic() - self.turn_since) if self.turn_since else 0.0

    def describe_busy(self) -> str:
        held = int(self.busy_for())
        turn = int(self.turn_for())
        if self.busy_tool:
            detail = f"{self.busy_tool} has been running for {held}s"
        else:
            detail = f"it has been running for {held}s"
        if turn > held:
            detail += f" ({turn}s into the question)"
        return detail


class _ProviderSessionStore:
    """Small, bounded in-process provider registry.

    Provider SDK objects contain mutable conversation history and are not safe
    to share.  The key includes the validated financial scope, so changing BU,
    ledger, year, or period always starts a separate context.  Expiry and a
    hard bound prevent abandoned browser tabs from growing memory forever.
    """

    def __init__(
        self,
        ttl_seconds: int = 30 * 60,
        max_entries: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.clock = clock
        self._entries: dict[tuple, _ProviderSession] = {}
        self._lock = threading.RLock()

    def _purge(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if now - entry.touched > self.ttl_seconds and not entry.lock.locked()
        ]
        for key in expired:
            self._entries.pop(key, None)

    def get_or_create(self, key: tuple, factory: Callable[[], object]) -> _ProviderSession:
        now = self.clock()
        with self._lock:
            self._purge(now)
            entry = self._entries.get(key)
            if entry is None:
                while len(self._entries) >= self.max_entries:
                    candidates = [
                        (candidate.touched, candidate_key)
                        for candidate_key, candidate in self._entries.items()
                        if not candidate.lock.locked()
                    ]
                    if not candidates:
                        break
                    self._entries.pop(min(candidates)[1], None)
                entry = _ProviderSession(provider=factory(), touched=now)
                self._entries[key] = entry
            entry.touched = now
            return entry

    async def reset_session(self, session_id: str) -> int:
        """Remove only one browser session; never clear another user's history.

        Detaching comes FIRST and is what Clear actually promises: the next
        question builds a fresh conversation with a fresh lock and does not
        queue behind whatever is still running. Resetting the old provider
        afterwards is hygiene on an object nobody can reach any more, so it
        is bounded — waiting on it unbounded meant Clear itself hung behind
        the stuck query, leaving no way out of a wedged conversation at all.
        """
        with self._lock:
            keys = [key for key in self._entries if key[0] == session_id]
            entries = [self._entries.pop(key) for key in keys]

        async def _release(entry) -> None:
            # A reset can arrive while a provider call is running in a worker
            # thread. Wait for that scoped turn before mutating its history.
            async with entry.lock:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(entry.provider.reset)

        async def _bounded(entry) -> None:
            try:
                await asyncio.wait_for(_release(entry), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass          # detached already; its history dies with it

        # Concurrently, so Clear's worst case is ~5 seconds, not 5 seconds
        # PER wedged conversation — a session with three stuck scopes made
        # the one escape hatch take fifteen seconds to answer.
        if entries:
            await asyncio.gather(*(_bounded(e) for e in entries))
        return len(entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_provider_sessions = _ProviderSessionStore(
    ttl_seconds=int(os.environ.get("PSTB_CHAT_SESSION_TTL_SECONDS", "1800")),
    max_entries=int(os.environ.get("PSTB_CHAT_MAX_SESSIONS", "100")),
)

_activity: dict = {}                    # session_id -> live turn activity
_activity_lock = threading.Lock()
_ACTIVITY_MAX_EVENTS = 60

# session_id -> the last turn's suggested next questions. Kept beside the
# session rather than inside the provider history on purpose: suggestions
# are a property of the RESULTS, not of the conversation, and a Clear that
# wipes the history must not leave a stale "you should look at C1004"
# pointing at evidence the page no longer shows.
_suggestions: dict = {}
_suggestions_lock = threading.Lock()
_SUGGESTION_SESSIONS = 200


def _suggestions_store(session_id: str, value: list) -> None:
    with _suggestions_lock:
        _suggestions[session_id] = value
        if len(_suggestions) > _SUGGESTION_SESSIONS:
            for key in list(_suggestions)[:len(_suggestions)
                                          - _SUGGESTION_SESSIONS // 2]:
                if key != session_id:
                    _suggestions.pop(key, None)


def _suggestions_for_turn(payloads, question: str, scope: dict) -> list:
    """Never let the follow-ups cost the answer.

    The person already has their result by the time this runs; a rule that
    trips over an unfamiliar payload shape must lose its suggestion and
    nothing else.
    """
    try:
        from ..suggest import suggestions_for
        return suggestions_for(
            payloads, question=question,
            business_unit=str((scope or {}).get("business_unit") or ""))
    except Exception as e:                        # noqa: BLE001
        print(f"[pstb] suggestions skipped: {type(e).__name__}: {e}",
              file=sys.stderr)
        return []


def _activity_begin(session_id: str, turn: str, phase: str = "") -> dict | None:
    """Claim the session's activity slot for ONE turn.

    Keyed by a turn token the browser mints per question, because keying it
    by session alone leaked the previous question's steps into the next
    one's spinner. Someone asked for an AR aging and watched it report
    "run_playbook playbook=close_readiness — running": that was the
    PREVIOUS turn's dangling event, still in the slot because the new turn
    had not reached the point where the slot was cleared. Anything that
    outlives its turn now writes into a slot that no longer belongs to it
    and is dropped.

    Returns the slot this claim displaced, so a request that is REFUSED —
    the busy 409, the queue timeout — can put the running turn's live
    display back instead of leaving the person watching a blank spinner
    while their first question is still genuinely working.
    """
    with _activity_lock:
        displaced = _activity.get(session_id)
        _activity[session_id] = {"turn": turn, "active": True, "events": [],
                                 "phase": phase, "started": time.time()}
        if len(_activity) > 200:            # forgotten sessions, bounded
            # Finished slots first — evicting by insertion order alone
            # threw away the longest-RUNNING turns' live display first,
            # which is exactly backwards.
            done = [k for k, s in _activity.items()
                    if not s.get("active") and k != session_id]
            stale = done + [k for k in _activity
                            if k != session_id and k not in done]
            for key in stale[:len(_activity) - 100]:
                _activity.pop(key, None)
        return displaced


def _activity_restore(session_id: str, my_turn: str,
                      slot: dict | None) -> None:
    """Put a displaced slot back after a refused claim.

    Only while the session still shows MY claim: if a third question has
    claimed the slot since, restoring would clobber it."""
    if slot is None or not slot.get("active"):
        return
    with _activity_lock:
        current = _activity.get(session_id)
        if current is not None and current.get("turn") == my_turn:
            _activity[session_id] = slot


def _activity_phase(session_id: str, turn: str, phase: str) -> None:
    """What the turn is doing BEFORE any tool runs.

    Scope validation, a queued turn ahead of this one, spawning a private
    answer engine, waiting on the model — all of it used to be a bare
    "Working…" because only tool calls were reported, and it is exactly the
    stretch where a slow turn looks stuck.
    """
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is not None and slot["turn"] == turn:
            slot["phase"] = phase


def _activity_add(session_id: str, turn: str, event: dict) -> None:
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is None or slot["turn"] != turn:
            return
        slot["events"].append({**event, "t": time.time()})
        del slot["events"][:-_ACTIVITY_MAX_EVENTS]


def _activity_done(session_id: str, turn: str) -> None:
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is None or slot["turn"] != turn:
            return
        slot["active"] = False
        slot["phase"] = ""
        # A turn that died between "running" and its result would otherwise
        # leave an event that claims to be running forever.
        for event in slot["events"]:
            if event.get("status") == "running":
                event["status"] = "failed"


_scope_cache: dict = {"expires": 0.0, "value": None, "refreshing": False}
_scope_cache_lock = threading.RLock()
# (bu, ledger, fy) -> (expires, last_period_with_data). The last query left
# on /api/meta's warm path; see the comment at its use.
_last_data_cache: dict = {}
# Freshness window. Was 60 seconds — which made every 61st second's visitor
# rebuild the whole catalog SYNCHRONOUSLY behind the lock, a minute-plus on a
# real instance. "Scope loading sometimes times out" was that exact person.
# A BU/ledger catalog changes approximately never intra-day, so staleness is
# cheap and waiting is not: past this window the STALE catalog is served
# instantly and one background thread refreshes it.
_SCOPE_CACHE_SECONDS = 900


def _scope_persist_path() -> Path:
    """Site-keyed file so a catalog from one database never leaks into
    another environment's process (dev refresh, config switch)."""
    import hashlib

    key = hashlib.sha256("|".join([
        cfg.db.backend, cfg.db.schema or "",
        cfg.db.oracle_dsn or cfg.db.sqlite_path or "",
    ]).encode()).hexdigest()[:16]
    path = cfg.resolve_path(f"logs/scope_cache_{key}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _persist_scope_catalog(value: dict) -> None:
    try:
        # Never downgrade the disk seed: an unverified catalog must not
        # overwrite a verified one. The boot prime and the background
        # refresh both persist, and the prime finishing SECOND — slow setup
        # tables, fast refresh — used to replace the refresh's verified
        # catalog with its unverified guess, which the next restart then
        # served as the seed.
        if value.get("verified") is False:
            existing = _load_persisted_scope_catalog()
            if existing is not None and existing.get("verified", True):
                return
        _scope_persist_path().write_text(json.dumps(value, default=str))
    except Exception:
        pass                     # a cache that cannot write is just a cache


def _load_persisted_scope_catalog() -> dict | None:
    """Last known catalog from disk — served STALE on the first request
    after a restart while a background refresh replaces it. Restarting the
    server used to reset discovery to zero, so the first visitor of the day
    paid the full minute; the catalog they get now may be yesterday's for a
    few seconds, which for a list of business units is the right trade."""
    try:
        raw = _scope_persist_path().read_text()
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("scopes") is not None:
            return value
    except Exception:
        pass
    return None


def _refresh_scope_catalog_async() -> None:
    def work() -> None:
        started = time.monotonic()
        try:
            value = engine.list_financial_scopes(include_activity=False)
        except Exception as e:                    # noqa: BLE001
            print(f"[pstb] scope catalog refresh failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            with _scope_cache_lock:
                _scope_cache["refreshing"] = False
            progress.end("scopes", ok=False, note=f"{type(e).__name__}: {e}")
            return
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with _scope_cache_lock:
            _scope_cache.update({
                "value": value,
                "expires": time.monotonic() + _SCOPE_CACHE_SECONDS,
                "refreshing": False,
            })
        _persist_scope_catalog(value)
        progress.end("scopes")
        print(f"[pstb] scope catalog refreshed in {elapsed_ms} ms",
              file=sys.stderr)

    threading.Thread(target=work, daemon=True,
                     name="scope-catalog-refresh").start()


def _prime_scope_catalog() -> None:
    """Warm the CHEAP catalog at boot. Only the cheap one.

    Discovery has two halves with wildly different costs. The setup reads
    are hundreds of rows and build in milliseconds anywhere; the ledger
    existence probes are batched DISTINCTs over the balance table, each
    able to eat the whole query timeout when the optimizer picks a bad
    plan. Priming BOTH at startup put that heavy half in direct
    competition with the first dashboard someone opened — same database,
    same Oracle session pool — and made every view slow for the first
    minutes of a restart, with nothing on screen to say why.

    So boot warms the unverified catalog and stops. Verification stays
    where it was: triggered by the page's own background /api/scopes call,
    once the person already has a usable screen.
    """
    def work() -> None:
        with _scope_cache_lock:
            if _scope_cache["value"] is not None:
                progress.end("scopes", note="served from the last run")
                return
        try:
            # setup_only: on a site where the setup records are not
            # granted, discovery falls back to per-BU probes and a DISTINCT
            # over PS_LEDGER — the expensive half this prime exists NOT to
            # run. Boot must not pay that on exactly the grant-limited
            # sites where it is slowest; the first browse view pays it
            # once, later, behind an honest "finding your business unit".
            built = engine.list_financial_scopes(include_activity=False,
                                                 verify_pairs=False,
                                                 setup_only=True)
        except Exception as e:                    # noqa: BLE001
            print(f"[pstb] scope catalog prime failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            progress.end("scopes", ok=False, note=f"{type(e).__name__}: {e}")
            return
        if not built.get("scopes"):
            progress.end("scopes", note="setup tables not readable here; "
                                        "discovery deferred to first use")
            return
        built["verified"] = False
        built["note"] = ((built.get("note") or "") +
                         " Scope pairs not yet verified against ledger "
                         "data; opening the page confirms them.").strip()
        cached = False
        with _scope_cache_lock:
            if _scope_cache["value"] is None:
                # expires=0.0 on purpose: unverified is stale the moment it
                # exists, so the first /api/scopes call replaces it.
                _scope_cache.update({"value": built, "expires": 0.0})
                cached = True
        # Persist only what was actually adopted: a prime that lost the
        # race to a real catalog must not overwrite it on disk either.
        if cached:
            _persist_scope_catalog(built)
        progress.end("scopes")

    progress.begin("scopes")
    threading.Thread(target=work, daemon=True,
                     name="scope-catalog-prime").start()


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_SCOPE_DISCOVERY_RE = re.compile(
    r"\b(list|what|which|available|all|exist)\b.*"
    r"\b(business\s+units|bus|bu'?s|ledgers|financial\s+scopes?|entities)\b",
    re.IGNORECASE,
)


class _ScopeRequired(ValueError):
    def __init__(self, detail: str, options: list[dict]):
        super().__init__(detail)
        self.detail = detail
        self.options = options


def _financial_scope_catalog(force: bool = False) -> dict:
    """The scope catalog: fresh if young, STALE-BUT-INSTANT if old.

    Nobody waits on a rebuild except the very first request of a
    deployment's life (no memory, no disk file). Stale requests get the
    previous catalog immediately and exactly one background thread
    refreshes; force=True (an explicit user refresh) still rebuilds
    synchronously because that user asked to wait for truth.
    """
    now = time.monotonic()
    with _scope_cache_lock:
        value = _scope_cache["value"]
        if not force and value is not None:
            if now >= _scope_cache["expires"] and not _scope_cache["refreshing"]:
                _scope_cache["refreshing"] = True
                _refresh_scope_catalog_async()
            return value
    # First-ever load: build the UNVERIFIED catalog — setup reads only,
    # milliseconds on any installation — because this is the one synchronous
    # build left, and if it could time out, the retry button would repeat
    # the same doomed build forever with nothing ever cached to serve stale.
    # The verified catalog (ledger existence probes and all) is what the
    # background refresh builds and replaces this with. An explicit
    # force=True refresh still builds verified synchronously: that user
    # asked to wait for truth.
    if force:
        built = engine.list_financial_scopes(include_activity=False)
    else:
        built = engine.list_financial_scopes(include_activity=False,
                                             verify_pairs=False)
        built["verified"] = False
        built["note"] = ((built.get("note") or "") +
                         " Scope pairs not yet verified against ledger "
                         "data; a background refresh is confirming them."
                         ).strip()
    with _scope_cache_lock:
        already_refreshing = _scope_cache["refreshing"]
        _scope_cache.update(
            {"value": built,
             # An unverified catalog is immediately stale on purpose, so the
             # next request triggers the verified background refresh.
             "expires": (time.monotonic() + _SCOPE_CACHE_SECONDS if force
                         else 0.0),
             "refreshing": already_refreshing}
        )
    _persist_scope_catalog(built)
    if not force:
        with _scope_cache_lock:
            if not _scope_cache["refreshing"]:
                _scope_cache["refreshing"] = True
                _refresh_scope_catalog_async()
    return built


def _visible_scopes(catalog: dict, access) -> list:
    """The catalog's units, narrowed to what this caller may see.

    Applied on the way OUT of the shared cache, never on the way in: the
    expensive discovery is the same for everyone and is built once, and a
    per-user catalog in a shared cache is how one person's reach ends up
    served to the next person who asks.
    """
    scopes = catalog.get("scopes") or []
    if access is None or getattr(access, "all_units", True):
        return scopes
    return [s for s in scopes if access.allows(s.get("business_unit"))]


def _warm_scope_catalog():
    """The cached catalog (stale is fine — it is a list of business units,
    not a balance), or None when this deployment has never discovered one.
    Never triggers discovery — that is the whole point of the async split."""
    with _scope_cache_lock:
        return _scope_cache["value"]


# A restart used to reset discovery to zero; seed from disk so the first
# request serves instantly and revalidates in the background.
_persisted = _load_persisted_scope_catalog()
if _persisted is not None:
    _scope_cache.update({"value": _persisted, "expires": 0.0})


def _scope_options(catalog: dict, business_unit: str = "") -> list[dict]:
    options: list[dict] = []
    for bu_scope in catalog.get("scopes") or []:
        bu = str(bu_scope.get("business_unit") or "").strip()
        if business_unit and bu != business_unit:
            continue
        for ledger_scope in bu_scope.get("ledgers") or []:
            last = ledger_scope.get("last_posted") or {}
            options.append(
                {
                    "business_unit": bu,
                    "descr": bu_scope.get("descr"),
                    "base_currency": bu_scope.get("base_currency"),
                    "ledger": str(ledger_scope.get("ledger") or "").strip(),
                    "fiscal_year": int(last.get("fiscal_year") or 0),
                    "period": int(last.get("period") or 0),
                    "fiscal_years": ledger_scope.get("fiscal_years") or [],
                }
            )
    return options


def _int_scope_value(raw: object, field_name: str) -> int:
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be an integer"
        ) from e


def _site_memory():
    """Site memory for prompt context. Read fresh each turn so an approval
    takes effect on the next question rather than after a restart."""
    from ..memory import SiteMemory
    return SiteMemory(cfg.resolve_path(
        getattr(cfg.tools, "site_memory", "site_memory.json")))


def _validated_scope(requested: object, catalog: Optional[dict] = None) -> dict:
    """Resolve and validate a client scope exclusively from DB-discovered values."""
    raw = requested if isinstance(requested, dict) else {}

    # A named secondary database is a complete scope of its own.  It has no
    # PeopleSoft business unit, ledger or accounting-period dimensions, and
    # forcing it through PS_LEDGER both mislabels the context and makes the
    # secondary unavailable whenever the primary is down.  Validate the
    # source before doing any PeopleSoft discovery and return only that hard
    # boundary.  An explicit ``default`` is preserved: in a multi-source UI it
    # means the person deliberately selected Finance, so an ad-hoc attempt to
    # reach another source must conflict rather than silently widen.
    source_supplied = "source" in raw or "db" in raw
    source = str(raw.get("source") or raw.get("db") or "").strip()
    if source_supplied:
        known = (engine.registry.names()
                 if engine.registry is not None else ["default"])
        resolved = (engine.registry.resolve_name(source)
                    if engine.registry is not None else "default")
        if resolved not in known:
            raise HTTPException(
                status_code=400,
                detail=(f"Unknown database source {source!r}. Configured: "
                        f"{', '.join(known)}."),
            )
        source = resolved
        if source != "default":
            return {"source": source}
        if not any(
            key in raw
            for key in ("business_unit", "bu", "ledger", "fiscal_year",
                        "fy", "period", "per")
        ):
            # The multi-source selector can explicitly pin Finance before a
            # BU is chosen. Keep that database lock, but do not invent a
            # financial scope from configured defaults.
            return {"source": "default"}

    catalog = catalog or _financial_scope_catalog()
    all_options = _scope_options(catalog)
    if not all_options:
        raise HTTPException(
            status_code=503,
            detail="No business-unit and ledger combinations were found in PS_LEDGER.",
        )

    bu = str(raw.get("business_unit") or raw.get("bu") or "").strip()
    if not bu:
        business_units = sorted({o["business_unit"] for o in all_options})
        if len(business_units) != 1:
            raise _ScopeRequired(
                "Choose a business unit before asking a financial-data question.",
                all_options,
            )
        bu = business_units[0]

    bu_options = [o for o in all_options if o["business_unit"] == bu]
    if not bu_options:
        raise HTTPException(
            status_code=400,
            detail=f"Business unit {bu!r} is not present in the connected PS_LEDGER.",
        )

    ledger = str(raw.get("ledger") or "").strip()
    if not ledger:
        if len(bu_options) != 1:
            raise _ScopeRequired(
                f"Choose a ledger for business unit {bu}.", bu_options
            )
        ledger = bu_options[0]["ledger"]

    matches = [o for o in bu_options if o["ledger"] == ledger]
    if not matches:
        known = ", ".join(sorted({o["ledger"] for o in bu_options}))
        raise HTTPException(
            status_code=400,
            detail=f"Ledger {ledger!r} is not valid for {bu}. Available: {known}.",
        )
    discovered = matches[0]

    if not discovered["fiscal_year"] or not discovered["period"]:
        latest_fy, latest_period = engine.last_posted_period(bu, ledger)
        discovered = {
            **discovered,
            "fiscal_year": latest_fy,
            "period": latest_period,
        }

    # An explicitly CLEARED time field (key present, value null/"any") means
    # "no time constraint" — the tools then use their own current-period
    # defaults and the question itself decides. An ABSENT key still falls
    # back to the discovered latest posted period.
    def _cleared(*keys) -> bool:
        for k in keys:
            if k in raw:
                return str(raw.get(k) or "").strip().lower() in ("", "any", "none")
        return False

    fy_cleared = _cleared("fiscal_year", "fy")
    period_cleared = _cleared("period", "per")

    fiscal_year = _int_scope_value(
        raw.get("fiscal_year", raw.get("fy")), "fiscal_year"
    ) or (None if fy_cleared else discovered["fiscal_year"])
    period = _int_scope_value(raw.get("period", raw.get("per")), "period")
    if not period:
        period = None if period_cleared else discovered["period"]
    years = discovered.get("fiscal_years") or []
    if fiscal_year is not None and len(years) >= 2:
        first, last = int(years[0]), int(years[-1])
        if fiscal_year < first or fiscal_year > last:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Fiscal year {fiscal_year} is outside the data range "
                    f"{first}-{last} for {bu}/{ledger}."
                ),
            )
    if fiscal_year is not None and (fiscal_year < 1 or fiscal_year > 9999):
        raise HTTPException(
            status_code=400,
            detail="fiscal_year must be between 1 and 9999",
        )
    # PeopleSoft calendars can have 13 regular periods and site-specific
    # adjustment/closing periods. Do not impose a 12-period calendar in the
    # chat boundary; the financial tool remains the source of truth.
    if period is not None and (period < 1 or period > 999):
        raise HTTPException(
            status_code=400,
            detail="period must be between 1 and 999",
        )
    scope = {"business_unit": bu, "ledger": ledger}
    if source_supplied:
        scope["source"] = source or "default"
    # Omit a cleared field entirely: apply_request_scope only injects what is
    # present, so an omitted period leaves each tool on its own default.
    if fiscal_year is not None:
        scope["fiscal_year"] = fiscal_year
    if period is not None:
        scope["period"] = period
    # The chip's period reached the LEDGER tools and stopped there. AR,
    # Billing and AP do not filter on FISCAL_YEAR/ACCOUNTING_PERIOD — they
    # take a DATE — so with FY2025 P12 selected the trial balance showed
    # 2025 and the receivables beside it showed today, with nothing on
    # screen admitting the two were different moments. Resolve the period
    # to its end date here, where the calendar is actually reachable.
    end = _period_end_date(fiscal_year, period)
    if end:
        scope["as_of_date"] = end
    return scope


def _period_end_date(fiscal_year, period) -> str:
    """The last day of a fiscal period, or "" when the calendar cannot say.

    Best effort on purpose: a site whose calendar record is not granted
    keeps the behaviour it has today (each tool's own default) rather than
    losing the whole scope. Cheap on repeat — list_periods is cached.
    """
    if not fiscal_year or not period:
        return ""
    try:
        for row in engine.list_periods(int(fiscal_year)).get("periods") or []:
            if int(row.get("period") or 0) == int(period):
                return str(row.get("end_dt") or "")[:10]
    except Exception:                       # noqa: BLE001
        return ""
    return ""


def _question_requires_scope(message: str) -> bool:
    from ..guards import evidence_intent

    return evidence_intent(message) in {"data", "mixed"}


def _is_scope_catalog_question(message: str) -> bool:
    from ..guards import requires_financial_evidence

    return bool(
        _SCOPE_DISCOVERY_RE.search(message)
        and not requires_financial_evidence(message)
    )


def _provider_key(
    session_id: str, provider_name: str, scope: Optional[dict]
) -> tuple:
    if not scope:
        return (
            session_id, provider_name, "__KNOWLEDGE_ONLY__", "", "", 0, 0
        )
    return (
        session_id,
        provider_name,
        # A named source is a hard database boundary, not a display label.
        # Keeping it out of this key reused provider history and prior tool
        # payloads after a user switched between Finance and a secondary
        # database with the same BU/ledger chips still underneath.
        str(scope.get("source") or "default").strip().lower(),
        scope.get("business_unit") or "",
        scope.get("ledger") or "",
        # Time fields are optional: a cleared year/period means "any", and
        # the session key must stay stable rather than raising KeyError.
        scope.get("fiscal_year") or 0,
        scope.get("period") or 0,
    )


def _session_id(payload: dict) -> str:
    session_id = str((payload or {}).get("session_id") or "").strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "session_id is required and must be 8-128 letters, numbers, "
                "dots, colons, underscores, or hyphens"
            ),
        )
    return session_id


def _guard(fn, **kw):
    try:
        return fn(**kw)
    except (EngineError, DbError, ReportError, ARError, ExportError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # surface the reason instead of a bare 500
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta(request: Request = None):
    d = cfg.defaults
    # /api/meta answers before sign-in (the page needs it to render the
    # sign-in form), so a caller with no identity gets the catalog EMPTY
    # rather than complete. Serving the full unit list to an unsigned
    # session would hand out the thing the feature exists to withhold.
    try:
        meta_access = access_for_request(request)
        meta_signed_in = True
    except HTTPException:
        meta_access, meta_signed_in = None, not row_security.enabled
    out = {
        "defaults": {
            "business_unit": d.business_unit,
            "ledger": d.ledger,
            "base_currency": d.base_currency,
            "adjustment_periods": d.adjustment_periods,
            "account_tree": d.account_tree,
        },
        "build": _build_info(),
        # The databases this deployment can reach. The selector needs them
        # to exist before anyone can pick one, and the single-source case
        # (every deployment today) gets a one-item list the UI hides.
        "sources": (engine.registry.describe()
                    if engine.registry is not None else []),
        "mcp_session": {"shared": _MCP["session"] is not None,
                        "state": _MCP.get("state", "starting"),
                        **({"error": _MCP["error"]} if _MCP["error"]
                           else {})},
        "backend": cfg.db.backend,
        "use_views": cfg.db.use_views,
        "wiki": getattr(wiki, "provider_name", None),
        "wiki_demo": bool(
            wiki is not None
            and getattr(wiki, "provider_name", "") == "localdocs"
            and (wiki.health() or {}).get("is_bundled_demo_content")
        ),
        "llm": {"provider": cfg.llm.provider,
                "model": provider_model(cfg)},
        "procurement": {
            "authority": (
                "coupa" if getattr(getattr(cfg, "coupa", None),
                                   "po_receipt_authority", False) is True
                else "peoplesoft"
            ),
            "coupa_receipt_events": bool(
                getattr(getattr(cfg, "coupa", None),
                        "po_receipt_authority", False) is True),
        },
        # The page reads this to decide whether to render the sign-in form
        # before anything else. is_authentication is stated, not implied:
        # a user ID with no password identifies nobody.
        "security": {"enabled": row_security.enabled,
                     "signed_in": meta_signed_in,
                     "oprid": (meta_access.oprid if meta_access else ""),
                     "all_units": (meta_access.all_units
                                   if meta_access else True),
                     "is_authentication": False},
        "raw_sql": cfg.tools.allow_raw_sql,
    }
    # PS_LEDGER is the authority for selectable financial scopes.  The GL
    # business-unit setup table is useful metadata, but it must not collapse the
    # UI to a configured sample BU when that table is unavailable to a read-only
    # service account.
    # /api/meta must return INSTANTLY. Building the catalog here meant the
    # page sat on its first paint for as long as discovery took — a minute on
    # a real WAN — with nothing on screen to explain it. The catalog is served
    # only when a previous request already warmed the cache; otherwise the
    # client fetches /api/scopes in the background and fills the bar in.
    warm = _warm_scope_catalog()
    if warm is not None and not meta_signed_in:
        warm = None                 # signed out: no catalog, no unit names
    if warm is not None:
        out["financial_scopes"] = _visible_scopes(warm, meta_access)
        out["business_units"] = [
            {
                "business_unit": item.get("business_unit"),
                "descr": item.get("descr"),
                "base_currency": item.get("base_currency"),
            }
            for item in out["financial_scopes"]
        ]
        out["scopes_ready"] = True
        # The boot prime warms an UNVERIFIED catalog — instant everywhere,
        # but it may offer a unit that setup knows and the ledger has never
        # been posted to. The page still fetches /api/scopes in the
        # background on this flag, which is what triggers verification; it
        # just does so with a populated bar rather than an empty one.
        out["scopes_verified"] = bool(warm.get("verified", True))
    else:
        out["financial_scopes"] = []
        out["business_units"] = []
        out["scopes_ready"] = False
        out["scopes_verified"] = False
    # The DEFAULT business unit is discovered from the database, validated
    # against config — but discovering it is not free and opening the page
    # does not need it. effective_defaults() falls through to
    # _ledger_scope_pairs(), whose verification probes PS_LEDGER, and
    # last_posted_period() is two MIN/MAX aggregates over the same table:
    # the slow query class here. Every cold page load paid for all of it
    # before the first paint.
    #
    # And paid it for almost nothing. The browse views that read this scope
    # bar are hidden (nav is display:none — Ask is the product), and the
    # chat's scope is the one the person picks in the chooser, which comes
    # from the catalog above. So this is served ONLY from caches something
    # else already warmed. Cold, the page opens on config values flagged
    # scope_ready:false, and whoever actually needs the discovered scope —
    # a browse view — fetches /api/scope and waits for it then.
    eff = engine.warm_effective_defaults()
    if eff is not None:
        # The cache read happens BEFORE the step is declared finished —
        # settling the bar first showed "done" over a page still waiting.
        posted = engine.warm_last_posted_period(eff["business_unit"],
                                                eff["ledger"])
        progress.end("defaults", note="served from cache")
    else:
        progress.skip("defaults", note="not needed to open the page")
        posted = None
    if eff is not None and posted is not None:
        fy0, per0 = posted
        out["scope"] = {
            "business_unit": eff["business_unit"],
            "ledger": eff["ledger"],
            "ledgers": eff["ledgers"] or [eff["ledger"]],
            "fiscal_year": fy0,
            "period": per0,
            "max_regular_period": engine._max_regular_period(fy0),
            "discovered": eff["discovered"],
            "notes": eff["notes"],
        }
        out["ledgers"] = out["scope"]["ledgers"]
        out["scope_ready"] = True
        if not any(b.get("business_unit") == eff["business_unit"]
                   for b in out["business_units"]):
            out["business_units"].append(
                {"business_unit": eff["business_unit"], "descr": "(discovered)"})
        progress.end("period", note="served from cache")
    else:
        out["scope"] = {"business_unit": d.business_unit, "ledger": d.ledger,
                        "ledgers": [d.ledger], "fiscal_year": 0, "period": 0,
                        "max_regular_period": 12, "discovered": False,
                        "notes": ["Configured defaults. The database-verified "
                                  "business unit loads when a view needs it."]}
        out["ledgers"] = [d.ledger]
        out["scope_ready"] = False
        progress.skip("period", note="not needed to open the page")

    # The calendar lookup is a small setup table, not the ledger, so it stays.
    # It is what puts a sensible year and period in the bar with no scope.
    try:
        cur = engine.resolve_period("")
        out["current"] = {"fiscal_year": cur["fiscal_year"], "period": cur["period"]}
    except Exception:
        out["current"] = {"fiscal_year": dt.date.today().year, "period": 12}

    # The calendar's current period may have no postings yet (early in a month,
    # or before close). Opening on an empty screen reads as "broken", so tell
    # the UI the newest period that actually has activity — but only when the
    # scope it would be measured against is already known. Cold, this was a
    # third MIN/MAX over the ledger for a number the chat never reads.
    #
    # Cached with the scope TTL: this was the last query left on the WARM
    # path — an endpoint documented "must return INSTANTLY" was issuing a
    # PS_LEDGER aggregate, the slow query class here, on every page load
    # forever once the caches were warm. Postings move, so it expires; a
    # page load within the window pays zero ledger queries.
    out["last_period_with_data"] = out["current"]["period"]
    if out["scope_ready"]:
        key = (out["scope"]["business_unit"], out["scope"]["ledger"],
               out["current"]["fiscal_year"])
        with _scope_cache_lock:
            hit = _last_data_cache.get(key)
        if hit and time.monotonic() < hit[0]:
            out["last_period_with_data"] = hit[1]
        else:
            try:
                rows, _ = db.query(
                    query_sql.scope_last_regular_period(
                        db, engine._adj_periods()),
                    {"bu": key[0], "led": key[1], "fy": key[2]},
                    max_rows=1,
                )
                p = rows[0]["last_period"] if rows else None
                if p is not None:
                    out["last_period_with_data"] = int(p)
                with _scope_cache_lock:
                    _last_data_cache[key] = (
                        time.monotonic() + _SCOPE_CACHE_SECONDS,
                        out["last_period_with_data"])
                    if len(_last_data_cache) > 200:
                        _last_data_cache.clear()
            except Exception:
                pass
    return out


@app.get("/api/session")
def whoami(request: Request = None):
    """Who this browser is signed in as, and what that reaches.

    Always 200 — the page uses this to decide whether to show the sign-in
    form, so a 401 here would be the page asking itself a question it
    cannot answer.
    """
    if not row_security.enabled:
        return {"security_enabled": False, "signed_in": True,
                "oprid": "", "units": [], "all_units": True,
                "detail": "Business-unit security is off for this deployment."}
    who = resolve_operator(request)
    if not who:
        return {"security_enabled": True, "signed_in": False, "oprid": "",
                "units": [], "all_units": False,
                "detail": "Sign in with your PeopleSoft user ID.",
                "is_authentication": False}
    try:
        access = row_security.access_for(who)
    except SecurityError as e:
        return {"security_enabled": True, "signed_in": False, "oprid": who,
                "units": [], "all_units": False, "error": str(e),
                "is_authentication": False}
    return {"security_enabled": True, "signed_in": True, "oprid": access.oprid,
            "units": sorted(access.units), "all_units": access.all_units,
            "privileged": access.privileged, "source": access.source,
            "detail": access.detail, "summary": access.describe(),
            "is_authentication": False}


@app.post("/api/signin")
def signin(payload: dict, request: Request = None):
    """Adopt a PeopleSoft user ID for this browser session.

    Deliberately not called 'login': there is no password and nothing is
    verified about the person. What it does is bind the session to a user
    ID so PeopleSoft's own row security can be applied to it — and the
    response says so, because a page that looks like a login while
    checking nothing teaches people it is one.
    """
    if not row_security.enabled:
        raise HTTPException(
            status_code=400,
            detail="Business-unit security is off (security.enabled: false); "
                   "there is nothing to sign in to.")
    who = str((payload or {}).get("oprid") or "").strip().upper()
    if not who or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,30}", who):
        raise HTTPException(
            status_code=400,
            detail="Enter a PeopleSoft user ID (letters, numbers, '_', '.', "
                   "'-'; up to 30 characters).")
    try:
        access = row_security.access_for(who)
    except SecurityError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    body = {"ok": True, "oprid": access.oprid,
            "units": sorted(access.units), "all_units": access.all_units,
            "privileged": access.privileged, "summary": access.describe(),
            "detail": access.detail,
            "note": ("This is a scope selector, not a login — no password "
                     "was asked for and none was checked.")}
    response = JSONResponse(content=body)
    # Session-scoped: closing the browser ends it, like the console's
    # confirmation. httponly so page script cannot read or forge it.
    response.set_cookie(USER_COOKIE, access.oprid, httponly=True,
                        samesite="strict", path="/")
    return response


@app.post("/api/signout")
def signout():
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(USER_COOKIE, path="/")
    return response


@app.get("/api/boot")
def boot_progress():
    """Which startup step the server is on RIGHT NOW.

    Polled by the page while its own /api/meta call is in flight, so a
    forty-second wait reads as "Finding the last posted period — 38s"
    instead of a logo animation that is indistinguishable from a hang.

    Deliberately touches nothing but an in-memory snapshot: an endpoint
    whose job is to explain a slow database must not be able to block on
    one. /api/meta is a sync def and therefore runs in FastAPI's
    threadpool, so this answers while that is still running.
    """
    snap = progress.snapshot()
    # error included so the page's post-boot watcher — which learns of a
    # degradation from THIS endpoint, since /api/meta is fetched exactly
    # once at first paint, usually while the engine is still starting —
    # can show the diagnosed cause, not just the fact of it.
    snap["mcp_session"] = {"state": _MCP.get("state", "starting"),
                           "shared": _MCP["session"] is not None,
                           "error": (_MCP.get("error") or "")[:600]}
    return snap


@app.get("/api/trial-balance")
def trial_balance(
    business_unit: str = "", ledger: str = "", fiscal_year: int = 0, period: int = 0,
    group_by: str = "", account: str = "", dept: str = "",
    include_adjustments: bool = False, max_rows: int = 500,
):
    return _guard(
        engine.trial_balance, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period, group_by=group_by, account=account,
        dept=dept, include_adjustments=include_adjustments, max_rows=max_rows,
    )


@app.get("/api/account/{account}")
def account_detail(
    account: str, business_unit: str = "", ledger: str = "",
    fiscal_year: int = 0, through_period: int = 0, dept: str = "",
):
    return _guard(
        engine.account_balance, account=account, business_unit=business_unit,
        ledger=ledger, fiscal_year=fiscal_year, through_period=through_period, dept=dept,
    )


@app.get("/api/journals")
def journals(
    account: str, period: int, business_unit: str = "", ledger: str = "",
    fiscal_year: int = 0, dept: str = "", limit: int = 200,
):
    return _guard(
        engine.drill_to_journals, account=account, period=period,
        business_unit=business_unit, ledger=ledger, fiscal_year=fiscal_year,
        dept=dept, limit=limit,
    )


@app.get("/api/integrity")
def integrity(business_unit: str = "", ledger: str = "", fiscal_year: int = 0, period: int = 0):
    return _guard(
        engine.tb_integrity_check, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period,
    )


@app.get("/api/diagnostics")
def diagnostics(include_timings: int = 0):
    """Site health: stats age, hot-table indexes, optional input timings.

    quick mode touches catalog views only; include_timings=1 runs the real
    close-readiness playbook to measure its inputs and can take minutes on
    a slow instance — the GUI keeps it behind its own explicit button.
    """
    from pstb import diagnostics as _diag
    from pstb.connectors import coupa as _coupa_mod
    return _guard(_diag.run, db=engine.db, engine=engine,
                  include_timings=bool(include_timings),
                  connectors=[_coupa_mod.from_env(cfg=cfg)])


@app.get("/api/question-report")
def question_report():
    """Deterministic what-to-optimize-next report over the question log."""
    from pstb import qlog_report as _qr
    if not qlog.path:
        return {"turns": 0, "failed": 0, "flags": {}, "tools": [],
                "repeat_failures": [], "recent_failed": [], "suggestions": [],
                "note": "question logging is not configured"}
    r = _guard(_qr.analyze, path=qlog.path)
    if isinstance(r, dict) and "error" not in r:
        r["text"] = _qr.report_text(r)
    return r


@app.post("/api/export")
def export_csv(payload: dict, request: Request = None):
    """Full-population CSV for one result card.

    The browser holds a display-capped preview; this re-runs the same tool
    server-side at the export ceiling so the file has the whole row set,
    not the page. Tools that cannot return more rows fall back to
    exporting the payload the caller already has, and the response says
    which happened.
    """
    from .. import export as _export
    from ..connectors import coupa as _coupa_mod
    from ..connectors import psquery_api as _qas_mod
    from ..psquery import QueryCatalog

    def _qas_from_config():
        try:
            target = (QueryCatalog(engine).integration_endpoints()
                      or {}).get("target_location") or ""
        except Exception:
            target = ""
        return _qas_mod.from_config(cfg, target)

    from ..modules import ModulePacks

    body = payload or {}
    tool = str(body.get("tool") or "")
    if not tool:
        raise HTTPException(status_code=400, detail="tool is required")
    # The unit rides in the BODY here, where the query-string gate cannot
    # see it — and export re-runs the tool at the full population ceiling,
    # so an unchecked one hands over more rows than the screen ever showed.
    access = access_for_request(request)
    if access is not None and not access.all_units:
        args = body.get("args") or {}
        named = str(args.get("business_unit") or "").strip()
        if named and named.upper() not in {"ALL", "*"} and not access.allows(named):
            raise HTTPException(status_code=403, detail=access.refusal(named))
        if not named:
            args = dict(args)
            args["business_unit"] = _default_unit_for(access)
            body = dict(body, args=args)
    registry = _export.build_registry(
        engine=engine, ar=ar, modules=ModulePacks(engine),
        report_runner=report_runner, coupa=_coupa_mod.from_env(cfg=cfg),
        qas=_qas_from_config())
    out = _guard(_export.export, tool=tool, args=body.get("args") or {},
                 registry=registry, payload=body.get("result"))
    return Response(
        content=out["csv"], media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{out["filename"]}"',
            "X-Export-Rows": str(out["rows"]),
            "X-Export-Truncated": "1" if out["truncated"] else "0",
            "X-Export-Rerun": "1" if out["rerun"] else "0",
            "X-Export-Note": out["note"],
            "Access-Control-Expose-Headers":
                "X-Export-Rows, X-Export-Truncated, X-Export-Rerun, "
                "X-Export-Note, Content-Disposition",
        })


@app.get("/api/rollup")
def rollup(
    business_unit: str = "", ledger: str = "", fiscal_year: int = 0,
    period: int = 0, tree_name: str = "", level: int = 2,
):
    return _guard(
        engine.rollup_trial_balance, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period, tree_name=tree_name, level=level,
    )


@app.get("/api/compare")
def compare(
    business_unit: str = "", ledger: str = "", fiscal_year: int = 0, period: int = 0,
    vs_fiscal_year: int = 0, vs_period: int = 0, top: int = 40, min_abs_change: float = 0.0,
):
    return _guard(
        engine.compare_trial_balance, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period, vs_fiscal_year=vs_fiscal_year,
        vs_period=vs_period, top=top, min_abs_change=min_abs_change,
    )


@app.get("/api/scopes")
def scopes_catalog(force: bool = False, request: Request = None):
    """Business-unit / ledger catalog, built on demand.

    Split out of /api/meta so the page can paint immediately and fill this in
    when it arrives. Safe to call repeatedly: it is cached.

    The CACHE is shared and the VIEW is per-user: discovery is expensive and
    identical for everyone, so it is built once and then narrowed to the
    caller's units on the way out. Caching a filtered catalog would serve
    one person's reach to the next person who asked.
    """
    access = access_for_request(request)

    def _build() -> dict:
        catalog = _financial_scope_catalog(force=force)
        scopes = _visible_scopes(catalog, access)
        return {
            "scopes": scopes,
            "business_units": [
                {"business_unit": s.get("business_unit"),
                 "descr": s.get("descr"),
                 "base_currency": s.get("base_currency")}
                for s in scopes
            ],
            "ready": True,
            # False while this response carries the boot prime's unverified
            # guess. The page re-asks until this flips — without the flag
            # it marked scopes_ready on the stale catalog and the verified
            # one, built minutes later, never reached anyone until reload.
            "verified": bool(catalog.get("verified", True)),
        }
    return _guard(_build)


@app.get("/api/scope")
def scope_for(business_unit: str = "", ledger: str = "", request: Request = None):
    """Ledgers and last posted period for a business unit — feeds the cascading
    scope bar so changing BU repopulates ledger/year/period from real data."""
    access = access_for_request(request)

    def _default_unit() -> str:
        """The discovered default, narrowed to what this caller may see.

        Called with no business_unit, this endpoint discovers the site's
        default — which is a real unit chosen from the whole installation,
        so for a restricted user it can easily be one they were never
        granted. Handing it back would put another unit's name in their
        scope bar and their next query behind a 403 they did not cause.
        """
        discovered = engine.effective_defaults()["business_unit"]
        if access is None or access.allows(discovered):
            return discovered
        mine = sorted(access.units)
        if not mine:
            raise EngineError(access.refusal(discovered))
        return mine[0]

    def _scope(business_unit: str, ledger: str) -> dict:
        bu = (business_unit or "").strip() or _default_unit()
        leds = engine.list_ledgers(bu)
        ledgers = leds.get("ledgers") or []
        led = (ledger or "").strip()
        if not led or led not in ledgers:
            led = next((l for l in ledgers if l.upper() == "ACTUALS"),
                       ledgers[0] if ledgers else engine.effective_defaults()["ledger"])
        # Fiscal years that actually hold data, so the scope editor offers
        # real choices instead of only the latest one.
        #
        # For THIS pair, by name. It used to build the activity catalog for
        # the WHOLE installation and then filter down to the one row it
        # wanted — and activity costs two MIN/MAX queries against PS_LEDGER
        # per pair, the slow query class here. On a site with a few hundred
        # BU/ledger pairs that is several hundred ledger queries to answer a
        # question about one of them, issued every time the scope bar
        # changed, all of it competing for the same eight-session Oracle
        # pool. Reported as "many queuing up in ledger".
        #
        # ONE cached call now. The first cut of this fix said "two queries"
        # while still calling last_posted_period beside the bounds lookup —
        # the same two MIN/MAX statements, issued twice. scope_period_details
        # answers both questions from one pass and feeds the posted-period
        # cache, so repeating the scope change costs zero ledger queries.
        try:
            bounds, last_posted = engine.scope_period_details(bu, led)
            years = [int(y) for y in bounds]
        except Exception:
            bounds, last_posted, years = [], None, []
        if last_posted is not None:
            fy, per = last_posted["fiscal_year"], last_posted["period"]
        else:
            fy, per = engine.last_posted_period(bu, led)
        if fy and fy not in years:
            years.append(fy)
        return {"business_unit": bu, "ledger": led, "ledgers": ledgers,
                "fiscal_year": fy, "period": per,
                "fiscal_years": sorted(years, reverse=True),
                "max_regular_period": engine._max_regular_period(fy),
                **({"scope_status": leds["scope_status"], "detail": leds.get("detail")}
                   if "scope_status" in leds else {})}
    return _guard(_scope, business_unit=business_unit, ledger=ledger)


@app.get("/api/reports")
def reports_list():
    return _guard(report_runner.list_reports)


@app.get("/api/report")
def report_run(
    name: str, business_unit: str = "", ledger: str = "",
    fiscal_year: int = 0, period: int = 0, include_adjustments: bool = False,
):
    return _guard(
        report_runner.run, report=name, business_unit=business_unit,
        ledger=ledger, fiscal_year=fiscal_year, period=period,
        include_adjustments=include_adjustments,
    )


@app.get("/api/ar/aging")
def ar_aging(business_unit: str = "", as_of_date: str = "",
             customer_id: str = "", detail: bool = False):
    return _guard(ar.aging, business_unit=business_unit, as_of_date=as_of_date,
                  customer_id=customer_id, detail=detail)


@app.get("/api/ar/customer")
def ar_customer(customer: str, business_unit: str = "", as_of_date: str = ""):
    return _guard(ar.customer, customer=customer, business_unit=business_unit,
                  as_of_date=as_of_date)


@app.get("/api/customer-360")
def customer_360(cust_id: str, business_unit: str = "",
                 include_family: bool = True, months: int = 12,
                 as_of_date: str = ""):
    return _guard(relationships.customer_financial_360, cust_id=cust_id,
                  business_unit=business_unit, include_family=include_family,
                  months=months, as_of_date=as_of_date)


@app.get("/api/vendor-network")
def vendor_network_view(vendor_id: str, business_unit: str = "",
                        include_family: bool = True, months: int = 12,
                        as_of_date: str = ""):
    return _guard(vendor_network.vendor_payables_network,
                  vendor_id=vendor_id, business_unit=business_unit,
                  include_family=include_family, months=months,
                  as_of_date=as_of_date)


@app.get("/api/match-exceptions")
def match_exceptions_view(business_unit: str = "", months: int = 12,
                          as_of_date: str = ""):
    return _guard(procurement.match_exceptions, business_unit=business_unit,
                  months=months, as_of_date=as_of_date)


@app.get("/api/procurement-chain")
def procurement_chain_view(reference: str, business_unit: str = "",
                           as_of_date: str = ""):
    return _guard(procurement.procurement_chain, reference=reference,
                  business_unit=business_unit, as_of_date=as_of_date)


@app.get("/api/entity-network")
def entity_network_view(entity: str, kind: str = "",
                        business_unit: str = "", limit: int = 40):
    return _guard(entity_graph.neighbourhood, entity=entity, kind=kind,
                  business_unit=business_unit, limit=limit)


@app.get("/api/concentration")
def concentration_view(kind: str = "customer", by: str = "",
                       business_unit: str = "", limit: int = 10):
    return _guard(entity_graph.concentration, kind=kind, by=by,
                  business_unit=business_unit, limit=limit)


@app.get("/api/entity-connection")
def entity_connection_view(source: str, target: str,
                           business_unit: str = "", hops: int = 3):
    return _guard(entity_graph.connection, source=source, target=target,
                  business_unit=business_unit, hops=hops)


@app.get("/api/entity-graph")
def entity_graph_view():
    return _guard(entity_graph.describe)


@app.get("/api/process")
def process_view(question: str, hops: int = 3, limit: int = 40):
    return _guard(process_graph.trace, question=question, hops=hops,
                  limit=limit)


@app.get("/api/process-graph")
def process_graph_view():
    return _guard(process_graph.describe)


@app.get("/api/vendors")
def vendors_search(query: str = "", limit: int = 25,
                   business_unit: str = ""):
    return _guard(modules.search_vendors, query=query, limit=limit,
                  business_unit=business_unit)


@app.get("/api/ar/customers")
def ar_customers(
    query: str = "", limit: int = 25, business_unit: str = ""
):
    return _guard(
        ar.search_customers,
        query=query,
        limit=limit,
        business_unit=business_unit,
    )


@app.get("/api/billing")
def billing(business_unit: str = "", days_stuck: int = 5, as_of_date: str = ""):
    return _guard(ar.billing_workbench, business_unit=business_unit,
                  days_stuck=days_stuck, as_of_date=as_of_date)


@app.get("/api/accounts")
def accounts(query: str = "", account_type: str = "", limit: int = 300):
    return _guard(engine.search_accounts, query=query, account_type=account_type, limit=limit)


@app.get("/api/wiki/health")
def wiki_health():
    if wiki is None:
        return {"provider": None, "connected": False,
                "verdict": "No wiki provider configured."}
    try:
        return wiki.health()
    except Exception as e:
        return {"provider": getattr(wiki, "provider_name", None),
                "connected": False, "error": str(e)}


@app.get("/api/wiki/lookup")
def wiki_lookup(question: str, max_pages: int = 3, max_passages: int = 6):
    if wiki is None:
        raise HTTPException(status_code=503, detail="No wiki provider configured")
    from ..wiki import lookup as _lookup
    try:
        return _lookup(wiki, question, max_pages=max_pages,
                       max_passages=max_passages)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/wiki/search")
def wiki_search(query: str, limit: int = 8):
    if wiki is None:
        raise HTTPException(status_code=503, detail="No wiki provider configured")
    try:
        return {"provider": wiki.provider_name, "results": wiki.search(query, limit)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/wiki/page")
def wiki_page(page_id: str):
    if wiki is None:
        raise HTTPException(status_code=503, detail="No wiki provider configured")
    try:
        return wiki.get_page(page_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/chat")
async def chat(payload: dict, request: Request = None):
    """Run one governed chat turn in a browser-session + DB-scope context."""
    # Row security is resolved ONCE per turn and handed to the agent loop,
    # so every tool call the model makes is checked against the same
    # answer. Resolving it per call would let a security change land
    # mid-turn and produce an answer assembled under two different rules.
    access = access_for_request(request)
    message = (payload or {}).get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    session_id = _session_id(payload)
    if "scope" not in payload:
        raise HTTPException(status_code=400, detail="scope is required")
    provider_name = str(
        (payload or {}).get("provider") or cfg.llm.provider
    ).strip().lower()
    if provider_name not in PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"provider must be one of {', '.join(PROVIDERS)}")

    # Claim the activity slot HERE, at the top of the request, not deep
    # inside after the scope lookup, the engine setup and the queue wait.
    # Everything before this point used to be reported as the previous
    # turn's steps, because the previous turn's steps were still what was
    # in the slot.
    #
    # A missing token gets a UNIQUE one, not a shared sentinel: two tabs
    # both defaulting to "-" wrote into each other's slot, which is the
    # cross-turn corruption the token exists to prevent.
    import uuid as _uuid
    turn_token = (str((payload or {}).get("turn_token") or "")[:64]
                  or f"srv-{_uuid.uuid4().hex[:16]}")
    displaced = _activity_begin(session_id, turn_token,
                                "Checking the database context")

    from ..client.chat import agent_turn, tool_result_limit
    from ..client.prompt import system_prompt

    calls: list = []
    # The turn id comes back in THIS dict, not off the function object: a
    # module-level slot is overwritten by whichever concurrent turn
    # finishes last, and a thumbs-down would then be logged against
    # somebody else's answer.
    turn_meta: dict = {}
    # Hoisted so the success return can read it even if the turn
    # never reached the block that fills it.
    turn_payloads: list = []
    # Owned OUTSIDE the try so a failed turn cannot leak it. On the
    # fallback path this stack holds an MCP subprocess — a Python process
    # plus its database logon — and closing it only on the success path
    # leaked one per errored turn until the GUI was restarted.
    per_turn: "contextlib.AsyncExitStack | None" = None
    # ONE try from the moment the slot is claimed. The first cut opened it
    # only after scope validation, so a DbError from the validation lookup
    # — a dead database, a latched credential — left the slot claiming to
    # be live work forever; the poll showed a question that had already
    # 500'd as still running.
    try:
        requested_scope = payload.get("scope")
        raw_source = ""
        if isinstance(requested_scope, dict):
            raw_source = str(
                requested_scope.get("source")
                or requested_scope.get("db") or ""
            ).strip()
        resolved_source = (
            engine.registry.resolve_name(raw_source)
            if engine.registry is not None else "default"
        )
        secondary_requested = bool(
            raw_source and resolved_source != "default"
        )

        if secondary_requested:
            # A named secondary source is independent of PS_LEDGER. Do not
            # make it wait for (or fail with) a primary-database discovery
            # query that cannot validate anything about this context.
            catalog = {"scopes": []}
        else:
            try:
                # This async route must not perform synchronous Oracle/SQL
                # Server I/O on FastAPI's event loop.
                catalog = await asyncio.to_thread(_financial_scope_catalog)
            except Exception as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"Financial scope discovery failed: {e}") from e

        # Scope discovery is deterministic and does not need an LLM round
        # trip. It also works before the user has chosen a BU, which is the
        # key escape hatch from a bad configured default.
        if not secondary_requested and _is_scope_catalog_question(message):
            options = _scope_options(catalog)
            return {
                "answer": (
                    "These business-unit and ledger combinations come "
                    "directly from PS_LEDGER. Select one to make it the "
                    "active chat scope."
                ),
                "tool_calls": [
                    {
                        "tool": "list_financial_scopes",
                        "args": {},
                        "ms": 0,
                        "ok": True,
                        "result": catalog,
                    }
                ],
                "scope_options": options,
                "provider": provider_name,
                "turn_id": None,
            }

        has_requested_scope = bool(
            isinstance(requested_scope, dict)
            and any(
                requested_scope.get(name) not in (None, "", 0, "0")
                for name in ("business_unit", "bu", "ledger", "fiscal_year",
                             "fy", "period", "per", "source", "db")
            )
        )
        active_scope: Optional[dict] = None
        # The scope the browser sends is a claim, and it is injected into
        # the prompt as "verified against PS_LEDGER" — so it is checked
        # against this person's grants BEFORE it becomes something the
        # model is told to trust.
        if access is not None and not access.all_units and has_requested_scope:
            claimed = str((requested_scope or {}).get("business_unit")
                          or (requested_scope or {}).get("bu") or "").strip()
            if claimed and not access.allows(claimed):
                return JSONResponse(status_code=403, content={
                    "error": access.refusal(claimed),
                    "scope_required": True,
                    "scope_options": [],
                    "tool_calls": []})
        if has_requested_scope or _question_requires_scope(message):
            try:
                # Validation can fall back to a latest-posted-period lookup
                # when a catalog record has no activity metadata, so it is
                # also offloaded.
                _activity_phase(
                    session_id, turn_token,
                    (f"Validating database source {resolved_source}"
                     if secondary_requested
                     else "Validating the scope against PS_LEDGER"),
                )
                active_scope = await asyncio.to_thread(
                    _validated_scope, requested_scope, catalog
                )
                if (
                    active_scope == {"source": "default"}
                    and _question_requires_scope(message)
                ):
                    raise _ScopeRequired(
                        "Choose a business unit and ledger before asking a "
                        "financial-data question.",
                        _scope_options(catalog),
                    )
            except _ScopeRequired as e:
                return {
                    "scope_required": True,
                    "error": e.detail,
                    "answer": e.detail,
                    "scope_options": e.options,
                    "tool_calls": [],
                    "provider": provider_name,
                    "turn_id": None,
                }

        # Use the shared, lifespan-owned server when it is up; otherwise fall
        # back to a per-turn subprocess so a chat never fails outright.
        # Session AND tools: the worker publishes tools first, but reading
        # both defensively costs nothing.
        shared = (_MCP.get("session") is not None
                  and _MCP.get("tools") is not None)
        if shared:
            session, tools = _MCP["session"], _MCP["tools"]
            per_turn = contextlib.AsyncExitStack()
        else:
            # The slow path, and the one worth naming: a whole Python start
            # and database logon before the question is even asked.
            _activity_phase(session_id, turn_token,
                            "Starting a private answer engine for this "
                            "question (the shared one is not up)")
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from ..client.chat import tool_specs
            env = dict(os.environ)
            env["PYTHONPATH"] = (str(Path(__file__).resolve().parents[2])
                                 + os.pathsep + env.get("PYTHONPATH", ""))
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "pstb.server"], env=env)
            per_turn = contextlib.AsyncExitStack()
            read, write = await per_turn.enter_async_context(
                stdio_client(params))
            session = await per_turn.enter_async_context(
                ClientSession(read, write))
            await session.initialize()
            tools = tool_specs(await session.list_tools())

        def make_provider():
            prompt = system_prompt(cfg, surface="gui",
                                   memory=_site_memory(),
                                   provider=provider_name)
            if (active_scope
                    and active_scope.get("source") not in
                    (None, "", "default")):
                source = active_scope["source"]
                prompt += (
                    "\n\n## Active database context selected by the user\n"
                    f"- Database source: {source}\n"
                    "This is a hard database boundary. Only guarded ad-hoc "
                    "discovery and read-only SQL tools that accept source= "
                    f"may query {source}, and they must use source={source}. "
                    "Do not attach PeopleSoft business-unit, ledger, fiscal "
                    "year, or accounting-period claims to this context. "
                    "Curated financial tools remain bound to the PeopleSoft "
                    "primary database; do not use one to claim it answered "
                    f"from {source}. If the requested fact needs a curated "
                    "PeopleSoft control, tell the user to switch Database "
                    "back to Finance."
                )
            elif (active_scope
                  and not active_scope.get("business_unit")):
                prompt += (
                    "\n\n## Active Finance database context selected by the user\n"
                    "The primary database is hard-selected, but no business "
                    "unit or ledger has been selected. Guarded ad-hoc "
                    "discovery and read-only SQL may use source=default. "
                    "Do not call a curated financial tool or state a balance, "
                    "transaction total, party amount, or control conclusion "
                    "until the user chooses a financial scope."
                )
            elif active_scope:
                prompt += (
                    "\n\n## Active scope selected by the user and verified "
                    "against PS_LEDGER\n"
                    f"- Business unit: {active_scope['business_unit']}\n"
                    f"- Ledger: {active_scope['ledger']}\n"
                    f"- Fiscal year: "
                    f"{active_scope.get('fiscal_year') or 'any (the question decides)'}\n"
                    f"- Period: "
                    f"{active_scope.get('period') or 'any (the question decides)'}\n"
                    "Business unit and ledger are FIXED — never change "
                    "them. Fiscal year and period are defaults: use "
                    "them when the question does not name its own, and "
                    "pass the period the user actually asked for when "
                    "they do. "
                    "If the question combines a "
                    "financial fact with a policy, retrieve the database "
                    "fact first and then retrieve the wiki passage; never "
                    "let wiki text replace database evidence."
                )
            else:
                prompt += (
                    "\n\n## Knowledge-only conversation\n"
                    "No financial database scope is selected. You may "
                    "answer general questions and retrieve approved wiki "
                    "passages, but do not call a financial-data tool. If "
                    "the user asks for a balance, transaction, customer, "
                    "invoice, report, or other financial fact, ask them "
                    "to select a database scope."
                )
            if provider_name == "gemini":
                from ..client.llm_gemini import GeminiVertexProvider as P
            elif provider_name == "claude":
                from ..client.llm_claude import ClaudeProvider as P
            else:
                from ..client.llm_ollama import OllamaProvider as P
            return P(cfg, prompt, tools)

        # The user is part of the key: provider entries hold conversation
        # history including prior tool payloads, and reusing one across
        # sign-ins would carry another person's figures into this turn's
        # grounding.
        key = _provider_key(session_id, provider_name, active_scope)
        if access is not None:
            key = key + (access.oprid,)
        provider_entry = _provider_sessions.get_or_create(
            key, make_provider
        )
        provider = provider_entry.provider

        def observe_tool(name, args, out, ms, ok):
            # Hand the browser the actual payload so it can render the
            # result inline — the model's prose never carries a figure
            # that the UI then re-displays.
            import json as _json

            try:
                data = _json.loads(out)
            except (ValueError, TypeError):
                # NOT None. A result that is not JSON is a result the
                # browser used to render as an empty card body — the tool
                # name and its timing in the header, and nothing at all
                # behind the arrow. Six tools spent a release in exactly
                # that state, raising a TypeError the UI turned into
                # silence. Hand the text through and let it be readable.
                data = {"error": str(out)[:4000],
                        "non_json_result": True}
            calls.append({
                "tool": name, "args": args, "ms": ms, "ok": ok,
                "result": data,
            })

        # One question at a time per browser tab and scope. Getting in is
        # now BOUNDED, because an unbounded wait is what turned one slow
        # query into three dead questions: the browser abandons its request
        # at 180s but the turn keeps running, so every question behind it
        # spent its own 180s in a queue and died the same way, with nothing
        # on screen to connect the second failure to the first.
        # The abandoned-turn refusal measures the TURN, not the last tool:
        # busy_since resets on every tool start, so a turn chaining several
        # sub-180s queries never looked abandoned however long it had held
        # the conversation — and the cascade this refusal exists to stop
        # simply came back for multi-tool turns.
        held = provider_entry.turn_for()
        if provider_entry.lock.locked() and held > _ABANDONED_AFTER:
            # The refused question hands the display back to the running
            # turn: killing the live feed of work that is still genuinely
            # progressing was the second half of the bad experience.
            _activity_restore(session_id, turn_token, displaced)
            return JSONResponse(status_code=409, content={
                "error": (
                    "The previous question in this conversation is still "
                    f"running against the database — {provider_entry.describe_busy()}, "
                    "past the point where its own browser request gave up. "
                    "Queueing behind it would only spend this question's "
                    "time too.\n\nPress Clear to start a fresh conversation "
                    "(it does not wait for the stuck query), or narrow the "
                    "scope — a business unit, a fiscal year, a period — "
                    "before asking again."),
                "tool_calls": []})
        if provider_entry.lock.locked():
            _activity_phase(session_id, turn_token,
                            "Waiting for the previous question in this tab "
                            "to finish")
        try:
            await asyncio.wait_for(provider_entry.lock.acquire(),
                                   timeout=_QUEUE_WAIT)
        except asyncio.TimeoutError:
            _activity_restore(session_id, turn_token, displaced)
            return JSONResponse(status_code=409, content={
                "error": (
                    "Still waiting on the previous question in this "
                    f"conversation after {int(_QUEUE_WAIT)}s — "
                    f"{provider_entry.describe_busy()}. Ask again once it "
                    "finishes, or press Clear to start a fresh conversation "
                    "that does not wait for it."),
                "tool_calls": []})
        try:
            result_limit = tool_result_limit(cfg, provider_name)
            _activity_phase(session_id, turn_token,
                            f"Asking {provider_name}")
            provider_entry.busy_since = time.monotonic()
            provider_entry.busy_tool = ""
            provider_entry.turn_since = time.monotonic()

            def _on_started(tool: str, args_preview: str,
                            blocked: bool) -> None:
                if not blocked:
                    # Named here so a question that cannot get in can say
                    # WHICH query is holding the conversation, not just that
                    # something is.
                    provider_entry.busy_tool = tool
                    provider_entry.busy_since = time.monotonic()
                _activity_add(session_id, turn_token, {
                    "status": "blocked" if blocked else "running",
                    "tool": tool, "args": args_preview})

            prior_payloads = list(provider_entry.payloads)
            # THIS turn's results only. Suggestions built from the whole
            # 12-payload window would keep offering follow-ups to a question
            # asked four turns ago, which reads as the page not listening.

            def _observe_and_record(name, args, out, ms, ok):
                provider_entry.busy_tool = ""
                _activity_add(session_id, turn_token, {
                    "status": "done" if ok else "failed",
                    "tool": name, "ms": ms})
                if ok and isinstance(out, str):
                    provider_entry.payloads.append((name, out))
                    del provider_entry.payloads[:-12]
                    turn_payloads.append((name, out))
                observe_tool(name, args, out, ms, ok)

            try:
                answer = await agent_turn(
                    provider,
                    session,
                    message,
                    qlog=qlog,
                    surface="gui",
                    scope=active_scope,
                    tool_observer=_observe_and_record,
                    tool_started=_on_started,
                    prior_payloads=prior_payloads,
                    result_limit=result_limit,
                    turn_meta=turn_meta,
                    access=access,
                    allow_raw_sql=bool(
                        getattr(cfg.security, "raw_sql_for_restricted",
                                False)),
                    # The catalog is already in hand; it is what lets a
                    # question that NAMES two units be recognised as
                    # crossing them.
                    known_units=[s.get("business_unit")
                                 for s in (catalog.get("scopes") or [])],
                )
            finally:
                _activity_done(session_id, turn_token)
        finally:
            provider_entry.busy_since = 0.0
            provider_entry.busy_tool = ""
            provider_entry.turn_since = 0.0
            provider_entry.lock.release()
    except HTTPException:
        raise
    except RuntimeError as e:
        return JSONResponse(status_code=400, content={"error": str(e), "tool_calls": calls})
    except BaseException as e:  # noqa: BLE001 - includes ExceptionGroup
        import traceback

        # anyio wraps failures in an ExceptionGroup whose str() hides the cause.
        # Unwrap to the innermost real exception so the UI can show something
        # actionable instead of "unhandled errors in a TaskGroup".
        def innermost(exc):
            subs = getattr(exc, "exceptions", None)
            return innermost(subs[0]) if subs else exc

        root = innermost(e)
        traceback.print_exception(type(root), root, root.__traceback__, file=sys.stderr)
        return JSONResponse(
            status_code=500,
            content={"error": f"{type(root).__name__}: {root}", "tool_calls": calls},
        )
    finally:
        # Also here, not only around agent_turn: a turn that died while
        # spawning the engine or building the provider never reached that
        # finally, and left an "active" slot the next poll would read as
        # live work.
        _activity_done(session_id, turn_token)
        if per_turn is not None:
            with contextlib.suppress(Exception):
                await per_turn.aclose()
    follow_ups = _suggestions_for_turn(turn_payloads, message, active_scope)
    _suggestions_store(session_id, follow_ups)
    return {"answer": answer, "tool_calls": calls, "provider": provider_name,
            "scope": active_scope, "suggestions": follow_ups,
            "turn_id": turn_meta.get("turn_id")}


def _console_reload() -> dict:
    """Rebuild what THIS process can rebuild, and be precise about the rest.

    The GUI resolves cfg/db/engine as module globals at call time, so
    swapping them genuinely reaches every /api/* handler. What cannot be
    swapped is the answer engine: pstb/server.py binds its whole object
    graph at import inside a separate stdio subprocess, and that
    subprocess's session was entered by the lifespan task — anyio cancel
    scopes must be exited by the task that entered them, so no request
    handler can respawn it.

    Rebuilding only the GUI half would leave /api/trial-balance answering
    from the new configuration while /api/chat still answered from the
    old: two live surfaces disagreeing about the same question, which is
    worse than not reloading. So this reloads the read-only views and says
    plainly that the chat path needs a restart.
    """
    global cfg, db, engine, ar, report_runner, relationships
    reloaded: list = []
    # Adopt what the console just wrote to .env BEFORE rebuilding. dotenv
    # loads with override=False, so os.environ still holds the values from
    # startup — a password fixed in the file would lose to the stale one in
    # the environment, and the rebuild would offer the WRONG password to
    # the database again, burning another FAILED_LOGIN_ATTEMPTS strike on
    # the very reload the refusal message recommends. Scoped to the keys
    # the console manages: a variable the operator exported by hand for
    # anything else keeps winning, as documented.
    try:
        from dotenv import dotenv_values

        from .. import settings as _settings
        env_file = dotenv_values(Path(cfg.root) / ".env", interpolate=False)
        for key in (_settings.SECRET_KEYS | _settings.ENV_KEYS):
            value = env_file.get(key)
            if value:
                os.environ[key] = value
            elif key in os.environ and key in env_file:
                os.environ.pop(key, None)     # explicitly cleared in the file
    except Exception:
        pass          # no dotenv, no .env — nothing to adopt
    try:
        # Same resolution the process booted with (app.py:47), so a
        # reload cannot quietly adopt a different config file.
        fresh = load_config(os.environ.get("PSTB_CONFIG"))
    except Exception as e:
        return {"reloaded": [], "error": f"{type(e).__name__}: {e}"}
    try:
        new_db = Database(fresh)
        new_engine = TBEngine(new_db, fresh)
        new_ar = ARBilling(new_engine)
        new_relationships = Relationships(new_ar)
        new_report = ReportRunner(new_engine)
    except Exception as e:
        # The old objects are still live and serving; say so.
        return {"reloaded": [],
                "error": f"kept the running configuration: {e}"}
    cfg, db, engine, ar, report_runner, relationships = (
        fresh, new_db, new_engine, new_ar, new_report, new_relationships)
    reloaded = ["trial balance", "AR and billing", "reports", "diagnostics"]
    _scope_cache.update({"value": None, "expires": 0.0})
    reloaded.append("scope catalog")
    return {"reloaded": reloaded}


console.register(app, lambda: cfg, _console_reload)


@app.post("/api/feedback")
def feedback(payload: dict):
    turn_id = (payload or {}).get("turn_id", "")
    if not turn_id:
        raise HTTPException(status_code=400, detail="turn_id required")
    qlog.log_feedback(turn_id, (payload or {}).get("verdict", "bad"),
                      (payload or {}).get("note", ""))
    return {"ok": True}


@app.get("/api/activity")
def activity(session_id: str = "", turn: str = ""):
    """What THIS turn is doing right now — polled by the page while its
    /api/chat request is in flight, so 'Working…' can say which tool is
    running instead of leaving a spinner to speak for a 40-second query.

    ``turn`` is the token the browser minted for the question it is waiting
    on. Without it the poll answered with whatever was last in the slot,
    which during the opening seconds of a new question is the PREVIOUS
    question's steps — reported to us as an AR aging that claimed to be
    running close_readiness. A token that does not match the slot returns
    empty and says so, rather than returning someone else's turn.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        return {"active": False, "events": [], "phase": "", "stale": False}
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is None:
            return {"active": False, "events": [], "phase": "", "stale": False}
        if turn and slot["turn"] != turn:
            return {"active": False, "events": [], "phase": "", "stale": True}
        return {"active": slot["active"], "events": list(slot["events"]),
                "phase": slot.get("phase", ""), "turn": slot["turn"],
                "stale": False}


@app.get("/api/suggestions")
def suggestions(session_id: str = ""):
    """The last turn's follow-ups for one session.

    NOT a reload restore: a fresh page deliberately mints a fresh session
    (index.html:389), because keeping the id would retain invisible model
    context after the messages themselves vanished from the screen — and a
    follow-up pointing at figures no longer displayed is exactly the
    unverifiable suggestion this feature exists to avoid.

    The browser gets its follow-ups inline on the /api/chat response and
    does not call this. It exists for anything ELSE holding a live session
    id — a script, a second surface — that wants the next questions
    without re-asking the first one.
    """
    if not _SESSION_ID_RE.match(session_id or ""):
        return {"suggestions": []}
    with _suggestions_lock:
        return {"suggestions": list(_suggestions.get(session_id) or [])}


@app.post("/api/chat/reset")
async def chat_reset(payload: dict):
    session_id = _session_id(payload or {})
    cleared = await _provider_sessions.reset_session(session_id)
    # Clear means clear. A follow-up left pointing at evidence the page no
    # longer shows is a suggestion nobody can check.
    with _suggestions_lock:
        _suggestions.pop(session_id, None)
    return {"ok": True, "histories_cleared": cleared}


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="PeopleSoft trial-balance web UI")
    # The deployment this is built for is a shared box inside the VPN, and
    # a default nobody can use is a default everyone overrides — every
    # start became "--host 0.0.0.0 --port 8016 --share" typed out again.
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default 0.0.0.0, reachable on the "
                         "network). Use 127.0.0.1 for this machine only.")
    ap.add_argument("--port", type=int, default=8016,
                    help="port to listen on (default 8016)")
    ap.add_argument("--open", action="store_true", help="open a browser window")
    ap.add_argument(
        "--share", action="store_true",
        help="accepted and no longer required: a routable bind is already "
             "the default and always prints what it exposes. Kept so "
             "existing commands and scripts keep working.")
    ap.add_argument(
        "--allow-host", action="append", default=[], metavar="NAME",
        help="on a routable bind, the only Host names accepted "
             "(repeatable). Omit to accept any name and rely on the token "
             "alone.")
    args = ap.parse_args()

    loopback = localguard.peer_is_loopback((args.host, args.port))
    # --share used to be a REQUIRED acknowledgement, and refusing to start
    # without it was worth it while the default was loopback: a routable
    # bind could then only happen on purpose. Once the default is routable
    # that reasoning inverts — the flag would refuse the out-of-the-box
    # command, and a gate that fires on the intended case is a gate people
    # delete rather than read.
    #
    # What actually informs is the banner below, which is printed on every
    # routable start whether or not the flag was passed, and which says in
    # plain words what is now reachable. That is kept, and so is everything
    # the flag never controlled: the Host check, the optional token, and
    # /console answering only from this machine no matter what the network
    # policy is.
    # Raw SQL, a routable bind and no row security is a COMBINATION, not
    # three settings. Any one alone is defensible: ad-hoc SQL is this
    # product's answer to every module without a curated tool; the network
    # bind is the deployment; security off is a single-user install.
    # Together they mean anyone who can reach the port can SELECT anything
    # the database account can read, and nothing on the way in asks who
    # they are.
    #
    # Refusing to start would be wrong — it would break the machine-local
    # workflow this has always supported. So the dangerous COMBINATION is
    # what fails closed: ad-hoc SQL switches off, and the banner names the
    # two switches that turn it back on. An operator who wants it anyway
    # says so once, in config, on purpose.
    raw_sql_off_reason = ""
    if (not loopback and cfg.tools.allow_raw_sql
            and not cfg.security.enabled
            and not getattr(cfg.tools, "raw_sql_on_shared_bind", False)):
        cfg.tools.allow_raw_sql = False
        raw_sql_off_reason = (
            "Ad-hoc SQL is OFF: this bind is reachable from the network and "
            "business-unit security is not on, so nothing identifies the "
            "caller. Turn on security.enabled to restore it per user, or "
            "set tools.raw_sql_on_shared_bind: true to accept that anyone "
            "who can reach this port may query the database.")

    if loopback and args.share:
        # Silently ignoring the flag taught the operator the wrong lesson —
        # they believed a token was required and it was not.
        print("\n  Note: --share has no effect on a loopback bind. This "
              "page is reachable from this machine only; no token is "
              "required. Bind a routable address (--host 0.0.0.0) to "
              "actually share it.", file=sys.stderr)

    token = ""
    if not loopback:
        # OPTIONAL. A token is on only when the operator set one; --share
        # alone serves a plain URL. Minting one nobody asked for meant a
        # link that had to be pasted around and that every restart
        # invalidated — friction this app decided on its owner's behalf.
        token = (os.environ.get("PSTB_AUTH_TOKEN") or "").strip()
        if token and not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", token):
            # The token travels in a URL, a cookie and a header. A value
            # with '&', '#', spaces or quotes would be silently split by
            # the first of those and the operator would be debugging a
            # lockout, not a validation error. Say it at startup instead.
            raise SystemExit(
                "\n  PSTB_AUTH_TOKEN must be 16-128 characters of A-Z, "
                "a-z, 0-9, '-' or '_': it is carried inside a URL and a "
                "cookie, where anything else gets split or re-encoded and "
                "locks out the people it was minted for.\n  Generate one: "
                "python3 -c \"import secrets; "
                "print(secrets.token_urlsafe(24))\"\n")
    localguard.configure(args.host, token, args.allow_host,
                         unauthenticated=not loopback and not token)

    url = f"http://{args.host}:{args.port}"
    print(f"\n  PeopleSoft Trial Balance — {url}")
    print(f"  data: {cfg.db.backend}{' (views)' if cfg.db.use_views else ''} | "
          f"llm: {cfg.llm.provider} | wiki: {getattr(wiki, 'provider_name', 'off')}")
    reachable = (f"http://<this-host>:{args.port}"
                 if args.host in ("0.0.0.0", "::", "*") else url)
    if not loopback and not token:
        # The plain URL, which is what people want to type. Said plainly
        # rather than prevented: whether this network is trusted is the
        # operator's call, and an app that refuses the answer just gets its
        # guard edited out — which turns off more than it turns on.
        print(f"\n  Open on the network — no token. Share this URL:")
        print(f"      {reachable}")
        print("  Anyone who can route to this host can read every balance, "
              "every customer and use the ad-hoc SQL tool. Keep it inside "
              "the VPN; this is cleartext HTTP.")
        print("  To require a token instead, set PSTB_AUTH_TOKEN and "
              "restart.")
        print(f"  To keep it on this machine only: --host 127.0.0.1, then "
              f"ssh -L {args.port}:localhost:{args.port} <this-host>.")
        if raw_sql_off_reason:
            print("  " + raw_sql_off_reason)
        print("  The configuration console is not shared either way: "
              "/console answers only from this machine (SSH tunnel).")
    elif token:
        # Printed INSIDE a URL, because a token someone has to assemble by
        # hand is a token someone emails around in plain text instead.
        print(f"\n  Shared mode: every request needs this token.")
        print(f"      {reachable}/?token={token}")
        print("  Anyone with that link has full read access to the ledger.")
        print("  The configuration console stays machine-local: /console "
              "answers only from this host (SSH tunnel), token or not.")
        print("  This is cleartext HTTP — keep it inside the VPN, or put a "
              "TLS proxy in front if the network is not trusted.")
    if not loopback and not args.allow_host:
        print("  Accepting any Host header; pass --allow-host <name> to "
              "narrow it.")
    print("\n  Ctrl+C to stop\n")
    if args.open:
        import threading
        import webbrowser

        opening = f"{url}/?token={token}" if token else url
        threading.Timer(1.0, lambda: webbrowser.open(opening)).start()
    uvicorn.run(app, host=args.host, port=args.port,
                log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
