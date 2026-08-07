"""Web UI for the trial-balance agent.

Financial figures are served straight from the engine and rendered by the
browser — the model never produces a number that reaches the screen. The chat
panel is an assistant over already-verified data, not the source of it.

Run:  python -m pstb.gui            (then open http://127.0.0.1:8000)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config import load_config
from ..version import build_info as _build_info
from ..db import Database, DbError
from ..engine import EngineError, TBEngine
from .. import queries as query_sql
from ..ar import ARBilling, ARError
from ..qlog import QuestionLog
from ..export import ExportError
from ..report import ReportError, ReportRunner
from ..wiki import WikiError, make_wiki

try:
    from fastapi import FastAPI, HTTPException
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
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
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
# The session is owned by the app LIFESPAN, not by a request. MCP's stdio
# client uses anyio cancel scopes, and a cancel scope must be exited by the
# same task that entered it — holding the stack inside a request handler and
# closing it from another raises "attempted to exit cancel scope in a
# different task". Entering at startup and exiting at shutdown keeps both ends
# in the lifespan task; individual tool calls are safe from request tasks
# because they only move messages over memory streams.
_MCP: dict = {"session": None, "tools": None, "error": None}


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


@contextlib.asynccontextmanager
async def _lifespan(_app):
    stack = contextlib.AsyncExitStack()
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from ..client.chat import tool_specs

        env = dict(os.environ)
        env["PYTHONPATH"] = (str(Path(__file__).resolve().parents[2])
                             + os.pathsep + env.get("PYTHONPATH", ""))
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "pstb.server"], env=env)
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        _MCP["session"] = session
        _MCP["tools"] = tool_specs(await session.list_tools())
    except Exception as e:                      # degrade, never fail to boot
        # The exception from a dead stdio subprocess is usually noise
        # ("unhandled errors in a TaskGroup") while the REAL reason — an
        # Oracle logon rejection, a config error, a broken pull — died with
        # the subprocess's stderr. Re-run the import in a subprocess that
        # captures stderr, so the user sees ORA-01017 instead of a shrug.
        detail = _server_import_check()
        _MCP["error"] = str(e) + (f" | {detail}" if detail else "")
        print(f"[pstb] shared MCP session unavailable ({_MCP['error']}); "
              "falling back to one server per turn", file=sys.stderr)
    try:
        yield
    finally:
        try:
            await stack.aclose()
        except Exception:
            pass
        _MCP.update({"session": None, "tools": None})


app = FastAPI(title="PeopleSoft Trial Balance", docs_url=None,
              redoc_url=None, lifespan=_lifespan)


@dataclass
class _ProviderSession:
    """One provider history, scoped to one browser session and DB scope."""

    provider: object
    touched: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Recent tool payloads, so a follow-up turn's restated figures ground
    # against what this conversation already fetched.
    payloads: list = field(default_factory=list)


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
        """Remove only one browser session; never clear another user's history."""
        with self._lock:
            keys = [key for key in self._entries if key[0] == session_id]
            entries = [self._entries.pop(key) for key in keys]
        for entry in entries:
            # A reset can arrive while a provider call is running in a worker
            # thread. Wait for that scoped turn before mutating its history.
            async with entry.lock:
                try:
                    await asyncio.to_thread(entry.provider.reset)
                except Exception:
                    pass
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


def _activity_reset(session_id: str) -> None:
    with _activity_lock:
        _activity[session_id] = {"active": True, "events": []}
        if len(_activity) > 200:            # forgotten sessions, bounded
            for key in list(_activity)[:-100]:
                _activity.pop(key, None)


def _activity_add(session_id: str, event: dict) -> None:
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is None:
            return
        slot["events"].append({**event, "t": time.time()})
        del slot["events"][:-_ACTIVITY_MAX_EVENTS]


def _activity_done(session_id: str) -> None:
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is not None:
            slot["active"] = False


