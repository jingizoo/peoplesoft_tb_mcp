"""Read-only, controller-grade General Ledger journal status evidence.

The control deliberately stays narrower than a general journal inquiry.  It
answers one auditable question for an exact business unit, ledger, fiscal year
and period: what is the *current* PeopleSoft header status of each journal in
the selected journal-date population, and do its signed ledger lines net?

``through_date`` is a population cutoff, not a time machine.  ``PS_JRNL_HEADER``
stores current state, so this module never describes that state as historical
"as-of" evidence.  Likewise, it only reports a posting date when the physical
record actually exposes ``POSTED_DATE``.

Record names and optional fields are discovered from live metadata.  A unique
catalog suffix is accepted (supporting company-prefixed physical tables), but
an ambiguous or unreadable record fails closed.  No row-changing SQL exists in
this module.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any

from . import queries as q
from .db import DbError
from .engine import TBEngine


class JournalControlError(RuntimeError):
    """Programming/constructor error for the journal-status control.

    Normal data, scope, shape and truncation problems are returned as an
    ``incomplete`` or ``no_data`` payload so callers cannot accidentally turn
    an exception into a clean control result.
    """


@dataclass(frozen=True)
class _Record:
    name: str
    columns: frozenset[str]
    basis: str


# Oracle PeopleSoft General Ledger documents these values for
# JRNL_HDR_STATUS.  Do not merge them into one generic "pending" class: D and
# M are intentionally non-posting states, while I/E/N/T/V require materially
# different follow-up and U is a completed unpost.
HEADER_STATUS: dict[str, dict[str, Any]] = {
    "D": {
        "label": "Deleted - anchor journal unposted",
        "disposition": "deleted",
        "action_class": "informational",
        "requires_close_action": False,
    },
    "I": {
        "label": "Posting incomplete - repost as soon as possible",
        "disposition": "posting_incomplete",
        "action_class": "urgent_repost",
        "requires_close_action": True,
    },
    "M": {
        "label": "Valid SJE model - do not post",
        "disposition": "sje_model",
        "action_class": "informational",
        "requires_close_action": False,
    },
    "E": {
        "label": "Journal has errors",
        "disposition": "edit_error",
        "action_class": "correct_errors",
        "requires_close_action": True,
    },
    "N": {
        "label": "No status - needs to be edited",
        "disposition": "needs_edit",
        "action_class": "edit_required",
        "requires_close_action": True,
    },
    "P": {
        "label": "Posted to ledger(s)",
        "disposition": "posted",
        "action_class": "none",
        "requires_close_action": False,
    },
    "T": {
        "label": "Journal entry incomplete",
        "disposition": "entry_incomplete",
        "action_class": "complete_entry",
        "requires_close_action": True,
    },
    "U": {
        "label": "Unposted - cannot be reposted",
        "disposition": "unposted",
        "action_class": "review_unpost",
        "requires_close_action": True,
    },
    "V": {
        "label": "Valid journal - edits completed, ready to post",
        "disposition": "valid_ready_to_post",
        "action_class": "ready_to_post",
        "requires_close_action": True,
    },
    "Z": {
        "label": "Upgrade journal - cannot unpost",
        "disposition": "upgrade_cannot_unpost",
        "action_class": "informational",
        "requires_close_action": False,
    },
}


_IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]*$")
_HEADER_REQUIRED = {
    "BUSINESS_UNIT",
    "JOURNAL_ID",
    "JOURNAL_DATE",
    "UNPOST_SEQ",
    "JRNL_HDR_STATUS",
    "FISCAL_YEAR",
    "ACCOUNTING_PERIOD",
}
_LINE_SCOPE_REQUIRED = {
    "BUSINESS_UNIT",
    "JOURNAL_ID",
    "JOURNAL_DATE",
    "UNPOST_SEQ",
    "LEDGER",
}
_LINE_NETTING_REQUIRED = {
    "MONETARY_AMOUNT",
    "CURRENCY_CD",
}

# These values are evidence only when the site's physical header really has
# the field.  They are intentionally returned raw: PeopleSoft Workflow, AWE
# and site customisations do not share one universal approval-code meaning.
_OPERATOR_FIELDS = (
    "OPRID",
    "LASTUPDOPRID",
    "POSTED_BY_OPRID",
)
_APPROVAL_FIELDS = (
    "APPROVAL_STATUS",
    "APPR_STATUS",
    "WF_STATUS",
)
_PROCESS_FIELDS = (
    "JRNL_PROCESS_REQST",
    "PROCESS_INSTANCE",
    "POSTED_DATE",
    "SYSTEM_SOURCE",
    "LEDGER_GROUP",
)
_LABEL_FIELDS = ("SOURCE",)
_OPTIONAL_FIELDS = tuple(dict.fromkeys(
    _LABEL_FIELDS + _OPERATOR_FIELDS + _APPROVAL_FIELDS + _PROCESS_FIELDS
))


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("amount is null or boolean")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("amount is not finite")
    return number


def _money(value: float) -> float:
    # Keep signed amounts.  Rounding only normalises driver-specific numeric
    # representations for the JSON payload; it does not take an absolute.
    return round(float(value), 2)


class JournalStatusControl:
    """Evaluate current journal status and signed line netting for one scope."""

    DEFAULT_LIMIT = 500
    HARD_LIMIT = 5_000
    CURRENCY_GROUPS_PER_JOURNAL = 25

    def __init__(self, engine: TBEngine):
        if engine is None or not hasattr(engine, "db"):
            raise JournalControlError("JournalStatusControl requires a TBEngine")
        self.e = engine
        self.db = engine.db

    # ---- record discovery -------------------------------------------------
    def _catalog_candidates(self, logical_name: str) -> tuple[list[str], bool]:
        params = {"pat": f"%{logical_name}%"}
        rows, truncated = self.db.query(
            q.table_list(self.db, params), params, max_rows=101
        )
        candidates: list[str] = []
        for row in rows:
            name = _text(row.get("table_name")).upper()
            if (name == logical_name or name.endswith(f"_{logical_name}")):
                if _IDENT.fullmatch(name):
                    candidates.append(name)
        return list(dict.fromkeys(candidates)), truncated

    def _resolve_record(self, logical_name: str) -> _Record:
        mapped = ""
        try:
            rows, truncated = self.db.query(
                f"SELECT SQLTABLENAME AS sqltablename "
                f"FROM {self.db.prefix}PSRECDEFN "
                "WHERE UPPER(RECNAME) = :rec",
                {"rec": logical_name},
                max_rows=2,
            )
            if truncated or len(rows) > 1:
                raise JournalControlError(
                    f"PSRECDEFN returned more than one {logical_name} mapping"
                )
            if rows:
                mapped = _text(rows[0].get("sqltablename")).upper()
        except DbError:
            # Many read-only reporting users cannot see PeopleTools metadata.
            # A unique live catalog suffix is the bounded, disclosed fallback.
            pass

        if mapped:
            if not _IDENT.fullmatch(mapped):
                raise JournalControlError(
                    f"PSRECDEFN.SQLTABLENAME for {logical_name} is not a safe "
                    "single catalog identifier"
                )
            possibilities = [mapped]
            if not mapped.startswith("PS_"):
                possibilities.append(f"PS_{mapped}")
            for name in possibilities:
                columns = self.db.columns(name)
                if columns:
                    return _Record(
                        name=name,
                        columns=frozenset(columns),
                        basis="PSRECDEFN.SQLTABLENAME",
                    )
            raise JournalControlError(
                f"PSRECDEFN maps {logical_name} to {mapped}, but the physical "
                "object is not readable; no similarly named fallback is used"
            )

        candidates, truncated = self._catalog_candidates(logical_name)
        if truncated:
            raise JournalControlError(
                f"Catalog discovery for {logical_name} exceeded 101 objects; "
                "a unique physical record cannot be established"
            )
        if not candidates:
            raise JournalControlError(
                f"No readable physical record uniquely matching {logical_name}"
            )
        if len(candidates) > 1:
            raise JournalControlError(
                f"More than one physical record matches {logical_name}: "
                f"{', '.join(candidates[:10])}; configure PeopleTools metadata"
            )
        name = candidates[0]
        columns = self.db.columns(name)
        if not columns:
            raise JournalControlError(
                f"{name} was discovered but its columns are not readable"
            )
        return _Record(
            name=name,
            columns=frozenset(columns),
            basis="unique live-catalog suffix; no table prefix assumed",
        )

    @staticmethod
    def _field_dict(row: dict, columns: tuple[str, ...]) -> dict:
        return {
            column.lower(): row.get(column.lower())
            for column in columns
            if column.lower() in row
        }

    # ---- public control ---------------------------------------------------
    def evaluate(
        self,
        *,
        business_unit: str,
        ledger: str,
        fiscal_year: int,
        period: int,
        through_date: str,
        journal_id: str = "",
        limit: int = DEFAULT_LIMIT,
        tolerance: float = 0.005,
    ) -> dict:
        """Return bounded journal-status evidence for one exact GL scope.

        ``through_date`` includes journals whose ``JOURNAL_DATE`` is on or
        before the date.  Header status remains the state observed when this
        method runs; it is never represented as the status that existed on the
        cutoff date.
        """
        bu = _text(business_unit)
        led = _text(ledger)
        jid = _text(journal_id)
        observed_at = dt.date.today().isoformat()
        scope = {
            "business_unit": bu,
            "ledger": led,
            "fiscal_year": fiscal_year,
            "period": period,
            "journal_id": jid or None,
        }
        cutoff = {
            "journal_date_through": _text(through_date) or None,
            "header_status_observed_at": observed_at,
            "historical_status_reconstructed": False,
            "basis": (
                "FISCAL_YEAR and ACCOUNTING_PERIOD on the header; "
                "JOURNAL_DATE through the selected cutoff; current header "
                "status observed at query time"
            ),
        }
        base = {
            "status": "incomplete",
            "evaluated": False,
            "status_evaluated": False,
            "status_control_passed": None,
            "control_passed": None,
            "netting_evaluated": False,
            "netting_passed": None,
            "netting_complete": False,
            "reason": "",
            "scope": scope,
            "cutoff": cutoff,
            "evidence_completeness": {
                "complete": False,
                "status_complete": False,
                "netting_complete": False,
                "population_complete": False,
                "statuses_classified": False,
                "reason": "not evaluated",
            },
            "population": {
                "returned_journals": 0,
                "population_complete": False,
                "exact_journal_requested": bool(jid),
            },
            "records": {},
            "journals": [],
            "exceptions": [],
            "status_exceptions": [],
            "netting_exceptions": [],
            "truncated": False,
        }

        def incomplete(reason: str, **extra: Any) -> dict:
            evidence = {
                **base["evidence_completeness"],
                "complete": False,
                "status_complete": False,
                "netting_complete": False,
                "population_complete": False,
                "statuses_classified": False,
                "reason": reason,
            }
            return {
                **base,
                "reason": reason,
                "evidence_completeness": evidence,
                **extra,
            }

        if not bu or not led:
            return incomplete(
                "business_unit and ledger are required; the control never "
                "widens a missing scope to all business units or ledgers"
            )
        try:
            fy, per = int(fiscal_year), int(period)
        except (TypeError, ValueError):
            return incomplete("fiscal_year and period must be integers")
        scope["fiscal_year"], scope["period"] = fy, per
        if fy < 1 or per < 0:
            return incomplete(
                "fiscal_year must be positive and period must be zero or greater"
            )
        try:
            period_row = next(
                row for row in self.e.list_periods(fy).get("periods", [])
                if int(row.get("period") or -1) == per
            )
            period_begin = dt.date.fromisoformat(
                _text(period_row.get("begin_dt"))[:10]
            )
            period_end = dt.date.fromisoformat(
                _text(period_row.get("end_dt"))[:10]
            )
        except Exception:
            return incomplete(
                f"Fiscal-calendar dates for FY{fy} period {per} are not "
                "readable; the journal-date cutoff cannot be aligned to the "
                "selected period"
            )
        cutoff["period_begin"] = period_begin.isoformat()
        cutoff["period_end"] = period_end.isoformat()
        cutoff_input = _text(through_date)
        if not cutoff_input:
            cutoff_input = period_end.isoformat()
            cutoff["resolved_from"] = "fiscal calendar period end"
        try:
            cutoff_day = dt.date.fromisoformat(cutoff_input)
        except ValueError:
            return incomplete("through_date must be an ISO date in YYYY-MM-DD format")
        cutoff["journal_date_through"] = cutoff_day.isoformat()
        if cutoff_day < period_begin or cutoff_day > period_end:
            return incomplete(
                f"through_date {cutoff_day.isoformat()} is outside FY{fy} "
                f"period {per} ({period_begin.isoformat()} through "
                f"{period_end.isoformat()})"
            )
        if cutoff_day > dt.date.today():
            return incomplete(
                f"through_date {cutoff_day.isoformat()} is in the future; "
                "future journal evidence is not evaluated"
            )
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            return incomplete("limit must be an integer")
        if requested_limit < 1:
            return incomplete("limit must be at least 1")
        effective_limit = min(requested_limit, self.HARD_LIMIT)
        try:
            tol = float(tolerance)
        except (TypeError, ValueError):
            return incomplete("tolerance must be a finite non-negative number")
        if not math.isfinite(tol) or tol < 0:
            return incomplete("tolerance must be a finite non-negative number")

        try:
            header = self._resolve_record("JRNL_HEADER")
        except JournalControlError as exc:
            return incomplete(str(exc))
        line: _Record | None = None
        line_resolution_reason = ""
        try:
            line = self._resolve_record("JRNL_LN")
        except JournalControlError as exc:
            line_resolution_reason = str(exc)
        ledger_group: _Record | None = None
        ledger_group_reason = ""
        if "LEDGER_GROUP" in header.columns:
            try:
                candidate = self._resolve_record("LED_GRP_TBL")
                required = {"LEDGER_GROUP", "LEDGER"}
                missing = sorted(required - set(candidate.columns))
                if missing:
                    ledger_group_reason = (
                        f"{candidate.name} missing {', '.join(missing)}"
                    )
                else:
                    ledger_group = candidate
            except JournalControlError as exc:
                ledger_group_reason = str(exc)

        records = {
            "header": {"name": header.name, "resolution_basis": header.basis},
            "line": (
                {"name": line.name, "resolution_basis": line.basis}
                if line else
                {"name": None, "available": False,
                 "reason": line_resolution_reason}
            ),
            "ledger_group_membership": (
                {"name": ledger_group.name,
                 "resolution_basis": ledger_group.basis}
                if ledger_group else
                {"name": None, "available": False,
                 "reason": ledger_group_reason}
            ),
        }
        base["records"] = records
        missing_header = sorted(_HEADER_REQUIRED - set(header.columns))
        missing_line_scope = sorted(
            _LINE_SCOPE_REQUIRED - set(line.columns) if line else
            _LINE_SCOPE_REQUIRED
        )
        missing_line_netting = sorted(
            _LINE_NETTING_REQUIRED - set(line.columns) if line else
            _LINE_NETTING_REQUIRED
        )
        line_scope_available = bool(line) and not missing_line_scope
        netting_shape_available = (
            line_scope_available and not missing_line_netting
        )
        optional_available = [
            field for field in _OPTIONAL_FIELDS if field in header.columns
        ]
        optional_unavailable = [
            field for field in _OPTIONAL_FIELDS if field not in header.columns
        ]
        evidence_shape = {
            "complete": False,
            "status_complete": False,
            "netting_complete": False,
            "population_complete": False,
            "statuses_classified": False,
            "reason": "not evaluated",
            "required_header_fields": sorted(_HEADER_REQUIRED),
            "line_scope_fields": sorted(_LINE_SCOPE_REQUIRED),
            "line_netting_fields": sorted(_LINE_NETTING_REQUIRED),
            "line_scope_available": line_scope_available,
            "line_netting_shape_available": netting_shape_available,
            "ledger_group_membership_available": bool(ledger_group),
            "missing_line_scope_fields": missing_line_scope,
            "missing_line_netting_fields": missing_line_netting,
            "optional_header_fields_available": optional_available,
            "optional_header_fields_unavailable": optional_unavailable,
            "approval_evidence_basis": (
                "raw physical fields only; no approval meaning is inferred"
            ),
            "posting_date_claim_available": "POSTED_DATE" in header.columns,
        }
        base["evidence_completeness"] = evidence_shape
        if missing_header:
            return incomplete(
                f"{header.name} missing {', '.join(missing_header)}. "
                "Required header identity and status fields are not "
                "approximated."
            )

        p = self.db.prefix
        hname = header.name
        lname = line.name if line else ""
        journal_predicate = ""
        params: dict[str, Any] = {
            "bu": bu,
            "ledger": led,
            "fy": fy,
            "per": per,
            "cutoff": cutoff_day.isoformat(),
        }
        if jid:
            journal_predicate = " AND H.JOURNAL_ID = :journal_id"
            params["journal_id"] = jid

        line_exists = ""
        if line_scope_available:
            line_exists = f"""EXISTS (
       SELECT 1 FROM {p}{lname} LX
        WHERE LX.BUSINESS_UNIT = H.BUSINESS_UNIT
          AND LX.JOURNAL_ID = H.JOURNAL_ID
          AND LX.JOURNAL_DATE = H.JOURNAL_DATE
          AND LX.UNPOST_SEQ = H.UNPOST_SEQ
          AND LX.LEDGER = :ledger
   )"""
        group_exists = ""
        if ledger_group:
            group_exists = f"""EXISTS (
       SELECT 1 FROM {p}{ledger_group.name} LG
        WHERE LG.LEDGER_GROUP = H.LEDGER_GROUP
          AND LG.LEDGER = :ledger
   )"""

        scope_proofs = [proof for proof in (line_exists, group_exists) if proof]
        # Exact-ID lookup first preserves the existing header so an unavailable
        # line grant or ledger-group setup becomes an explicit scope limitation,
        # not the false statement that the journal does not exist.  A period
        # population, by contrast, includes only headers whose selected ledger
        # is proven by a line or delivered ledger-group membership.
        if jid:
            ledger_scope_predicate = ""
        elif scope_proofs:
            ledger_scope_predicate = " AND (" + " OR ".join(scope_proofs) + ")"
        else:
            return incomplete(
                "The selected ledger cannot be established for a period-wide "
                "journal population: neither readable JRNL_LN ledger keys nor "
                "LED_GRP_TBL membership are available"
            )

        dynamic = [
            f"H.{field} AS {field.lower()}" for field in optional_available
        ]
        if line_exists:
            dynamic.append(
                f"CASE WHEN {line_exists} THEN 1 ELSE 0 END "
                "AS line_ledger_match"
            )
        if group_exists:
            dynamic.append(
                f"CASE WHEN {group_exists} THEN 1 ELSE 0 END "
                "AS group_ledger_match"
            )
        select_dynamic = (",\n       " + ",\n       ".join(dynamic)) if dynamic else ""

        header_sql = f"""SELECT H.BUSINESS_UNIT AS business_unit,
       H.JOURNAL_ID AS journal_id, H.JOURNAL_DATE AS journal_date,
       H.UNPOST_SEQ AS unpost_seq, H.JRNL_HDR_STATUS AS jrnl_hdr_status,
       H.FISCAL_YEAR AS fiscal_year,
       H.ACCOUNTING_PERIOD AS accounting_period{select_dynamic}
  FROM {p}{hname} H
 WHERE H.BUSINESS_UNIT = :bu
   AND H.FISCAL_YEAR = :fy
   AND H.ACCOUNTING_PERIOD = :per
   AND H.JOURNAL_DATE <= {self.db.date_bind('cutoff')}{journal_predicate}
   {ledger_scope_predicate}
 ORDER BY H.JOURNAL_DATE, H.JOURNAL_ID, H.UNPOST_SEQ"""
        try:
            header_rows, header_truncated = self.db.query(
                header_sql, params, max_rows=effective_limit
            )
        except DbError as exc:
            return incomplete(
                f"Journal headers could not be read for the exact scope: {exc}"
            )

        base["population"] = {
            "returned_journals": len(header_rows),
            "population_complete": not header_truncated,
            "exact_journal_requested": bool(jid),
            "requested_limit": requested_limit,
            "effective_limit": effective_limit,
            "hard_limit": self.HARD_LIMIT,
            "identity": [
                "BUSINESS_UNIT",
                "JOURNAL_ID",
                "JOURNAL_DATE",
                "UNPOST_SEQ",
            ],
        }
        if not header_rows:
            reason = (
                f"No journal with ID {jid!r} exists in the selected business "
                "unit, ledger, fiscal year, period and cutoff."
                if jid else
                "No journal headers with lines in the selected ledger exist "
                "in the exact business unit, fiscal year, period and cutoff."
            )
            return {
                **base,
                "status": "no_data",
                "reason": reason + " This is not a zero, pass, or clean population.",
                "evidence_completeness": {
                    **evidence_shape,
                    "complete": False,
                    "status_complete": False,
                    "netting_complete": False,
                    "population_complete": False,
                    "statuses_classified": False,
                    "reason": "the selected population is empty",
                },
            }

        if jid:
            scoped_rows = []
            ambiguous_rows = []
            outside_rows = []
            for row in header_rows:
                line_match = bool(row.get("line_ledger_match"))
                group_match = bool(row.get("group_ledger_match"))
                if line_match or group_match:
                    scoped_rows.append(row)
                    continue
                group_value = _text(row.get("ledger_group"))
                if ledger_group and group_value:
                    # Governed group membership was readable and did not
                    # include the selected ledger: this version is proven out
                    # of scope.  Without that setup evidence, an empty line set
                    # only means ledger scope is unknown, not wrong.
                    outside_rows.append(row)
                else:
                    ambiguous_rows.append(row)
            if ambiguous_rows:
                partial = [
                    self._header_observation(row, optional_available)
                    for row in header_rows
                ]
                for source_row, row in zip(header_rows, partial):
                    row["ledger"] = led
                    line_match = bool(source_row.get("line_ledger_match"))
                    group_match = bool(source_row.get("group_ledger_match"))
                    row["ledger_scope_confirmed"] = (
                        line_match or group_match
                    )
                    row["ledger_scope_basis"] = (
                        "PS_JRNL_LN.LEDGER" if line_match else
                        "LED_GRP_TBL ledger-group membership"
                        if group_match else None
                    )
                    row["netting"] = None
                return incomplete(
                    f"Journal {jid!r} exists in the selected business unit, "
                    "fiscal year, period and cutoff, but the requested ledger "
                    "cannot be confirmed for every version because neither a "
                    "selected-ledger journal line nor governed ledger-group "
                    "membership proves those version(s).",
                    journals=partial,
                    population={
                        **base["population"],
                        "population_complete": False,
                    },
                )
            if not scoped_rows:
                return {
                    **base,
                    "status": "no_data",
                    "reason": (
                        f"Journal {jid!r} exists in the selected business "
                        "unit, fiscal year, period and cutoff, but no version "
                        f"is attributable to ledger {led!r} by a journal line "
                        "or governed ledger-group membership. This is not a "
                        "zero, pass, or clean population."
                    ),
                    "population": {
                        **base["population"],
                        "returned_journals": 0,
                        "population_complete": False,
                        "headers_outside_selected_ledger": len(outside_rows),
                    },
                }
            header_rows = scoped_rows
            base["population"] = {
                **base["population"],
                "returned_journals": len(header_rows),
            }

        identities: set[tuple[str, str, str, int]] = set()
        duplicate_keys: list[tuple[str, str, str, int]] = []
        for row in header_rows:
            try:
                key = (
                    _text(row.get("business_unit")),
                    _text(row.get("journal_id")),
                    _text(row.get("journal_date"))[:10],
                    int(row.get("unpost_seq")),
                )
            except (TypeError, ValueError):
                return incomplete(
                    "A journal header has a null or invalid full identity key; "
                    "versions cannot be safely joined or deduplicated"
                )
            if not all(key[:3]):
                return incomplete(
                    "A journal header has a blank full identity key; versions "
                    "cannot be safely joined or deduplicated"
                )
            if key in identities:
                duplicate_keys.append(key)
            identities.add(key)
        if duplicate_keys:
            return incomplete(
                "Duplicate PS_JRNL_HEADER rows share the full business-unit, "
                "journal-ID, journal-date and unpost-sequence key; line totals "
                "would fan out, so no conclusion is produced"
            )

        # Once a period population crosses the journal cap, status rows are
        # returned as partial observations but line totals are deliberately not
        # computed for a subset that could be mistaken for the full control.
        if header_truncated:
            partial = [self._header_observation(row, optional_available)
                       for row in header_rows]
            reason = (
                f"At least {effective_limit + 1:,} journals matched; the "
                f"bounded limit is {effective_limit:,}. The prior/complete "
                "population is not represented and no monetary conclusion is "
                "produced. Narrow to journal_id or raise the caller limit up "
                f"to the hard cap of {self.HARD_LIMIT:,}."
            )
            return incomplete(
                reason,
                journals=partial,
                truncated=True,
                population={**base["population"], "population_complete": False},
            )

        aggregate_sql = f"""SELECT L.BUSINESS_UNIT AS business_unit,
       L.JOURNAL_ID AS journal_id, L.JOURNAL_DATE AS journal_date,
       L.UNPOST_SEQ AS unpost_seq, L.CURRENCY_CD AS currency,
       COUNT(*) AS line_count,
       SUM(CASE WHEN L.MONETARY_AMOUNT IS NULL THEN 1 ELSE 0 END)
           AS null_amount_count,
       SUM(CASE WHEN L.MONETARY_AMOUNT > 0
                THEN L.MONETARY_AMOUNT ELSE 0 END) AS debit_total,
       SUM(CASE WHEN L.MONETARY_AMOUNT < 0
                THEN -L.MONETARY_AMOUNT ELSE 0 END) AS credit_total,
       SUM(L.MONETARY_AMOUNT) AS signed_net
  FROM {p}{lname} L
  JOIN {p}{hname} H
    ON H.BUSINESS_UNIT = L.BUSINESS_UNIT
   AND H.JOURNAL_ID = L.JOURNAL_ID
   AND H.JOURNAL_DATE = L.JOURNAL_DATE
   AND H.UNPOST_SEQ = L.UNPOST_SEQ
 WHERE H.BUSINESS_UNIT = :bu
   AND L.BUSINESS_UNIT = :bu
   AND L.LEDGER = :ledger
   AND H.FISCAL_YEAR = :fy
   AND H.ACCOUNTING_PERIOD = :per
   AND H.JOURNAL_DATE <= {self.db.date_bind('cutoff')}{journal_predicate}
 GROUP BY L.BUSINESS_UNIT, L.JOURNAL_ID, L.JOURNAL_DATE,
          L.UNPOST_SEQ, L.CURRENCY_CD
 ORDER BY L.JOURNAL_DATE, L.JOURNAL_ID, L.UNPOST_SEQ, L.CURRENCY_CD"""
        aggregate_cap = effective_limit * self.CURRENCY_GROUPS_PER_JOURNAL
        aggregate_rows: list[dict] = []
        netting_query_reason = ""
        if netting_shape_available:
            try:
                aggregate_rows, aggregate_truncated = self.db.query(
                    aggregate_sql, params, max_rows=aggregate_cap
                )
                if aggregate_truncated:
                    netting_query_reason = (
                        f"Journal line aggregation exceeded {aggregate_cap:,} "
                        "journal/currency groups"
                    )
                    aggregate_rows = []
            except DbError as exc:
                netting_query_reason = (
                    "Journal lines could not be aggregated for signed "
                    f"netting: {exc}"
                )
        else:
            details = []
            if line_resolution_reason:
                details.append(line_resolution_reason)
            if missing_line_netting:
                details.append(
                    "missing " + ", ".join(missing_line_netting)
                )
            netting_query_reason = (
                "Signed line netting fields are unavailable"
                + (": " + "; ".join(details) if details else "")
            )

        by_key: dict[tuple[str, str, str, int], list[dict]] = {}
        for row in aggregate_rows:
            try:
                key = (
                    _text(row.get("business_unit")),
                    _text(row.get("journal_id")),
                    _text(row.get("journal_date"))[:10],
                    int(row.get("unpost_seq")),
                )
            except (TypeError, ValueError):
                return incomplete(
                    "A journal line aggregation has an invalid full identity key"
                )
            by_key.setdefault(key, []).append(row)
        if not set(by_key).issubset(identities):
            return incomplete(
                "The ledger-line population contains a full journal key not "
                "present in the bounded header population; evidence "
                "completeness cannot be established"
            )

        journals: list[dict] = []
        status_exceptions: list[dict] = []
        netting_exceptions: list[dict] = []
        status_quality: list[str] = []
        netting_quality: list[str] = (
            [netting_query_reason] if netting_query_reason else []
        )
        for row in header_rows:
            key = (
                _text(row.get("business_unit")),
                _text(row.get("journal_id")),
                _text(row.get("journal_date"))[:10],
                int(row.get("unpost_seq")),
            )
            observed = self._header_observation(row, optional_available)
            line_ledger_match = bool(row.get("line_ledger_match"))
            group_ledger_match = bool(row.get("group_ledger_match"))
            observed["ledger_scope_confirmed"] = (
                line_ledger_match or group_ledger_match
            )
            observed["ledger_scope_basis"] = (
                "PS_JRNL_LN.LEDGER"
                if line_ledger_match else
                "LED_GRP_TBL ledger-group membership"
                if group_ledger_match else None
            )
            code = observed["header_status_code"]
            status_info = HEADER_STATUS.get(code)
            if not code:
                status_quality.append(
                    f"{key[1]} {key[2]} seq {key[3]} has blank JRNL_HDR_STATUS"
                )
            elif status_info is None:
                status_quality.append(
                    f"{key[1]} {key[2]} seq {key[3]} has unknown "
                    f"JRNL_HDR_STATUS {code!r}"
                )

            currency_totals: list[dict] = []
            for group in by_key.get(key, []):
                currency = _text(group.get("currency")).upper()
                try:
                    nulls = int(group.get("null_amount_count") or 0)
                    count = int(group.get("line_count") or 0)
                    debit = _number(group.get("debit_total"))
                    credit = _number(group.get("credit_total"))
                    net = _number(group.get("signed_net"))
                except (TypeError, ValueError) as exc:
                    netting_quality.append(
                        f"{key[1]} {key[2]} seq {key[3]} has invalid signed "
                        f"line amounts: {exc}"
                    )
                    continue
                if nulls:
                    netting_quality.append(
                        f"{key[1]} {key[2]} seq {key[3]} has {nulls} null "
                        "MONETARY_AMOUNT line(s)"
                    )
                if not currency:
                    netting_quality.append(
                        f"{key[1]} {key[2]} seq {key[3]} has blank "
                        "CURRENCY_CD"
                    )
                currency_totals.append({
                    "currency": currency or None,
                    "line_count": count,
                    "debit_total": _money(debit),
                    "credit_total": _money(credit),
                    "signed_net": _money(net),
                    "netting": abs(net) <= tol,
                    "null_amount_count": nulls,
                })
            observed["ledger"] = led
            observed["currency_totals"] = currency_totals
            observed["amount_basis"] = (
                "Signed PS_JRNL_LN.MONETARY_AMOUNT for the selected ledger, "
                "kept separate by observed PS_JRNL_LN.CURRENCY_CD; debits are "
                "positive and credits are negative; no FX conversion"
            )
            observed["currency_basis_complete"] = (
                len(currency_totals) == 1
                and currency_totals[0]["currency"] is not None
                and currency_totals[0]["null_amount_count"] == 0
            )
            if len(currency_totals) == 1:
                observed["ledger_scope_basis"] = "PS_JRNL_LN.LEDGER"
                observed["line_count"] = currency_totals[0]["line_count"]
                observed["debit_total"] = currency_totals[0]["debit_total"]
                observed["credit_total"] = currency_totals[0]["credit_total"]
                observed["signed_net"] = currency_totals[0]["signed_net"]
                observed["currency"] = currency_totals[0]["currency"]
                observed["netting"] = currency_totals[0]["netting"]
            else:
                observed.update({
                    "line_count": sum(x["line_count"] for x in currency_totals),
                    "debit_total": None,
                    "credit_total": None,
                    "signed_net": None,
                    "currency": None,
                    "netting": None,
                })
                if not currency_totals:
                    if not netting_query_reason:
                        netting_quality.append(
                            f"{key[1]} {key[2]} seq {key[3]} has a journal "
                            "header but no line in the selected ledger; signed "
                            "netting is unavailable"
                        )
                else:
                    netting_quality.append(
                        f"{key[1]} {key[2]} seq {key[3]} has "
                        f"{len(currency_totals)} currency groups; totals are "
                        "not silently combined"
                    )

            if (status_info is not None
                    and bool(status_info["requires_close_action"])):
                status_exceptions.append({
                    "category": status_info["action_class"],
                    "journal_key": observed["journal_key"],
                    "observed_status": code,
                    "expected_for_close_readiness": (
                        "P, or an explicitly non-actionable D/M/Z status"
                    ),
                    "explanation": status_info["label"],
                    "evidence": "observed current PS_JRNL_HEADER status",
                })
            if observed.get("netting") is False:
                netting_exceptions.append({
                    "category": "journal_not_netting_in_selected_ledger",
                    "journal_key": observed["journal_key"],
                    "observed_signed_net": observed["signed_net"],
                    "currency": observed["currency"],
                    "expected": f"absolute signed net <= {tol:g}",
                    "evidence": "aggregated signed PS_JRNL_LN amounts",
                })
            journals.append(observed)

        if status_quality:
            reason = (
                "Journal status evidence is incomplete: "
                + "; ".join(status_quality[:8])
            )
            if len(status_quality) > 8:
                reason += f"; and {len(status_quality) - 8} more issue(s)"
            return incomplete(
                reason,
                journals=journals,
                exceptions=status_exceptions + netting_exceptions,
                status_exceptions=status_exceptions,
                netting_exceptions=netting_exceptions,
                population={**base["population"], "population_complete": True},
            )

        status_passed = not status_exceptions
        netting_evaluated = not netting_quality
        netting_passed = (
            not netting_exceptions if netting_evaluated else None
        )
        status_reason = (
            "No actionable current journal status was observed."
            if status_passed else
            f"{len(status_exceptions)} actionable current journal status "
            "exception(s) were observed."
        )
        netting_reason = (
            "Signed ledger lines net within tolerance."
            if netting_passed is True else
            f"{len(netting_exceptions)} signed line netting exception(s) "
            "were observed."
            if netting_passed is False else
            "Signed line netting was not evaluated completely: "
            + "; ".join(netting_quality[:8])
        )
        reason = (
            status_reason + " " + netting_reason
            + " This is control evidence for Finance review, not an "
              "audit-deficiency classification."
        )
        return {
            **base,
            "status": "evaluated",
            "evaluated": True,
            "status_evaluated": True,
            "status_control_passed": status_passed,
            "control_passed": status_passed,
            "netting_evaluated": netting_evaluated,
            "netting_passed": netting_passed,
            "netting_complete": netting_evaluated,
            "reason": reason,
            "evidence_completeness": {
                **evidence_shape,
                "complete": True,
                "status_complete": True,
                "netting_complete": netting_evaluated,
                "population_complete": True,
                "statuses_classified": True,
                "reason": (
                    "All current header statuses in the bounded population "
                    "were classified. " + netting_reason
                    + " Optional approval/process fields are raw only."
                ),
            },
            "population": {**base["population"], "population_complete": True},
            "journals": journals,
            "exceptions": status_exceptions + netting_exceptions,
            "status_exceptions": status_exceptions,
            "netting_exceptions": netting_exceptions,
            "truncated": False,
        }

    @staticmethod
    def _header_observation(row: dict, optional_available: list[str]) -> dict:
        code = _text(row.get("jrnl_hdr_status")).upper()
        status = HEADER_STATUS.get(code)
        observed = {
            "journal_key": {
                "business_unit": _text(row.get("business_unit")),
                "journal_id": _text(row.get("journal_id")),
                "journal_date": _text(row.get("journal_date"))[:10],
                "unpost_seq": row.get("unpost_seq"),
            },
            "journal_date": _text(row.get("journal_date"))[:10],
            "unpost_sequence": row.get("unpost_seq"),
            "header_status_code": code or None,
            "header_status_label": status["label"] if status else None,
            "header_status_disposition": status["disposition"] if status else "unknown",
            "status_meaning": status["label"] if status else None,
            "action_class": status["action_class"] if status else "unknown",
            "requires_close_action": (
                status["requires_close_action"] if status else None
            ),
            "is_posted": code == "P" if code else None,
            "status_basis": "current PS_JRNL_HEADER.JRNL_HDR_STATUS",
        }
        if "source" in row:
            observed["source"] = row.get("source")
        operator = JournalStatusControl._field_dict(row, _OPERATOR_FIELDS)
        approval = JournalStatusControl._field_dict(row, _APPROVAL_FIELDS)
        process = JournalStatusControl._field_dict(row, _PROCESS_FIELDS)
        if operator:
            observed["operator_fields"] = operator
        if approval:
            observed["approval_fields"] = approval
        if process:
            observed["process_fields"] = process
            if "posted_date" in process:
                observed["posted_date"] = process["posted_date"]
        # Flat aliases keep presentation code simple; journal_key remains the
        # authoritative compound identity used for joins and evidence.
        observed["journal_id"] = observed["journal_key"]["journal_id"]
        observed["unpost_seq"] = observed["journal_key"]["unpost_seq"]
        observed["optional_fields_present"] = [
            field for field in optional_available if field.lower() in row
        ]
        return observed
