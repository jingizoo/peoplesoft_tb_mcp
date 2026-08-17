"""Current-date, same-BU, PO-linked GRNI review candidates.

This module deliberately answers a narrower question than either all GRNI or
a booked accrual reconciliation.  It reconstructs the same-business-unit,
PO-linked document population that a controller would review: eligible
receipt value through today's cutoff, less eligible posted voucher-line value
attributable to the same purchase-order line/schedule.  Non-PO, inventory,
miscellaneous and cross-business-unit receipt accruals are outside this
control.  It does *not* assert that PeopleSoft Receipt Accrual created an RAC
accounting line or that Journal Generator posted a corresponding GL journal.

The distinction is an accounting control, not wording polish.  Proving a
booked receipt accrual requires RECV_LN_ACCTG (or an authoritative custom
equivalent), RAC activity/reversals, distribution status, complete Journal
Generator keys, and the posted GL journal.  Receipt and voucher documents
alone cannot prove any of those things.

Amounts stay in their transaction currency.  No currency conversion or
cross-currency grand total is attempted.  Missing source dates, document
keys, status, currency, non-finite amounts, ambiguous custom-record mappings,
or capped source populations make the result incomplete rather than partial
figures masquerading as a conclusion.  A historical cutoff also returns
incomplete: ENTERED_DT does not make mutable current status, amount, match,
and reference fields effective dated.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .db import DbError
from .modules import ModuleError, ModulePacks


_SAFE_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]*")
_DEFAULT_ROW_CAP = 50_000
_HARD_ROW_CAP = 100_000
_MONEY = Decimal("0.01")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _integer(value: Any, label: str) -> int:
    if value is None or isinstance(value, bool) or _text(value) == "":
        raise ValueError(f"{label} is blank")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not an integer") from exc
    if number <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return number


def _decimal(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool) or _text(value) == "":
        raise ValueError(f"{label} is blank")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not numeric") from exc
    if not amount.is_finite():
        raise ValueError(f"{label} is not finite")
    return amount


def _money(value: Decimal) -> float:
    return float(value.quantize(_MONEY, rounding=ROUND_HALF_UP))


def _date(value: Any, label: str) -> dt.date:
    raw = _text(value)
    if not raw:
        raise ValueError(f"{label} is blank")
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date") from exc


def _age_bucket(days: int) -> str:
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "91_plus"


class GRNIControl:
    """Build a review-only GRNI population from PeopleSoft documents.

    ``modules`` supplies the repository's existing database connection,
    business-unit defaulting, and live-catalog resolver.  The method itself
    always predicates both receipt and voucher reads on the selected caller
    business unit; it never performs an installation-wide fallback.
    """

    def __init__(self, modules: ModulePacks):
        self.modules = modules
        self.e = modules.e
        self.db = modules.db

    # ------------------------------------------------------------------
    # Physical record resolution.  An explicit PeopleTools SQLTABLENAME
    # wins.  Without it, exactly one live-catalog suffix may resolve.  The
    # delivered PS_ spelling is only the last exact-catalog fallback.
    def _resolve_record(self, logical_name: str) -> tuple[str, str]:
        logical = logical_name.strip().upper()
        mapped = ""
        try:
            rows, truncated = self.db.query(
                "SELECT SQLTABLENAME AS sqltablename "
                f"FROM {self.db.prefix}PSRECDEFN "
                "WHERE UPPER(RECNAME) = :rec",
                {"rec": logical},
                max_rows=2,
            )
            if truncated or len(rows) > 1:
                raise ModuleError(
                    f"PSRECDEFN returned more than one {logical} mapping; "
                    "no physical source was selected."
                )
            if rows:
                mapped = _text(rows[0].get("sqltablename")).upper()
        except DbError:
            # Some service accounts do not have PeopleTools grants.  The
            # live catalog remains an explainable, lower-confidence route.
            mapped = ""

        if mapped:
            if not _SAFE_IDENTIFIER.fullmatch(mapped):
                raise ModuleError(
                    f"PSRECDEFN maps {logical} to unsafe or qualified object "
                    f"name {mapped!r}; no fallback was attempted."
                )
            if not self.db.columns(mapped):
                raise ModuleError(
                    f"PSRECDEFN maps {logical} to {mapped}, but that object "
                    "is not readable; no similarly named table was used."
                )
            return mapped, "PSRECDEFN.SQLTABLENAME"

        candidates: list[str] = []
        try:
            listing = self.e.list_tables(logical)
            if listing.get("truncated"):
                raise ModuleError(
                    f"The live-catalog search for {logical} was truncated; "
                    "a company-prefixed match may be outside the returned "
                    "page, so no delivered fallback was selected."
                )
            rows = listing.get("tables", [])
            for row in rows:
                name = _text(row.get("table_name")).upper()
                if not _SAFE_IDENTIFIER.fullmatch(name):
                    continue
                if (name == logical or name == f"PS_{logical}"
                        or name.endswith(f"_{logical}")):
                    candidates.append(name)
        except ModuleError:
            raise
        except Exception:  # raw catalog tools may be disabled
            candidates = []
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) > 1:
            raise ModuleError(
                f"More than one physical object matches {logical}: "
                + ", ".join(candidates)
                + ". Configure an authoritative PSRECDEFN mapping; no "
                  "prefix or sort-order guess was made."
            )
        if candidates:
            if not self.db.columns(candidates[0]):
                raise ModuleError(
                    f"Catalog object {candidates[0]} for {logical} is not "
                    "readable."
                )
            return candidates[0], "unique live-catalog suffix"

        delivered = f"PS_{logical}"
        if self.db.columns(delivered):
            return (delivered,
                    "exact delivered catalog fallback; richer mapping unavailable")
        raise ModuleError(
            f"No unambiguous readable physical source was found for "
            f"{logical}."
        )

    @staticmethod
    def _require(record: str, columns: set[str], required: set[str]) -> None:
        missing = sorted(required - columns)
        if missing:
            raise ModuleError(
                f"{record} is missing the GRNI source fields: "
                + ", ".join(missing)
            )

    @staticmethod
    def _in(prefix: str, values: list[str]) -> tuple[str, dict[str, Any]]:
        binds = {f"{prefix}{index}": value
                 for index, value in enumerate(values)}
        return ("(" + ", ".join(f":{name}" for name in binds) + ")",
                binds)

    def period_end_accrual(
        self,
        business_unit: str = "",
        as_of_date: str = "",
        materiality: float = 0.0,
        aging_days: int = 30,
        max_rows: int = _DEFAULT_ROW_CAP,
    ) -> dict:
        """Return current-date PO-linked review candidates for one BU.

        ``materiality`` is applied independently in each transaction
        A prior date is accepted only to return an explainable incomplete
        result; current document rows cannot reconstruct historical mutable
        status. ``materiality`` is applied independently in each transaction
        currency; it is not a reporting-currency threshold.  ``max_rows`` is
        configurable for large sites but is clamped to a 100,000-row hard
        safety ceiling per source population.
        """
        bu = self.modules._bu(business_unit)
        if not bu:
            raise ModuleError(
                "business_unit is required; GRNI never scans all business "
                "units by default."
            )
        if bu.strip().upper() in ("ALL", "*"):
            # Passed through as a literal unit this read "no receipts found
            # for ALL", which a reader takes as "no GRNI anywhere" rather
            # than "ALL is not a business unit".
            raise ModuleError(
                f"{bu!r} is not a business unit. This control evaluates one "
                "unit at a time because the receipt, PO and voucher units "
                "must match; name a single business unit."
            )
        try:
            cutoff = dt.date.fromisoformat(
                (as_of_date or "").strip()[:10]
                if (as_of_date or "").strip()
                else dt.date.today().isoformat()
            )
        except ValueError as exc:
            raise ModuleError(
                "as_of_date must be an ISO date in YYYY-MM-DD format"
            ) from exc
        if cutoff > dt.date.today():
            raise ModuleError(
                f"as_of_date {cutoff.isoformat()} is in the future; no "
                "period-end population was evaluated."
            )
        try:
            threshold = _decimal(materiality, "materiality")
        except ValueError as exc:
            raise ModuleError(str(exc)) from exc
        if threshold < 0:
            raise ModuleError("materiality cannot be negative")
        try:
            age_threshold = int(aging_days)
        except (TypeError, ValueError) as exc:
            raise ModuleError("aging_days must be an integer") from exc
        if isinstance(aging_days, bool) or not 0 <= age_threshold <= 3650:
            raise ModuleError("aging_days must be between 0 and 3650")
        try:
            requested_cap = int(max_rows)
        except (TypeError, ValueError) as exc:
            raise ModuleError("max_rows must be an integer") from exc
        if isinstance(max_rows, bool) or requested_cap < 1:
            raise ModuleError("max_rows must be greater than zero")
        row_cap = min(requested_cap, _HARD_ROW_CAP)

        empty_exceptions = {
            "material_or_aged": [],
            "over_invoiced": [],
            "unmatched_voucher_references": [],
            "excluded_voucher_statuses": [],
            "excluded_receipt_statuses": [],
        }
        base = {
            "status": "incomplete",
            "evaluated": False,
            "conclusion": "not_evaluated",
            "business_unit": bu,
            "as_of_date": cutoff.isoformat(),
            "truncated": False,
            "coverage": {
                "classification": "po_linked_document_review_only",
                "all_grni_complete": False,
                "point_in_time_complete": cutoff == dt.date.today(),
                "business_unit_basis": (
                    "receipt, PO, and AP voucher business units must all "
                    "equal the selected caller business unit"
                ),
                "included": (
                    "same-business-unit, PO-linked receipt shipment lines "
                    "eligible by current receipt status"
                ),
                "excluded": [
                    "non-PO receipts",
                    "inventory and miscellaneous receipt accrual populations",
                    "cross-business-unit PO/voucher relationships",
                    "booked RAC receipt-accounting and posted GL evidence",
                ],
                "historical_limitation": (
                    "receipt/voucher status, amount, match and reference "
                    "fields are current state and are not effective dated"
                ),
            },
            "source_records": {},
            "candidate_basis": {
                "classification": "review_candidate_only",
                "formula": (
                    "same-business-unit, PO-linked, current-status-eligible "
                    "receipt-line value through cutoff less eligible posted "
                    "voucher-line value attributable through cutoff to the "
                    "same PO line/schedule"
                ),
                "amount_basis": (
                    "transaction-currency merchandise amounts; no currency "
                    "conversion and no cross-currency total"
                ),
                "does_not_prove": (
                    "a booked receipt-accrual accounting line, Journal "
                    "Generator distribution, posted GL journal, or required "
                    "journal entry"
                ),
            },
            "cutoff_basis": {},
            "matching_basis": {},
            "population": {
                "complete": False,
                "truncated": False,
                "requested_row_cap": requested_cap,
                "effective_row_cap": row_cap,
                "hard_row_cap": _HARD_ROW_CAP,
                "candidate_count": None,
            },
            "totals_by_currency": None,
            "rni_totals_by_currency": None,
            "lines": [],
            "exceptions": empty_exceptions,
            "booked_status": "not_evaluated",
            "booked_basis": (
                "Receipt/voucher documents cannot prove a booked GL accrual. "
                "That requires the authoritative receipt-accounting source, "
                "RAC/reversal semantics, distribution status, complete "
                "Journal Generator keys, and posted GL evidence."
            ),
        }

        def incomplete(reason: str, **extra: Any) -> dict:
            return {**base, "reason": reason, **extra}

        if cutoff < dt.date.today():
            return incomplete(
                f"{cutoff.isoformat()} is a historical cutoff, but receipt "
                "and voucher status, amount, matching and reference fields "
                "are current state without effective-dated history. "
                "ENTERED_DT can prevent a newly entered backdated voucher, "
                "but cannot reconstruct those mutable fields; no exact "
                "point-in-time PO-linked candidate population was evaluated."
            )

        # Resolve all five sources before querying the transaction population.
        logicals = ("PO_HDR", "RECV_HDR", "RECV_LN_SHIP", "VOUCHER",
                    "VOUCHER_LINE")
        sources: dict[str, str] = {}
        source_basis: dict[str, str] = {}
        try:
            for logical in logicals:
                source, basis = self._resolve_record(logical)
                sources[logical] = source
                source_basis[logical] = basis
        except ModuleError as exc:
            return incomplete(str(exc))
        base["source_records"] = {
            logical: {"physical": sources[logical],
                      "resolution_basis": source_basis[logical]}
            for logical in logicals
        }

        columns = {logical: self.db.columns(source)
                   for logical, source in sources.items()}
        try:
            self._require(sources["PO_HDR"], columns["PO_HDR"], {
                "BUSINESS_UNIT", "PO_ID", "CURRENCY_CD"})
            self._require(sources["RECV_HDR"], columns["RECV_HDR"], {
                "BUSINESS_UNIT", "RECEIVER_ID", "RECEIPT_DT",
                "RECV_STATUS"})
            self._require(sources["RECV_LN_SHIP"],
                          columns["RECV_LN_SHIP"], {
                "BUSINESS_UNIT", "RECEIVER_ID", "RECV_LN_NBR", "PO_ID",
                "LINE_NBR", "MERCHANDISE_AMT"})
            self._require(sources["VOUCHER"], columns["VOUCHER"], {
                "BUSINESS_UNIT", "VOUCHER_ID", "CURRENCY_CD",
                "ENTRY_STATUS", "POST_STATUS", "MATCH_STATUS_VCHR"})
            self._require(sources["VOUCHER_LINE"],
                          columns["VOUCHER_LINE"], {
                "BUSINESS_UNIT", "VOUCHER_ID", "VOUCHER_LINE_NUM",
                "PO_ID", "LINE_NBR", "MERCHANDISE_AMT"})
        except ModuleError as exc:
            return incomplete(str(exc))

        recv_seq_column = next((name for name in (
            "RECV_SHIP_SEQ_NBR", "RECV_SHP_SEQ_NBR")
            if name in columns["RECV_LN_SHIP"]), "")
        if not recv_seq_column:
            return incomplete(
                f"{sources['RECV_LN_SHIP']} has no receipt shipment sequence "
                "field; receipt rows cannot be uniquely identified."
            )
        voucher_date_column = next((name for name in (
            "ACCOUNTING_DT", "INVOICE_DT", "ENTERED_DT")
            if name in columns["VOUCHER"]), "")
        if not voucher_date_column:
            return incomplete(
                f"{sources['VOUCHER']} has no ACCOUNTING_DT, INVOICE_DT or "
                "ENTERED_DT; vouchers cannot be attributed through the "
                "selected cutoff."
            )
        availability_column = (
            "ENTERED_DT" if "ENTERED_DT" in columns["VOUCHER"] else ""
        )
        use_schedule = (
            "SCHED_NBR" in columns["RECV_LN_SHIP"]
            and "SCHED_NBR" in columns["VOUCHER_LINE"]
        )
        recv_po_bu = ("L.BUSINESS_UNIT_PO"
                      if "BUSINESS_UNIT_PO" in columns["RECV_LN_SHIP"]
                      else "L.BUSINESS_UNIT")
        voucher_po_bu = ("L.BUSINESS_UNIT_PO"
                         if "BUSINESS_UNIT_PO" in columns["VOUCHER_LINE"]
                         else "L.BUSINESS_UNIT")
        vendor_expr = ("P.VENDOR_ID"
                       if "VENDOR_ID" in columns["PO_HDR"] else "NULL")
        po_status_expr = ("P.PO_STATUS"
                          if "PO_STATUS" in columns["PO_HDR"] else "NULL")
        qty_expr = ("L.QTY_SH_ACCPT_VUOM"
                    if "QTY_SH_ACCPT_VUOM" in columns["RECV_LN_SHIP"]
                    else "NULL")
        recv_sched_expr = "L.SCHED_NBR" if use_schedule else "NULL"

        base["cutoff_basis"] = {
            "receipts": (
                f"{sources['RECV_HDR']}.RECEIPT_DT <= "
                f"{cutoff.isoformat()}"
            ),
            "vouchers": (
                f"{sources['VOUCHER']}.{voucher_date_column} <= "
                f"{cutoff.isoformat()}"
            ),
            "availability_guard": (
                f"{sources['VOUCHER']}.{availability_column} <= cutoff"
                if availability_column else
                "no separate voucher entry/availability date exists; the "
                f"document is attributed by {voucher_date_column}"
            ),
            "receipt_status": (
                "current M (Moved), N (Not Recv'd), O (Open), and R "
                "(Received) are eligible; H (Hold), C (Closed/Complete), "
                "and X (Canceled) are excluded; blank/unknown is incomplete"
            ),
            "voucher_status": (
                "only ENTRY_STATUS='P' and POST_STATUS='P' voucher lines "
                "reduce candidates; recycle/deleted/unposted lines stay in "
                "the review population; MATCH_STATUS_VCHR is validated and "
                "disclosed but does not prove GL posting"
            ),
        }
        base["matching_basis"] = {
            "primary_key": (["PO_BUSINESS_UNIT", "PO_ID", "LINE_NBR",
                             "SCHED_NBR"] if use_schedule else
                            ["PO_BUSINESS_UNIT", "PO_ID", "LINE_NBR"]),
            "precision": ("po_line_schedule" if use_schedule else "po_line"),
            "receipt_reference": (
                "voucher RECEIVER_ID/RECV_LN_NBR is validated when supplied; "
                "blank receipt references fall back only to the primary "
                "PO line/schedule key"
            ),
            "reduced_precision": not use_schedule,
            "business_unit": (
                "receipt BU = PO BU = AP voucher BU = selected caller BU; "
                "cross-unit relationships are refused, not followed"
            ),
        }

        p = self.db.prefix
        receipt_sql = f"""
