"""The ledger calls you: a bounded loop that re-runs tie-outs and diffs.

Every check this product knows how to run has always waited to be asked.
This module inverts that for a curated few: a runner re-executes them on a
cadence, a diff store remembers the last outcome per check, and a feed
serves only what CHANGED -- a new exception, a delta that moved, an
exception that cleared. The close stops being a monthly archaeology dig
and becomes a surface that is green until it is not.

Three rules shape everything here, in order of importance:

1. GRANT SURVIVAL. This loop runs unattended against a production
   reporting account, and an unattended loop that hammers the database is
   exactly what loses the read-only grant. So the ticker is OFF by
   default, every tick has a hard query budget and a wall-clock budget
   spent by reservation (the next check's worst case is charged before it
   starts), a check cut by the budget is disclosed rather than skipped,
   and repeated failure trips a breaker that turns the loop off loudly.

2. NO ROW VALUES PERSIST. The store and the feed hold check identities,
   closed-vocabulary statuses, bounded integer counts, deltas and
   timestamps -- never a journal id, an amount from a row, or raw error
   text (errors persist as qlog's closed refusal categories). Whoever
   wants the offending rows clicks through to the live, guarded tools,
   which see the operator's current vetoes and the caller's own access.

3. STALENESS IS LOUDER THAN ABSENCE. A dead runner must read as UNKNOWN,
   never as "no exceptions": the feed computes staleness against the
   configured cadence and fails closed to stale when a timestamp is
   missing or unreadable. A check that stopped running is an event, not
   a silence.

The runner honours the operator's record exclusions on every tick (the
veto is re-read each time, so an exclusion approved mid-flight takes
effect on the next tick), and there is no model anywhere in the loop:
machinery computes, and only machinery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .db import _is_credential_failure
from .qlog import refusal_category

SCHEMA_VERSION = 1

# What one tb_integrity_check run may cost, at worst, in queries: the TB
# aggregate, two plan-gated journal probes (a plan ask plus a run each),
# and the retained-earnings roll's two period aggregates. Budgets reserve
# this BEFORE the check starts, so a tick can never discover mid-check
# that it has overspent.
TB_INTEGRITY_QUERY_COST = 8

# Every record tb_integrity_check reads, MEASURED by auditing its queries
# against the sample rather than typed from memory -- a veto gate that
# names two of seven tables is a veto the operator believes and does not
# have. A test re-measures this list and fails when the check grows.
TB_INTEGRITY_READS = (
    "PS_BUS_UNIT_TBL_GL", "PS_CAL_DETP_TBL", "PS_GL_ACCOUNT_TBL",
    "PS_JRNL_HEADER", "PS_JRNL_LN", "PS_LEDGER", "PS_SET_CNTRL_REC",
)

# The AP invoice pipeline check (modules.open_payables), measured the
# same way: 3 queries on the sample plus catalog describes on a first
# Oracle tick, reserved as 4. The read list is the SUPERSET the check may
# touch -- PS_PYMNT_VCHR_XREF is only read on installations whose voucher
# table has no CLOSE_STATUS, but a veto gate that names what a check
# MIGHT read is the only one an operator can rely on. A test asserts the
# measured set stays within this list.
AP_PIPELINE_QUERY_COST = 4
AP_PIPELINE_READS = ("PS_PYMNT_VCHR_XREF", "PS_VENDOR", "PS_VOUCHER")

STATUSES = frozenset({
    "passed", "exceptions_found", "checks_incomplete", "not_run",
    "refused", "error",
})
EVENT_KINDS = frozenset({
    "baseline", "new_exceptions", "worsened", "improved", "cleared",
    "became_incomplete", "refused", "error", "recovered", "scope_changed",
    "budget_cut", "no_longer_scheduled", "baseline_reset",
})
_CHECK_ID = re.compile(r"^[a-z0-9_]{1,40}(?::[A-Za-z0-9_.\-]{1,60}){0,5}$")
_METRIC = re.compile(r"^[a-z0-9_]{1,64}$")
_BU = re.compile(r"^[A-Za-z0-9_\-]{0,30}$")
_MAX_COUNT = 10 ** 12


class TickerError(RuntimeError):
    """A ticker rule was violated; the message names the remedy."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class TickerLimits:
    """Budgets with a floor AND a ceiling on every field.

    MetadataBuildLimits taught the lesson the hard way: its validate()
    skipped the mine_* budgets, so a config typo of 240,000 probes would
    have passed. Here every number is bounded on both sides, because a
    standing loop multiplies any mistake by every tick it survives.
    """
    cadence_minutes: int = 30
    max_queries_per_tick: int = 40
    max_seconds_per_tick: int = 600
    history_per_check: int = 200
    events_kept: int = 500
    failure_trip: int = 3

    _BOUNDS = {
        "cadence_minutes": (5, 24 * 60),
        "max_queries_per_tick": (1, 400),
        "max_seconds_per_tick": (30, 3300),
        "history_per_check": (10, 2000),
        "events_kept": (50, 5000),
        "failure_trip": (1, 10),
    }

    @classmethod
    def from_config(cls, cfg=None, **overrides) -> "TickerLimits":
        source = cfg or object()
        values = {}
        for name in cls._BOUNDS:
            raw = overrides.get(name, getattr(source, name,
                                              getattr(cls, name)))
            try:
                values[name] = int(raw)
            except (TypeError, ValueError) as exc:
                raise TickerError(
                    f"ticker.{name} must be a whole number: {exc}") from exc
        out = cls(**values)
        out.validate()
        return out

    def validate(self) -> None:
        for name, (floor, ceiling) in self._BOUNDS.items():
            value = getattr(self, name)
            if value < floor or value > ceiling:
                raise TickerError(
                    f"ticker.{name} must be between {floor} and "
                    f"{ceiling:,}; received {value:,}. The bounds exist "
                    "because this loop runs unattended against a "
                    "read-only reporting account.")


