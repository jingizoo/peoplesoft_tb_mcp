"""Which business units may this person see?

WHAT THIS IS. PeopleSoft already knows the answer — row-level security by
business unit is standard FSCM configuration, held either against the user
ID or against their row-security permission list. This module reads that
configuration and turns it into one object the rest of the app can ask:
``access.allows("US200")``. It does not invent a parallel permission model,
because a second source of truth about who may see what is worse than
none: the two drift, and the one people trust is the wrong one.

WHAT THIS IS NOT — READ THIS BEFORE TRUSTING IT. The sign-in that feeds
this asks for a user ID and no password. Anyone who can reach the page can
type any user ID, including a privileged one. So today this is a SCOPE
SELECTOR that mirrors PeopleSoft's rules, not an access control that
enforces them: it stops an honest person seeing another unit's numbers by
accident, and stops the model wandering across units. It stops nobody who
is trying. The seam where that changes is identity — resolve_operator() in
pstb/gui/app.py — and nothing else in this file has to move when SSO
supplies a verified user instead of a typed one.

WHY IT FAILS CLOSED. If security is switched on and the security records
cannot be read — not granted, not present, a dead session — this returns
NO access rather than all access, and says which grant is missing. The
alternative is a deployment that silently shows every unit to everyone the
first time a grant is wrong, which is the exact failure the feature exists
to prevent. `security.on_unavailable: allow` exists for a site that would
rather degrade the other way, and it is not the default.

WHY THE RECORD NAMES ARE DISCOVERED, NOT ASSUMED. PS_SEC_BU_OPR and
PS_SEC_BU_CLS are the stock FSCM records, and a real installation may have
neither: sites customise this, and a read-only reporting account is often
granted the ledger and not the security tables. So the candidates are
probed against the live catalog, the site can name its own record in
config, and `scripts/diagnose_bu_security.py` prints what is actually
there. Guessing a record name and hardcoding it is how you get an app that
works on one instance and refuses everything on the next.
"""
from __future__ import annotations

import threading
import time
import contextvars
from dataclasses import dataclass, field
from typing import Callable, Optional

from .db import DbError

# The stock FSCM row-security records, in the order they are tried.
#
#   by user           PS_SEC_BU_OPR (OPRID, BUSINESS_UNIT)
#   by permission     PS_SEC_BU_CLS (OPRCLASS, BUSINESS_UNIT), joined to the
#                     user through PSOPRDEFN.ROWSECCLASS
#
# A site that keeps this somewhere else sets security.unit_record /
# security.unit_key in config.yaml and this list is not consulted.
USER_UNIT_RECORDS: tuple = (("PS_SEC_BU_OPR", "OPRID"),)
CLASS_UNIT_RECORDS: tuple = (("PS_SEC_BU_CLS", "OPRCLASS"),)

OPERATOR_RECORD = "PSOPRDEFN"
ROW_SECURITY_FIELD = "ROWSECCLASS"

# How long a resolved access set is trusted. Security changes in PeopleSoft
# are rare and deliberate; re-reading per query would put a round trip in
# front of everything. Short enough that a grant added during a support call
# takes effect without a restart.
ACCESS_TTL_SECONDS = 300

# A discovered record is installation metadata, but it is still live
# metadata: grants can be added after the process starts and a temporary
# catalog outage must not become the answer until restart.  Only a positive
# discovery is cached, and even that is periodically revalidated.
SOURCE_TTL_SECONDS = 300


class SecurityError(Exception):
    """Access could not be established. The message is shown to the user, so
    it names the missing grant rather than the failing SQL."""


