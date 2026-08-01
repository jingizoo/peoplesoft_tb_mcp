"""Count, on the real 9.1 data, how many rows a mapping would damage.

The mapping engine says "this column shortens from 30 to 10, values may
truncate". Pre-flight answers the question that actually decides the
migration: *how many rows*, right now, in this database. Zero means proceed;
non-zero means fix the mapping or accept a documented loss.

Every probe is a read-only aggregate against the 9.1 source, filtered by the
mapping's WHERE clause so the numbers describe the rows that will really move.
Probes that a dialect cannot express are reported as unavailable rather than
skipped silently — an unrun check must never read as a passed one.
"""
from __future__ import annotations

from .catalog import RecordCatalog
from .convert import RecordMapping
from .spec import BLOCKER, INFO, WARNING, RecordDef, type_family

# Probe kinds.
TRUNCATION = "truncation"
ROUNDING = "rounding"
OVERFLOW = "overflow"
KEY_COLLISION = "key_collision"


def _count(catalog: RecordCatalog, table: str, predicate: str,
           where: str = "") -> int:
    clauses = [c for c in (predicate, where) if c]
    sql = f"SELECT COUNT(*) AS N FROM {catalog.prefix}{table}"
    if clauses:
        sql += " WHERE " + " AND ".join(f"({c})" for c in clauses)
    rows, _ = catalog.db.query(sql, {}, max_rows=1)
    return int(rows[0]["n"]) if rows else 0


def _probe(kind: str, column: str, at_risk: int, total: int, detail: str,
           severity: str) -> dict:
    return {"probe": kind, "column": column, "rows_at_risk": at_risk,
            "rows_examined": total, "detail": detail,
            "severity": severity if at_risk else INFO,
            "ok": at_risk == 0}


def run(mapping: RecordMapping, src: RecordDef, tgt: RecordDef,
        source: RecordCatalog) -> dict:
    """Probe one record's mapping. Returns per-probe counts plus a verdict."""
    sfields = {f.fieldname.upper(): f for f in src.fields
               if f.fieldtype is not None}
    tfields = {f.fieldname.upper(): f for f in tgt.fields
               if f.fieldtype is not None}
    table = mapping.source_table
    probes: list = []
    unavailable: list = []

    try:
        total = _count(source, table, "", mapping.where)
    except Exception as e:
        return {"recname": mapping.recname, "ok": False,
                "error": f"source table unreadable: {e}"}

    for cm in mapping.columns:
        if not cm.sourced:
            continue
        sf, tf = sfields.get(cm.source_expr), tfields.get(cm.target_column)
        if sf is None or tf is None:
            continue
        fam = type_family(tf.fieldtype)
        try:
            if fam == "char" and tf.length < sf.length:
                n = _count(source, table,
                           f"LENGTH({cm.source_expr}) > {tf.length}",
                           mapping.where)
                probes.append(_probe(
                    TRUNCATION, cm.target_column, n, total,
                    f"values longer than {tf.length} characters "
                    f"(9.1 allows {sf.length})", WARNING))
            elif fam == "num":
                if tf.decimalpos < sf.decimalpos:
                    n = _count(source, table,
                               f"{cm.source_expr} <> "
                               f"ROUND({cm.source_expr}, {tf.decimalpos})",
                               mapping.where)
                    probes.append(_probe(
                        ROUNDING, cm.target_column, n, total,
                        f"values with more than {tf.decimalpos} decimals "
                        f"(9.1 allows {sf.decimalpos})", WARNING))
                int_digits = tf.length - tf.decimalpos
                if int_digits > 0 and int_digits < (sf.length - sf.decimalpos):
                    limit = 10 ** int_digits
                    n = _count(source, table,
                               f"ABS({cm.source_expr}) >= {limit}",
                               mapping.where)
                    probes.append(_probe(
                        OVERFLOW, cm.target_column, n, total,
                        f"absolute values at or above {limit} exceed the 9.2 "
                        f"precision", BLOCKER))
        except Exception as e:
            unavailable.append({"column": cm.target_column,
                                "reason": str(e)[:200]})

    probes.append(_key_collision(mapping, source, table, total, unavailable))

    at_risk = [p for p in probes if not p["ok"]]
    blockers = [p for p in at_risk if p["severity"] == BLOCKER]
    return {
        "recname": mapping.recname,
        "source_table": table,
        "rows_examined": total,
        "where": mapping.where,
        "ok": not at_risk and not unavailable,
        "blocking": bool(blockers),
        "probes": probes,
        "unavailable_probes": unavailable,
        "summary": (f"{len(at_risk)} of {len(probes)} probes found rows at "
                    f"risk" + (f", {len(blockers)} blocking" if blockers else "")
                    + (f"; {len(unavailable)} probe(s) could not run"
                       if unavailable else "")),
    }


def _key_collision(mapping: RecordMapping, source: RecordCatalog, table: str,
                   total: int, unavailable: list) -> dict:
    """Rows that would collide on the 9.2 key.

    Grouping uses the SOURCE expressions behind the 9.2 key columns, so it
    measures what the insert will actually do. A key fed by a constant
    (a 9.2 key column with no 9.1 source) collapses every row onto one key —
    reported as the whole table at risk rather than as a passing probe.
    """
    key_cols = [c for c in mapping.columns if c.is_key]
    if not key_cols:
        return _probe(KEY_COLLISION, "(none)", 0, total,
                      "9.2 table declares no key columns", INFO)
    constant = [c.target_column for c in key_cols if not c.sourced
                and c.kind != "expression"]
    if constant:
        return _probe(KEY_COLLISION, ", ".join(constant), max(total - 1, 0),
                      total,
                      f"key column(s) {', '.join(constant)} have no 9.1 "
                      f"source and would receive the same constant for every "
                      f"row", BLOCKER)
    exprs = [c.source_expr for c in key_cols]
    group = ", ".join(exprs)
    where = f" WHERE {mapping.where}" if mapping.where else ""
    sql = (f"SELECT COUNT(*) AS N FROM (SELECT {group}, COUNT(*) AS C "
           f"FROM {source.prefix}{table}{where} GROUP BY {group} "
           f"HAVING COUNT(*) > 1) T")
    try:
        rows, _ = source.db.query(sql, {}, max_rows=1)
        n = int(rows[0]["n"]) if rows else 0
    except Exception as e:
        unavailable.append({"column": "key collision", "reason": str(e)[:200]})
        return _probe(KEY_COLLISION, group, 0, total,
                      "probe could not run", INFO)
    return _probe(KEY_COLLISION, group, n, total,
                  f"distinct 9.2 key values carrying more than one 9.1 row",
                  BLOCKER)
