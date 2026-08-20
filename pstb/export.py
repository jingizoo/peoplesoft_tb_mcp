"""Full-data CSV export.

Every table on screen is capped for display — tool payloads carry a few
hundred rows so an answer stays readable and the model's context stays
small. The moment someone wants the data in Excel, that cap is exactly
wrong: they asked for the whole population, not the preview. So export
does not serialize what the browser is holding. It RE-RUNS the same tool
server-side with an export ceiling and streams the full result.

Three properties matter more than convenience here:

* Honest truncation. If the export itself hits its ceiling, the row is
  never silently dropped — the count and the ceiling are reported and the
  FILENAME says so, because a spreadsheet outlives the browser tab that
  produced it and nobody remembers a toast a week later.
* No formula injection. A cell of text beginning = + - @ executes when
  the file is opened in Excel, and this data comes from a database other
  people write to. Text cells are neutralized; numbers are untouched.
* Shape tolerance. The table finder works on ANY payload — it looks for
  the largest list of records rather than a hard-coded key per tool — so
  a tool added later is exportable the day it ships.
"""
from __future__ import annotations

import csv
import datetime as dt
import inspect
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

# Ceiling for one export. High enough that a real population fits, low
# enough that a mistyped query cannot pull a multi-million-row table
# through the web process.
EXPORT_MAX_ROWS = 50_000

# Tools whose full population can be re-created from their recorded arguments.
# A result-only control is still downloadable through the ordinary CSV button,
# but it must never advertise a million-row batch that would only package its
# already-capped preview.
BATCH_REPLAY_TOOLS = frozenset({
    "get_trial_balance", "run_sql", "get_budget_variance",
    "drill_to_journals", "rollup_trial_balance", "compare_trial_balance",
    "explain_balance_change", "search_accounts", "get_invoice_lifecycle",
    "get_dso_trend", "get_cash_outlook", "get_customer_intelligence",
    "get_invoice_totals", "get_ar_aging", "get_top_billing_customers",
    "get_customer_ar", "get_billing_workbench", "get_open_payables",
    "get_vendor_intelligence", "get_duplicate_payments",
    "get_vendor_payments", "get_asset_register", "get_project_costs",
    "run_report", "run_ps_query", "get_coupa_invoices",
    "get_coupa_stuck_approvals", "get_coupa_budget_lines",
    "get_coupa_rni", "get_coupa_supplier_spend",
})

# Keys that hold the answer's table, best first. The finder falls back to
# "largest list of dicts" for anything not listed, so new tools work
# without touching this.
_PREFERRED_KEYS = (
    "rows", "customers", "invoices", "vouchers", "lines", "items",
    "accounts", "journals", "assets", "projects", "vendors", "suppliers",
    "payables", "payments", "stuck", "amount_breaks", "missing_in_ap",
    "buckets", "steps", "scopes", "tables", "records", "passages",
)

_RISKY_PREFIX = ("=", "+", "-", "@", "\t", "\r")


class ExportError(RuntimeError):
    """An export problem stated with its remedy."""


