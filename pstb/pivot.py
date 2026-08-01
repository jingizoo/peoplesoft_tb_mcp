"""Consolidate many rows into one cross-tab, with the arithmetic done here.

"How has ICICI's revenue moved over the last six months" wants one table:
the customer down the side, the months across the top, and the totals and
movement at the edges. The data for that was always reachable — one GROUP BY
returns every period at once — but the answer was not, for a reason that had
nothing to do with SQL.

Every total, delta and percentage in such a table is a DERIVED figure: it
appears in no tool result, because nothing computed it. The number guard in
guards.py rejects exactly that, and it is right to — a model stating a figure
no tool produced is the failure mode that guard exists to stop. The effect was
that consolidating was penalised: combining results meant computing numbers,
and computed numbers were blocked.

So the arithmetic moves here, the same way policy figures moved out of the
model's head and into a bind. The server pivots, sums, differences and
percentages; every one of those numbers is in the payload; the guard grounds
them because they are now facts a tool produced rather than claims a model
made. What the model contributes is the question's shape — which field is the
row, which is the column, which is the value — and the narration afterwards.

Sparse input is normal and is not an error: a customer with no invoice in
March has no March row, and that cell is a true zero rather than a gap.
"""
from __future__ import annotations

from typing import Optional


class PivotError(RuntimeError):
    pass


MAX_COLUMNS = 60           # a year of months, a quarter of days — not a scan
MAX_ROWS = 500


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_key(label):
    """Order columns numerically when they are numbers, else as text.

    Periods arrive as 1..12 and must not sort as "1, 10, 11, 12, 2" — a trend
    read in that order is not a trend.
    """
    try:
        return (0, float(label), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(label))


def pivot_rows(rows: list, row_field: str, column_field: str,
               value_field: str, totals: bool = True,
               movement: bool = True) -> dict:
    """Cross-tabulate flat rows into row x column cells with derived edges.

    rows          flat records, each carrying the three named fields
    row_field     what goes down the side (customer, account, department)
    column_field  what goes across the top (period, month, year)
    value_field   the measure to sum
    """
    if not isinstance(rows, list):
        raise PivotError("pivot needs a list of rows")
    missing = [f for f in (row_field, column_field, value_field) if not f]
    if missing:
        raise PivotError(
            "pivot needs row_field, column_field and value_field — name the "
            "column of your query that supplies each")
    if not rows:
        return {"columns": [], "rows": [], "row_count": 0,
                "note": "the query returned no rows, so there is nothing to "
                        "consolidate — say so rather than reporting zeros"}

    sample = rows[0]
    for field in (row_field, column_field, value_field):
        if field not in sample:
            raise PivotError(
                f"the query result has no column {field!r}; it returned "
                f"{', '.join(sorted(sample))}. Alias your SELECT so the "
                "row, column and value fields are named.")

    cells: dict = {}
    col_labels: list = []
    row_labels: list = []
    skipped_non_numeric = 0
    for record in rows:
        r = record.get(row_field)
        c = record.get(column_field)
        v = _num(record.get(value_field))
        if v is None:
            skipped_non_numeric += 1
            continue
        r = "(none)" if r is None else r
        c = "(none)" if c is None else c
        if r not in row_labels:
            row_labels.append(r)
        if c not in col_labels:
            col_labels.append(c)
        cells[(r, c)] = cells.get((r, c), 0.0) + v

    col_labels.sort(key=_sort_key)
    if len(col_labels) > MAX_COLUMNS:
        raise PivotError(
            f"that would be {len(col_labels)} columns; {MAX_COLUMNS} is the "
            "limit. Narrow the range, or put the wider dimension down the "
            "side instead of across the top.")
    truncated_rows = len(row_labels) > MAX_ROWS

    out_rows: list = []
    for label in row_labels[:MAX_ROWS]:
        # A missing cell is a true zero here: the row exists, this column
        # simply had no activity. Reporting it as blank would make a reader
        # wonder whether the query failed for that month.
        values = [round(cells.get((label, c), 0.0), 2) for c in col_labels]
        entry = {"row": label, "values": values}
        if totals:
            entry["total"] = round(sum(values), 2)
        if movement and len(values) >= 2:
            # First column to last column, which is what "change over the
            # period" means — but say so, because on sparse data it is easy to
            # misread. A customer that billed heavily in the middle months and
            # nothing at either end has a change of zero, and that is
            # arithmetically true and narratively misleading. Naming the two
            # endpoints lets a reader see which comparison was made.
            first, last = values[0], values[-1]
            entry["change"] = round(last - first, 2)
            # Percent change is undefined from a zero base, and reporting 0%
            # or 100% there is a fabricated fact. Say it is not computable.
            entry["change_pct"] = (round((last - first) / abs(first) * 100, 1)
                                   if first else None)
            entry["direction"] = ("up" if last > first
                                  else "down" if last < first else "flat")
            active = [i for i, v in enumerate(values) if v]
            if active and (active[0] != 0 or active[-1] != len(values) - 1):
                # Endpoints are empty, so first-to-last understates the story.
                entry["activity_span"] = {
                    "first_active_column": col_labels[active[0]],
                    "last_active_column": col_labels[active[-1]],
                    "note": ("this row has no activity at one or both ends of "
                             "the range, so 'change' compares two empty or "
                             "partly empty columns — describe the active span "
                             "rather than implying a flat trend"),
                }
        out_rows.append(entry)

    result: dict = {
        "columns": col_labels,
        "column_field": column_field,
        "row_field": row_field,
        "value_field": value_field,
        "rows": out_rows,
        "row_count": len(out_rows),
        "truncated": truncated_rows,
    }
    if totals and out_rows:
        per_column = [round(sum(r["values"][i] for r in out_rows), 2)
                      for i in range(len(col_labels))]
        result["column_totals"] = per_column
        result["grand_total"] = round(sum(per_column), 2)
    if skipped_non_numeric:
        result["skipped_non_numeric_rows"] = skipped_non_numeric
    result["change_basis"] = (
        f"last column ({col_labels[-1]}) minus first column ({col_labels[0]})"
        if len(col_labels) >= 2 else None)
    result["note"] = (
        "Every figure here — each cell, the totals, the change and the "
        "percentage — was computed server-side and is safe to quote directly. "
        "Do NOT recompute them; present this as a table with "
        f"{row_field} down the side and {column_field} across the top. "
        "Empty cells are true zeros: the row exists, that column had no "
        "activity. 'change' is the last column minus the first; where a row "
        "carries activity_span its ends are empty, so report the active span "
        "instead of calling it flat."
    )
    if truncated_rows:
        result["truncation_note"] = (
            f"Only the first {MAX_ROWS} of {len(row_labels)} rows are shown. "
            "Say so, and narrow the question rather than implying this is "
            "the whole picture.")
    return result
