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
import re
from typing import Any, Callable, Optional

# Ceiling for one export. High enough that a real population fits, low
# enough that a mistyped query cannot pull a multi-million-row table
# through the web process.
EXPORT_MAX_ROWS = 50_000

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