def _flatten(value: Any) -> Any:
    """One cell's worth of value. Nested structures become compact JSON
    rather than Python reprs, so a cell stays machine-readable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        if all(isinstance(v, (str, int, float, bool)) or v is None
               for v in value):
            return "; ".join("" if v is None else str(v) for v in value)
        return json.dumps(list(value), default=str)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)


def _pivot_table(pivot: dict) -> dict:
    """A cross-tab exports as the grid the user is looking at."""
    cols = [str(c) for c in (pivot.get("columns") or [])]
    label = str(pivot.get("row_field") or "row")
    columns = [label] + cols + ["total", "change", "change_pct"]
    rows = []
    for r in pivot.get("rows") or []:
        values = list(r.get("values") or [])
        values += [None] * (len(cols) - len(values))
        rows.append({label: r.get("row"),
                     **{c: values[i] for i, c in enumerate(cols)},
                     "total": r.get("total"), "change": r.get("change"),
                     "change_pct": r.get("change_pct")})
    if pivot.get("column_totals"):
        totals = list(pivot["column_totals"])
        totals += [None] * (len(cols) - len(totals))
        rows.append({label: "TOTAL",
                     **{c: totals[i] for i, c in enumerate(cols)},
                     "total": pivot.get("grand_total"),
                     "change": None, "change_pct": None})
    return {"columns": columns, "rows": rows, "label": "pivot"}


def tabular(payload: Any) -> Optional[dict]:
    """Find the exportable table inside any tool payload.

    Returns {"columns", "rows", "label"} or None when the payload has no
    table (a health check, a single balance, a wiki page).
    """
    if not isinstance(payload, dict):
        if isinstance(payload, list) and payload and \
                all(isinstance(r, dict) for r in payload):
            return _table_from(payload, "rows")
        return None
    if isinstance(payload.get("pivot"), dict) and \
            (payload["pivot"].get("rows") or []):
        return _pivot_table(payload["pivot"])
    best: tuple[int, int, str, list] = (-1, 0, "", [])
    for key, value in payload.items():
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(r, dict) for r in value):
            continue
        try:
            pref = len(_PREFERRED_KEYS) - _PREFERRED_KEYS.index(str(key))
        except ValueError:
            pref = 0
        score = (pref, len(value), str(key), value)
        if (score[0], score[1]) > (best[0], best[1]):
            best = score
    if best[3]:
        return _table_from(best[3], best[2])
    return None


def _table_from(records: list, label: str) -> dict:
    columns: list[str] = []
    for r in records:
        for k in r:
            if k not in columns:
                columns.append(str(k))
    return {"columns": columns, "rows": records, "label": str(label)}


def _cell(value: Any) -> Any:
    v = _flatten(value)
    if isinstance(v, str) and v[:1] in _RISKY_PREFIX:
        # Excel executes a cell that opens with = + - @. This data comes
        # from records other people write to, so a supplier named
        # "=cmd|..." must arrive as text, not as a formula. Numbers reach
        # here as int/float and are never touched, so negative amounts
        # keep their minus sign.
        return "'" + v
    return v


def to_csv(table: dict) -> str:
    """RFC 4180 CSV with a UTF-8 BOM so Excel reads accents correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    columns = table["columns"]
    # The header row goes through _cell as well. A pivot's columns ARE
    # database values — account descriptions, customer names, period labels —
    # so "neutralise the cells but trust the header" leaves the injection
    # open on exactly the row Excel evaluates first.
    writer.writerow([_cell(c) for c in columns])
    for r in table["rows"]:
        writer.writerow([_cell(r.get(c)) for c in columns])
    return "﻿" + buf.getvalue()


def preview_payload(payload: Any, row_limit: int = 100) -> tuple[Any, int]:
    """Copy a payload while capping every record collection for the browser.

    Totals, control statuses, scope notes and source provenance remain intact;
    only lists of row dictionaries are shortened.  The original payload still
    exists in the answer engine and can be re-run by the export endpoint.
    """
    limit = max(1, int(row_limit or 100))
    omitted = 0

    def trim(node):
        nonlocal omitted
        if isinstance(node, dict):
            return {k: trim(v) for k, v in node.items()}
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node):
                omitted += max(0, len(node) - limit)
                return [trim(item) for item in node[:limit]]
            return [trim(item) for item in node]
        return node

    return trim(payload), omitted


def batch_hint(tool: str, payload: Any, *, inline_rows: int = 100,
               source: str = "default") -> Optional[dict]:
    """Describe a lazy batch action when a card is larger than chat."""
    if not isinstance(payload, dict):
        return None
    table = tabular(payload)
    if table is None:
        # A status/control payload can legitimately report that one of its
        # internal checks was truncated without containing a row set. Do not
        # offer a CSV button that can only fail after it is clicked.
        return None
    visible = len(table["rows"]) if table else 0
    reported = payload.get("row_count")
    if not isinstance(reported, int) or isinstance(reported, bool):
        reported = visible

    def cut(node) -> bool:
        if isinstance(node, dict):
            return any(
                ((k in {"truncated", "display_truncated"} and v is True)
                 or (k == "rows_omitted_for_context"
                     and isinstance(v, int) and not isinstance(v, bool)
                     and v > 0)
                 or cut(v)) for k, v in node.items())
        if isinstance(node, list):
            return any(cut(item) for item in node[:20])
        return False

    limit = max(1, int(inline_rows or 100))
    large = visible > limit or reported > limit or cut(payload)
    if not large:
        return None
    canonical = str(source or "default")
    capable = (str(tool) == "run_sql" if canonical != "default"
               else str(tool) in BATCH_REPLAY_TOOLS)
    if not capable:
        return None
    return {
        "available": True,
        "required": True,
        "inline_rows": limit,
        "preview_rows": min(visible, limit),
        "reported_rows": reported,
        "source_truncated": cut(payload),
        "mode": "streamed" if str(tool) == "run_sql" else "background",
        "note": (
            f"This result is larger than {limit:,} rows. Only a preview is "
            "shown; prepare the full CSV when needed. The original question "
            "does not run an export query."
        ),
    }