SELECT H.BUSINESS_UNIT AS receipt_bu,
       H.RECEIVER_ID AS receiver_id,
       H.RECEIPT_DT AS receipt_dt,
       H.RECV_STATUS AS receipt_status,
       L.RECV_LN_NBR AS recv_line,
       L.{recv_seq_column} AS recv_ship_seq,
       {recv_po_bu} AS po_bu,
       L.PO_ID AS po_id,
       L.LINE_NBR AS line_nbr,
       {recv_sched_expr} AS sched_nbr,
       {qty_expr} AS accepted_qty,
       L.MERCHANDISE_AMT AS receipt_amount,
       {vendor_expr} AS vendor_id,
       P.CURRENCY_CD AS currency,
       {po_status_expr} AS po_status
  FROM {p}{sources['RECV_HDR']} H
  JOIN {p}{sources['RECV_LN_SHIP']} L
    ON L.BUSINESS_UNIT = H.BUSINESS_UNIT
   AND L.RECEIVER_ID = H.RECEIVER_ID
  LEFT JOIN {p}{sources['PO_HDR']} P
    ON P.BUSINESS_UNIT = {recv_po_bu}
   AND P.PO_ID = L.PO_ID
 WHERE H.BUSINESS_UNIT = :bu
   AND (H.RECEIPT_DT <= {self.db.date_bind('asof')}
        OR H.RECEIPT_DT IS NULL)
 ORDER BY H.RECEIPT_DT, H.RECEIVER_ID, L.RECV_LN_NBR,
          L.{recv_seq_column}