_scope_cache: dict = {"expires": 0.0, "value": None, "refreshing": False}
_scope_cache_lock = threading.RLock()
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
            return
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with _scope_cache_lock:
            _scope_cache.update({
                "value": value,
                "expires": time.monotonic() + _SCOPE_CACHE_SECONDS,
                "refreshing": False,
            })
        _persist_scope_catalog(value)
        print(f"[pstb] scope catalog refreshed in {elapsed_ms} ms",
              file=sys.stderr)

    threading.Thread(target=work, daemon=True,
                     name="scope-catalog-refresh").start()
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
    # Omit a cleared field entirely: apply_request_scope only injects what is
    # present, so an omitted period leaves each tool on its own default.
    if fiscal_year is not None:
        scope["fiscal_year"] = fiscal_year
    if period is not None:
        scope["period"] = period
    return scope


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
        return (session_id, provider_name, "__KNOWLEDGE_ONLY__", "", 0, 0)
    return (
        session_id,
        provider_name,
        scope["business_unit"],
        scope["ledger"],
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
def meta():
    d = cfg.defaults
    out = {
        "defaults": {
            "business_unit": d.business_unit,
            "ledger": d.ledger,
            "base_currency": d.base_currency,
            "adjustment_periods": d.adjustment_periods,
            "account_tree": d.account_tree,
        },
        "build": _build_info(),
        "mcp_session": {"shared": _MCP["session"] is not None,
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
                "model": cfg.llm.ollama_model if cfg.llm.provider == "ollama"
                else cfg.llm.gemini_model},
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
    if warm is not None:
        out["financial_scopes"] = warm.get("scopes") or []
        out["business_units"] = [
            {
                "business_unit": item.get("business_unit"),
                "descr": item.get("descr"),
                "base_currency": item.get("base_currency"),
            }
            for item in out["financial_scopes"]
        ]
        out["scopes_ready"] = True
    else:
        out["financial_scopes"] = []
        out["business_units"] = []
        out["scopes_ready"] = False
    # Scope comes from the DATABASE, validated against config — not raw config.
    # On a real instance the config defaults are often the sample values.
    try:
        eff = engine.effective_defaults()
        fy0, per0 = engine.last_posted_period(eff["business_unit"], eff["ledger"])
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
        if not any(b.get("business_unit") == eff["business_unit"]
                   for b in out["business_units"]):
            out["business_units"].append(
                {"business_unit": eff["business_unit"], "descr": "(discovered)"})
    except Exception as e:
        out["scope"] = {"business_unit": d.business_unit, "ledger": d.ledger,
                        "ledgers": [d.ledger], "fiscal_year": 0, "period": 0,
                        "max_regular_period": 12,
                        "discovered": False, "notes": [f"scope discovery failed: {e}"]}
        out["ledgers"] = [d.ledger]
    try:
        cur = engine.resolve_period("")
        out["current"] = {"fiscal_year": cur["fiscal_year"], "period": cur["period"]}
    except Exception:
        fy, per = engine._current_fy_period()
        out["current"] = {"fiscal_year": fy, "period": per}

    # The calendar's current period may have no postings yet (early in a month,
    # or before close). Opening on an empty screen reads as "broken", so tell
    # the UI the newest period that actually has activity.
    try:
        rows, _ = db.query(
            query_sql.scope_last_regular_period(db, engine._adj_periods()),
            {"bu": out["scope"]["business_unit"], "led": out["scope"]["ledger"],
             "fy": out["current"]["fiscal_year"]},
            max_rows=1,
        )
        p = rows[0]["last_period"] if rows else None
        out["last_period_with_data"] = int(p) if p is not None else out["current"]["period"]
    except Exception:
        out["last_period_with_data"] = out["current"]["period"]
    return out


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
                  connectors=[_coupa_mod.from_env()])


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
def export_csv(payload: dict):
    """Full-population CSV for one result card.

    The browser holds a display-capped preview; this re-runs the same tool
    server-side at the export ceiling so the file has the whole row set,
    not the page. Tools that cannot return more rows fall back to
    exporting the payload the caller already has, and the response says
    which happened.
    """
    from .. import export as _export
    from ..connectors import coupa as _coupa_mod
    from ..modules import ModulePacks

    body = payload or {}
    tool = str(body.get("tool") or "")
    if not tool:
        raise HTTPException(status_code=400, detail="tool is required")
    registry = _export.build_registry(
        engine=engine, ar=ar, modules=ModulePacks(engine),
        report_runner=report_runner, coupa=_coupa_mod.from_env())
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
def scopes_catalog(force: bool = False):
    """Business-unit / ledger catalog, built on demand.

    Split out of /api/meta so the page can paint immediately and fill this in
    when it arrives. Safe to call repeatedly: it is cached.
    """
    def _build() -> dict:
        catalog = _financial_scope_catalog(force=force)
        scopes = catalog.get("scopes") or []
        return {
            "scopes": scopes,
            "business_units": [
                {"business_unit": s.get("business_unit"),
                 "descr": s.get("descr"),
                 "base_currency": s.get("base_currency")}
                for s in scopes
            ],
            "ready": True,
        }
    return _guard(_build)


@app.get("/api/scope")
def scope_for(business_unit: str = "", ledger: str = ""):
    """Ledgers and last posted period for a business unit — feeds the cascading
    scope bar so changing BU repopulates ledger/year/period from real data."""
    def _scope(business_unit: str, ledger: str) -> dict:
        bu = (business_unit or "").strip() or engine.effective_defaults()["business_unit"]
        leds = engine.list_ledgers(bu)
        ledgers = leds.get("ledgers") or []
        led = (ledger or "").strip()
        if not led or led not in ledgers:
            led = next((l for l in ledgers if l.upper() == "ACTUALS"),
                       ledgers[0] if ledgers else engine.effective_defaults()["ledger"])
        fy, per = engine.last_posted_period(bu, led)
        # Fiscal years that actually hold data, so the scope editor offers
        # real choices instead of only the latest one.
        try:
            years = []
            for sc in engine.list_financial_scopes(
                    include_activity=True).get("scopes") or []:
                if sc.get("business_unit") != bu:
                    continue
                for lg in sc.get("ledgers") or []:
                    if lg.get("ledger") == led:
                        years = [int(y) for y in (lg.get("fiscal_years") or [])]
        except Exception:
            years = []
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
async def chat(payload: dict):
    """Run one governed chat turn in a browser-session + DB-scope context."""
    message = (payload or {}).get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    session_id = _session_id(payload)
    if "scope" not in payload:
        raise HTTPException(status_code=400, detail="scope is required")
    provider_name = str(
        (payload or {}).get("provider") or cfg.llm.provider
    ).strip().lower()
    if provider_name not in {"gemini", "ollama"}:
        raise HTTPException(status_code=400, detail="provider must be gemini or ollama")

    try:
        # This async route must not perform synchronous Oracle/SQL Server I/O
        # on FastAPI's event loop.
        catalog = await asyncio.to_thread(_financial_scope_catalog)
    except Exception as e:
        raise HTTPException(
            status_code=503, detail=f"Financial scope discovery failed: {e}"
        ) from e

    # Scope discovery is deterministic and does not need an LLM round trip.
    # It also works before the user has chosen a BU, which is the key escape
    # hatch from a bad configured default.
    if _is_scope_catalog_question(message):
        options = _scope_options(catalog)
        return {
            "answer": (
                "These business-unit and ledger combinations come directly "
                "from PS_LEDGER. Select one to make it the active chat scope."
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

    requested_scope = payload.get("scope")
    has_requested_scope = bool(
        isinstance(requested_scope, dict)
        and any(
            requested_scope.get(name) not in (None, "", 0, "0")
            for name in ("business_unit", "bu", "ledger", "fiscal_year", "fy",
                         "period", "per")
        )
    )
    active_scope: Optional[dict] = None
    if has_requested_scope or _question_requires_scope(message):
        try:
            # Validation can fall back to a latest-posted-period lookup when a
            # catalog record has no activity metadata, so it is also offloaded.
            active_scope = await asyncio.to_thread(
                _validated_scope, requested_scope, catalog
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

    from ..client.chat import agent_turn, tool_result_limit
    from ..client.prompt import system_prompt

    calls: list = []
    # Owned OUTSIDE the try so a failed turn cannot leak it. On the
    # fallback path this stack holds an MCP subprocess — a Python process
    # plus its database logon — and closing it only on the success path
    # leaked one per errored turn until the GUI was restarted.
    per_turn: "contextlib.AsyncExitStack | None" = None
    try:
        # Use the shared, lifespan-owned server when it is up; otherwise fall
        # back to a per-turn subprocess so a chat never fails outright.
        shared = _MCP.get("session") is not None
        if shared:
            session, tools = _MCP["session"], _MCP["tools"]
            per_turn = contextlib.AsyncExitStack()
        else:
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
            prompt = system_prompt(cfg, surface="gui", memory=_site_memory())
            if active_scope:
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
            else:
                from ..client.llm_ollama import OllamaProvider as P
            return P(cfg, prompt, tools)

        key = _provider_key(session_id, provider_name, active_scope)
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
                data = None
            calls.append({
                "tool": name, "args": args, "ms": ms, "ok": ok,
                "result": data,
            })

        async with provider_entry.lock:
            result_limit = tool_result_limit(cfg, provider_name)
            _activity_reset(session_id)

            def _on_started(tool: str, args_preview: str,
                            blocked: bool) -> None:
                _activity_add(session_id, {
                    "status": "blocked" if blocked else "running",
                    "tool": tool, "args": args_preview})

            prior_payloads = list(provider_entry.payloads)

            def _observe_and_record(name, args, out, ms, ok):
                _activity_add(session_id, {
                    "status": "done" if ok else "failed",
                    "tool": name, "ms": ms})
                if ok and isinstance(out, str):
                    provider_entry.payloads.append(out)
                    del provider_entry.payloads[:-12]
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
                )
            finally:
                _activity_done(session_id)
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
        if per_turn is not None:
            with contextlib.suppress(Exception):
                await per_turn.aclose()
    return {"answer": answer, "tool_calls": calls, "provider": provider_name,
            "scope": active_scope,
            "turn_id": getattr(agent_turn, "last_turn_id", None)}


@app.post("/api/feedback")
def feedback(payload: dict):
    turn_id = (payload or {}).get("turn_id", "")
    if not turn_id:
        raise HTTPException(status_code=400, detail="turn_id required")
    qlog.log_feedback(turn_id, (payload or {}).get("verdict", "bad"),
                      (payload or {}).get("note", ""))
    return {"ok": True}


@app.get("/api/activity")
def activity(session_id: str = ""):
    """What the current turn is doing RIGHT NOW — polled by the page while
    its /api/chat request is in flight, so 'Working…' can say which tool is
    running instead of leaving a spinner to speak for a 40-second query."""
    if not _SESSION_ID_RE.match(session_id or ""):
        return {"active": False, "events": []}
    with _activity_lock:
        slot = _activity.get(session_id)
        if slot is None:
            return {"active": False, "events": []}
        return {"active": slot["active"], "events": list(slot["events"])}


@app.post("/api/chat/reset")
async def chat_reset(payload: dict):
    session_id = _session_id(payload or {})
    cleared = await _provider_sessions.reset_session(session_id)
    return {"ok": True, "histories_cleared": cleared}


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="PeopleSoft trial-balance web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open a browser window")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  PeopleSoft Trial Balance — {url}")
    print(f"  data: {cfg.db.backend}{' (views)' if cfg.db.use_views else ''} | "
          f"llm: {cfg.llm.provider} | wiki: {getattr(wiki, 'provider_name', 'off')}")
    print("  Ctrl+C to stop\n")
    if args.open:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