@dataclass
class CheckOutcome:
    """One check's result, already reduced to what may persist."""
    check_id: str
    source: str
    business_unit: str
    status: str
    counts: dict = field(default_factory=dict)
    fiscal_year: int = 0
    period: int = 0
    narrowed: bool = False
    error_category: str = ""


def reduce_tb_integrity(result: Mapping) -> tuple:
    """(status, counts, fiscal_year, period, narrowed) from a full result.

    The engine's payload carries journal ids, OPRIDs and amounts -- the
    right shape for a person asking, the wrong shape for a store that
    outlives the connection. Only lengths and verdicts survive, and each
    list the engine caps (unposted journals at 50, orphans at 20) yields
    a count bounded by that same cap; the feed says so rather than
    presenting a saturated count as exact.
    """
    def n(key):
        value = result.get(key)
        return len(value) if isinstance(value, list) else 0

    re_roll = result.get("retained_earnings_roll")
    counts = {
        "issues": n("issues"),
        "suspense_accounts": n("suspense_balances"),
        "accounts_missing_definition": n("accounts_missing_definition"),
        "inactive_with_balances": n("inactive_accounts_with_balances"),
        "unposted_journals": n("unposted_journals"),
        "out_of_balance_journals": n("out_of_balance_journals"),
        "re_roll_mismatch": int(isinstance(re_roll, Mapping)
                                and re_roll.get("status") == "mismatch"),
        "checks_incomplete": n("checks_incomplete"),
        "checks_narrowed": n("checks_narrowed"),
        "unbalanced": int(not result.get("balanced", True)),
    }
    status = str(result.get("control_status") or "")
    if result.get("scope_status") == "not_run" or status == "not_run":
        status = "not_run"
    elif status not in STATUSES:
        status = "error"
    try:
        fy = int(result.get("fiscal_year") or 0)
        per = int(result.get("through_period") or 0)
    except (TypeError, ValueError):
        fy, per = 0, 0
    return status, counts, fy, per, counts["checks_narrowed"] > 0


def reduce_open_payables(result: Mapping) -> tuple:
    """(status, counts, narrowed) from a full open_payables answer.

    The full payload carries voucher ids, vendor ids and amounts. What a
    standing feed needs from it is one fact -- how many vouchers are
    stuck where a payment run cannot see them -- so only exception-shaped
    counts survive. The open-voucher total is deliberately NOT a metric:
    it rises with ordinary volume, and a "worsened" event every time a
    voucher is entered is churn that trains an operator to stop reading.
    """
    exceptions = [e for e in (result.get("pipeline_exceptions") or ())
                  if isinstance(e, Mapping)]
    recycle = sum(1 for e in exceptions
                  if e.get("why") == "recycle status")
    counts = {
        "stuck_vouchers": len(exceptions),
        "in_recycle": recycle,
        "unposted": len(exceptions) - recycle,
        "result_capped": int(int(result.get("voucher_count") or 0)
                             >= 10_000),
    }
    status = "exceptions_found" if exceptions else "passed"
    # point_in_time_complete is False for every reason the answer is an
    # approximation (no CLOSE_STATUS, no date column, capped rows). The
    # differ treats a narrowed-flag CHANGE as scope_changed, so numbers
    # measured under different completeness are never compared.
    narrowed = not bool(result.get("point_in_time_complete"))
    return status, counts, narrowed