class CsvStreamSink:
    """CSV writer resettable across a dead-session retry."""

    def __init__(self, path: Path, *, progress: Callable[[int], None],
                 fetch_size: int = 2_000,
                 max_bytes: int = 1_073_741_824):
        self.path = Path(path)
        self.progress = progress
        self.fetch_size = max(1, int(fetch_size or 2_000))
        self.max_bytes = max(1, int(max_bytes))
        self.rows = 0
        self.columns: list[str] = []
        self._fh = None
        self._writer = None

    def start(self, columns: list[str]) -> None:
        self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8", newline="")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._fh.write("﻿")
        self._writer = csv.writer(self._fh, lineterminator="\r\n")
        self.columns = [str(c) for c in columns]
        self.rows = 0
        self._writer.writerow([_cell(c) for c in self.columns])
        self._check_size()

    def _check_size(self) -> None:
        if self._fh is not None and self._fh.tell() > self.max_bytes:
            raise ExportError(
                "The batch CSV exceeded its configured file-size ceiling. "
                "Narrow the result or raise batch_exports.max_file_mb after "
                "checking available disk space.")

    def write_rows(self, rows: list[dict]) -> None:
        if self._writer is None:
            raise ExportError("CSV stream was not initialized")
        for row in rows:
            self._writer.writerow([_cell(row.get(c)) for c in self.columns])
            self.rows += 1
            # Enforce while writing, not after a whole fetch batch. A query
            # may project a CLOB; checking only after 2,000 such rows can
            # overshoot the configured disk ceiling by gigabytes.
            self._check_size()
        self.progress(self.rows)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
                self._writer = None


def batch_to_file(tool: str, args: dict, registry: dict, *, engine=None,
                  payload: Any = None, path: Path, row_cap: int = 1_000_000,
                  fetch_size: int = 2_000,
                  max_bytes: int = 1_073_741_824,
                  progress: Callable[[int], None] = lambda _rows: None,
                  today: Optional[dt.date] = None) -> dict:
    """Write one batch export to ``path`` without retaining it in memory.

    Ad-hoc SQL uses a cursor-to-file stream and may reach ``row_cap``. Other
    curated tools retain their existing 50k in-memory safety ceiling but run
    off the request thread, which keeps chat responsive and preserves their
    domain-specific aggregation semantics.
    """
    cap = max(1, int(row_cap or 1_000_000))
    clean_args = dict(args or {})
    for key in ("source", "db", "_export_view"):
        if key != "_export_view":
            clean_args.pop(key, None)
    if (str(tool) == "run_sql" and engine is not None
            and not clean_args.get("partition")
            and not clean_args.get("pivot")):
        sink = CsvStreamSink(path, progress=progress, fetch_size=fetch_size,
                             max_bytes=max_bytes)
        try:
            result = _call_filtered(
                engine.run_sql, clean_args,
                {"max_rows": cap, "row_ceiling": cap,
                 "_batch_sink": sink})
        finally:
            sink.close()
        rows = int(result.get("row_count") or sink.rows)
        truncated = bool(result.get("truncated"))
        return {
            "rows": rows, "columns": len(sink.columns),
            "truncated": truncated,
            "filename": filename(str(tool), rows, truncated, today),
            "note": (
                f"Streamed {rows:,} rows directly from the database. "
                "The query was re-run when this batch started, so the CSV "
                "reflects that live-data time rather than a snapshot of the "
                "earlier chat preview. "
                + (f"The {cap:,}-row safety ceiling was reached; narrow the "
                   "query and export the remaining population separately."
                   if truncated else "The complete selected population was written.")
            ),
        }

    # Curated tools already own their correctness and shape rules. Reuse that
    # path in the worker rather than teaching a second exporter their schemas.
    out = export(tool, clean_args, registry, payload=payload,
                 row_cap=min(cap, EXPORT_MAX_ROWS), today=today)
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        fh.write(out.pop("csv"))
    if Path(path).stat().st_size > max(1, int(max_bytes)):
        Path(path).unlink(missing_ok=True)
        raise ExportError(
            "The batch CSV exceeded its configured file-size ceiling. "
            "Narrow the result or raise batch_exports.max_file_mb after "
            "checking available disk space.")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    progress(int(out.get("rows") or 0))
    out["note"] = (
        str(out.get("note") or "")
        + " The tool was re-run when this batch started, so the CSV reflects "
          "that live-data time rather than a snapshot of the earlier chat preview."
    ).strip()
    return out