@dataclass(frozen=True)
class Access:
    """One person's business-unit reach, and how it was decided.

    ``source`` and ``detail`` are carried because an empty result has two
    very different meanings — "PeopleSoft says this user has no units" and
    "we could not read the security tables" — and a user staring at an
    empty screen deserves to know which.
    """

    oprid: str
    all_units: bool = False
    units: frozenset = field(default_factory=frozenset)
    source: str = ""
    detail: str = ""
    privileged: bool = False

    def allows(self, business_unit: str) -> bool:
        bu = (business_unit or "").strip().upper()
        if not bu:
            return True                  # "no unit named" is not a breach
        if self.all_units:
            return True
        return bu in self.units

    def visible(self, units) -> list:
        """Filter a catalog of units down to what this person may see."""
        if self.all_units:
            return list(units)
        return [u for u in units
                if str(u or "").strip().upper() in self.units]

    def refusal(self, business_unit: str) -> str:
        bu = (business_unit or "").strip().upper()
        if self.units:
            allowed = ", ".join(sorted(self.units))
            return (f"{self.oprid} is not authorised for business unit {bu}. "
                    f"PeopleSoft grants this user: {allowed}.")
        return (f"{self.oprid} is not authorised for business unit {bu}. "
                f"{self.detail or 'No business units are granted to this user.'}")

    def narrow(self, units) -> tuple:
        """(allowed, dropped) from a candidate unit list.

        The cross-unit counterpart to allows(). A tool that answers ACROSS
        units cannot be gated by checking one business_unit argument — the
        argument says "ALL" and means "every unit that exists", which for a
        restricted user is a superset of what they may see. So the unit list
        is intersected here and the tool DISCLOSES what it dropped, rather
        than quietly returning a narrower answer that looks complete.
        """
        seen = [str(u or "").strip().upper() for u in units]
        seen = [u for u in seen if u]
        if self.all_units:
            return seen, []
        allowed = [u for u in seen if u in self.units]
        return allowed, [u for u in seen if u not in self.units]

    def describe(self) -> str:
        if self.privileged:
            return f"{self.oprid} — every business unit (privileged)"
        if self.all_units:
            return f"{self.oprid} — every business unit"
        if not self.units:
            return f"{self.oprid} — no business units"
        shown = sorted(self.units)
        if len(shown) > 6:
            return (f"{self.oprid} — {len(shown)} business units "
                    f"({', '.join(shown[:6])}, …)")
        return f"{self.oprid} — {', '.join(shown)}"


def _normalise(values) -> frozenset:
    return frozenset(str(v or "").strip().upper()
                     for v in values if str(v or "").strip())