def _validate_outcome(outcome: CheckOutcome) -> CheckOutcome:
    """The persistence boundary. Nothing unvalidated goes past here.

    qlog's lesson, applied: the reducer is one line of defence, and a
    check that accidentally puts a row value into its result dict must
    ALSO be stopped where the write happens. Counts are integers under a
    hard bound, metric names come from a closed shape, statuses and
    error categories from closed vocabularies, and there is nowhere in
    the schema for free text to live.
    """
    if not _CHECK_ID.fullmatch(outcome.check_id or ""):
        raise TickerError(f"invalid check id {outcome.check_id!r}")
    if outcome.status not in STATUSES:
        raise TickerError(f"invalid status {outcome.status!r} for "
                          f"{outcome.check_id}")
    if not _BU.fullmatch(outcome.business_unit or ""):
        raise TickerError(f"invalid business unit for {outcome.check_id}")
    clean: dict = {}
    for key, value in (outcome.counts or {}).items():
        if not _METRIC.fullmatch(str(key)):
            raise TickerError(
                f"metric name {str(key)[:40]!r} is not a closed-shape "
                f"identifier ({outcome.check_id}); a metric that can "
                "carry arbitrary text can carry a row value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TickerError(
                f"metric {key} must be a plain integer, got "
                f"{type(value).__name__} ({outcome.check_id}); only "
                "counts persist, never values")
        if value < 0 or value > _MAX_COUNT:
            raise TickerError(f"metric {key} out of range "
                              f"({outcome.check_id})")
        clean[str(key)] = value
    outcome.counts = clean
    category = str(outcome.error_category or "")
    if category and category != refusal_category(category) and \
            category not in {"budget", "wall_clock", "operator_exclusion",
                             "store_unwritable"}:
        # Anything else is free text; classify it instead of keeping it.
        # The credential markers come from db.py's own list, so a raw
        # ORA-01017 lands in "credentials" rather than "tool_error".
        outcome.error_category = ("credentials"
                                  if _is_credential_failure(category)
                                  else refusal_category(category))
    return outcome


