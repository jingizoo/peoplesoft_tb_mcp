"""Read-only verification that the port actually happened.

verify_build: after App Designer Build on 9.2, confirm each planned table
physically exists with every expected column — catching the classic failure
where the project copied but Build was skipped, which Data Mover would later
turn into a confusing import error.

reconcile: after data load, row counts plus SUM() of every numeric field on
both instances. Sums catch what counts cannot — the truncated load that
dropped the last partial batch, or the mapping that zeroed a column.
"""
from __future__ import annotations

from .catalog import RecordCatalog
from .spec import DATA_DMS, DATA_MAPPED_SQL, PlanItem


def verify_build(items: list, source: RecordCatalog,
                 target: RecordCatalog) -> list:
    out = []
    for it in items:
        if not it.needs_definition_port:
            continue
        rec = source.record(it.recname)
        if rec is None:
            out.append({"recname": it.recname, "ok": False,
                        "problem": "source definition unavailable"})
            continue
        if not rec.has_table and it.rectype != 7:
            # Views/subrecords/derived: definition-only; physical check n/a.
            out.append({"recname": it.recname, "ok": True,
                        "check": "definition-only record, no table expected"})
            continue
        cols = target.physical_columns(rec.table_name)
        if not cols:
            out.append({"recname": it.recname, "table": rec.table_name,
                        "ok": False,
                        "problem": "table not found in 9.2 — Build not run, "
                                   "or the catalog is unreadable"})
            continue
        expected = {f.fieldname.upper() for f in rec.fields
                    if f.fieldtype is not None}
        missing = sorted(expected - cols)
        out.append({
            "recname": it.recname, "table": rec.table_name,
            "ok": not missing,
            **({"missing_columns": missing} if missing else {}),
            "extra_columns": sorted(cols - expected),  # informational: 9.2 merge may add
        })
    return out


def reconcile(items: list, source: RecordCatalog, target: RecordCatalog,
              recnames: list | None = None) -> list:
    wanted = {r.upper() for r in recnames} if recnames else None
    out = []
    for it in items:
        if it.data_plan not in (DATA_DMS, DATA_MAPPED_SQL):
            continue
        if wanted is not None and it.recname not in wanted:
            continue
        out.append(_reconcile_one(it, source, target))
    return out


def _reconcile_one(it: PlanItem, source: RecordCatalog,
                   target: RecordCatalog) -> dict:
    rec = source.record(it.recname)
    if rec is None or not rec.has_table:
        return {"recname": it.recname, "ok": False,
                "problem": "no source table to reconcile"}
    t = rec.table_name
    result: dict = {"recname": it.recname, "table": t}
    try:
        src_n = source.table_row_count(t)
    except Exception as e:
        return {**result, "ok": False, "problem": f"source count failed: {e}"}
    try:
        tgt_n = target.table_row_count(t)
    except Exception as e:
        return {**result, "ok": False, "source_rows": src_n,
                "problem": f"target count failed (table missing or no grant): {e}"}
    result.update(source_rows=src_n, target_rows=tgt_n)
    mismatches = []
    if src_n != tgt_n:
        mismatches.append(f"row count {src_n} -> {tgt_n}")
    # Numeric sums only when both sides have the column — a drift merge can
    # legitimately drop one; the shape diff already documents that.
    tgt_cols = target.physical_columns(t)
    for col in rec.numeric_fields():
        if tgt_cols and col.upper() not in tgt_cols:
            continue
        try:
            s, g = source.column_sum(t, col), target.column_sum(t, col)
        except Exception as e:
            mismatches.append(f"{col}: sum probe failed: {e}")
            continue
        if abs(s - g) > 1e-6:
            mismatches.append(f"{col}: sum {s} -> {g}")
    result["ok"] = not mismatches
    if mismatches:
        result["mismatches"] = mismatches
    return result