"""
        try:
            receipt_rows, receipt_truncated = self.db.query(
                receipt_sql, {"bu": bu, "asof": cutoff.isoformat()},
                max_rows=row_cap)
        except DbError as exc:
            return incomplete(
                f"Receipt population could not be read: {exc}")
        if receipt_truncated:
            population = {
                **base["population"],
                "receipt_rows_returned": len(receipt_rows),
                "complete": False,
                "truncated": True,
            }
            return incomplete(
                f"More than {row_cap:,} receipt shipment rows matched "
                f"{bu} through {cutoff.isoformat()}; no candidate total was "
                "evaluated from the partial population.",
                truncated=True,
                population=population,
            )
        if not receipt_rows:
            return {
                **base,
                "status": "no_data",
                "reason": (
                    f"No rows were found in the resolved PO receipt-shipment "
                    f"source for {bu} through {cutoff.isoformat()}. This is "
                    "no data for the scoped PO-linked control, not proof of "
                    "a zero booked accrual or a zero all-GRNI population."
                ),
                "population": {
                    **base["population"],
                    "complete": True,
                    "candidate_count": 0,
                    "receipt_rows_returned": 0,
                    "voucher_rows_returned": 0,
                },
            }

        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        receipt_events: set[tuple[Any, ...]] = set()
        non_po_rows = 0
        cross_bu_po_rows = 0
        excluded_receipt_rows: list[dict[str, Any]] = []
        eligible_receipt_statuses = {"M", "N", "O", "R"}
        excluded_receipt_statuses = {"H", "C", "X"}
        try:
            for row in receipt_rows:
                receipt_bu = _text(row.get("receipt_bu"))
                receiver = _text(row.get("receiver_id"))
                recv_line = _integer(row.get("recv_line"), "RECV_LN_NBR")
                recv_seq = _integer(row.get("recv_ship_seq"),
                                    recv_seq_column)
                po_bu = _text(row.get("po_bu"))
                po_id = _text(row.get("po_id"))
                receipt_day = _date(row.get("receipt_dt"), "RECEIPT_DT")
                status = _text(row.get("receipt_status")).upper()
                amount = _decimal(row.get("receipt_amount"),
                                  "receipt MERCHANDISE_AMT")
                if receipt_bu != bu:
                    raise ValueError(
                        "receipt query returned a row outside the selected "
                        "business unit")
                if not receiver:
                    raise ValueError(
                        "receipt source key RECEIVER_ID is blank")
                if not status:
                    raise ValueError("RECV_STATUS is blank")
                if status not in (eligible_receipt_statuses
                                  | excluded_receipt_statuses):
                    raise ValueError(
                        f"RECV_STATUS {status!r} is not a governed eligible/"
                        "excluded value")
                event = (receipt_bu, receiver, recv_line, recv_seq)
                if event in receipt_events:
                    raise ValueError(
                        "duplicate receipt shipment key "
                        f"{receiver}/{recv_line}/{recv_seq}")
                receipt_events.add(event)
                if not po_id:
                    non_po_rows += 1
                    continue
                if not po_bu:
                    raise ValueError(
                        f"receipt {receiver}/{recv_line}/{recv_seq} has a PO "
                        "but no PO business unit")
                if po_bu != bu:
                    cross_bu_po_rows += 1
                    continue
                line = _integer(row.get("line_nbr"), "receipt LINE_NBR")
                schedule = (_integer(row.get("sched_nbr"),
                                     "receipt SCHED_NBR")
                            if use_schedule else None)
                currency = _text(row.get("currency")).upper()
                if not currency:
                    raise ValueError(
                        f"same-unit PO {po_id} has no readable CURRENCY_CD "
                        "or matching PO header")
                if status in excluded_receipt_statuses:
                    excluded_receipt_rows.append({
                        "receiver_id": receiver,
                        "recv_line_nbr": recv_line,
                        "recv_ship_seq": recv_seq,
                        "po_id": po_id,
                        "line_nbr": line,
                        "sched_nbr": schedule,
                        "receipt_status": status,
                        "currency": currency,
                        "amount": _money(amount),
                        "reason": (
                            "current receipt status is not accrual-eligible "
                            "for this PO-linked candidate population"
                        ),
                    })
                    continue
                key = ((po_bu, po_id, line, schedule)
                       if use_schedule else (po_bu, po_id, line))
                group = groups.setdefault(key, {
                    "po_business_unit": po_bu,
                    "po_id": po_id,
                    "line_nbr": line,
                    "sched_nbr": schedule,
                    "vendor_id": _text(row.get("vendor_id")),
                    "po_status": _text(row.get("po_status")),
                    "currency": currency,
                    "received": Decimal("0"),
                    "invoiced": Decimal("0"),
                    "receipt_dates": [],
                    "receivers": set(),
                    "receiver_lines": set(),
                    "voucher_ids": set(),
                    "voucher_lines": 0,
                    "exact_receipt_references": 0,
                    "schedule_fallback_references": 0,
                    "receipt_rows": 0,
                    "negative_receipt_rows": 0,
                })
                if group["currency"] != currency:
                    raise ValueError(
                        f"receipt schedule {po_id}/{line}/{schedule or ''} "
                        "has more than one PO currency")
                group["received"] += amount
                group["receipt_dates"].append(receipt_day)
                group["receivers"].add(receiver)
                group["receiver_lines"].add((receiver, recv_line))
                group["receipt_rows"] += 1
                if amount < 0:
                    group["negative_receipt_rows"] += 1
        except ValueError as exc:
            return incomplete(
                f"Receipt population is not reconstructible: {exc}",
                population={
                    **base["population"],
                    "receipt_rows_returned": len(receipt_rows),
                    "complete": False,
                },
            )

        if cross_bu_po_rows:
            return incomplete(
                f"{cross_bu_po_rows} receipt shipment row(s) in {bu} reference "
                "a different PO business unit. The control will not widen "
                "caller scope or assume the AP voucher business unit, so no "
                "candidate total was evaluated.",
                population={
                    **base["population"],
                    "receipt_rows_returned": len(receipt_rows),
                    "cross_business_unit_po_rows": cross_bu_po_rows,
                    "complete": False,
                },
            )

        if not groups:
            return {
                **base,
                "status": "no_data",
                "reason": (
                    "No current-status-eligible, same-business-unit, "
                    "PO-linked receipt shipment rows remain in this scoped "
                    "population. This says nothing about non-PO, inventory, "
                    "miscellaneous, cross-unit, or booked-GL accruals."
                ),
                "population": {
                    **base["population"],
                    "complete": True,
                    "candidate_count": 0,
                    "receipt_rows_returned": len(receipt_rows),
                    "eligible_receipt_rows": 0,
                    "non_po_receipt_rows_excluded": non_po_rows,
                    "receipt_rows_excluded_by_status":
                        len(excluded_receipt_rows),
                    "voucher_rows_returned": 0,
                },
                "exceptions": {
                    **empty_exceptions,
                    "excluded_receipt_statuses": excluded_receipt_rows,
                },
            }

        # Pull only vouchers for POs that have an eligible receipt through
        # cutoff.  Batches keep Oracle below its 1,000-value IN limit.
        po_ids_by_bu: dict[str, set[str]] = {}
        for key in groups:
            po_ids_by_bu.setdefault(str(key[0]), set()).add(str(key[1]))
        voucher_rows: list[dict] = []
        voucher_truncated = False
        voucher_sched_expr = "L.SCHED_NBR" if use_schedule else "NULL"
        voucher_receiver_expr = (
            "L.RECEIVER_ID" if "RECEIVER_ID" in columns["VOUCHER_LINE"]
            else "NULL")
        voucher_recv_line_expr = (
            "L.RECV_LN_NBR" if "RECV_LN_NBR" in columns["VOUCHER_LINE"]
            else "NULL")
        availability_expr = (
            f"V.{availability_column}" if availability_column else "NULL")
        post_status_expr = (
            "V.POST_STATUS")
        match_status_expr = "V.MATCH_STATUS_VCHR"
        invoice_date_expr = (
            "V.INVOICE_DT" if "INVOICE_DT" in columns["VOUCHER"]
            else "NULL")
        for po_bu, po_ids_set in sorted(po_ids_by_bu.items()):
            po_ids = sorted(po_ids_set)
            for start in range(0, len(po_ids), 500):
                if len(voucher_rows) >= row_cap:
                    voucher_truncated = True
                    break
                chunk = po_ids[start:start + 500]
                in_sql, in_binds = self._in("po", chunk)
                voucher_sql = f"""
