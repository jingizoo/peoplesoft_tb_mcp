"""Coupa: procurement truth beside the ledger's accounting truth.

Coupa knows what was requested, approved, received and invoiced; the
ledger knows what was vouchered and posted. The curated methods answer
the close-cycle questions each side alone cannot: what is stuck in
approval, what was received but never invoiced (the accrual candidates),
and whether everything Coupa approved actually landed in AP.

Every method filters client-side even though live calls also pass query
params — the same code path then serves fixtures and live traffic, and a
Coupa view whose server-side filter silently ignores a param cannot
quietly widen a result.

Amounts are grouped by currency and NEVER summed across currencies —
the same rule the AR tools follow.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import FIXTURE_DIR, ConnectorError, FixtureTransport, RestConnector


def _norm_name(s: str) -> str:
    """Supplier names differ in punctuation and suffixes across systems."""
    s = re.sub(r"[^a-z0-9 ]", "", str(s or "").lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|co|company|services|supply)\b", "", s)
    return " ".join(s.split())


def _iso(v) -> str:
    return str(v or "")[:10]


# Coupa receipt and invoice APIs expose high-precision decimal values.  Keep
# their validated magnitude in the JSON number; presentation may round later,
# but rounding every PO line here can make displayed detail cease to add to
# the independently aggregated population total.
_AMOUNT_TOLERANCE = Decimal("0.000001")
_COUPA_PAGE_SIZE = 50
_DEFAULT_RNI_ROW_CAP = 5_000
_HARD_RNI_ROW_CAP = 100_000
_DEFAULT_RNI_DISPLAY_ROWS = 200
_HARD_RNI_DISPLAY_ROWS = 50_000
_MAX_RNI_NESTED_EVIDENCE_ROWS = 20
_DEFAULT_ACTIVE_INVOICE_STATUSES = frozenset({"approved"})
_DEFAULT_RECEIPT_STATUSES = frozenset({"created"})
_KNOWN_INVOICE_STATUSES = frozenset({
    "new", "ap_hold", "draft", "on_hold", "pending_receipt",
    "rejected", "abandoned", "disputed", "pending_approval",
    "booking_hold", "save_as_draft", "pending_action", "approved",
    "voided", "processing", "invalid", "payable_adjustment",
})
_RNI_RECEIPT_TYPES = frozenset({
    "InventoryReceipt",
    "ReceivingQuantityReturnToSupplier",
    "ReceivingAmountReturnToSupplier",
    "VoidInventoryReceipt",
    "VoidReceivingQuantityReturnToSupplier",
    "VoidReceivingAmountReturnToSupplier",
})
# Closed allow-list from Coupa's receiving-transaction type contract.  A new
# tenant/custom type is not silently called consumption and omitted from a
# clean candidate total.
_NON_SUPPLIER_RECEIPT_TYPES = frozenset({
    "ReceivingQuantityConsumption", "ReceivingAmountConsumption",
    "ReceivingQuantityDisposal", "ReceivingAmountDisposal",
    "VoidReceivingQuantityConsumption", "VoidReceivingAmountConsumption",
    "VoidReceivingQuantityDisposal", "VoidReceivingAmountDisposal",
})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool) or not _text(value):
        raise ValueError(f"{label} is blank")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not numeric") from exc
    if not amount.is_finite():
        raise ValueError(f"{label} is not finite")
    # Coupa's widest invoice-line amount is decimal(46,20). Refuse values
    # outside that source contract before Decimal-to-JSON conversion can
    # overflow or manufacture Infinity.
    if len(amount.as_tuple().digits) > 46 or amount.adjusted() > 45:
        raise ValueError(f"{label} exceeds Coupa's supported decimal range")
    return amount


def _money(value: Decimal) -> float:
    return float(value)


def _date(value: Any, label: str) -> dt.date:
    raw = _text(value)
    if not raw:
        raise ValueError(f"{label} is blank")
    try:
        if len(raw) == 10:
            return dt.date.fromisoformat(raw)
        return dt.datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw).date()
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date") from exc


def _aware_datetime(value: Any, label: str) -> dt.datetime:
    """Parse a governed Coupa datetime with an explicit UTC offset."""
    raw = _text(value)
    if not raw or len(raw) <= 10:
        raise ValueError(f"{label} is not an ISO datetime")
    try:
        parsed = dt.datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} has no timezone offset")
    return parsed


def _datetime(value: Any, label: str) -> str:
    return _aware_datetime(value, label).isoformat()


def _business_date(value: Any, label: str,
                   zone: Optional[ZoneInfo]) -> dt.date:
    """Resolve one governed Coupa source timestamp on its company calendar.

    The event fields used by this control are datetimes, not bare dates.  An
    explicit source offset is essential near company-calendar midnight; a
    date-only or naive value cannot establish which Coupa business day owns
    the event and therefore fails closed.
    """
    parsed = _aware_datetime(value, label)
    return parsed.astimezone(zone or dt.timezone.utc).date()


def _path_value(row: dict, path: str) -> Any:
    """Read one explicitly configured Coupa JSON path, never a guessed one."""
    value: Any = row
    for part in (piece for piece in str(path or "").split(".") if piece):
        if not isinstance(value, dict):
            return None
        if part not in value:
            return None
        value = value[part]
    return value


class CoupaConnector(RestConnector):
    name = "coupa"
    ping_path = "/api/suppliers"

    def __init__(
        self,
        *args,
        rni_business_unit_path: str = "",
        rni_business_timezone: str = "",
        rni_receipt_business_unit_filter: str = "",
        rni_invoice_business_unit_filter: str = "",
        rni_business_unit_map: Optional[dict] = None,
        rni_eligible_invoice_statuses: Optional[Iterable[str]] = None,
        rni_receipt_statuses: Optional[Iterable[str]] = None,
        rni_invoice_scope_order_line_invariant: bool = False,
        rni_max_rows: int = _DEFAULT_RNI_ROW_CAP,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.rni_business_unit_path = str(rni_business_unit_path or "").strip()
        self.rni_business_timezone = _text(rni_business_timezone)
        self.rni_business_timezone_error = ""
        self._rni_business_zone: Optional[ZoneInfo] = None
        if self.rni_business_timezone:
            try:
                self._rni_business_zone = ZoneInfo(
                    self.rni_business_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                self.rni_business_timezone_error = (
                    "coupa.business_timezone is not a recognized IANA "
                    f"timezone: {self.rni_business_timezone!r}")
        self.rni_receipt_business_unit_filter = str(
            rni_receipt_business_unit_filter or "").strip()
        self.rni_invoice_business_unit_filter = str(
            rni_invoice_business_unit_filter or "").strip()
        self.rni_business_unit_map: dict[str, str] = {}
        self.rni_business_unit_map_error = ""
        if rni_business_unit_map is not None and not isinstance(
                rni_business_unit_map, dict):
            self.rni_business_unit_map_error = (
                "Coupa business_unit_map must be an object of PeopleSoft "
                "business-unit keys to scalar Coupa values")
        elif isinstance(rni_business_unit_map, dict):
            for key, value in rni_business_unit_map.items():
                if (isinstance(key, bool) or isinstance(value, bool)
                        or not isinstance(key, (str, int))
                        or not isinstance(value, (str, int))
                        or not _text(key) or not _text(value)):
                    self.rni_business_unit_map_error = (
                        "Coupa business_unit_map keys and values must be "
                        "nonblank scalar strings or integers")
                    break
                normalized_key = _text(key).upper()
                if (normalized_key in self.rni_business_unit_map
                        and self.rni_business_unit_map[normalized_key]
                        != _text(value)):
                    self.rni_business_unit_map_error = (
                        f"Coupa business_unit_map has conflicting entries for "
                        f"{normalized_key}")
                    break
                self.rni_business_unit_map[normalized_key] = _text(value)
        configured_invoice_statuses = frozenset(
            _text(status).lower() for status in (
                rni_eligible_invoice_statuses
                if rni_eligible_invoice_statuses is not None
                else _DEFAULT_ACTIVE_INVOICE_STATUSES
            ) if _text(status)
        )
        # Coupa's official header status contract uses ``approved``; paid is
        # a separate boolean.  Accepting "paid" in existing site config means
        # that boolean may corroborate an already-approved header, never that
        # a pending/draft header becomes eligible.
        self.rni_configured_invoice_eligibility = configured_invoice_statuses
        self.rni_eligible_invoice_statuses = frozenset(
            configured_invoice_statuses - {"paid"})
        self.rni_paid_flag_enabled = "paid" in configured_invoice_statuses
        self.rni_invoice_scope_order_line_invariant = (
            rni_invoice_scope_order_line_invariant
            if isinstance(rni_invoice_scope_order_line_invariant, bool)
            else None)
        self.rni_receipt_statuses = frozenset(
            _text(status).lower() for status in (
                rni_receipt_statuses
                if rni_receipt_statuses is not None
                else _DEFAULT_RECEIPT_STATUSES
            ) if _text(status)
        )
        try:
            configured_cap = (0 if isinstance(rni_max_rows, bool)
                              else int(rni_max_rows))
        except (TypeError, ValueError):
            configured_cap = 0
        self.rni_max_rows = min(configured_cap, _HARD_RNI_ROW_CAP)
        self.rni_max_rows_error = "" if configured_cap > 0 else (
            "Coupa rni_max_rows must be greater than zero")

    # ------------------------------------------------------------ raw reads
    def _invoices(self) -> list[dict]:
        rows = self.get("/api/invoices", {"limit": 200}) or []
        out = []
        for r in rows:
            out.append({
                "id": r.get("id"),
                "number": str(r.get("invoice-number")
                              or r.get("invoice_number") or ""),
                "supplier": str((r.get("supplier") or {}).get("name")
                               or r.get("supplier-name") or ""),
                "status": str(r.get("status") or ""),
                "total": float(r.get("total") or 0.0),
                "currency": str((r.get("currency") or {}).get("code")
                                or r.get("currency-code") or ""),
                "invoice_date": _iso(r.get("invoice-date")
                                     or r.get("invoice_date")),
                "pending_since": _iso(r.get("pending-since")
                                      or r.get("submitted-at")),
                "approver": str(((r.get("current-approval") or {})
                                 .get("approver") or {}).get("name") or ""),
            })
        return out

    def _po_lines(self) -> list[dict]:
        rows = self.get("/api/purchase_order_lines", {"limit": 200}) or []
        out = []
        for r in rows:
            out.append({
                "po": str(r.get("order-header-number") or r.get("po") or ""),
                "line": r.get("line-num") or r.get("line"),
                "supplier": str((r.get("supplier") or {}).get("name")
                               or r.get("supplier-name") or ""),
                "description": str(r.get("description") or ""),
                "received_amt": float(r.get("received-amount")
                                      or r.get("received_amt") or 0.0),
                "invoiced_amt": float(r.get("invoiced-amount")
                                      or r.get("invoiced_amt") or 0.0),
                "currency": str((r.get("currency") or {}).get("code")
                                or r.get("currency-code") or ""),
            })
        return out

    # Coupa Core API index calls are capped at 50 rows.  These helpers keep
    # paging and shape validation beside the connector instead of relying on
    # each financial view to remember them.
    @staticmethod
    def _collection(value: Any, endpoint: str) -> list[dict]:
        if isinstance(value, list):
            rows = value
        elif isinstance(value, dict):
            leaf = endpoint.rsplit("/", 1)[-1]
            keys = (leaf, leaf.replace("_", "-"), "data", "results")
            rows = next((value[key] for key in keys
                         if isinstance(value.get(key), list)), None)
            if rows is None:
                raise ConnectorError(
                    f"Coupa returned an unrecognized collection wrapper "
                    f"from {endpoint}; no completeness claim was made.")
        else:
            raise ConnectorError(
                f"Coupa returned {type(value).__name__}, not a JSON "
                f"collection, from {endpoint}.")
        if any(not isinstance(row, dict) for row in rows):
            raise ConnectorError(
                f"Coupa returned a non-object row from {endpoint}; the "
                "population cannot be validated.")
        return list(rows)

    def _paged_collection(self, endpoint: str, row_cap: int,
                          filters: Optional[dict] = None) -> tuple[list[dict], dict]:
        governed_filters = dict(filters or {})
        if self.mode == "fixtures":
            rows = self._collection(
                self.get(endpoint, {**governed_filters,
                                    "limit": _COUPA_PAGE_SIZE},
                         cache_ttl=0), endpoint)
            return rows[:row_cap], {
                "endpoint": endpoint,
                "complete": len(rows) <= row_cap,
                "truncated": len(rows) > row_cap,
                "rows_returned": min(len(rows), row_cap),
                "pages_read": 1,
                "page_size": _COUPA_PAGE_SIZE,
                "basis": "full recorded fixture endpoint",
            }

        rows: list[dict] = []
        seen: set[str] = set()
        offset = 0
        pages = 0
        while True:
            page = self._collection(self.get(
                endpoint, {**governed_filters, "limit": _COUPA_PAGE_SIZE,
                           "offset": offset}, cache_ttl=0),
                endpoint)
            pages += 1
            if len(page) > _COUPA_PAGE_SIZE:
                return rows, {
                    "endpoint": endpoint, "complete": False,
                    "truncated": False, "rows_returned": len(rows),
                    "pages_read": pages, "page_size": _COUPA_PAGE_SIZE,
                    "basis": "Coupa offset pagination",
                    "reason": (
                        f"Coupa returned {len(page)} rows after a "
                        f"{_COUPA_PAGE_SIZE}-row request; the server ignored "
                        "the governed page size"),
                }
            if not page:
                return rows, {
                    "endpoint": endpoint, "complete": True,
                    "truncated": False, "rows_returned": len(rows),
                    "pages_read": pages, "page_size": _COUPA_PAGE_SIZE,
                    "basis": "Coupa offset pagination exhausted",
                }
            for row in page:
                identifier = _text(row.get("id"))
                if not identifier:
                    return rows, {
                        "endpoint": endpoint, "complete": False,
                        "truncated": False, "rows_returned": len(rows),
                        "pages_read": pages, "page_size": _COUPA_PAGE_SIZE,
                        "basis": "Coupa offset pagination",
                        "reason": "a page row has no Coupa id",
                    }
                if identifier in seen:
                    return rows, {
                        "endpoint": endpoint, "complete": False,
                        "truncated": False, "rows_returned": len(rows),
                        "pages_read": pages, "page_size": _COUPA_PAGE_SIZE,
                        "basis": "Coupa offset pagination",
                        "reason": (
                            "a Coupa id repeated across pages; the collection "
                            "changed or offset was ignored"),
                    }
                seen.add(identifier)
                if len(rows) >= row_cap:
                    return rows, {
                        "endpoint": endpoint, "complete": False,
                        "truncated": True, "rows_returned": len(rows),
                        "pages_read": pages, "page_size": _COUPA_PAGE_SIZE,
                        "basis": "Coupa offset pagination",
                        "reason": f"the {row_cap:,}-row safety cap was reached",
                    }
                rows.append(row)
            if len(page) < _COUPA_PAGE_SIZE:
                return rows, {
                    "endpoint": endpoint, "complete": True,
                    "truncated": False, "rows_returned": len(rows),
                    "pages_read": pages, "page_size": _COUPA_PAGE_SIZE,
                    "basis": "Coupa offset pagination exhausted",
                }
            offset += len(page)

    def _rni_business_unit(self, row: dict) -> tuple[str, str]:
        if self.rni_business_unit_path:
            value = _path_value(row, self.rni_business_unit_path)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                return _text(value), self.rni_business_unit_path
            return "", self.rni_business_unit_path
        return "", "unconfigured"

    def _mapped_business_unit(self, requested: str) -> tuple[str, str, str]:
        """Return Coupa scope value, mapping basis, or a fail-closed reason."""
        if self.rni_business_unit_map_error:
            return "", "", self.rni_business_unit_map_error
        if not self.rni_business_unit_map:
            return requested, "explicit_identity", ""
        key = requested.upper()
        mapped = self.rni_business_unit_map.get(key, "")
        if not mapped:
            return "", "", (
                f"business_unit {requested!r} is not present in the governed "
                "Coupa business_unit_map")
        aliases = sorted(ps_bu for ps_bu, coupa_value
                         in self.rni_business_unit_map.items()
                         if coupa_value == mapped)
        if len(aliases) != 1:
            return "", "", (
                f"Coupa business-unit value {mapped!r} is mapped from more "
                "than one PeopleSoft business unit ("
                + ", ".join(aliases) + "); row security is ambiguous")
        return mapped, "configured_business_unit_map", ""

    @staticmethod
    def _currency(row: dict, parent: Optional[dict] = None) -> str:
        for source in (row, parent or {}):
            value = source.get("currency")
            code = (_text(value.get("code")) if isinstance(value, dict)
                    else _text(value))
            code = code or _text(source.get("currency-code")
                                 or source.get("currency_code"))
            if code:
                return code.upper()
        return ""

    @staticmethod
    def _order_line(row: dict) -> tuple[str, str, str, str, str, dict]:
        order_line = row.get("order-line") or row.get("order_line") or {}
        if not isinstance(order_line, dict):
            order_line = {}
        identifier = _text(
            row.get("order-line-id") or row.get("order_line_id")
            or order_line.get("id"))
        line = _text(
            row.get("order-line-num") or row.get("order_line_num")
            or order_line.get("line-num") or order_line.get("line_num"))
        po = _text(
            row.get("po-number") or row.get("po_number")
            or row.get("order-header-num") or row.get("order_header_num")
            or order_line.get("order-header-number")
            or order_line.get("order_header_number"))
        po_id = _text(
            row.get("order-header-id") or row.get("order_header_id")
            or order_line.get("order-header-id")
            or order_line.get("order_header_id"))
        line_type = _text(order_line.get("type")
                          or row.get("order-line-type")
                          or row.get("order_line_type"))
        return identifier, po, po_id, line, line_type, order_line

    @staticmethod
    def _invoice_lines(invoice: dict) -> Optional[list[dict]]:
        for key in ("invoice-lines", "invoice_lines"):
            if key in invoice:
                value = invoice[key]
                return value if isinstance(value, list) else None
        return None

    @staticmethod
    def _is_credit(invoice: dict, _line: dict) -> bool:
        flag = invoice.get("is-credit-note")
        if flag is None:
            flag = invoice.get("is_credit_note")
        if flag is None:
            flag = invoice.get("credit-note")
        if flag is None:
            flag = invoice.get("credit_note")
        if flag is not None:
            return flag is True
        labels = [_text(value).lower() for value in (
            invoice.get("document-type"), invoice.get("document_type"),
            invoice.get("type"))]
        normalized = {re.sub(r"[^a-z]", "", label) for label in labels}
        return bool(normalized & {"creditnote", "creditmemo",
                                  "invoicecreditnote"})

    @staticmethod
    def _allocation_issue(row: dict) -> str:
        """MVP supports only direct-account rows, never split allocations."""
        sources = [("row", row)]
        order_line = row.get("order-line") or row.get("order_line")
        if order_line is not None:
            if not isinstance(order_line, dict):
                return "embedded order-line is not an object"
            sources.append(("embedded order-line", order_line))
        for label, source in sources:
            for key in ("account-allocations", "account_allocations"):
                if key not in source or source[key] is None:
                    continue
                allocations = source[key]
                if not isinstance(allocations, list):
                    return f"{label} account-allocations is not an array"
                if allocations:
                    return (
                        f"{label} has account-allocations; proportional/split "
                        "business-unit attribution is unsupported")
        return ""

    # -------------------------------------------------------- curated views
    def invoices(self, status: str = "", supplier: str = "",
                 days: int = 30, max_rows: int = 50,
                 today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=max(int(days or 30), 1))
                 ).isoformat()
        want_status = str(status or "").strip().lower()
        want_sup = _norm_name(supplier)
        rows = [r for r in self._invoices()
                if r["invoice_date"] >= since
                and (not want_status or r["status"].lower() == want_status)
                and (not want_sup or want_sup in _norm_name(r["supplier"]))]
        rows.sort(key=lambda r: r["invoice_date"], reverse=True)
        totals: dict[str, float] = {}
        for r in rows:
            totals[r["currency"]] = round(
                totals.get(r["currency"], 0.0) + r["total"], 2)
        return {"source": "coupa", "mode": self.mode, "since": since,
                "count": len(rows), "totals_by_currency": totals,
                "invoices": rows[:max(int(max_rows or 50), 1)],
                "truncated": len(rows) > max_rows}

    def stuck_approvals(self, days_pending: int = 3,
                        today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        cutoff = (today - dt.timedelta(days=max(int(days_pending or 3), 1))
                  ).isoformat()
        rows = [r for r in self._invoices()
                if r["status"].lower() == "pending_approval"
                and r["pending_since"] and r["pending_since"] <= cutoff]
        for r in rows:
            r["days_pending"] = (today - dt.date.fromisoformat(
                r["pending_since"])).days
        rows.sort(key=lambda r: -r["days_pending"])
        return {"source": "coupa", "mode": self.mode,
                "days_pending_threshold": int(days_pending or 3),
                "count": len(rows), "stuck": rows,
                "note": ("Every invoice here has sat with its current "
                         "approver past the threshold — the close cannot "
                         "book what approval is still holding.")
                if rows else "No approvals stuck past the threshold."}

    def _legacy_rni_snapshot(self, threshold: Decimal,
                             reason: str,
                             collection_date: dt.date) -> dict:
        """Keep old PO counters visible only as explicitly incomplete context."""
        try:
            po_lines = self._po_lines()
        except Exception as exc:  # noqa: BLE001 - retain the first remedy too
            po_lines = []
            reason = f"{reason} Legacy PO-line snapshot also failed: {exc}"
        rows = []
        totals: dict[str, Decimal] = {}
        for row in po_lines:
            candidate = Decimal(str(round(
                row["received_amt"] - row["invoiced_amt"], 2)))
            if candidate <= threshold:
                continue
            entry = {**row, "rni_amt": _money(candidate),
                     "rni_candidate_amount": _money(candidate)}
            rows.append(entry)
            currency = row["currency"]
            totals[currency] = totals.get(currency, Decimal("0")) + candidate
        rows.sort(key=lambda row: -row["rni_amt"])
        money_totals = {key: _money(value) for key, value in totals.items()}
        return {
            "source": "coupa", "mode": self.mode,
            "status": "incomplete", "evaluated": False,
            "conclusion": "not_evaluated", "reason": reason,
            "scope": {
                "business_timezone": self.rni_business_timezone or None},
            "coverage": {
                "classification": "coupa_po_linked_event_review_only",
                "all_grni_complete": False,
                "point_in_time_complete": False,
                "collection_complete": False,
                "cutoff_classification": "current_date_only",
                "current_date": collection_date.isoformat(),
                "business_timezone": self.rni_business_timezone or None,
                "current_date_basis": (
                    "configured_coupa_company_timezone"),
                "eligible_receipt_statuses": sorted(
                    self.rni_receipt_statuses),
                "legacy_snapshot_only": True,
            },
            "snapshot": {
                "classification": "legacy_current_po_line_aggregate",
                "complete": False,
                "business_timezone": self.rni_business_timezone or None,
                "reason": (
                    "mutable PO-line received/invoiced counters have no "
                    "receipt-event, nested invoice-line, pagination, return/"
                    "void, or cutoff proof"),
            },
            "candidate_basis": {
                "classification": "review_candidate_only",
                "formula": "current PO-line received counter less invoiced counter",
                "does_not_prove": (
                    "a complete cutoff population, Coupa export, ERP booking, "
                    "posted journal, or ending GRNI liability"),
            },
            "population": {"complete": False, "truncated": None,
                           "candidate_count": None},
            "count": len(rows), "totals_by_currency": money_totals,
            "rni_totals_by_currency": money_totals, "lines": rows,
            "booked_status": "not_evaluated",
            "export_evidence": {
                "evaluated": False,
                "meaning": "Coupa export only; not ERP booking or GL posting",
            },
            "note": (
                "Legacy current aggregate shown only as incomplete context. "
                "Do not use it as a period-end accrual or clean-RNI result."),
        }

    def received_not_invoiced(
        self,
        min_amount: float = 0.0,
        business_unit: str = "",
        as_of_date: str = "",
        max_rows: Optional[int] = None,
        display_rows: int = _DEFAULT_RNI_DISPLAY_ROWS,
        today: Optional[dt.date] = None,
    ) -> dict:
        """Coupa PO-line received-not-invoiced review candidates.

        The evaluated path reads every receiving-transaction and invoice page,
        nets linked return/void events, then joins nested invoice lines on the
        exact Coupa order-line ID.  That is PO-line aggregate precision: absent
        ``matching_allocations`` it never labels one individual receipt as
        uninvoiced.  Coupa export evidence remains separate from ERP booking
        and GL posting evidence.

        Standard invoice payloads expose current mutable status, not status
        history, so historical cutoffs fail closed.  Old fixtures without the
        event endpoint retain the former PO-line-counter view only as an
        explicit incomplete snapshot.

        ``max_rows`` may only lower the deployment's configured scan cap;
        it cannot raise that governed memory/completeness safeguard.
        ``display_rows`` is an internal presentation/export limit independent
        of the source scan and is always bounded by its own hard ceiling.
        """
        if today is not None and (not isinstance(today, dt.date)
                                  or isinstance(today, dt.datetime)):
            raise ConnectorError("today must be a date")
        current_day = today or (
            dt.datetime.now(self._rni_business_zone).date()
            if self._rni_business_zone is not None
            else dt.date.today())
        if self.rni_max_rows_error:
            raise ConnectorError(self.rni_max_rows_error)
        try:
            cutoff = (_date(as_of_date, "as_of_date")
                      if _text(as_of_date) else current_day)
            threshold = _decimal(min_amount, "min_amount")
            requested_cap = int(self.rni_max_rows if max_rows is None
                                else max_rows)
            requested_display_cap = int(display_rows)
        except (TypeError, ValueError) as exc:
            raise ConnectorError(str(exc)) from exc
        if threshold < 0:
            raise ConnectorError("min_amount cannot be negative")
        if isinstance(max_rows, bool) or requested_cap < 1:
            raise ConnectorError("max_rows must be greater than zero")
        if isinstance(display_rows, bool) or requested_display_cap < 1:
            raise ConnectorError("display_rows must be greater than zero")
        row_cap = min(
            requested_cap, self.rni_max_rows, _HARD_RNI_ROW_CAP)
        display_cap = min(requested_display_cap, _HARD_RNI_DISPLAY_ROWS)
        bu = _text(business_unit)

        base = {
            "source": "coupa", "mode": self.mode,
            "status": "incomplete", "evaluated": False,
            "conclusion": "not_evaluated", "business_unit": bu or None,
            "as_of_date": cutoff.isoformat(),
            "min_amount": _money(threshold),
            "scope": {
                "business_unit": bu or None,
                "business_timezone": self.rni_business_timezone or None},
            "coverage": {
                "classification": "coupa_po_linked_event_review_only",
                "all_grni_complete": False,
                "point_in_time_complete": False,
                "cutoff_classification": "current_date_only",
                "current_date": current_day.isoformat(),
                "business_timezone": self.rni_business_timezone or None,
                "current_date_basis": (
                    "configured_coupa_company_timezone"),
                "eligible_receipt_statuses": sorted(
                    self.rni_receipt_statuses),
                "matching_precision": "order_line_aggregate",
                "invoice_scope_order_line_invariant": (
                    self.rni_invoice_scope_order_line_invariant is True),
                "included": (
                    "credential-visible selected-business-unit receiving "
                    "events and exact order-line invoice lines"),
                "excluded": [
                    "receipt-level invoice attribution without Coupa matching_allocations",
                    "non-PO procurement activity",
                    "split-account-allocation rows",
                    "ERP accounting entries and posted journals",
                    "ending or all-source GRNI liability",
                ],
            },
            "candidate_basis": {
                "classification": "review_candidate_only",
                "formula": (
                    "net dated Coupa receipt activity, including linked return/"
                    "void events, less eligible invoice-line coverage on the "
                    "exact Coupa order-line ID; quantity coverage is revalued "
                    "at the proven receipt price"),
                "eligible_invoice_statuses": sorted(
                    self.rni_eligible_invoice_statuses),
                "configured_invoice_eligibility": sorted(
                    self.rni_configured_invoice_eligibility),
                "eligible_receipt_statuses": sorted(
                    self.rni_receipt_statuses),
                "receipt_status_basis": (
                    "only explicitly configured Coupa receiving-transaction "
                    "statuses are included; any other observed status makes "
                    "the control incomplete"),
                "invoice_status_basis": (
                    "eligible current invoice-header status only. Coupa paid "
                    "is validated as a separate boolean and never rescues a "
                    "pending, draft, voided, rejected, or unknown header"),
                "threshold_basis": (
                    "display/select candidates where amount is strictly "
                    "greater than min_amount; all positive candidate counts "
                    "and totals are also disclosed before that threshold"),
                "display_order_basis": (
                    "currency code first, then descending candidate amount "
                    "within that currency; nominal amounts in different "
                    "currencies are never ranked against each other"),
                "amount_line_basis": "receipt total less eligible invoice-line total",
                "quantity_line_basis": (
                    "remaining received quantity valued at the single proven "
                    "receipt/order-line price; Coupa receipt face total and "
                    "its face-to-valuation difference are disclosed "
                    "separately, and invoice price variance is not used to "
                    "change the review candidate"),
                "compatibility_aliases": {
                    "net_receipt_amount": (
                        "net_receipt_value_at_receipt_valuation; for quantity "
                        "lines this is net quantity times the single proven "
                        "receipt price, not the Coupa source face total"),
                    "eligible_invoice_amount": (
                        "eligible invoice coverage at candidate/receipt "
                        "valuation, not necessarily invoice face amount")},
                "does_not_prove": (
                    "Coupa export acceptance, ERP booking, posted GL activity, "
                    "or an ending GRNI liability"),
            },
            "snapshot": {
                "classification": "current_api_collection",
                "complete": False, "atomic": False,
                "business_timezone": self.rni_business_timezone or None,
                "reason": (
                    "receipt and invoice endpoints are collected sequentially; "
                    "pagination completeness is tested but no cross-endpoint "
                    "transaction token exists"),
            },
            "population": {
                "complete": False, "truncated": False,
                "requested_row_cap": requested_cap,
                "configured_row_cap": self.rni_max_rows,
                "effective_row_cap": row_cap,
                "hard_row_cap": _HARD_RNI_ROW_CAP,
                "requested_display_row_cap": requested_display_cap,
                "display_row_cap": display_cap,
                "hard_display_row_cap": _HARD_RNI_DISPLAY_ROWS,
                "candidate_count": None,
                "totals_complete": False,
            },
            "totals_by_currency": None, "rni_totals_by_currency": None,
            "lines": [],
            "exceptions": {
                "invoice_present_not_eligible": [], "over_invoiced": [],
                "net_credit_invoice_activity": [],
                "excluded_receiving_types": [],
            },
            "booked_status": "not_evaluated",
            "booked_basis": (
                "Coupa operational and export fields are not ERP accounting "
                "or posted-journal evidence."),
            "export_evidence": {
                "evaluated": False,
                "meaning": "Coupa export only; not ERP booking or GL posting",
            },
        }

        def incomplete(reason: str, **extra: Any) -> dict:
            return {**base, "reason": reason, **extra}

        if self.mode == "live" and (
                not self.rni_business_timezone
                or self.rni_business_timezone_error):
            return incomplete(
                self.rni_business_timezone_error
                or "coupa.business_timezone is required for live RNI so "
                   "current-day receipt and invoice cutoffs use the Coupa "
                   "company calendar; no population was scanned.")

        # The standard invoice endpoint exposes mutable current approval
        # state.  Refuse a non-current cutoff before fixture fallback or
        # tenant-scope configuration checks can obscure that fundamental
        # evidence limitation.
        if cutoff > current_day:
            return incomplete(
                f"as_of_date {cutoff.isoformat()} is in the future; no "
                "candidate population was evaluated.")
        if cutoff < current_day:
            return incomplete(
                f"{cutoff.isoformat()} is historical, but the standard Coupa "
                "invoice API exposes current mutable status rather than status "
                "history. Dated receipt and invoice-line creation events cannot "
                "prove which invoices were approved, voided, or abandoned at "
                "that cutoff; no point-in-time total is reported.")

        if not bu:
            if self.mode == "fixtures":
                return self._legacy_rni_snapshot(
                    threshold,
                    "The legacy recorded fixture has no governed business-"
                    "unit scope for event-level RNI evaluation.",
                    current_day)
            return incomplete(
                "business_unit is required. Configure an explicit Coupa "
                "business-unit field/path and pass the caller-authorized unit; "
                "the connector never scans every unit as evidence.")
        if not self.rni_business_unit_path:
            if self.mode == "fixtures":
                return self._legacy_rni_snapshot(
                    threshold,
                    "COUPA_RNI_BUSINESS_UNIT_PATH is not configured in the "
                    "legacy recorded fixture.", current_day)
            return incomplete(
                "COUPA_RNI_BUSINESS_UNIT_PATH is required. Coupa account "
                "segments and content-group fields are tenant-specific, so "
                "the connector will not guess which field means business unit.")
        coupa_bu, mapping_basis, mapping_error = self._mapped_business_unit(bu)
        if mapping_error:
            return incomplete(
                mapping_error + "; no Coupa population was scanned.",
                scope={"business_unit": bu,
                       "business_timezone": self.rni_business_timezone,
                       "coupa_business_unit": None,
                       "mapping_basis": "incomplete"})
        invalid_eligible_statuses = {
            status for status in self.rni_configured_invoice_eligibility
            if status != "paid"
            and not re.fullmatch(r"[a-z0-9_-]{1,64}", status)}
        if invalid_eligible_statuses:
            return incomplete(
                "Configured Coupa invoice eligibility contains unsupported "
                "header status value(s): "
                + ", ".join(sorted(invalid_eligible_statuses)) + ".")
        if (self.rni_paid_flag_enabled
                and "approved" not in self.rni_eligible_invoice_statuses):
            return incomplete(
                "Configured Coupa invoice eligibility contains paid without "
                "approved. Coupa paid is a boolean, not a header status, and "
                "cannot define eligibility by itself; configure approved "
                "explicitly or remove paid.")
        delivered_ineligible_statuses = (
            self.rni_configured_invoice_eligibility
            & (_KNOWN_INVOICE_STATUSES - {"approved"}))
        if delivered_ineligible_statuses:
            return incomplete(
                "Configured Coupa invoice eligibility includes delivered "
                "header status value(s) whose documented meaning is not an "
                "approved outbound invoice: "
                + ", ".join(sorted(delivered_ineligible_statuses))
                + ". Only approved, the separately validated paid boolean "
                "contract, or an explicitly governed tenant-specific "
                "approved-equivalent status may reduce a candidate.")
        invalid_receipt_statuses = {
            status for status in self.rni_receipt_statuses
            if not re.fullmatch(r"[a-z0-9_-]{1,64}", status)}
        if not self.rni_receipt_statuses or invalid_receipt_statuses:
            detail = (", ".join(sorted(invalid_receipt_statuses))
                      or "no nonblank status")
            return incomplete(
                "Configured Coupa receipt eligibility contains unsupported "
                f"status value(s): {detail}.")
        if self.mode == "live" and (
                not self.rni_receipt_business_unit_filter
                or not self.rni_invoice_business_unit_filter):
            return incomplete(
                "Live RNI requires governed Coupa server-side filters for both "
                "receiving transactions and invoices. Configure "
                "coupa.receipt_business_unit_filter and "
                "coupa.invoice_business_unit_filter with exact tenant-tested "
                "query keys; client-side scope "
                "rechecking still runs after paging.")
        if self.mode == "live" and (
                self.rni_invoice_scope_order_line_invariant is not True):
            return incomplete(
                "Live RNI requires coupa.invoice_scope_order_line_invariant "
                "to be true after tenant testing proves the configured invoice "
                "filter returns every header whose line references an in-scope "
                "PO/order-line, including after distribution-account changes. "
                "A current invoice-account business-unit filter alone cannot "
                "prove a complete invoice population.")
        filter_pattern = re.compile(
            r"[A-Za-z0-9_-]+(?:\[[A-Za-z0-9_-]+\])*")
        for label, value in (
            ("receipt", self.rni_receipt_business_unit_filter),
            ("invoice", self.rni_invoice_business_unit_filter),
        ):
            if value and not filter_pattern.fullmatch(value):
                return incomplete(
                    f"The configured Coupa {label} business-unit filter "
                    f"{value!r} is not a safe scalar query key.")
        try:
            receipt_rows, receipt_page = self._paged_collection(
                "/api/receiving_transactions", row_cap,
                ({self.rni_receipt_business_unit_filter: coupa_bu}
                 if self.rni_receipt_business_unit_filter else None))
        except ConnectorError as exc:
            if self.mode == "fixtures":
                return self._legacy_rni_snapshot(
                    threshold,
                    "The recorded fixture has no complete receiving-event "
                    f"population: {exc}", current_day)
            return incomplete(str(exc))
        if not receipt_page.get("complete"):
            return incomplete(
                "Coupa receiving-transaction pagination is incomplete: "
                + _text(receipt_page.get("reason") or "unknown paging gap"),
                pagination={"receipts": receipt_page},
                population={**base["population"], "truncated": bool(
                    receipt_page.get("truncated"))})
        try:
            invoice_rows, invoice_page = self._paged_collection(
                "/api/invoices", row_cap,
                ({self.rni_invoice_business_unit_filter: coupa_bu}
                 if self.rni_invoice_business_unit_filter else None))
        except ConnectorError as exc:
            return incomplete(
                f"The Coupa invoice-line population could not be collected: "
                f"{exc}", pagination={"receipts": receipt_page})
        pagination = {"receipts": receipt_page, "invoices": invoice_page}
        if not invoice_page.get("complete"):
            return incomplete(
                "Coupa invoice pagination is incomplete: "
                + _text(invoice_page.get("reason") or "unknown paging gap"),
                pagination=pagination,
                population={**base["population"], "truncated": bool(
                    invoice_page.get("truncated"))})
        if not receipt_rows:
            return {
                **base, "status": "no_data", "pagination": pagination,
                "reason": (
                    f"No receiving transactions were visible as of "
                    f"{cutoff.isoformat()}. This is not a zero-RNI or "
                    "completeness pass."),
                "scope": {"business_unit": bu,
                          "business_timezone": self.rni_business_timezone,
                          "coupa_business_unit": coupa_bu,
                          "mapping_basis": mapping_basis,
                          "business_unit_path": self.rni_business_unit_path},
                "population": {**base["population"], "complete": True,
                               "receipt_rows_read": 0,
                               "invoice_headers_read": len(invoice_rows)},
            }

        # Parse the complete receiving-event population before calculating any
        # amount. A single malformed row makes every downstream total partial.
        all_receipt_ids: set[str] = set()
        events: list[dict] = []
        excluded_types: list[dict] = []
        missing_scope: list[str] = []
        outside_receipt_scope = 0
        try:
            for raw in receipt_rows:
                receipt_id = _text(raw.get("id"))
                if not receipt_id:
                    raise ValueError("a receiving transaction has no Coupa id")
                if receipt_id in all_receipt_ids:
                    raise ValueError(
                        f"receiving transaction id {receipt_id} is duplicated")
                all_receipt_ids.add(receipt_id)
                row_bu, scope_path = self._rni_business_unit(raw)
                if not row_bu:
                    missing_scope.append(receipt_id)
                    continue
                if row_bu != coupa_bu:
                    outside_receipt_scope += 1
                    continue
                event_type = _text(raw.get("type"))
                status = _text(raw.get("status")).lower()
                if not event_type:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has blank type")
                if not status:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has blank status")
                if status not in self.rni_receipt_statuses:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has unsupported "
                        f"status {status!r}; configured statuses are "
                        f"{', '.join(sorted(self.rni_receipt_statuses))}")
                event_day = _business_date(
                    raw.get("transaction-date")
                    or raw.get("transaction_date"),
                    f"receiving transaction {receipt_id} transaction-date",
                    self._rni_business_zone)
                created_day = _business_date(
                    raw.get("created-at") or raw.get("created_at"),
                    f"receiving transaction {receipt_id} created-at",
                    self._rni_business_zone)
                if event_day > cutoff or created_day > cutoff:
                    continue
                if event_type in _NON_SUPPLIER_RECEIPT_TYPES:
                    excluded_types.append({
                        "receipt_transaction_id": receipt_id,
                        "type": event_type,
                        "reason": (
                            "Coupa's documented consumption/disposal type is "
                            "not supplier receipt value")})
                    continue
                if event_type not in _RNI_RECEIPT_TYPES:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has unknown type "
                        f"{event_type!r}; an unrecognized movement cannot be "
                        "silently excluded from a clean candidate population")
                allocation_issue = self._allocation_issue(raw)
                if allocation_issue:
                    raise ValueError(
                        f"receiving transaction {receipt_id} {allocation_issue}; "
                        "the configured business-unit path cannot attribute it "
                        "safely")
                (order_line_id, po, po_id, line_num, line_type,
                 order_line) = self._order_line(raw)
                if not order_line_id:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has no exact "
                        "order-line id")
                line_type_low = line_type.lower()
                if "quantity" in line_type_low:
                    amount_basis = "quantity"
                elif "amount" in line_type_low or "service" in line_type_low:
                    amount_basis = "amount"
                else:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has unknown order-"
                        f"line type {line_type!r}; quantity and amount lines "
                        "cannot share arithmetic")
                if ("Quantity" in event_type and amount_basis != "quantity"):
                    raise ValueError(
                        f"receiving transaction {receipt_id} type "
                        f"{event_type!r} conflicts with amount order-line type")
                if ("Amount" in event_type and amount_basis != "amount"):
                    raise ValueError(
                        f"receiving transaction {receipt_id} type "
                        f"{event_type!r} conflicts with quantity order-line type")
                currency = self._currency(raw, order_line)
                if not currency:
                    raise ValueError(
                        f"receiving transaction {receipt_id} has blank currency")
                total = _decimal(raw.get("total"),
                                 f"receiving transaction {receipt_id} total")
                is_void = event_type.startswith("Void")
                is_return = "ReturnToSupplier" in event_type
                is_reversal = is_void or is_return
                quantity = price = None
                if amount_basis == "quantity":
                    quantity = _decimal(raw.get("quantity"),
                                        f"receiving transaction {receipt_id} "
                                        "quantity")
                    price = _decimal(raw.get("price")
                                     if raw.get("price") is not None
                                     else order_line.get("price"),
                                     f"receiving transaction {receipt_id} price")
                    if price < 0:
                        raise ValueError(
                            f"receiving transaction {receipt_id} has negative "
                            "price")
                    if quantity < 0 and not is_reversal:
                        raise ValueError(
                            f"receiving transaction {receipt_id} has negative "
                            "quantity without a return/void type")
                    if abs(abs(total) - abs(quantity) * price) > Decimal("0.02"):
                        raise ValueError(
                            f"receiving transaction {receipt_id} total does not "
                            "agree in magnitude with quantity times price")
                exported = raw.get("exported")
                if exported is not None and not isinstance(exported, bool):
                    raise ValueError(
                        f"receiving transaction {receipt_id} exported is not "
                        "boolean")
                if event_type == "InventoryReceipt":
                    if total < 0:
                        raise ValueError(
                            f"InventoryReceipt {receipt_id} has negative total; "
                            "no return/void type proves the sign")
                    amount_effect = total
                    quantity_effect = quantity
                elif is_void and is_return:
                    amount_effect = abs(total)
                    quantity_effect = abs(quantity) if quantity is not None else None
                else:
                    amount_effect = -abs(total)
                    quantity_effect = (-abs(quantity)
                                       if quantity is not None else None)
                original_id = _text(
                    raw.get("original_transaction_id")
                    or raw.get("original-transaction-id")
                    or ((raw.get("original_transaction") or {}).get("id")
                        if isinstance(raw.get("original_transaction"), dict)
                        else ""))
                if (is_void or is_return) and not original_id:
                    raise ValueError(
                        f"{event_type} {receipt_id} has no "
                        "original_transaction_id")
                voided_value = raw.get("voided_value")
                if voided_value is None:
                    voided_value = raw.get("voided-value")
                if voided_value not in (None, ""):
                    _decimal(voided_value,
                             f"receiving transaction {receipt_id} voided_value")
                last_exported_raw = _text(
                    raw.get("last-exported-at")
                    or raw.get("last_exported_at"))
                last_exported_at = ""
                last_exported_at_valid = True
                if last_exported_raw:
                    try:
                        last_exported_at = _datetime(
                            last_exported_raw,
                            f"receiving transaction {receipt_id} "
                            "last-exported-at")
                    except ValueError:
                        # Export timing is a separate evidence leg from RNI
                        # arithmetic. Redact the malformed source value and
                        # make export evidence incomplete without discarding
                        # an otherwise valid candidate population.
                        last_exported_at_valid = False
                supplier_value = order_line.get("supplier") or {}
                events.append({
                    "id": receipt_id, "business_unit": row_bu,
                    "scope_path": scope_path, "type": event_type,
                    "status": status, "transaction_date": event_day,
                    "order_line_id": order_line_id, "po": po,
                    "po_id": po_id, "line": line_num,
                    "line_type": line_type, "amount_basis": amount_basis,
                    "currency": currency, "amount_effect": amount_effect,
                    "quantity_effect": quantity_effect, "price": price,
                    "original_id": original_id,
                    "voided_value": voided_value, "exported": exported,
                    "last_exported_at": last_exported_at,
                    "last_exported_at_valid": last_exported_at_valid,
                    "supplier": (_text(supplier_value.get("name"))
                                 if isinstance(supplier_value, dict) else ""),
                })
        except (TypeError, ValueError) as exc:
            return incomplete(
                f"Coupa receiving-transaction evidence is incomplete: {exc}.",
                pagination=pagination)
        if missing_scope:
            return incomplete(
                f"{len(missing_scope)} receiving transaction(s) have no "
                "verifiable business-unit value at the configured path; row "
                "security and completeness cannot be established.",
                pagination=pagination,
                scope={"business_unit": bu,
                       "business_timezone": self.rni_business_timezone,
                       "coupa_business_unit": coupa_bu,
                       "mapping_basis": mapping_basis,
                       "mapping": self.rni_business_unit_path
                       or "unconfigured",
                       "missing_receipt_scope_count": len(missing_scope)})
        if self.mode == "live" and outside_receipt_scope:
            return incomplete(
                "The governed Coupa receiving-transaction server filter "
                f"returned {outside_receipt_scope} row(s) from another "
                "business unit. The filter may have been ignored or "
                "misconfigured; no cross-unit rows or totals are returned.",
                pagination=pagination,
                scope={"business_unit": bu,
                       "business_timezone": self.rni_business_timezone,
                       "coupa_business_unit": coupa_bu,
                       "mapping_basis": mapping_basis,
                       "business_unit_path": self.rni_business_unit_path})
        if not events:
            return {
                **base, "status": "no_data", "pagination": pagination,
                "reason": (
                    "No supported supplier receiving events were found in the "
                    "governed Coupa business-unit population as of "
                    f"{cutoff.isoformat()}. This is not a zero-RNI or "
                    "completeness pass."),
                "scope": {"business_unit": bu,
                          "business_timezone": self.rni_business_timezone,
                          "coupa_business_unit": coupa_bu,
                          "mapping_basis": mapping_basis,
                          "business_unit_path": self.rni_business_unit_path},
                "population": {
                    **base["population"], "complete": True,
                    "receipt_rows_read": len(receipt_rows),
                    "receipt_events_in_scope": 0,
                    "receipt_rows_outside_business_unit": outside_receipt_scope,
                    "excluded_receiving_types": len(excluded_types),
                    "invoice_headers_read": len(invoice_rows),
                },
                "exceptions": {
                    **base["exceptions"],
                    "excluded_receiving_types": excluded_types[
                        :display_cap],
                },
            }

        # Validate every return/void against its original. ``voided_value`` is
        # only a cross-check against linked void rows; applying it again would
        # double the reversal event.
        by_id = {event["id"]: event for event in events}
        children: dict[str, list[dict]] = {}
        expected_parents = {
            "ReceivingQuantityReturnToSupplier": {"InventoryReceipt"},
            "ReceivingAmountReturnToSupplier": {"InventoryReceipt"},
            "VoidInventoryReceipt": {"InventoryReceipt"},
            "VoidReceivingQuantityReturnToSupplier": {
                "ReceivingQuantityReturnToSupplier"},
            "VoidReceivingAmountReturnToSupplier": {
                "ReceivingAmountReturnToSupplier"},
        }
        try:
            for event in events:
                if not event["original_id"]:
                    continue
                if event["original_id"] == event["id"]:
                    raise ValueError(
                        f"{event['type']} {event['id']} references itself")
                original = by_id.get(event["original_id"])
                if original is None:
                    raise ValueError(
                        f"{event['type']} {event['id']} references original "
                        f"{event['original_id']} outside the selected dated "
                        "population")
                if (original["order_line_id"] != event["order_line_id"]
                        or original["currency"] != event["currency"]
                        or original["business_unit"] != event["business_unit"]
                        or original["amount_basis"] != event["amount_basis"]):
                    raise ValueError(
                        f"return/void {event['id']} does not match its original "
                        "transaction's line, currency, scope, and line type")
                allowed = expected_parents.get(event["type"], set())
                if original["type"] not in allowed:
                    raise ValueError(
                        f"{event['type']} {event['id']} cannot reverse parent "
                        f"type {original['type']!r}")
                children.setdefault(event["original_id"], []).append(event)
            for event in events:
                direct_children = children.get(event["id"], [])
                unit = ("quantity_effect" if event["amount_basis"] == "quantity"
                        else "amount_effect")
                parent_magnitude = abs(event[unit])
                child_magnitude = sum(abs(child[unit])
                                      for child in direct_children)
                if child_magnitude - parent_magnitude > _AMOUNT_TOLERANCE:
                    raise ValueError(
                        f"linked return/void activity for receiving transaction "
                        f"{event['id']} exceeds its original "
                        f"{event['amount_basis']} magnitude")
                if event["voided_value"] in (None, ""):
                    continue
                value = _decimal(event["voided_value"], "voided_value")
                if value < 0:
                    raise ValueError(
                        f"receiving transaction {event['id']} has negative "
                        "voided_value")
                linked_void_magnitude = sum(
                    abs(child[unit]) for child in direct_children
                    if child["type"].startswith("Void"))
                if abs(value - linked_void_magnitude) > _AMOUNT_TOLERANCE:
                    raise ValueError(
                        f"receiving transaction {event['id']} voided_value "
                        f"{value} does not equal linked void "
                        f"{event['amount_basis']} magnitude "
                        f"{linked_void_magnitude}")
        except ValueError as exc:
            return incomplete(
                f"Coupa return/void evidence is incomplete: {exc}.",
                pagination=pagination)

        groups: dict[tuple[str, str], dict] = {}
        line_currencies: dict[str, set[str]] = {}
        for event in events:
            line_currencies.setdefault(event["order_line_id"], set()).add(
                event["currency"])
            key = (event["order_line_id"], event["currency"])
            group = groups.setdefault(key, {
                "business_unit": bu,
                "coupa_business_unit": coupa_bu,
                "order_line_id": event["order_line_id"],
                "po": event["po"], "po_id": event["po_id"],
                "line": event["line"], "line_type": event["line_type"],
                "amount_basis": event["amount_basis"],
                "supplier": event["supplier"], "currency": event["currency"],
                "receipt_amount": Decimal("0"),
                "receipt_quantity": Decimal("0"), "prices": set(),
                "receipt_ids": [], "receipt_dates": [],
                "return_or_void_count": 0,
                "eligible_invoice_amount": Decimal("0"),
                "eligible_invoice_quantity": Decimal("0"),
                "eligible_invoice_face_total": Decimal("0"),
                "eligible_invoice_lines": [], "ineligible_invoice_lines": [],
                "eligible_invoice_line_count": 0,
                "ineligible_invoice_line_count": 0,
            })
            if group["amount_basis"] != event["amount_basis"]:
                return incomplete(
                    f"Order line {event['order_line_id']} mixes quantity and "
                    "amount receipt types; no common arithmetic is safe.",
                    pagination=pagination)
            for label in ("po", "po_id", "line", "line_type"):
                if (group[label] and event[label]
                        and group[label] != event[label]):
                    return incomplete(
                        f"Order line {event['order_line_id']} has conflicting "
                        f"{label} metadata across receiving events; no common "
                        "population identity is safe.", pagination=pagination)
            group["receipt_amount"] += event["amount_effect"]
            if event["quantity_effect"] is not None:
                group["receipt_quantity"] += event["quantity_effect"]
            if event["price"] is not None:
                group["prices"].add(event["price"])
            group["receipt_ids"].append(event["id"])
            group["receipt_dates"].append(event["transaction_date"])
            if event["type"] != "InventoryReceipt":
                group["return_or_void_count"] += 1
            for label in ("po", "po_id", "line", "supplier"):
                group[label] = group[label] or event[label]
        mixed_currency_lines = [line for line, currencies in line_currencies.items()
                                if len(currencies) > 1]
        if mixed_currency_lines:
            return incomplete(
                "A Coupa order line appears in multiple receipt currencies: "
                + ", ".join(mixed_currency_lines[:10])
                + ". Currency populations are not netted.",
                pagination=pagination)
        for group in groups.values():
            if group["receipt_amount"] < 0 or group["receipt_quantity"] < 0:
                return incomplete(
                    f"Order line {group['order_line_id']} has negative net "
                    "receipt activity after returns/voids; the chain is not a "
                    "safe candidate basis.", pagination=pagination)
            if (group["amount_basis"] == "quantity"
                    and len(group["prices"]) != 1):
                return incomplete(
                    f"Quantity order line {group['order_line_id']} does not "
                    "have one proven receipt price; remaining quantity cannot "
                    "be valued safely.", pagination=pagination)

        # Nested invoice lines provide exact order-line attribution. They do
        # not provide receipt-level attribution without matching_allocations.
        invoice_header_ids: set[str] = set()
        invoice_line_ids: set[str] = set()
        missing_invoice_scope: list[str] = []
        invoice_lines_read = non_po_lines = outside_invoice_scope = 0
        invoice_headers_without_target_scope = 0
        try:
            for invoice in invoice_rows:
                # Establish nested row scope before reading or validating any
                # header values. A live child-association filter may return a
                # foreign-only header when ignored/misconfigured; surfacing
                # its id, status, or other fields would leak cross-BU data.
                lines = self._invoice_lines(invoice)
                if (lines is None
                        or any(not isinstance(line, dict) for line in lines)):
                    if self.mode == "live":
                        invoice_headers_without_target_scope += 1
                    continue
                if invoice_lines_read + len(lines) > row_cap:
                    return incomplete(
                        "Coupa nested invoice-line population exceeds the "
                        f"{row_cap:,}-row safety cap; no partial totals are "
                        "reported.", pagination=pagination,
                        population={**base["population"], "truncated": True})
                scoped_lines: list[tuple[dict, str, str, str, str, str]] = []
                for line in lines:
                    invoice_lines_read += 1
                    line_id = _text(line.get("id"))
                    (order_line_id, po, _, line_num, _,
                     _) = self._order_line(line)
                    line_bu, scope_path = self._rni_business_unit(line)
                    if not line_bu:
                        if order_line_id or po:
                            missing_invoice_scope.append(line_id or "redacted")
                        else:
                            non_po_lines += 1
                        continue
                    if line_bu != coupa_bu:
                        outside_invoice_scope += 1
                        if order_line_id in line_currencies:
                            raise ValueError(
                                "an invoice line outside the selected business "
                                "unit references an in-scope receipt order-line "
                                "id; ownership/allocation is inconsistent")
                        continue
                    scoped_lines.append((
                        line, line_id, order_line_id, po, line_num,
                        scope_path))
                if not scoped_lines:
                    if self.mode == "live":
                        invoice_headers_without_target_scope += 1
                    continue

                invoice_id = _text(invoice.get("id"))
                if not invoice_id:
                    raise ValueError("an invoice header has no Coupa id")
                if invoice_id in invoice_header_ids:
                    raise ValueError(f"invoice id {invoice_id} is duplicated")
                invoice_header_ids.add(invoice_id)
                header_status = _text(invoice.get("status")).lower()
                allowed_header_statuses = (
                    _KNOWN_INVOICE_STATUSES
                    | self.rni_eligible_invoice_statuses)
                if (not header_status
                        or header_status not in allowed_header_statuses):
                    raise ValueError(
                        f"invoice {invoice_id} has blank or unsupported status "
                        f"{header_status!r}")
                canceled = invoice.get("canceled")
                if type(canceled) is not bool:
                    raise ValueError(
                        f"invoice {invoice_id} has no governed boolean "
                        "canceled state")
                paid = invoice.get("paid")
                if (paid is not None and type(paid) is not bool):
                    raise ValueError(
                        f"invoice {invoice_id} paid is not boolean")
                if self.rni_paid_flag_enabled and type(paid) is not bool:
                    raise ValueError(
                        f"invoice {invoice_id} has no governed boolean paid "
                        "state required by configured invoice eligibility")
                for credit_key in (
                        "is-credit-note", "is_credit_note",
                        "credit-note", "credit_note"):
                    if (credit_key in invoice
                            and not isinstance(invoice[credit_key], bool)):
                        raise ValueError(
                            f"invoice {invoice_id} {credit_key} is not boolean")
                for (line, line_id, order_line_id, po, line_num,
                     scope_path) in scoped_lines:
                    if not line_id:
                        raise ValueError(
                            f"invoice {invoice_id} has a line without a Coupa id")
                    if not order_line_id:
                        if po:
                            raise ValueError(
                                f"invoice line {line_id} names PO {po} but has "
                                "no exact order-line-id")
                        non_po_lines += 1
                        continue
                    if line_id in invoice_line_ids:
                        raise ValueError(f"invoice line id {line_id} is duplicated")
                    invoice_line_ids.add(line_id)
                    allocation_issue = self._allocation_issue(line)
                    if allocation_issue:
                        raise ValueError(
                            f"invoice line {line_id} {allocation_issue}; "
                            "business-unit attribution is unsafe")
                    created_day = _business_date(
                        line.get("created-at") or line.get("created_at"),
                        f"invoice line {line_id} created-at",
                        self._rni_business_zone)
                    if created_day > cutoff:
                        continue
                    line_status = _text(line.get("status")).lower()
                    if line_status and line_status not in _KNOWN_INVOICE_STATUSES:
                        raise ValueError(
                            f"invoice line {line_id} has unsupported status "
                            f"{line_status!r}")
                    currency = self._currency(line, invoice)
                    if not currency:
                        raise ValueError(
                            f"invoice line {line_id} has blank currency")
                    if order_line_id in line_currencies \
                            and currency not in line_currencies[order_line_id]:
                        raise ValueError(
                            f"invoice line {line_id} currency {currency} does "
                            f"not match receipt currency for order line "
                            f"{order_line_id}")
                    group = groups.get((order_line_id, currency))
                    if group is None:
                        continue
                    if po and group["po"] and po != group["po"]:
                        raise ValueError(
                            f"invoice line {line_id} PO {po!r} conflicts with "
                            f"receipt PO {group['po']!r} for order-line id "
                            f"{order_line_id}")
                    if (line_num and group["line"]
                            and line_num != group["line"]):
                        raise ValueError(
                            f"invoice line {line_id} line number {line_num!r} "
                            f"conflicts with receipt line {group['line']!r} "
                            f"for order-line id {order_line_id}")
                    invoice_line_type = _text(line.get("type"))
                    if (group["amount_basis"] == "quantity"
                            and "quantity" not in invoice_line_type.lower()):
                        raise ValueError(
                            f"invoice line {line_id} type "
                            f"{invoice_line_type!r} does not prove quantity-"
                            "line arithmetic")
                    if (group["amount_basis"] == "amount"
                            and "amount" not in invoice_line_type.lower()):
                        raise ValueError(
                            f"invoice line {line_id} type "
                            f"{invoice_line_type!r} does not prove amount-line "
                            "arithmetic")
                    total_value = (line.get("total")
                                   if line.get("total") is not None
                                   else line.get("line-total")
                                   if line.get("line-total") is not None
                                   else line.get("line_total"))
                    face_total = _decimal(total_value,
                                          f"invoice line {line_id} total")
                    credit = self._is_credit(invoice, line)
                    if face_total < 0 and not credit:
                        raise ValueError(
                            f"invoice line {line_id} is negative without an "
                            "explicit credit-note type")
                    if credit:
                        face_total = -abs(face_total)
                    quantity = None
                    valued_amount = face_total
                    if group["amount_basis"] == "quantity":
                        quantity = _decimal(line.get("quantity"),
                                            f"invoice line {line_id} quantity")
                        if quantity < 0 and not credit:
                            raise ValueError(
                                f"invoice line {line_id} has negative quantity "
                                "without an explicit credit-note type")
                        if credit:
                            quantity = -abs(quantity)
                        valued_amount = quantity * next(iter(group["prices"]))
                    eligible = (
                        not canceled
                        and header_status in self.rni_eligible_invoice_statuses)
                    evidence = {
                        "invoice_id": invoice_id, "invoice_line_id": line_id,
                        "invoice_number": _text(invoice.get("invoice-number")
                                                or invoice.get("invoice_number")),
                        "order_line_id": order_line_id,
                        "po": po or group["po"],
                        "line": line_num or group["line"],
                        "line_status": line_status or None,
                        "header_status": header_status, "currency": currency,
                        "face_amount": _money(face_total),
                        "candidate_valuation_amount": _money(valued_amount),
                        "quantity": (float(quantity)
                                     if quantity is not None else None),
                        "created_at": created_day.isoformat(),
                        "scope_path": scope_path, "canceled": canceled,
                        "paid": paid,
                        "credit_note": credit,
                    }
                    if eligible:
                        group["eligible_invoice_amount"] += valued_amount
                        group["eligible_invoice_face_total"] += face_total
                        if quantity is not None:
                            group["eligible_invoice_quantity"] += quantity
                        group["eligible_invoice_line_count"] += 1
                        if len(group["eligible_invoice_lines"]) < \
                                _MAX_RNI_NESTED_EVIDENCE_ROWS:
                            group["eligible_invoice_lines"].append(evidence)
                    else:
                        group["ineligible_invoice_line_count"] += 1
                        if len(group["ineligible_invoice_lines"]) < \
                                _MAX_RNI_NESTED_EVIDENCE_ROWS:
                            group["ineligible_invoice_lines"].append(evidence)
        except (TypeError, ValueError) as exc:
            return incomplete(
                f"Coupa invoice-line evidence is incomplete: {exc}.",
                pagination=pagination)
        if self.mode == "live" and invoice_headers_without_target_scope:
            return incomplete(
                "The governed Coupa invoice server filter returned "
                f"{invoice_headers_without_target_scope} header(s) with no "
                "nested line in the selected business unit. The filter may "
                "have been ignored or misconfigured; no totals are returned.",
                pagination=pagination,
                scope={"business_unit": bu,
                       "business_timezone": self.rni_business_timezone,
                       "coupa_business_unit": coupa_bu,
                       "mapping_basis": mapping_basis,
                       "business_unit_path": self.rni_business_unit_path})
        if missing_invoice_scope:
            return incomplete(
                f"{len(missing_invoice_scope)} PO-linked invoice line(s) have "
                "no verifiable business-unit value at the configured path; "
                "row security and completeness cannot be established.",
                pagination=pagination,
                scope={"business_unit": bu,
                       "business_timezone": self.rni_business_timezone,
                       "coupa_business_unit": coupa_bu,
                       "mapping_basis": mapping_basis,
                       "mapping": self.rni_business_unit_path
                       or "unconfigured",
                       "missing_invoice_line_scope_count": len(
                           missing_invoice_scope)})

        selected: list[tuple[Decimal, dict, Optional[Decimal],
                            Optional[Decimal]]] = []
        ineligible: list[dict] = []
        ineligible_count = 0
        over_invoiced: list[dict] = []
        over_invoiced_count = 0
        net_credit_activity: list[dict] = []
        net_credit_activity_count = 0
        selected_totals: dict[str, Decimal] = {}
        positive_totals: dict[str, Decimal] = {}
        positive_candidate_count = 0
        receipt_valuation_totals: dict[str, Decimal] = {}
        receipt_face_totals: dict[str, Decimal] = {}
        receipt_face_to_valuation_differences: dict[str, Decimal] = {}
        invoice_totals: dict[str, Decimal] = {}
        invoice_face_totals: dict[str, Decimal] = {}
        for group in groups.values():
            if group["amount_basis"] == "quantity":
                unit_price = next(iter(group["prices"]))
                remaining_quantity = (group["receipt_quantity"]
                                      - group["eligible_invoice_quantity"])
                net_receipt_valuation = (
                    group["receipt_quantity"] * unit_price)
                candidate = remaining_quantity * unit_price
            else:
                unit_price = None
                remaining_quantity = None
                net_receipt_valuation = group["receipt_amount"]
                candidate = (net_receipt_valuation
                             - group["eligible_invoice_amount"])
            group["net_receipt_valuation"] = net_receipt_valuation
            group["receipt_face_to_valuation_difference"] = (
                group["receipt_amount"] - net_receipt_valuation)
            currency = group["currency"]
            receipt_valuation_totals[currency] = (
                receipt_valuation_totals.get(currency, Decimal("0"))
                + net_receipt_valuation)
            receipt_face_totals[currency] = receipt_face_totals.get(
                currency, Decimal("0")) + group["receipt_amount"]
            receipt_face_to_valuation_differences[currency] = (
                receipt_face_to_valuation_differences.get(
                    currency, Decimal("0"))
                + group["receipt_face_to_valuation_difference"])
            invoice_totals[currency] = invoice_totals.get(
                currency, Decimal("0")) + group["eligible_invoice_amount"]
            invoice_face_totals[currency] = invoice_face_totals.get(
                currency, Decimal("0")) + group["eligible_invoice_face_total"]
            ineligible_count += group["ineligible_invoice_line_count"]
            room = display_cap - len(ineligible)
            if room > 0:
                ineligible.extend(group["ineligible_invoice_lines"][:room])
            if (group["eligible_invoice_amount"] < 0
                    or group["eligible_invoice_quantity"] < 0):
                net_credit_activity_count += 1
                if len(net_credit_activity) < display_cap:
                    net_credit_activity.append({
                        "business_unit": bu,
                        "coupa_business_unit": coupa_bu,
                        "order_line_id": group["order_line_id"],
                        "po": group["po"], "line": group["line"],
                        "currency": group["currency"],
                        "net_receipt_amount": _money(
                            group["net_receipt_valuation"]),
                        "net_receipt_value_at_receipt_valuation": _money(
                            group["net_receipt_valuation"]),
                        "net_receipt_face_amount": _money(
                            group["receipt_amount"]),
                        "receipt_face_to_valuation_difference": _money(
                            group["receipt_face_to_valuation_difference"]),
                        "eligible_invoice_coverage_at_receipt_valuation": _money(
                            group["eligible_invoice_amount"]),
                        "eligible_invoice_face_amount": _money(
                            group["eligible_invoice_face_total"]),
                        "reason": (
                            "approved credit-note activity exceeds approved "
                            "debit invoice activity on this order line; a "
                            "candidate greater than receipts is not reported"),
                    })
                continue
            if candidate < 0:
                over_invoiced_count += 1
                if len(over_invoiced) < display_cap:
                    over_invoiced.append({
                        "business_unit": bu,
                        "coupa_business_unit": coupa_bu,
                        "order_line_id": group["order_line_id"],
                        "po": group["po"], "line": group["line"],
                        "currency": group["currency"],
                        "net_receipt_amount": _money(
                            group["net_receipt_valuation"]),
                        "net_receipt_value_at_receipt_valuation": _money(
                            group["net_receipt_valuation"]),
                        "net_receipt_face_amount": _money(
                            group["receipt_amount"]),
                        "receipt_face_to_valuation_difference": _money(
                            group["receipt_face_to_valuation_difference"]),
                        "eligible_invoice_amount": _money(
                            group["eligible_invoice_amount"]),
                        "eligible_invoice_coverage_at_receipt_valuation": _money(
                            group["eligible_invoice_amount"]),
                        "eligible_invoice_face_amount": _money(
                            group["eligible_invoice_face_total"]),
                        "difference": _money(candidate),
                        "reason": (
                            "eligible invoice activity exceeds net receipt "
                            "activity; this is an exception, not a negative "
                            "accrual candidate"),
                    })
                continue
            if candidate > 0:
                positive_candidate_count += 1
                positive_totals[currency] = positive_totals.get(
                    currency, Decimal("0")) + candidate
            if candidate <= threshold:
                continue
            selected_totals[currency] = selected_totals.get(
                currency, Decimal("0")) + candidate
            selected.append((candidate, group, remaining_quantity, unit_price))

        selected.sort(key=lambda item: (
            item[1]["currency"], -item[0], item[1]["order_line_id"]))
        selected_candidate_count = len(selected)
        displayed = selected[:display_cap]
        candidates: list[dict] = []
        for candidate, group, remaining_quantity, unit_price in displayed:
            candidates.append({
                "business_unit": bu,
                "coupa_business_unit": coupa_bu,
                "order_line_id": group["order_line_id"],
                "po": group["po"], "po_id": group["po_id"],
                "line": group["line"], "line_type": group["line_type"],
                "matching_precision": "order_line_aggregate",
                "supplier": group["supplier"],
                "currency": group["currency"],
                "net_receipt_amount": _money(
                    group["net_receipt_valuation"]),
                "net_receipt_value_at_receipt_valuation": _money(
                    group["net_receipt_valuation"]),
                "net_receipt_face_amount": _money(group["receipt_amount"]),
                "receipt_face_to_valuation_difference": _money(
                    group["receipt_face_to_valuation_difference"]),
                "net_receipt_valuation_basis": (
                    "net quantity times single proven receipt price"
                    if group["amount_basis"] == "quantity"
                    else "Coupa receiving-transaction face total"),
                "eligible_invoice_amount": _money(
                    group["eligible_invoice_amount"]),
                "eligible_invoice_coverage_at_receipt_valuation": _money(
                    group["eligible_invoice_amount"]),
                "eligible_invoice_face_amount": _money(
                    group["eligible_invoice_face_total"]),
                "rni_candidate_amount": _money(candidate),
                "rni_amt": _money(candidate),
                "net_receipt_quantity": (
                    float(group["receipt_quantity"])
                    if group["amount_basis"] == "quantity" else None),
                "eligible_invoice_quantity": (
                    float(group["eligible_invoice_quantity"])
                    if group["amount_basis"] == "quantity" else None),
                "remaining_quantity": (float(remaining_quantity)
                                       if remaining_quantity is not None else None),
                "valuation_unit_price": (float(unit_price)
                                         if unit_price is not None else None),
                "receipt_transaction_count": len(group["receipt_ids"]),
                "return_or_void_transaction_count": group[
                    "return_or_void_count"],
                "eligible_invoice_line_count": group[
                    "eligible_invoice_line_count"],
                "first_receipt_date": min(group["receipt_dates"]).isoformat(),
                "last_receipt_date": max(group["receipt_dates"]).isoformat(),
                "receipt_transaction_ids": group["receipt_ids"][
                    :_MAX_RNI_NESTED_EVIDENCE_ROWS],
                "receipt_id_evidence_displayed_count": min(
                    len(group["receipt_ids"]),
                    _MAX_RNI_NESTED_EVIDENCE_ROWS),
                "receipt_id_evidence_truncated": (
                    len(group["receipt_ids"])
                    > _MAX_RNI_NESTED_EVIDENCE_ROWS),
                "eligible_invoice_lines": group["eligible_invoice_lines"],
                "eligible_invoice_evidence_displayed_count": len(
                    group["eligible_invoice_lines"]),
                "eligible_invoice_evidence_truncated": (
                    group["eligible_invoice_line_count"]
                    > len(group["eligible_invoice_lines"])),
            })
        known_export = [event for event in events
                        if event["exported"] is not None]
        invalid_export_timestamps = sum(
            1 for event in events
            if not event["last_exported_at_valid"])
        export_complete = (
            len(known_export) == len(events)
            and invalid_export_timestamps == 0)
        export_rows = sorted(
            events,
            key=lambda event: (
                event["transaction_date"], event["id"], event["type"]))
        displayed_export_rows = export_rows[:display_cap]
        export_evidence = {
            "evaluated": export_complete, "complete": export_complete,
            "population_basis": (
                "all validated, supported supplier receiving events in the "
                "selected governed business-unit collection; independent of "
                "candidate threshold and invoice coverage"),
            "receipt_transaction_count": len(export_rows),
            "displayed_receipt_transaction_count": len(
                displayed_export_rows),
            "display_truncated": (
                len(export_rows) > len(displayed_export_rows)),
            "exported_receipt_transactions": sum(
                1 for event in known_export if event["exported"]),
            "not_exported_receipt_transactions": sum(
                1 for event in known_export if not event["exported"]),
            "unknown_export_receipt_transactions": (
                len(events) - len(known_export)),
            "invalid_last_exported_at_transactions": (
                invalid_export_timestamps),
            "receipt_transactions": [{
                "receipt_transaction_id": event["id"],
                "order_line_id": event["order_line_id"],
                "type": event["type"],
                "transaction_date": event["transaction_date"].isoformat(),
                "exported": event["exported"],
                "last_exported_at": event["last_exported_at"] or None,
                "last_exported_at_valid": event[
                    "last_exported_at_valid"],
            } for event in displayed_export_rows],
            "meaning": (
                "Coupa exported flag only; export does not prove ERP receipt, "
                "accounting entry, or posted GL journal. A nonblank export "
                "timestamp is governed only when it is an ISO datetime with "
                "an explicit timezone offset"),
        }
        money_totals = {
            key: _money(value) for key, value in selected_totals.items()}
        positive_money_totals = {
            key: _money(value) for key, value in positive_totals.items()}
        exception_count = (ineligible_count + over_invoiced_count
                           + net_credit_activity_count
                           + len(excluded_types))
        if selected_candidate_count:
            conclusion = "po_linked_candidates_present"
        elif positive_candidate_count:
            conclusion = "no_candidates_above_threshold"
        elif exception_count:
            conclusion = "exceptions_present_no_positive_candidates"
        else:
            conclusion = "no_po_linked_candidates"
        return {
            **base, "status": "evaluated", "evaluated": True,
            "conclusion": conclusion,
            "scope": {"business_unit": bu,
                      "business_timezone": self.rni_business_timezone,
                      "coupa_business_unit": coupa_bu,
                      "mapping_basis": mapping_basis,
                      "business_unit_path": self.rni_business_unit_path},
            "coverage": {
                **base["coverage"], "point_in_time_complete": False,
                "collection_complete": True,
                "business_unit_complete": True,
                "business_unit_basis": self.rni_business_unit_path
                or "unconfigured",
                "business_unit_mapping_basis": mapping_basis,
                "coupa_business_unit": coupa_bu,
                "server_side_filters": {
                    "receipts": self.rni_receipt_business_unit_filter
                    or "recorded fixture collection",
                    "invoices": self.rni_invoice_business_unit_filter
                    or "recorded fixture collection",
                },
            },
            "snapshot": {
                **base["snapshot"], "complete": False,
                "collection_complete": True, "atomic": False,
                "as_of": cutoff.isoformat(),
                "basis": (
                    "complete current receiving-event and nested invoice-line "
                    "collections; not an atomic cross-endpoint snapshot"),
            },
            "pagination": pagination,
            "population": {
                **base["population"], "complete": True,
                "totals_complete": True,
                "aggregation_basis": (
                    "all complete, validated source rows before bounded "
                    "candidate and exception display"),
                "candidate_count": selected_candidate_count,
                "positive_candidate_count": positive_candidate_count,
                "displayed_candidate_count": len(candidates),
                "display_truncated": (
                    selected_candidate_count > len(candidates)),
                "receipt_rows_read": len(receipt_rows),
                "receipt_events_in_scope": len(events),
                "receipt_rows_outside_business_unit": outside_receipt_scope,
                "invoice_headers_read": len(invoice_rows),
                "invoice_lines_read": invoice_lines_read,
                "invoice_lines_outside_business_unit": outside_invoice_scope,
                "non_po_invoice_lines_excluded": non_po_lines,
                "excluded_receiving_types": len(excluded_types),
                "invoice_present_not_eligible_count": ineligible_count,
                "over_invoiced_count": over_invoiced_count,
                "net_credit_invoice_activity_count": (
                    net_credit_activity_count),
            },
            "observed": {
                "net_receipts_by_currency": {
                    key: _money(value)
                    for key, value in receipt_valuation_totals.items()},
                "net_receipt_values_at_receipt_valuation_by_currency": {
                    key: _money(value)
                    for key, value in receipt_valuation_totals.items()},
                "net_receipt_face_totals_by_currency": {
                    key: _money(value)
                    for key, value in receipt_face_totals.items()},
                "receipt_face_to_valuation_differences_by_currency": {
                    key: _money(value)
                    for key, value in
                    receipt_face_to_valuation_differences.items()},
                "eligible_invoice_coverage_by_currency": {
                    key: _money(value) for key, value in invoice_totals.items()},
                "eligible_invoice_face_totals_by_currency": {
                    key: _money(value)
                    for key, value in invoice_face_totals.items()},
                "candidate_totals_by_currency": money_totals,
                "all_positive_candidate_totals_by_currency": (
                    positive_money_totals),
            },
            "totals_by_currency": money_totals,
            "rni_totals_by_currency": money_totals,
            "all_positive_candidate_totals_by_currency": positive_money_totals,
            "count": selected_candidate_count, "lines": candidates,
            "exceptions": {
                "invoice_present_not_eligible": ineligible,
                "over_invoiced": over_invoiced,
                "net_credit_invoice_activity": net_credit_activity,
                "excluded_receiving_types": excluded_types[
                    :display_cap],
                "counts": {
                    "invoice_present_not_eligible": ineligible_count,
                    "over_invoiced": over_invoiced_count,
                    "net_credit_invoice_activity": (
                        net_credit_activity_count),
                    "excluded_receiving_types": len(excluded_types),
                },
                "displayed_counts": {
                    "invoice_present_not_eligible": len(ineligible),
                    "over_invoiced": len(over_invoiced),
                    "net_credit_invoice_activity": len(
                        net_credit_activity),
                    "excluded_receiving_types": min(
                        len(excluded_types), display_cap),
                },
                "display_truncated": {
                    "invoice_present_not_eligible": (
                        ineligible_count > len(ineligible)),
                    "over_invoiced": (
                        over_invoiced_count > len(over_invoiced)),
                    "net_credit_invoice_activity": (
                        net_credit_activity_count > len(net_credit_activity)),
                    "excluded_receiving_types": (
                        len(excluded_types) > display_cap),
                },
            },
            "export_evidence": export_evidence,
            "note": (
                "Coupa PO-line review candidates only. Pending/draft/held "
                "invoice lines remain visible and are not subtracted by "
                "default. Receipt-level attribution requires Coupa "
                "matching_allocations. A zero candidate count is not an "
                "exact-instant clean pass; it means none were observed in the "
                "completed sequential collections. It is never an all-GRNI, "
                "ERP-booked, or posted-GL conclusion."),
        }

    def supplier_spend(self, months: int = 12, top_n: int = 10,
                       today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=max(int(months or 12), 1) * 30)
                 ).isoformat()
        by: dict[tuple, dict] = {}
        for r in self._invoices():
            if r["invoice_date"] < since or not r["supplier"]:
                continue
            key = (r["supplier"], r["currency"])
            s = by.setdefault(key, {"supplier": r["supplier"],
                                    "currency": r["currency"],
                                    "invoices": 0, "spend": 0.0})
            s["invoices"] += 1
            s["spend"] = round(s["spend"] + r["total"], 2)
        ranked = sorted(by.values(), key=lambda s: -s["spend"])
        return {"source": "coupa", "mode": self.mode, "since": since,
                "suppliers": ranked[:max(int(top_n or 10), 1)],
                "count": len(ranked), "truncated": len(ranked) > top_n}

    # ------------------------------------------------------- reconciliation
    def budget_lines(self, period: str = "") -> dict:
        """Budget lines as Coupa holds them: account segments, period,
        amount. segment-1 is conventionally the natural account and
        segment-2 the cost centre, but that is a per-tenant CHART
        CONFIGURATION, so the mapping is reported, never assumed silently.
        """
        rows = self.get("/api/budget_lines", {"limit": 500}) or []
        want = (period or "").strip().upper()
        out = []
        for r in rows:
            acct = r.get("account") or {}
            entry = {
                "id": r.get("id"),
                "name": str(r.get("name") or ""),
                "period": str(r.get("period") or "").upper(),
                "account": str(acct.get("segment-1")
                               or acct.get("segment_1") or ""),
                "cost_centre": str(acct.get("segment-2")
                                   or acct.get("segment_2") or ""),
                "budget": float(r.get("budgeted-amount")
                                or r.get("budgeted_amount") or 0.0),
                "currency": str((r.get("currency") or {}).get("code") or ""),
            }
            if want and entry["period"] != want:
                continue
            out.append(entry)
        return {"source": "coupa", "mode": self.mode,
                "period": want or "all", "count": len(out),
                "budget_lines": out,
                "segment_map": {"segment-1": "natural account",
                                "segment-2": "cost centre"},
                "note": ("Budget lives in Coupa at this deployment, not in "
                         "a PeopleSoft budget ledger. Segment meanings are "
                         "a per-tenant chart configuration — confirm them "
                         "before trusting a mapping.")}

    def budget_variance(self, engine, business_unit: str = "",
                        fiscal_year: int = 0, period: int = 0,
                        top: int = 25) -> dict:
        """Coupa BUDGET vs PeopleSoft ACTUALS, matched on natural account.

        The cross-system shape this site actually has. Match basis is
        disclosed, and the two failure directions are reported separately
        because they mean different things: a budget line with no ledger
        activity is unspent plan, while ledger spend with no budget line
        is unbudgeted — the one a controller wants to see first.
        """
        bu = (business_unit or "").strip() or \
            engine.effective_defaults()["business_unit"]
        led = engine.resolve_ledger_for(bu)
        fy = int(fiscal_year or 0) or engine.last_posted_period(bu, led)[0]
        per = int(period or 0) or engine.last_posted_period(bu, led)[1]
        if not fy:
            return {"evaluated": False,
                    "reason": f"No posted ledger data for {bu!r} to compare "
                              "the Coupa budget against."}
        lines = self.budget_lines(period=f"FY{fy}")["budget_lines"]
        if not lines:
            return {"evaluated": False, "business_unit": bu,
                    "fiscal_year": fy,
                    "reason": (f"Coupa holds no budget lines for FY{fy}. "
                               "Check the budget period naming in Coupa "
                               "(this tool matches on 'FY<year>').")}
        rows = engine._period_sums(bu, led, fy, per, include_adj=True)
        actual: dict = {}
        descr: dict = {}
        types: dict = {}
        for r in rows:
            p_ = int(r.get("period") or 0)
            if p_ < 1 or p_ > per:
                continue
            acct = str(r.get("account") or "")
            actual[acct] = actual.get(acct, 0.0) + float(r.get("amt") or 0.0)
            descr.setdefault(acct, str(r.get("descr") or ""))
            types.setdefault(acct, str(r.get("acct_type") or ""))
        budget: dict = {}
        currencies = set()
        for line in lines:
            budget[line["account"]] = round(
                budget.get(line["account"], 0.0) + line["budget"], 2)
            currencies.add(line["currency"])
        compared, unbudgeted, unspent = [], [], []
        revenue_excluded = 0
        for acct in sorted(set(actual) | set(budget)):
            a = round(actual.get(acct, 0.0), 2)
            b = round(budget.get(acct, 0.0), 2)
            atype = types.get(acct, "")
            if acct not in budget:
                # Coupa budgets SPEND. Revenue having no Coupa budget line
                # is the expected shape, not a finding, and balance-sheet
                # accounts were never in scope — flagging either as
                # "unbudgeted" is the noise that makes a real unbudgeted
                # expense easy to miss.
                if atype == "E" and a:
                    unbudgeted.append({"account": acct,
                                       "descr": descr.get(acct, ""),
                                       "actual": a})
                elif atype == "R":
                    revenue_excluded += 1
                continue
            if acct not in actual:
                unspent.append({"account": acct, "budget": b,
                                "descr": descr.get(acct, "")})
                continue
            variance = round(a - b, 2)
            compared.append({
                "account": acct, "descr": descr.get(acct, ""),
                "account_type": atype, "actual": a, "budget": b,
                "variance": variance,
                "variance_pct": (round(variance / abs(b) * 100.0, 2)
                                 if b else None),
                # Expense and revenue both read "favourable" when the
                # variance is negative; see the PS-side tool for why the
                # account TYPE decides this and never the sign.
                "favourable": variance < 0 if atype in ("R", "E") else None,
            })
        compared.sort(key=lambda r: -abs(r["variance"]))
        truncated = len(compared) > max(int(top or 25), 1)
        return {
            "source": "coupa+peoplesoft", "evaluated": True,
            "business_unit": bu, "ledger": led,
            "fiscal_year": fy, "through_period": per,
            "mode": self.mode,
            "budget_currencies": sorted(c for c in currencies if c),
            "match_basis": ("Coupa budget-line account segment-1 matched to "
                            "the PeopleSoft natural ACCOUNT; ledger side is "
                            f"year-to-date activity, periods 1..{per}"),
            "rows": compared[:max(int(top or 25), 1)],
            "row_count": len(compared), "truncated": truncated,
            "unbudgeted_spend": unbudgeted,
            "budget_not_spent": unspent,
            "population": {
                "concept": "Coupa spend budget vs ledger actuals",
                "applied": [
                    {"predicate": "ACCOUNT_TYPE = 'E' for unbudgeted spend",
                     "source": "Coupa budgets procurable SPEND",
                     "meaning": "revenue and balance-sheet accounts are not "
                                "expected to carry a Coupa budget line"},
                    {"predicate": f"periods 1..{per} of FY{fy}",
                     "source": "the request scope",
                     "meaning": "year-to-date ledger activity"},
                ],
                "revenue_accounts_excluded": revenue_excluded,
            },
            "note": ("Budget from Coupa, actuals from the ledger. Unbudgeted "
                     "spend and unspent budget are listed separately — they "
                     "are different problems. Expenses that never flow "
                     "through procurement (depreciation, payroll "
                     "allocations) legitimately have no Coupa budget — "
                     "judge the unbudgeted list with that in mind. Confirm "
                     "the segment mapping before acting: segment meanings "
                     "are per-tenant."
                     + (" SAMPLE procurement fixtures, not live data."
                        if self.mode == "fixtures" else "")),
        }

    def ap_tie(self, db, days: int = 90,
               today: Optional[dt.date] = None) -> dict:
        """Approved Coupa invoices vs PS vouchers, matched server-side.

        Match basis (disclosed in the payload): invoice number equality,
        then normalized supplier name against the vendor master. Amount
        differences on matched pairs are listed — a matched-but-different
        pair is the most dangerous kind, because both systems look right
        alone.
        """
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=max(int(days or 90), 1))
                 ).isoformat()
        asof = today.isoformat()
        coupa = [r for r in self._invoices()
                 if r["status"].lower() in {"approved", "paid"}
                 and since <= r["invoice_date"] <= asof]
        p = db.prefix
        try:
            vouchers, _ = db.query(
                f"SELECT V.INVOICE_ID AS inv, V.VOUCHER_ID AS voucher, "
                f"V.GROSS_AMT AS amt, V.CURRENCY_CD AS currency, "
                f"N.NAME1 AS vendor "
                f"FROM {p}PS_VOUCHER V LEFT JOIN {p}PS_VENDOR N "
                f"ON N.VENDOR_ID = V.VENDOR_ID "
                f"WHERE V.INVOICE_DT >= {db.date_bind('since')} "
                f"AND V.INVOICE_DT <= {db.date_bind('asof')}",
                {"since": since, "asof": asof}, max_rows=5000)
        except Exception as e:
            return {"source": "coupa+peoplesoft", "evaluated": False,
                    "reason": f"Could not read PS_VOUCHER: {e}"}
        by_number: dict[str, dict] = {}
        for v in vouchers:
            by_number.setdefault(str(v["inv"] or ""), dict(v))
        matched, amount_breaks, missing_in_ap = [], [], []
        for c in coupa:
            v = by_number.get(c["number"])
            if not v:
                missing_in_ap.append(c)
                continue
            names_agree = (_norm_name(v.get("vendor"))
                           == _norm_name(c["supplier"]))
            pair = {"invoice": c["number"], "voucher": v["voucher"],
                    "coupa_total": c["total"],
                    "ps_gross": float(v["amt"] or 0.0),
                    "currency": c["currency"],
                    "supplier": c["supplier"],
                    "vendor_name_match": names_agree}
            if round(pair["coupa_total"] - pair["ps_gross"], 2) != 0.0 \
                    or c["currency"] != str(v.get("currency") or ""):
                amount_breaks.append({
                    **pair, "difference": round(
                        pair["coupa_total"] - pair["ps_gross"], 2)})
            else:
                matched.append(pair)
        ties = not missing_in_ap and not amount_breaks
        return {
            "source": "coupa+peoplesoft", "evaluated": True, "ties": ties,
            "since": since, "as_of": asof, "match_basis": (
                "invoice number equality against PS_VOUCHER.INVOICE_ID, "
                "supplier verified against the vendor master by normalized "
                "name"),
            "coupa_invoices": len(coupa), "matched": len(matched),
            "amount_breaks": amount_breaks,
            "missing_in_ap": [{k: c[k] for k in
                               ("number", "supplier", "total", "currency",
                                "invoice_date", "status")}
                              for c in missing_in_ap],
            "note": ("Every approved Coupa invoice in the window has a "
                     "matching voucher at the same amount." if ties else
                     "Breaks listed — missing_in_ap never reached AP; "
                     "amount_breaks landed at a different amount and need "
                     "eyes even though both systems look right alone."),
        }


def from_env(root=None, cfg=None) -> CoupaConnector:
    """Live when COUPA_BASE_URL is set; bundled fixtures otherwise.

    Credentials and OAuth scope come from the environment.  Tenant meaning
    (business-unit path/map, eligible status and safety cap) comes from the
    reviewable ``coupa`` configuration, never an undocumented environment
    convention.
    """
    semantic = getattr(cfg, "coupa", cfg) if cfg is not None else None
    business_unit_path = _text(
        getattr(semantic, "business_unit_path", ""))
    business_timezone = _text(
        getattr(semantic, "business_timezone", ""))
    business_unit_map = getattr(semantic, "business_unit_map", {})
    eligible_statuses = getattr(
        semantic, "invoice_eligible_statuses",
        sorted(_DEFAULT_ACTIVE_INVOICE_STATUSES))
    receipt_statuses = getattr(
        semantic, "receipt_eligible_statuses",
        sorted(_DEFAULT_RECEIPT_STATUSES))
    rni_max_rows = getattr(semantic, "rni_max_rows", _DEFAULT_RNI_ROW_CAP)
    connector_kwargs = {
        "rni_business_unit_path": business_unit_path,
        "rni_business_timezone": business_timezone,
        "rni_receipt_business_unit_filter": _text(getattr(
            semantic, "receipt_business_unit_filter", "")),
        "rni_invoice_business_unit_filter": _text(getattr(
            semantic, "invoice_business_unit_filter", "")),
        "rni_business_unit_map": business_unit_map,
        "rni_eligible_invoice_statuses": eligible_statuses,
        "rni_receipt_statuses": receipt_statuses,
        "rni_invoice_scope_order_line_invariant": getattr(
            semantic, "invoice_scope_order_line_invariant", False),
        "rni_max_rows": rni_max_rows,
    }
    base = os.environ.get("COUPA_BASE_URL", "").strip()
    if base:
        return CoupaConnector(
            base,
            api_key=os.environ.get("COUPA_API_KEY", "").strip(),
            client_id=os.environ.get("COUPA_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("COUPA_CLIENT_SECRET", "").strip(),
            scope=os.environ.get("COUPA_SCOPE",
                                 "core.invoice.read core.purchase_order.read "
                                 "core.supplier.read "
                                 "core.inventory.receiving.read").strip(),
            **connector_kwargs,
        )
    fixture = FIXTURE_DIR / "coupa.json"
    return CoupaConnector(transport=FixtureTransport(fixture),
                          **connector_kwargs)