def filename(tool: str, rows: int, truncated: bool,
             today: Optional[dt.date] = None) -> str:
    """A name that still tells the truth a week later, in a downloads
    folder, with no UI around it."""
    stem = re.sub(r"[^a-z0-9]+", "_", str(tool or "export").lower()).strip("_")
    day = (today or dt.date.today()).isoformat()
    suffix = f"_TRUNCATED_at_{rows}_rows" if truncated else f"_{rows}_rows"
    return f"{stem}_{day}{suffix}.csv"


def _call_filtered(fn: Callable, args: dict, extra: dict) -> Any:
    """Call a pack method with only the arguments it actually accepts.

    Card arguments are whatever the model passed, plus scope fields the
    GUI adds. A tool that does not take `ledger` must not fail an export
    because the chat turn happened to carry one.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**{**args, **extra})
    accepts = {p.name for p in sig.parameters.values()
               if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(**{**args, **extra})
    merged = {**{k: v for k, v in (args or {}).items() if k in accepts},
              **{k: v for k, v in extra.items() if k in accepts}}
    return fn(**merged)


def build_registry(engine=None, ar=None, modules=None, report_runner=None,
                   coupa=None, qas=None) -> dict[str, Callable]:
    """tool name -> (args, row_cap) -> full payload.

    Only tools that can return MORE rows when asked need an entry; the
    endpoint falls back to re-running nothing and exporting the payload
    the caller already has.
    """
    reg: dict[str, Callable] = {}
    if engine is not None:
        reg["get_trial_balance"] = lambda a, cap: _call_filtered(
            engine.trial_balance, a, {"max_rows": cap})
        reg["run_sql"] = lambda a, cap: _call_filtered(
            engine.run_sql, a, {"max_rows": cap, "row_ceiling": cap})
        reg["get_budget_variance"] = lambda a, cap: _call_filtered(
            engine.budget_variance, a, {"top": cap})
        reg["drill_to_journals"] = lambda a, cap: _call_filtered(
            engine.drill_to_journals, a, {"max_rows": cap})
        reg["rollup_trial_balance"] = lambda a, cap: _call_filtered(
            engine.rollup_trial_balance, a, {})
        reg["compare_trial_balance"] = lambda a, cap: _call_filtered(
            engine.compare_trial_balance, a, {"top": cap})
        reg["explain_balance_change"] = lambda a, cap: _call_filtered(
            engine.explain_balance_change, a, {"top": cap})
        reg["search_accounts"] = lambda a, cap: _call_filtered(
            engine.search_accounts, a, {"max_rows": cap})
    if ar is not None:
        reg["get_invoice_lifecycle"] = lambda a, cap: _call_filtered(
            ar.invoice_lifecycle, a, {})
        reg["get_dso_trend"] = lambda a, cap: _call_filtered(
            ar.dso_trend, a, {})
        reg["get_cash_outlook"] = lambda a, cap: _call_filtered(
            ar.cash_outlook, a, {})
        reg["get_customer_intelligence"] = lambda a, cap: _call_filtered(
            ar.customer_intelligence, a, {"n": cap})
        reg["get_invoice_totals"] = lambda a, cap: _call_filtered(
            ar.invoice_totals, a, {})
        reg["get_ar_aging"] = lambda a, cap: _call_filtered(
            ar.aging, a, {"detail": True, "max_rows": cap})
        reg["get_top_billing_customers"] = lambda a, cap: _call_filtered(
            ar.top_billing_customers, a, {"n": cap})
        reg["get_customer_ar"] = lambda a, cap: _call_filtered(
            ar.customer, a, {})
        reg["get_billing_workbench"] = lambda a, cap: _call_filtered(
            ar.billing_workbench, a, {})
    if modules is not None:
        reg["get_open_payables"] = lambda a, cap: _call_filtered(
            modules.open_payables, a, {})
        reg["get_vendor_intelligence"] = lambda a, cap: _call_filtered(
            modules.vendor_intelligence, a, {"n": cap})
        reg["get_duplicate_payments"] = lambda a, cap: _call_filtered(
            modules.duplicate_payments, a, {})
        reg["get_vendor_payments"] = lambda a, cap: _call_filtered(
            modules.vendor_payments, a, {"n": cap})
        reg["get_asset_register"] = lambda a, cap: _call_filtered(
            modules.asset_register, a, {})
        reg["get_project_costs"] = lambda a, cap: _call_filtered(
            modules.project_costs, a, {})
    if report_runner is not None:
        reg["run_report"] = lambda a, cap: _call_filtered(
            report_runner.run, a, {})
    if qas is not None:
        reg["run_ps_query"] = lambda a, cap: _call_filtered(
            qas.execute, a, {"max_rows": cap})
    if coupa is not None:
        reg["get_coupa_invoices"] = lambda a, cap: _call_filtered(
            coupa.invoices, a, {"max_rows": cap})
        reg["get_coupa_stuck_approvals"] = lambda a, cap: _call_filtered(
            coupa.stuck_approvals, a, {})
        reg["get_coupa_budget_lines"] = lambda a, cap: _call_filtered(
            coupa.budget_lines, a, {})
        reg["get_coupa_rni"] = lambda a, cap: _call_filtered(
            coupa.received_not_invoiced, a, {"display_rows": cap})
        reg["get_coupa_supplier_spend"] = lambda a, cap: _call_filtered(
            coupa.supplier_spend, a, {"top_n": cap})
    return reg


def export(tool: str, args: dict, registry: dict, *,
           payload: Any = None, row_cap: int = EXPORT_MAX_ROWS,
           today: Optional[dt.date] = None) -> dict:
    """Produce {csv, filename, rows, truncated, rerun} for one card.

    Re-runs the tool at the export ceiling when it is registered; falls
    back to the payload the caller already holds (still a real export,
    just of the rows it has) and says which happened.
    """
    cap = max(1, min(int(row_cap or EXPORT_MAX_ROWS), EXPORT_MAX_ROWS))
    runner = registry.get(str(tool))
    rerun = False
    if runner is not None:
        payload = runner(dict(args or {}), cap)
        rerun = True
    if payload is None:
        raise ExportError(
            f"{tool} has nothing to export: no rows were captured for this "
            "card and the tool is not re-runnable. Re-ask the question, "
            "then export the fresh result.")
    export_view = str((args or {}).get("_export_view") or "").strip()
    if tool == "get_coupa_rni" and export_view == "receipt_export_state":
        evidence = (payload.get("export_evidence") or {}
                    if isinstance(payload, dict) else {})
        records = evidence.get("receipt_transactions") or []
        table = (_table_from(records, "receipt_transactions")
                 if isinstance(records, list) and records else None)
        source_total = evidence.get("receipt_transaction_count")
        source_truncated = evidence.get("display_truncated") is True
    else:
        table = tabular(payload)
        if tool == "get_coupa_rni" and isinstance(payload, dict):
            population = payload.get("population") or {}
            source_total = population.get("candidate_count")
            source_truncated = population.get("display_truncated") is True
        else:
            source_total = None
            source_truncated = False
    if not table or not table["rows"]:
        raise ExportError(
            f"{tool} returned no table to export — this result is a single "
            "figure or a status, not a row set.")
    visible_total = len(table["rows"])
    total = (source_total if isinstance(source_total, int)
             and not isinstance(source_total, bool)
             and source_total >= visible_total else visible_total)
    truncated = source_truncated or total > cap or visible_total > cap
    if visible_total > cap:
        table = {**table, "rows": table["rows"][:cap]}
    rows = len(table["rows"])
    return {
        "csv": to_csv(table),
        "filename": filename(
            f"{tool}_{export_view}" if export_view else tool,
            rows, truncated, today),
        "rows": rows, "columns": len(table["columns"]),
        "truncated": truncated, "row_cap": cap, "rerun": rerun,
        "note": (f"{total:,} rows matched; export contains {rows:,} of "
                 f"{total:,} rows. The source "
                 "or export display ceiling was reached. Narrow the scope "
                 "and export again for the rest — the filename records "
                 "the cut."
                 if truncated else
                 ("Full result re-run server-side, beyond the rows shown "
                  "on screen." if rerun else
                  "Exported the rows captured for this card.")),
    }