class RowSecurity:
    """Reads PeopleSoft's business-unit security for a user ID."""

    def __init__(self, db, cfg,
                 clock: Callable[[], float] = time.monotonic):
        self.db = db
        self.cfg = cfg
        self._clock = clock
        self._cache: dict = {}
        self._lock = threading.RLock()
        self._source_cache: Optional[tuple[float, tuple]] = None
        # Invalidation can race a slow catalog/access query.  A generation
        # makes the clear linearizable: work that began before invalidate()
        # may finish for its caller, but it cannot repopulate either cache.
        self._cache_generation = 0
        # Invalidation is not the only ordering hazard: two ordinary misses
        # can overlap and the older snapshot can finish last.  Only the most
        # recently started attempt for a user/source is allowed to publish.
        self._access_attempt_sequence = 0
        self._latest_access_attempt: dict[str, int] = {}
        self._source_attempt_sequence = 0
        self._latest_source_attempt: Optional[int] = None

    # ---- configuration ---------------------------------------------------
    @property
    def _sec(self):
        return getattr(self.cfg, "security", None)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._sec, "enabled", False))

    @property
    def privileged_users(self) -> frozenset:
        return _normalise(getattr(self._sec, "privileged_users", ()) or ())

    def _fail_open(self) -> bool:
        return str(getattr(self._sec, "on_unavailable", "refuse")
                   ).strip().lower() == "allow"

    # ---- discovery -------------------------------------------------------
    def source_record(self) -> tuple:
        """(record, key_field, kind) for this site, or ("", "", "none").

        Positive discoveries are remembered briefly: the catalog lookup is
        itself a query, and this is asked on every sign-in.  A negative probe
        is never cached because it can mean a transient connection or grant
        failure rather than "this installation has no security record".
        """
        with self._lock:
            now = self._clock()
            hit = self._source_cache
            if hit is not None and now < hit[0]:
                return hit[1]
            self._source_cache = None
            generation = self._cache_generation
            self._source_attempt_sequence += 1
            attempt = self._source_attempt_sequence
            self._latest_source_attempt = attempt
        configured = str(getattr(self._sec, "unit_record", "") or "").strip()
        if configured:
            key = (str(getattr(self._sec, "unit_key", "") or "OPRID").strip()
                   or "OPRID")
            kind = "class" if key.upper() != "OPRID" else "user"
            found = (configured.upper(), key.upper(), kind)
        else:
            found = ("", "", "none")
            for record, key in USER_UNIT_RECORDS + CLASS_UNIT_RECORDS:
                cols = self._columns(record)
                if cols and key in cols and "BUSINESS_UNIT" in cols:
                    kind = "user" if key == "OPRID" else "class"
                    found = (record, key, kind)
                    break
        with self._lock:
            if found[0]:
                if (generation == self._cache_generation
                        and attempt == self._latest_source_attempt):
                    self._source_cache = (
                        self._clock() + SOURCE_TTL_SECONDS, found)
            if attempt == self._latest_source_attempt:
                self._latest_source_attempt = None
        return found

    def _columns(self, record: str) -> set:
        try:
            return {c.upper() for c in self.db.columns(record)}
        except Exception:
            return set()


    # ---- the question ----------------------------------------------------
    def access_for(self, oprid: str) -> Access:
        """Resolve one user's reach. Never raises for a normal 'no access'
        answer — that is a legitimate result, and the caller renders it."""
        who = (oprid or "").strip().upper()
        if not who:
            raise SecurityError("Sign in with a PeopleSoft user ID.")
        if not self.enabled:
            return Access(oprid=who, all_units=True, source="disabled",
                          detail=("Business-unit security is switched off "
                                  "for this deployment "
                                  "(security.enabled: false)."))
        with self._lock:
            now = self._clock()
            hit = self._cache.get(who)
            if hit and now < hit[0]:
                return hit[1]
            generation = self._cache_generation
            self._access_attempt_sequence += 1
            attempt = self._access_attempt_sequence
            self._latest_access_attempt[who] = attempt
        try:
            access = self._resolve(who)
        except BaseException:
            with self._lock:
                if self._latest_access_attempt.get(who) == attempt:
                    self._latest_access_attempt.pop(who, None)
            raise
        # "Unavailable" is not a PeopleSoft grant.  In fail-open mode it is
        # represented as all-units so the request can proceed, but remembering
        # that answer would keep broad access alive after the database or grant
        # recovers.  Valid empty grants remain cacheable: those are an
        # authoritative result from the configured security record.
        with self._lock:
            if access.source != "unavailable":
                if (generation == self._cache_generation
                        and self._latest_access_attempt.get(who) == attempt):
                    # Bound a grant from the start of its statement-consistent
                    # read.  A slow query must not extend a revoked grant by
                    # its latency; an already-expired entry simply refreshes
                    # on the next request.
                    self._cache[who] = (
                        now + ACCESS_TTL_SECONDS, access)
            if self._latest_access_attempt.get(who) == attempt:
                self._latest_access_attempt.pop(who, None)
        return access

    def invalidate(self, oprid: str = "") -> None:
        with self._lock:
            self._cache_generation += 1
            self._latest_access_attempt.clear()
            self._latest_source_attempt = None
            if oprid:
                self._cache.pop(oprid.strip().upper(), None)
            else:
                self._cache.clear()
            self._source_cache = None

    def _resolve(self, who: str) -> Access:
        if who in self.privileged_users:
            # Named in config, so it does not depend on the security tables
            # being readable — this is the account that has to keep working
            # while a grant is being fixed.
            return Access(oprid=who, all_units=True, privileged=True,
                          source="config",
                          detail=("Privileged in config.yaml "
                                  "(security.privileged_users)."))
        known, why = self._operator_exists(who)
        if known is False:
            raise SecurityError(
                f"{who} is not a user in {OPERATOR_RECORD}. Check the user ID "
                "— PeopleSoft user IDs are usually upper case.")

        record, key, kind = self.source_record()
        if not record:
            return self._unavailable(
                who,
                "No business-unit security record was found. Looked for "
                + ", ".join(r for r, _ in USER_UNIT_RECORDS + CLASS_UNIT_RECORDS)
                + ". If this site keeps unit security in another record, set "
                  "security.unit_record and security.unit_key in config.yaml; "
                  "run scripts/diagnose_bu_security.py on the host to see "
                  "what is readable.")
        try:
            units = (self._units_by_user(record, key, who) if kind == "user"
                     else self._units_by_class(record, key, who))
        except DbError as e:
            dependency = (record if kind == "user" else
                          f"{record} or {OPERATOR_RECORD}.{ROW_SECURITY_FIELD}")
            return self._unavailable(
                who, f"Could not read {dependency}: {e}. A read-only reporting "
                     f"account often lacks SELECT on the security records; "
                     f"that grant is what this needs.")
        if not units:
            return Access(
                oprid=who, units=frozenset(), source=record,
                detail=(f"{record} grants {who} no business units."
                        + (f" {why}" if why else "")))
        return Access(oprid=who, units=units, source=record,
                      detail=f"{len(units)} business unit(s) from {record}.")

    def _unavailable(self, who: str, why: str) -> Access:
        if self._fail_open():
            return Access(oprid=who, all_units=True, source="unavailable",
                          detail=("Security could not be read, and this "
                                  "deployment is configured to allow access "
                                  "when that happens "
                                  "(security.on_unavailable: allow). " + why))
        raise SecurityError(
            "Business-unit security is switched on but could not be read, so "
            "no data is shown — showing everything instead would defeat the "
            "point of the setting.\n  " + why +
            "\n  To run without unit security, set security.enabled: false.")

    def _operator_exists(self, who: str) -> tuple:
        """(True/False/None, note). None means 'cannot tell' — never refuse
        a sign-in because the operator table is not granted."""
        cols = self._columns(OPERATOR_RECORD)
        if not cols or "OPRID" not in cols:
            return None, ""
        try:
            rows, _ = self.db.query(
                f"SELECT OPRID FROM {self.db.prefix}{OPERATOR_RECORD} "
                "WHERE UPPER(OPRID) = :who", {"who": who}, max_rows=1)
        except DbError:
            return None, ""
        return (bool(rows), "")

    def _units_by_user(self, record: str, key: str, who: str) -> frozenset:
        rows, _ = self.db.query(
            f"SELECT DISTINCT BUSINESS_UNIT AS bu "
            f"FROM {self.db.prefix}{record} WHERE UPPER({key}) = :who",
            {"who": who}, max_rows=5000)
        return _normalise(r.get("bu") for r in rows)

    def _units_by_class(self, record: str, key: str, who: str) -> frozenset:
        """Units reached through the user's ROW SECURITY permission list.

        PSOPRDEFN.ROWSECCLASS is the field PeopleSoft itself uses for this;
        a site whose users carry no row-security class gets an empty answer
        here, which is the same answer PeopleSoft would give.
        """
        classes = self._row_security_classes(who)
        if not classes:
            return frozenset()
        names = sorted(classes)
        binds = {f"c{i}": name for i, name in enumerate(names)}
        placeholders = ", ".join(f":{k}" for k in binds)
        rows, _ = self.db.query(
            f"SELECT DISTINCT BUSINESS_UNIT AS bu "
            f"FROM {self.db.prefix}{record} "
            f"WHERE UPPER({key}) IN ({placeholders})",
            binds, max_rows=5000)
        return _normalise(r.get("bu") for r in rows)

    def _row_security_classes(self, who: str) -> frozenset:
        """Row-security classes for a user, or frozenset() if this site has none.

        Two situations must not share an answer, and the catalog cannot tell
        them apart: on Oracle a table you lack SELECT on DESCRIBES AS ZERO
        COLUMNS rather than raising, so "no ROWSECCLASS column" and "no grant
        on PSOPRDEFN" look identical from db.columns().

        Reading a failure as absence manufactures an authoritative empty
        grant out of an outage. Reading absence as a failure hands every user
        all_units on a fail-open site, for a schema shape that is stable and
        legitimate — sites really do expose a read-only PSOPRDEFN view
        without the column.

        So neither is inferred from the catalog. The query runs, and if it
        fails we ask a question the catalog cannot fudge: is PSOPRDEFN
        readable AT ALL? A successful OPRID read alongside a failing
        ROWSECCLASS read means the table is granted and the column is
        genuinely absent. Both failing means we cannot see the record, which
        is an outage and must reach _resolve() as unavailable.
        """
        try:
            rows, _ = self.db.query(
                f"SELECT {ROW_SECURITY_FIELD} AS cls "
                f"FROM {self.db.prefix}{OPERATOR_RECORD} "
                "WHERE UPPER(OPRID) = :who", {"who": who}, max_rows=10)
        except DbError:
            try:
                self.db.query(
                    f"SELECT OPRID FROM {self.db.prefix}{OPERATOR_RECORD} "
                    "WHERE UPPER(OPRID) = :who", {"who": who}, max_rows=1)
            except DbError:
                raise               # cannot read the record: a real outage
            # The record is readable, so the column is the thing missing.
            # That is an authoritative "this site has no classes".
            return frozenset()
        return _normalise(r.get("cls") for r in rows)

    # ---- what the console and the diagnose script report ------------------
    def health(self) -> dict:
        record, key, kind = self.source_record()
        return {
            "enabled": self.enabled,
            "record": record,
            "key": key,
            "kind": kind,
            "operator_record_readable": bool(self._columns(OPERATOR_RECORD)),
            "privileged_users": sorted(self.privileged_users),
            "on_unavailable": ("allow" if self._fail_open() else "refuse"),
            "is_authentication": False,
        }