SELECT V.BUSINESS_UNIT AS voucher_bu,
       V.VOUCHER_ID AS voucher_id,
       V.{voucher_date_column} AS voucher_cutoff_dt,
       {availability_expr} AS voucher_available_dt,
       {invoice_date_expr} AS invoice_dt,
       V.CURRENCY_CD AS currency,
       V.ENTRY_STATUS AS entry_status,
       {post_status_expr} AS post_status,
       {match_status_expr} AS match_status,
       L.VOUCHER_LINE_NUM AS voucher_line,
       {voucher_po_bu} AS po_bu,
       L.PO_ID AS po_id,
       L.LINE_NBR AS line_nbr,
       {voucher_sched_expr} AS sched_nbr,
       {voucher_receiver_expr} AS receiver_id,
       {voucher_recv_line_expr} AS recv_line,
       L.MERCHANDISE_AMT AS voucher_amount
  FROM {p}{sources['VOUCHER']} V
  JOIN {p}{sources['VOUCHER_LINE']} L
    ON L.BUSINESS_UNIT = V.BUSINESS_UNIT
   AND L.VOUCHER_ID = V.VOUCHER_ID
 WHERE V.BUSINESS_UNIT = :bu
   AND L.BUSINESS_UNIT = :bu
   AND {voucher_po_bu} = :po_bu
   AND L.PO_ID IN {in_sql}
   AND (V.{voucher_date_column} <= {self.db.date_bind('asof')}
        OR V.{voucher_date_column} IS NULL)
 ORDER BY V.VOUCHER_ID, L.VOUCHER_LINE_NUM
