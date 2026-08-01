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
              recnames: list | None = None, mappings: dict | None = None) -> list:
    wanted = {r.upper() for r in recnames} if recnames else None
    mappings = mappings or {}
    out = []
    for it in items:
        if it.data_plan not in (DATA_DMS, DATA_MAPPED_SQL):
            continue
        if wanted is not None and it.recname not in wanted:
            continue
        out.append(_reconcile_one(it, source, target,
                                  mappings.get(it.recname)))
    return out


def _reconcile_one(it: PlanItem, source: RecordCatalog,
                   target: RecordCatalog, mapping=None) -> dict:
    """Counts and sums on both sides.

    With a mapping in play the two sides are NOT symmetric: the target column
    may be renamed, and the load may have carried only the rows the mapping's
    filter selects. Comparing raw table totals in that case reports a failure
    for a correct migration, so the source side is filtered and the columns
    are paired through the mapping.
    """
    rec = source.record(it.recname)
    if rec is None or not rec.has_table:
        return {"recname": it.recname, "ok": False,
                "problem": "no source table to reconcile"}
    src_t = rec.table_name
    tgt_t = mapping.target_table if mapping else src_t
    where = mapping.where if mapping else ""
    result: dict = {"recname": it.recname, "table": src_t}
    if where:
        result["source_filter"] = where
    try:
        src_n = source.table_row_count(src_t, where)
    except Exception as e:
        return {**result, "ok": False, "problem": f"source count failed: {e}"}
    try:
        tgt_n = target.table_row_count(tgt_t)
    except Exception as e:
        return {**result, "ok": False, "source_rows": src_n,
                "problem": f"target count failed (table missing or no grant): {e}"}
    result.update(source_rows=src_n, target_rows=tgt_n)
    mismatches = []
    if src_n != tgt_n:
        mismatches.append(f"row count {src_n} -> {tgt_n}")

    numeric = set(rec.numeric_fields())
    if mapping is not None:
        pairs = [(t, s) for t, s in mapping.sourced_pairs() if s in numeric]
        # Unverifiable columns are numeric on the TARGET side — a 9.2-only
        # column is absent from the source's numeric list by definition, so
        # checking against it would report nothing and quietly imply the
        # column had been verified.
        tgt_rec = target.record(it.recname)
        tgt_numeric = set(tgt_rec.numeric_fields()) if tgt_rec else set()
        unverifiable = [c.target_column for c in mapping.columns
                        if not c.sourced and c.target_column in tgt_numeric]
        if unverifiable:
            result["unverifiable_columns"] = sorted(unverifiable)
    else:
        pairs = [(c, c) for c in sorted(numeric)]
    tgt_cols = target.physical_columns(tgt_t)
    for tcol, scol in pairs:
        if tgt_cols and tcol.upper() not in tgt_cols:
            continue
        try:
            s = source.column_sum(src_t, scol, where)
            g = target.column_sum(tgt_t, tcol)
        except Exception as e:
            mismatches.append(f"{tcol}: sum probe failed: {e}")
            continue
        if abs(s - g) > 1e-6:
            label = tcol if tcol == scol else f"{scol} -> {tcol}"
            mismatches.append(f"{label}: sum {s} -> {g}")
    result["ok"] = not mismatches
    if mismatches:
        result["mismatches"] = mismatches
    return result