# --------------------------------------------------------------------------
# WHO IS ASKING, for tools that answer across units.
#
# Every other gate in this app checks a business_unit ARGUMENT, and that is
# enough for a tool that answers about one unit. It is not enough for a tool
# that answers about all of them: business_unit="ALL" means "every unit that
# exists", the check skips it as harmless, and a user granted one unit gets
# rows from every unit. That was live — get_top_billing_customers(
# business_unit="ALL") returned another company's customers and amounts to a
# restricted user.
#
# The fix cannot be a tool argument. Tool arguments are written by the MODEL,
# so an "allowed_units" parameter is a grant the model can widen by typing a
# different value — and the model is steered by the question, which is
# written by the user. The grant has to arrive by a path the model cannot
# reach at all.
#
# So it arrives here, in a context variable the SERVER sets from the
# authenticated session before dispatch and clears afterwards. Tools read it;
# nothing the model emits can change it.
#
# Threads: a contextvar is not inherited by a bare threading.Thread, and two
# tools here fan out over a pool. Read current_access() ONCE at the top of a
# tool, before any fan-out, and pass the resolved unit list down — which is
# what the tools below do, and what a new one must do.
_ACCESS: "contextvars.ContextVar" = contextvars.ContextVar(
    "pstb_access", default=None)


def current_access():
    """The caller's Access for this request, or None when unrestricted.

    None means "no row security in force" — a terminal session, a script, a
    deployment with security.enabled false. It must read as UNRESTRICTED and
    not as "no units", or every tool would answer nothing on the machine of
    the person who installed it.
    """
    return _ACCESS.get()


def set_access(access):
    """Bind the caller for this request. Returns the token to reset with."""
    return _ACCESS.set(access)


def reset_access(token) -> None:
    try:
        _ACCESS.reset(token)
    except ValueError:
        # Reset from a different context than the set — nothing to undo, and
        # raising here would fail a request that already succeeded.
        _ACCESS.set(None)


class access_scope:
    """`with access_scope(access):` around a dispatch."""

    def __init__(self, access):
        self.access = access
        self._token = None

    def __enter__(self):
        self._token = set_access(self.access)
        return self.access

    def __exit__(self, *exc):
        reset_access(self._token)
        return False


def allowed_units(candidates) -> tuple:
    """(allowed, dropped) for the caller in force. Unrestricted -> all."""
    access = current_access()
    seen = [str(c or "").strip().upper() for c in candidates]
    seen = [c for c in seen if c]
    if access is None:
        return seen, []
    return access.narrow(seen)