"""
                remaining = row_cap - len(voucher_rows)
                try:
                    rows, was_truncated = self.db.query(
                        voucher_sql,
                        {"bu": bu, "po_bu": po_bu,
                         "asof": cutoff.isoformat(), **in_binds},
                        max_rows=remaining,
                    )
                except DbError as exc:
                    return incomplete(
                        f"Voucher population could not be read: {exc}",
                        population={
                            **base["population"],
                            "receipt_rows_returned": len(receipt_rows),
                            "complete": False,
                        },
                    )
                voucher_rows.extend(rows)
                if was_truncated:
                    voucher_truncated = True
                    break
            if voucher_truncated:
                break
        if voucher_truncated:
            return incomplete(
                f"More than {row_cap:,} attributable voucher lines matched "
                f"{bu} through {cutoff.isoformat()}; no candidate total was "
                "evaluated from the partial population.",
                truncated=True,
                population={
                    **base["population"],
                    "receipt_rows_returned": len(receipt_rows),
                    "voucher_rows_returned": len(voucher_rows),
                    "complete": False,
                    "truncated": True,
                },
            )

        voucher_lines_seen: set[tuple[str, str, int]] = set()
        unmatched: list[dict[str, Any]] = []
        outside_receipt_population = 0
        vouchers_after_availability_cutoff = 0
        status_counts: dict[str, int] = {}
        excluded_status_rows: list[dict[str, Any]] = []
        attributed_rows = 0
        exact_references = 0
        fallback_references = 0
        try:
            for row in voucher_rows:
                voucher_bu = _text(row.get("voucher_bu"))
                voucher_id = _text(row.get("voucher_id"))
                voucher_line = _integer(row.get("voucher_line"),
                                        "VOUCHER_LINE_NUM")
                po_bu = _text(row.get("po_bu"))
                po_id = _text(row.get("po_id"))
                line = _integer(row.get("line_nbr"),
                                "voucher LINE_NBR")
                schedule = (_integer(row.get("sched_nbr"),
                                     "voucher SCHED_NBR")
                            if use_schedule else None)
                cutoff_day = _date(row.get("voucher_cutoff_dt"),
                                   voucher_date_column)
                entry_day = (_date(row.get("voucher_available_dt"),
                                   availability_column)
                             if availability_column else None)
                status = _text(row.get("entry_status")).upper()
                post_status = _text(row.get("post_status")).upper()
                match_status = _text(row.get("match_status")).upper()
                currency = _text(row.get("currency")).upper()
                amount = _decimal(row.get("voucher_amount"),
                                  "voucher MERCHANDISE_AMT")
                if voucher_bu != bu:
                    raise ValueError(
                        "voucher query returned a row outside the selected "
                        "business unit")
                if not voucher_id or not po_bu or not po_id:
                    raise ValueError(
                        "voucher source key is blank (VOUCHER_ID, PO business "
                        "unit, or PO_ID)")
                if not status:
                    raise ValueError("voucher ENTRY_STATUS is blank")
                if not post_status:
                    raise ValueError("voucher POST_STATUS is blank")
                if not match_status:
                    raise ValueError("voucher MATCH_STATUS_VCHR is blank")
                if status not in {"P", "R", "D"}:
                    raise ValueError(
                        f"voucher ENTRY_STATUS {status!r} is not a governed "
                        "included/excluded value")
                if post_status not in {"P", "U"}:
                    raise ValueError(
                        f"voucher POST_STATUS {post_status!r} is not a "
                        "governed included/excluded value")
                if match_status not in {"T", "E", "D", "N", "O"}:
                    raise ValueError(
                        f"voucher MATCH_STATUS_VCHR {match_status!r} is not "
                        "a governed match value")
                if not currency:
                    raise ValueError("voucher CURRENCY_CD is blank")
                identity = (voucher_bu, voucher_id, voucher_line)
                if identity in voucher_lines_seen:
                    raise ValueError(
                        f"duplicate voucher-line key {voucher_id}/"
                        f"{voucher_line}")
                voucher_lines_seen.add(identity)
                if cutoff_day > cutoff:
                    continue
                if entry_day is not None and entry_day > cutoff:
                    vouchers_after_availability_cutoff += 1
                    continue
                key = ((po_bu, po_id, line, schedule)
                       if use_schedule else (po_bu, po_id, line))
                group = groups.get(key)
                if group is None:
                    outside_receipt_population += 1
                    continue
                status_key = f"ENTRY={status};POST={post_status};MATCH={match_status}"
                status_counts[status_key] = status_counts.get(status_key, 0) + 1
                if status != "P" or post_status != "P":
                    excluded_status_rows.append({
                        "voucher_id": voucher_id,
                        "voucher_line": voucher_line,
                        "po_business_unit": po_bu,
                        "po_id": po_id,
                        "line_nbr": line,
                        "sched_nbr": schedule,
                        "currency": currency,
                        "amount": _money(amount),
                        "entry_status": status,
                        "post_status": post_status,
                        "match_status": match_status,
                        "reason": (
                            "voucher line is not both entry-status P and "
                            "post-status P; it remains in the review candidate "
                            "instead of suppressing receipt exposure"
                        ),
                    })
                    continue
                if currency != group["currency"]:
                    raise ValueError(
                        f"voucher {voucher_id}/{voucher_line} is {currency} "
                        f"but receipt schedule {po_id}/{line}/"
                        f"{schedule or ''} is {group['currency']}; no "
                        "cross-currency subtraction was made")
                receiver = _text(row.get("receiver_id"))
                recv_line_raw = row.get("recv_line")
                recv_line = None
                if receiver and recv_line_raw is not None \
                        and _text(recv_line_raw) != "":
                    recv_line = _integer(recv_line_raw,
                                         "voucher RECV_LN_NBR")
                if receiver:
                    valid_reference = (
                        (receiver, recv_line) in group["receiver_lines"]
                        if recv_line is not None else
                        receiver in group["receivers"]
                    )
                    if not valid_reference:
                        unmatched.append({
                            "voucher_id": voucher_id,
                            "voucher_line": voucher_line,
                            "po_business_unit": po_bu,
                            "po_id": po_id,
                            "line_nbr": line,
                            "sched_nbr": schedule,
                            "receiver_id": receiver,
                            "recv_line_nbr": recv_line,
                            "currency": currency,
                            "amount": _money(amount),
                            "reason": (
                                "explicit receipt reference does not match a "
                                "receipt in this cutoff population"
                            ),
                        })
                        continue
                    group["exact_receipt_references"] += 1
                    exact_references += 1
                else:
                    group["schedule_fallback_references"] += 1
                    fallback_references += 1
                group["invoiced"] += amount
                group["voucher_ids"].add(voucher_id)
                group["voucher_lines"] += 1
                attributed_rows += 1
        except ValueError as exc:
            return incomplete(
                f"Voucher population is not reconstructible: {exc}",
                population={
                    **base["population"],
                    "receipt_rows_returned": len(receipt_rows),
                    "voucher_rows_returned": len(voucher_rows),
                    "complete": False,
                },
            )

        # An explicit receiver reference that conflicts with the receipt
        # population is not silently demoted to a broad schedule match.
        if unmatched:
            exceptions = {
                **empty_exceptions,
                "unmatched_voucher_references": unmatched[:100],
                "excluded_voucher_statuses": excluded_status_rows[:100],
            }
            return incomplete(
                f"{len(unmatched)} voucher line(s) carry an explicit receipt "
                "reference that does not match the cutoff receipt population; "
                "no candidate total was concluded.",
                exceptions=exceptions,
                population={
                    **base["population"],
                    "receipt_rows_returned": len(receipt_rows),
                    "voucher_rows_returned": len(voucher_rows),
                    "voucher_rows_attributed": attributed_rows,
                    "complete": False,
                },
            )

        lines: list[dict[str, Any]] = []
        over_invoiced: list[dict[str, Any]] = []
        totals: dict[str, dict[str, Decimal]] = {}
        age_counts = {"0_30": 0, "31_60": 0, "61_90": 0,
                      "91_plus": 0}
        for _key, group in sorted(groups.items(), key=lambda item: item[0]):
            received = group["received"]
            invoiced = group["invoiced"]
            currency = group["currency"]
            total = totals.setdefault(currency, {
                "received": Decimal("0"),
                "invoiced": Decimal("0"),
                "candidate": Decimal("0"),
                "over_invoiced": Decimal("0"),
            })
            total["received"] += received
            total["invoiced"] += invoiced
            if received < 0:
                return incomplete(
                    f"Receipt schedule {group['po_id']}/"
                    f"{group['line_nbr']}/{group['sched_nbr'] or ''} has a "
                    "negative net receipt amount; reversal chronology is "
                    "required before an accrual candidate can be concluded.",
                    population={
                        **base["population"],
                        "receipt_rows_returned": len(receipt_rows),
                        "voucher_rows_returned": len(voucher_rows),
                        "complete": False,
                    },
                )
            net = received - invoiced
            if net < 0:
                excess = -net
                total["over_invoiced"] += excess
                over_invoiced.append({
                    "po_business_unit": group["po_business_unit"],
                    "po_id": group["po_id"],
                    "line_nbr": group["line_nbr"],
                    "sched_nbr": group["sched_nbr"],
                    "vendor_id": group["vendor_id"],
                    "currency": currency,
                    "received_amount": _money(received),
                    "attributed_voucher_amount": _money(invoiced),
                    "over_invoiced_amount": _money(excess),
                    "reason": (
                        "voucher-line value through cutoff exceeds accepted "
                        "receipt-line value on this matching key"
                    ),
                })
                net = Decimal("0")
            total["candidate"] += net
            if net <= 0:
                continue
            first_day = min(group["receipt_dates"])
            last_day = max(group["receipt_dates"])
            age = (cutoff - first_day).days
            bucket = _age_bucket(age)
            age_counts[bucket] += 1
            material = net >= threshold if threshold > 0 else True
            aged = age >= age_threshold
            lines.append({
                "classification": "review_candidate_only",
                "po_business_unit": group["po_business_unit"],
                "po_id": group["po_id"],
                "line_nbr": group["line_nbr"],
                "sched_nbr": group["sched_nbr"],
                "vendor_id": group["vendor_id"],
                "po_status": group["po_status"],
                "currency": currency,
                "received_amount": _money(received),
                "attributed_voucher_amount": _money(invoiced),
                "rni_candidate_amount": _money(net),
                "candidate_amount": _money(net),
                "first_receipt_date": first_day.isoformat(),
                "receipt_date": first_day.isoformat(),
                "last_receipt_date": last_day.isoformat(),
                "age_days": age,
                "age_bucket": bucket,
                "material": material,
                "aged": aged,
                "receipt_rows": group["receipt_rows"],
                "negative_receipt_rows": group["negative_receipt_rows"],
                "receiver_ids": sorted(group["receivers"]),
                "voucher_ids": sorted(group["voucher_ids"]),
                "voucher_lines": group["voucher_lines"],
                "exact_receipt_references":
                    group["exact_receipt_references"],
                "schedule_fallback_references":
                    group["schedule_fallback_references"],
                "matching_precision": (
                    "po_line_schedule" if use_schedule else "po_line"),
            })

        lines.sort(key=lambda row: (
            -float(row["rni_candidate_amount"]),
            -int(row["age_days"]),
            str(row["currency"]), str(row["po_id"]),
        ))
        over_invoiced.sort(key=lambda row: -float(
            row["over_invoiced_amount"]))
        material_or_aged = [row for row in lines
                            if row["material"] or row["aged"]]
        totals_rows = [{
            "currency": currency,
            "received_amount": _money(values["received"]),
            "attributed_voucher_amount": _money(values["invoiced"]),
            "rni_candidate_amount": _money(values["candidate"]),
            "amount": _money(values["candidate"]),
            "total": _money(values["candidate"]),
            "over_invoiced_amount": _money(values["over_invoiced"]),
        } for currency, values in sorted(totals.items())]
        rni_map = {row["currency"]: row["rni_candidate_amount"]
                   for row in totals_rows}
        complete_population = {
            **base["population"],
            "complete": True,
            "truncated": False,
            "receipt_rows_returned": len(receipt_rows),
            "eligible_receipt_rows": sum(
                int(group["receipt_rows"]) for group in groups.values()),
            "non_po_receipt_rows_excluded": non_po_rows,
            "receipt_rows_excluded_by_status": len(excluded_receipt_rows),
            "receipt_matching_keys": len(groups),
            "voucher_rows_returned": len(voucher_rows),
            "voucher_rows_attributed": attributed_rows,
            "voucher_rows_excluded_by_status": len(excluded_status_rows),
            "voucher_rows_outside_cutoff_receipt_population":
                outside_receipt_population,
            "vouchers_excluded_by_availability_date":
                vouchers_after_availability_cutoff,
            "exact_receipt_references": exact_references,
            "schedule_fallback_references": fallback_references,
            "voucher_status_counts": [
                {"status": status, "lines": count}
                for status, count in sorted(status_counts.items())
            ],
            "currencies": sorted(totals),
            "age_bucket_counts": age_counts,
            "candidate_count": len(lines),
        }
        return {
            **base,
            "status": "evaluated",
            "evaluated": True,
            "conclusion": ("po_linked_candidates_present" if lines else
                           "no_po_linked_candidates"),
            "population": complete_population,
            "totals_by_currency": totals_rows,
            "rni_totals_by_currency": rni_map,
            "lines": lines,
            "exceptions": {
                "material_or_aged": material_or_aged,
                "over_invoiced": over_invoiced,
                "unmatched_voucher_references": [],
                "excluded_voucher_statuses": excluded_status_rows,
                "excluded_receipt_statuses": excluded_receipt_rows,
            },
            "materiality": {
                "amount": _money(threshold),
                "application": (
                    "applied separately to each transaction-currency line; "
                    "not a reporting-currency threshold"
                ),
                "aging_days": age_threshold,
            },
            "note": (
                "These are same-unit, PO-linked document review candidates, "
                "not all GRNI, not a proposed journal entry, and not evidence "
                "that an accrual is booked."
            ),
        }