def store_path(root: Path, source: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", str(source or "default").lower())
    digest = hashlib.sha256(
        str(source or "default").encode("utf-8")).hexdigest()[:12]
    return Path(root) / "ticker" / f"{slug or 'default'}-{digest}.db"


_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE outcomes (
  check_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  business_unit TEXT NOT NULL,
  status TEXT NOT NULL,
  counts TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  period INTEGER NOT NULL,
  narrowed INTEGER NOT NULL,
  error_category TEXT NOT NULL,
  retired INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL,
  last_changed TEXT NOT NULL,
  last_run TEXT NOT NULL
);
CREATE TABLE history (
  check_id TEXT NOT NULL,
  run_at TEXT NOT NULL,
  status TEXT NOT NULL,
  counts TEXT NOT NULL,
  PRIMARY KEY (check_id, run_at)
);
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  check_id TEXT NOT NULL,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  metric TEXT NOT NULL,
  before_n INTEGER,
  after_n INTEGER
);
"""


class TickerStore:
    """Last-outcome-per-check plus bounded history, bound to one source.

    The file is bound to the source AND its endpoint fingerprint, checked
    at every open: baselines diffed against a different database would
    report "exception cleared" about data nobody fixed. A changed
    fingerprint archives the old file beside itself and starts fresh,
    with a baseline_reset event -- disclosed, never silent.

    Writers take a cross-process advisory lock: nothing prevents two GUI
    processes locally, and two runners writing one store interleaved is
    how a diff ends up computed against a half-written baseline.
    """

    def __init__(self, path: Path, *, source: str, fingerprint: str,
                 limits: TickerLimits):
        self.path = Path(path)
        self.source = str(source or "default")
        self.fingerprint = str(fingerprint or "")
        self.limits = limits
        self._lock = threading.RLock()
        self.reset_category = ""

    # -- lifecycle ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise TickerError(f"{self.path} is a symlink; the ticker "
                              "refuses to follow links for its store")
        fresh = not self.path.exists()
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=DELETE")
        if fresh:
            con.executescript(_DDL)
            now = _utcnow()
            con.executemany(
                "INSERT INTO meta VALUES (?,?)",
                [("schema_version", str(SCHEMA_VERSION)),
                 ("source", self.source),
                 ("source_fingerprint", self.fingerprint),
                 ("created_at", now)])
            con.commit()
            os.chmod(self.path, 0o600)
            return con
        meta = {row["key"]: row["value"]
                for row in con.execute("SELECT key, value FROM meta")}
        mismatch = ""
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            mismatch = "schema_changed"
        elif meta.get("source") != self.source:
            mismatch = "source_changed"
        elif meta.get("source_fingerprint") != self.fingerprint:
            mismatch = "endpoint_changed"
        if mismatch:
            # Baselines from another schema version or another endpoint
            # are not baselines; diffing against them would manufacture
            # cleared/new events about the wrong world. Archive, reset,
            # and say so through a baseline_reset event on the new store.
            con.close()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.path.replace(
                self.path.with_name(self.path.name +
                                    f".superseded-{stamp}"))
            self.reset_category = mismatch
            return self._connect()
        return con

    def _connect_readonly(self) -> sqlite3.Connection:
        """A reader may refuse; it may never MUTATE. The archive path
        renames the store, and a GET endpoint that renames files on a
        metadata mismatch is a write disguised as a read -- and it runs
        without the writer's lock. Readers open read-only and report a
        mismatch as unreadable; the next tick, under the lock, resets."""
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        meta = {row["key"]: row["value"]
                for row in con.execute("SELECT key, value FROM meta")}
        if meta.get("schema_version") != str(SCHEMA_VERSION) or                 meta.get("source") != self.source or                 meta.get("source_fingerprint") != self.fingerprint:
            con.close()
            raise TickerError(
                "these baselines belong to a different endpoint or "
                "schema version; the next tick will archive and reset "
                "them")
        return con

    def _flock(self):
        # The lock is taken BEFORE the store is opened, so the parent
        # directory must exist here too: the lock serialises the
        # archive-and-recreate path, which must not race a second writer.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.parent / f".{self.path.name}.lock"
        handle = open(lock_path, "a+b")
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:                       # pragma: no cover
            pass
        return handle

    # -- writing -----------------------------------------------------------
    def record_tick(self, outcomes: Iterable[CheckOutcome],
                    scheduled_ids: Iterable[str], *,
                    tick_at: str = "", queries_used: int = 0,
                    duration_ms: int = 0, partial: bool = False) -> list:
        """Persist one tick and return the events its diffs produced."""
        now = tick_at or _utcnow()
        validated = [_validate_outcome(o) for o in outcomes]
        scheduled = {str(s) for s in scheduled_ids}
        with self._lock:
            guard = self._flock()
            try:
                con = self._connect()
                try:
                    events = self._diff_and_write(
                        con, validated, scheduled, now)
                    if self.reset_category:
                        events.insert(0, self._event(
                            con, "ticker", now, "baseline_reset",
                            metric=self.reset_category))
                        self.reset_category = ""
                    con.execute(
                        "INSERT INTO meta VALUES ('last_tick', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps({
                            "at": now, "checks": len(validated),
                            "queries_used": int(queries_used),
                            "duration_ms": int(duration_ms),
                            "partial": bool(partial)}),))
                    self._prune(con)
                    con.commit()
                finally:
                    con.close()
            finally:
                guard.close()
        return events

    def _event(self, con, check_id, at, kind, *, metric="",
               before=None, after=None) -> dict:
        if kind not in EVENT_KINDS:
            raise TickerError(f"invalid event kind {kind!r}")
        event = {"event_id": uuid.uuid4().hex, "check_id": check_id,
                 "at": at, "kind": kind, "metric": metric,
                 "before_n": before, "after_n": after}
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                    (event["event_id"], check_id, at, kind, metric,
                     before, after))
        return event

    def _diff_and_write(self, con, outcomes, scheduled, now) -> list:
        events: list = []
        previous = {row["check_id"]: row for row in
                    con.execute("SELECT * FROM outcomes")}
        for outcome in outcomes:
            prev = previous.get(outcome.check_id)
            if prev is not None and outcome.status in (
                    "error", "refused", "not_run"):
                # A failure knows nothing about scope or magnitude; it
                # must inherit both. A failure that reported fy=0 was
                # read by the differ as a period roll and RE-BASELINED a
                # standing exception -- and a failure that wiped the
                # counts made the eventual recovery diff against zero,
                # reporting every standing exception as brand new.
                if not outcome.counts:
                    outcome.counts = json.loads(prev["counts"])
                if not outcome.fiscal_year and not outcome.period:
                    outcome.fiscal_year = prev["fiscal_year"]
                    outcome.period = prev["period"]
                outcome.narrowed = bool(prev["narrowed"])
            events.extend(self._diff_one(con, prev, outcome, now))
            changed = (prev is None
                       or prev["status"] != outcome.status
                       or json.loads(prev["counts"]) != outcome.counts
                       or bool(prev["narrowed"]) != outcome.narrowed)
            con.execute(
                "INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?) "
                "ON CONFLICT(check_id) DO UPDATE SET source=excluded.source,"
                "business_unit=excluded.business_unit,"
                "status=excluded.status, counts=excluded.counts,"
                "fiscal_year=excluded.fiscal_year, period=excluded.period,"
                "narrowed=excluded.narrowed,"
                "error_category=excluded.error_category, retired=0,"
                "last_changed=CASE WHEN ? THEN excluded.last_changed "
                "ELSE outcomes.last_changed END,"
                "last_run=excluded.last_run",
                (outcome.check_id, outcome.source, outcome.business_unit,
                 outcome.status, json.dumps(outcome.counts),
                 outcome.fiscal_year, outcome.period,
                 int(outcome.narrowed), outcome.error_category,
                 now, now, now, changed))
            con.execute(
                "INSERT OR REPLACE INTO history VALUES (?,?,?,?)",
                (outcome.check_id, now, outcome.status,
                 json.dumps(outcome.counts)))
        # A check that used to run and is no longer scheduled is the
        # loudest signal there is: the next verdict is not comparable,
        # and a feed that stays green because nothing updates it is the
        # ticker's stated worst failure.
        for check_id, row in previous.items():
            if check_id not in scheduled and not row["retired"]:
                con.execute("UPDATE outcomes SET retired=1 "
                            "WHERE check_id=?", (check_id,))
                events.append(self._event(con, check_id, now,
                                          "no_longer_scheduled"))
        return events

    def _diff_one(self, con, prev, outcome: CheckOutcome, now) -> list:
        totals = sum(v for k, v in outcome.counts.items()
                     if k != "checks_narrowed")
        if prev is None:
            return [self._event(con, outcome.check_id, now, "baseline",
                                after=totals)]
        if (prev["fiscal_year"], prev["period"]) != (
                outcome.fiscal_year, outcome.period):
            # A new period is a new world. Diffed across the roll, every
            # cleared item reads as fixed and every new one as a
            # regression -- the monitor script wrote that lesson down
            # and the ticker keeps it.
            return [self._event(con, outcome.check_id, now, "baseline",
                                after=totals)]
        if bool(prev["narrowed"]) != outcome.narrowed:
            # The scope changed, so the numbers are not comparable this
            # tick; a narrowing that read as "exceptions cleared" would
            # be a lie of the most convincing kind.
            return [self._event(con, outcome.check_id, now,
                                "scope_changed")]
        if prev["status"] != outcome.status:
            kind = {
                "exceptions_found": "new_exceptions",
                "checks_incomplete": "became_incomplete",
                "refused": "refused",
                "error": "error",
                "not_run": "budget_cut",
            }.get(outcome.status)
            if outcome.status == "passed":
                kind = ("cleared" if prev["status"] == "exceptions_found"
                        else "recovered")
            return [self._event(con, outcome.check_id, now, kind,
                                after=totals)]
        if outcome.status == "exceptions_found":
            before = json.loads(prev["counts"])
            ups = [(k, before.get(k, 0), v)
                   for k, v in outcome.counts.items()
                   if v > int(before.get(k, 0) or 0)]
            downs = [(k, before.get(k, 0), v)
                     for k, v in outcome.counts.items()
                     if v < int(before.get(k, 0) or 0)]
            if ups:
                metric, b, a = max(ups, key=lambda t: t[2] - t[1])
                return [self._event(con, outcome.check_id, now,
                                    "worsened", metric=metric,
                                    before=b, after=a)]
            if downs:
                metric, b, a = max(downs, key=lambda t: t[1] - t[2])
                return [self._event(con, outcome.check_id, now,
                                    "improved", metric=metric,
                                    before=b, after=a)]
        return []

    def _prune(self, con) -> None:
        for (check_id,) in con.execute(
                "SELECT DISTINCT check_id FROM history"):
            con.execute(
                "DELETE FROM history WHERE check_id=? AND run_at NOT IN "
                "(SELECT run_at FROM history WHERE check_id=? "
                "ORDER BY run_at DESC LIMIT ?)",
                (check_id, check_id, self.limits.history_per_check))
        con.execute(
            "DELETE FROM events WHERE event_id NOT IN "
            "(SELECT event_id FROM events ORDER BY at DESC, event_id "
            "LIMIT ?)", (self.limits.events_kept,))

    # -- reading -----------------------------------------------------------
    def read_feed(self, *, now: str = "") -> dict:
        """Everything the /api/exceptions envelope needs, revalidated.

        One malformed row fails the whole read closed (readable=False,
        no rows): a store that has been tampered with or corrupted must
        not have its plausible rows served around the damage.
        """
        current = _parse_ts(now or _utcnow())
        if not self.path.exists():
            return {"readable": True, "rows": [], "events": [],
                    "last_tick": None, "stale": True, "age_seconds": None,
                    "note": "no ticks have been recorded yet"}
        try:
            con = self._connect_readonly()
        except (TickerError, sqlite3.Error) as exc:
            return {"readable": False, "rows": [], "events": [],
                    "last_tick": None, "stale": True, "age_seconds": None,
                    "note": f"the ticker store is not readable "
                            f"({exc}); no exception state "
                            "is being shown rather than partial state"}
        try:
            rows = []
            for row in con.execute(
                    "SELECT * FROM outcomes ORDER BY last_changed DESC"):
                counts = json.loads(row["counts"])
                if (row["status"] not in STATUSES
                        or not _CHECK_ID.fullmatch(row["check_id"])
                        or not isinstance(counts, dict)
                        or any(not _METRIC.fullmatch(str(k))
                               or isinstance(v, bool)
                               or not isinstance(v, int)
                               for k, v in counts.items())):
                    raise TickerError("malformed outcome row")
                rows.append({
                    "check_id": row["check_id"], "source": row["source"],
                    "business_unit": row["business_unit"],
                    "status": row["status"], "counts": counts,
                    "fiscal_year": row["fiscal_year"],
                    "period": row["period"],
                    "narrowed": bool(row["narrowed"]),
                    "error_category": row["error_category"],
                    "retired": bool(row["retired"]),
                    "first_seen": row["first_seen"],
                    "last_changed": row["last_changed"],
                    "last_run": row["last_run"],
                })
            events = [{
                "check_id": row["check_id"], "at": row["at"],
                "kind": row["kind"], "metric": row["metric"],
                "before_n": row["before_n"], "after_n": row["after_n"],
            } for row in con.execute(
                "SELECT * FROM events ORDER BY at DESC, event_id "
                "LIMIT 100")]
            raw = con.execute(
                "SELECT value FROM meta WHERE key='last_tick'").fetchone()
            last_tick = json.loads(raw["value"]) if raw else None
        except (TickerError, ValueError, sqlite3.Error) as exc:
            return {"readable": False, "rows": [], "events": [],
                    "last_tick": None, "stale": True, "age_seconds": None,
                    "note": f"the ticker store failed validation "
                            f"({type(exc).__name__}); no exception state "
                            "is being shown rather than partial state"}
        finally:
            con.close()
        tick_at = _parse_ts((last_tick or {}).get("at"))
        # Staleness fails CLOSED: no tick, or a timestamp that will not
        # parse, is stale -- an old result presented as current is this
        # feature's worst failure mode.
        stale = True
        age_seconds = None
        if tick_at is not None and current is not None:
            delta = int((current - tick_at).total_seconds())
            age_seconds = max(delta, 0)
            # A tick from the FUTURE is clock skew, and clock skew makes
            # every age computation untrustworthy -- clamping it to zero
            # would present precisely the wrong verdict, "fresh".
            stale = (delta < 0
                     or delta > 2 * self.limits.cadence_minutes * 60)
        return {"readable": True, "rows": rows, "events": events,
                "last_tick": last_tick, "stale": stale,
                "age_seconds": age_seconds, "note": ""}


class TickerRunner:
    """The loop. One daemon thread, one check at a time, budget first.

    ``resolve`` is called on EVERY tick and returns the current engine
    and config: the console reload swaps the GUI's module globals at
    runtime, and a runner that captured them once would keep checking
    the previous database -- the documented failure the scope catalog's
    generation counter exists for.
    """

    def __init__(self, resolve: Callable[[], object], *,
                 store_factory: Callable[[object, TickerLimits],
                                         TickerStore] | None = None):
        self._resolve = resolve
        self._store_factory = store_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self.state = "idle"
        self.consecutive_failures = 0
        self.last_error_category = ""
        self.last_tick_at = ""
        self._terminal = False

    # -- public surface ----------------------------------------------------
    def status(self) -> dict:
        return {"state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "last_error_category": self.last_error_category,
                "last_tick_at": self.last_tick_at}

    def start(self) -> None:
        if self._thread is not None:
            return
        # One runner per config root, across processes: nothing stops a
        # second GUI process locally, and two tickers against one store
        # is double the database load with interleaved baselines. The
        # loser stays passive -- it serves the feed, it never queries.
        try:
            context = self._resolve()
            root = Path(getattr(context.cfg, "root", "."))
            lock_dir = root / "ticker"
            lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._runner_lock = open(lock_dir / ".runner.lock", "a+b")
            import fcntl
            fcntl.flock(self._runner_lock.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.state = "passive"
            return
        except Exception:                         # noqa: BLE001
            pass
        self._thread = threading.Thread(
            target=self._loop, name="pstb-ticker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self.state != "tripped":
            self.state = "stopped"

    def run_tick_once(self, *, now: str = "") -> dict:
        """One tick, synchronously. The thread calls this; so do tests."""
        # Overlap prevention: a tick that finds the previous one still
        # running SKIPS and says so. Stacking missed ticks against a
        # production database is a retry storm with a schedule.
        if not self._tick_lock.acquire(blocking=False):
            return {"ran": False, "reason": "previous tick still running"}
        try:
            return self._tick(now or _utcnow())
        finally:
            self._tick_lock.release()
    # -- internals ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                context = self._resolve()
                limits = TickerLimits.from_config(
                    getattr(context.cfg, "ticker", None))
                delay = limits.cadence_minutes * 60
            except Exception:                     # noqa: BLE001
                delay = 1800
            if self._stop.wait(delay):
                return
            try:
                result = self.run_tick_once()
            except Exception:                     # noqa: BLE001
                # run_tick_once already classifies its own failures; a
                # raise reaching here must not kill the thread silently.
                self.consecutive_failures += 1
                result = {}
            if result.get("reason") == "disabled":
                self.state = "stopped"
                return
            if self.state == "tripped":
                return

    def _tick(self, now: str) -> dict:
        if self.state == "tripped" or self._terminal:
            return {"ran": False, "reason": "circuit breaker tripped; "
                    "restart the process after fixing the cause"}
        self.state = "running"
        started = time.monotonic()
        try:
            context = self._resolve()
            cfg = context.cfg
            engine = context.engine
            source = getattr(context, "source", "default")
            limits = TickerLimits.from_config(getattr(cfg, "ticker", None))
        except Exception as exc:                  # noqa: BLE001
            return self._fail(refusal_category(str(exc)), limits=None)
        if getattr(getattr(cfg, "ticker", None),
                   "enabled", False) is not True:
            # The console reload can swap the config under a running
            # loop; a loop that keeps querying after the operator turned
            # it off is a loop the operator cannot turn off. One resolve
            # serves both this check and the tick, so the two cannot
            # straddle a reload and disagree.
            self.state = "idle"
            return {"ran": False, "reason": "disabled"}

        ticker_cfg = getattr(cfg, "ticker", None)
        units = [str(u) for u in
                 (getattr(ticker_cfg, "business_units", None) or [])
                 if str(u).strip()]
        if not units:
            default_bu = str(getattr(getattr(cfg, "defaults", None),
                                     "business_unit", "") or "")
            units = [default_bu] if default_bu else []
        ledger = str(getattr(ticker_cfg, "ledger", "") or "")

        # The plan, built before any query: which checks run this tick,
        # each with the worst-case cost the budget reserves BEFORE it
        # starts. A check that leaves the plan (the operator flips its
        # flag off) leaves `scheduled`, and the differ turns that into a
        # no_longer_scheduled event rather than a silence.
        plan = [("tb_integrity", TB_INTEGRITY_QUERY_COST,
                 lambda u, cid: self._run_tb_integrity(
                     engine, source, u, ledger, cid))]
        if getattr(ticker_cfg, "watch_invoicing", False) is True:
            plan.append(("ap_pipeline", AP_PIPELINE_QUERY_COST,
                         lambda u, cid: self._run_ap_pipeline(
                             context, engine, source, u, cid)))

        outcomes: list[CheckOutcome] = []
        scheduled: list[str] = []
        queries_used = 0
        partial = False
        for check_name, check_cost, run_check in plan:
            for unit in units:
                check_id = f"{check_name}:{source}:{unit}"
                scheduled.append(check_id)
                elapsed = time.monotonic() - started
                if queries_used + check_cost > \
                        limits.max_queries_per_tick:
                    partial = True
                    outcomes.append(CheckOutcome(
                        check_id=check_id, source=source,
                        business_unit=unit,
                        status="not_run", error_category="budget"))
                    continue
                if elapsed > limits.max_seconds_per_tick:
                    partial = True
                    outcomes.append(CheckOutcome(
                        check_id=check_id, source=source,
                        business_unit=unit,
                        status="not_run", error_category="wall_clock"))
                    continue
                outcomes.append(run_check(unit, check_id))
                queries_used += check_cost

        try:
            store = self._store(context, limits)
            events = store.record_tick(
                outcomes, scheduled, tick_at=now,
                queries_used=queries_used,
                duration_ms=int((time.monotonic() - started) * 1000),
                partial=partial)
        except Exception as exc:                  # noqa: BLE001
            return self._fail("store_unwritable" if not isinstance(
                exc, TickerError) else refusal_category(str(exc)),
                limits=limits)
        # A terminal condition discovered mid-tick (refused credentials)
        # must survive the epilogue: the tick still records its outcomes,
        # but the runner stays tripped rather than being reset to idle by
        # the very success of writing the failure down.
        all_errored = bool(outcomes) and all(
            o.status == "error" for o in outcomes)
        if all_errored and not self._terminal:
            # The store wrote fine, but every check failed. A breaker
            # that only counts plumbing failures lets a dead database be
            # retried forever -- each attempt holding sessions for up to
            # the query timeout -- while the runner reports "idle".
            self.consecutive_failures += 1
            self.last_error_category = outcomes[0].error_category or                 "tool_error"
            if self.consecutive_failures >= limits.failure_trip:
                self.state = "tripped"
            else:
                self.state = "idle"
        elif not self._terminal:
            self.state = "idle"
            self.consecutive_failures = 0
            self.last_error_category = ""
        self.last_tick_at = now
        return {"ran": True, "checks": len(outcomes),
                "queries_used": queries_used, "partial": partial,
                "events": len(events)}

    def _veto_refusal(self, engine, base: CheckOutcome,
                      reads: tuple) -> CheckOutcome | None:
        """The operator's veto binds background work exactly as it binds
        a question, re-read every tick so a veto approved mid-flight
        takes effect on the next one. None means the check may proceed."""
        try:
            engine._require_records_allowed(
                reads, action="Continuous tie-out monitoring")
        except Exception as exc:                  # noqa: BLE001
            base.status = "refused"
            base.error_category = "operator_exclusion" if \
                "excluded" in str(exc).lower() else \
                refusal_category(str(exc))
            return base
        return None

    def _failure(self, engine, base: CheckOutcome,
                 exc: Exception) -> CheckOutcome:
        """Classify one check's failure, tripping terminally on refused
        credentials: every further attempt would bury the remedy under
        repetition, and rebuilding connections against refused
        credentials is how a service account gets locked."""
        text = str(exc)
        category = ("credentials" if _is_credential_failure(text)
                    else refusal_category(text))
        base.error_category = category
        if category == "credentials" or getattr(
                getattr(engine, "db", None), "_credentials_refused", ""):
            self._terminal = True
            self.state = "tripped"
            self.last_error_category = category
        return base

    def _run_tb_integrity(self, engine, source, unit, ledger,
                          check_id) -> CheckOutcome:
        base = CheckOutcome(check_id=check_id, source=source,
                            business_unit=unit, status="error")
        refused = self._veto_refusal(engine, base, TB_INTEGRITY_READS)
        if refused is not None:
            return refused
        try:
            result = engine.tb_integrity_check(
                business_unit=unit, ledger=ledger)
        except Exception as exc:                  # noqa: BLE001
            return self._failure(engine, base, exc)
        status, counts, fy, per, narrowed = reduce_tb_integrity(result)
        return CheckOutcome(
            check_id=check_id, source=source, business_unit=unit,
            status=status, counts=counts, fiscal_year=fy, period=per,
            narrowed=narrowed)

    def _run_ap_pipeline(self, context, engine, source, unit,
                         check_id) -> CheckOutcome:
        """Vouchers stuck in recycle or unposted status: money that is
        owed but invisible to a payment run until someone fixes the
        entry. One bounded query, no external system -- the playbook
        that also covers this ground (ap_completeness) is structurally
        always-incomplete and calls the Coupa API on every run, which is
        exactly what a standing loop must never schedule."""
        base = CheckOutcome(check_id=check_id, source=source,
                            business_unit=unit, status="error")
        refused = self._veto_refusal(engine, base, AP_PIPELINE_READS)
        if refused is not None:
            return refused
        modules = getattr(context, "modules", None)
        if modules is None:
            # A context without the module pack (an embedding without the
            # GUI's object graph) is a wiring gap, not a database fact;
            # the row says so instead of the loop dying on it.
            base.error_category = "tool_error"
            return base
        try:
            result = modules.open_payables(business_unit=unit)
        except Exception as exc:                  # noqa: BLE001
            return self._failure(engine, base, exc)
        status, counts, narrowed = reduce_open_payables(result)
        return CheckOutcome(
            check_id=check_id, source=source, business_unit=unit,
            status=status, counts=counts, narrowed=narrowed)

    def _store(self, context, limits: TickerLimits) -> TickerStore:
        if self._store_factory is not None:
            return self._store_factory(context, limits)
        from .metadata import source_fingerprint
        cfg = context.cfg
        source = getattr(context, "source", "default")
        return TickerStore(
            store_path(Path(getattr(cfg, "root", ".")), source),
            source=source,
            fingerprint=source_fingerprint(cfg, source),
            limits=limits)

    def _fail(self, category: str, *, limits) -> dict:
        self.consecutive_failures += 1
        self.last_error_category = category
        trip = limits.failure_trip if limits is not None else 3
        if self.consecutive_failures >= trip:
            self.state = "tripped"
        else:
            self.state = "idle"
        return {"ran": False, "reason": category,
                "consecutive_failures": self.consecutive_failures,
                "state": self.state}
