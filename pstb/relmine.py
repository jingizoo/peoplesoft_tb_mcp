"""Joins nobody declared, measured from the data itself.

The PeopleSoft primary carries thousands of declared constraints and a
PeopleTools layer that documents its records. The custom schemas carry
neither: no foreign keys, no comments, names that lie — and every
reconciliation-shaped question ("which stage rows never made it to the
header", "does the subledger tie to the parent") depends on exactly the
join knowledge those schemas refuse to write down. join_path can only
walk edges that exist; on a constraint-free schema it walks nothing.

This module mines the missing edges the only way that cannot be fooled by
a name: by measuring value containment. If a bounded sample of distinct
values from A.INVOICE_NO is (nearly) all present in B.INV_NBR, then A
plausibly references B — whatever either column is called. Classic
inclusion-dependency mining, adapted to this codebase's rules:

* CANDIDATES come from the artifact, not the wire: only tables the
  profiler measured POPULATED, never a shadow copy, ranked by value
  score and capped. Column pairs must be type-compatible and key-shaped
  (shared name, or both carrying a key suffix); amount- and date-shaped
  columns are never candidates — sums that happen to collide are not
  relationships.
* PROBES are bounded and dialect-safe: one DISTINCT sample per child
  column (LIMIT/FETCH FIRST), one IN-list containment count per
  direction, a hard cap on total probe queries per build. No full-table
  scan is ever issued.
* EVIDENCE is retained; VALUES are not. The artifact stores overlap
  percentages, sample sizes and directions — never a sampled value, so
  nothing row-level rides into a file that outlives the connection.
* A declared foreign key silences the miner for its column pair: mined
  edges answer only where the schema is silent, and a mined edge is
  ALWAYS labeled derived/likely so no consumer can mistake measurement
  for declaration.

Confidence is honest about what containment can and cannot prove: 100%
overlap on 200 sampled values is strong evidence of a reference and no
evidence of intent. Every edge carries its numbers, and join_path
surfaces them with the caveat attached.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

# Columns that JOIN: identifiers, codes, numbers-as-names. The suffixes are
# deliberately conservative — a false candidate costs two probe queries,
# but a false EDGE costs a wrong reconciliation later.
_KEY_SUFFIX = re.compile(
    r"(?:^|_)(?:ID|CD|CODE|NBR|NO|NUM|KEY|SETID|UNIT|OPRID|EMPLID|"
    r"BU|GUID|UUID|REF)$")
# Columns that must never be join candidates however their names read:
# amounts and quantities collide numerically without meaning anything,
# and dates match everything in the same fiscal month.
_VALUE_SHAPED = re.compile(
    r"(?:^|_)(?:AMT|AMOUNT|QTY|PCT|RATE|PRICE|TOTAL|SUM|BAL|BALANCE)"
    r"(?:_|$)|^(?:DT|DATE)$|(?:^|_)(?:DT|DATE|DTTM|TIME|TIMESTAMP)$")
_NUMERIC_TYPES = frozenset(
    {"NUMBER", "INTEGER", "INT", "BIGINT", "SMALLINT", "DECIMAL",
     "NUMERIC", "FLOAT", "REAL"})
_SAFE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_TEXT_TYPES = frozenset(
    {"VARCHAR2", "VARCHAR", "NVARCHAR2", "CHAR", "NCHAR", "TEXT",
     "CLOB", "STRING"})


def _base_type(declared: object) -> str:
    text = str(declared or "").strip().upper()
    return re.split(r"[^A-Z0-9]", text)[0] if text else ""


def _type_family(declared: object) -> str:
    base = _base_type(declared)
    if base in _NUMERIC_TYPES:
        return "numeric"
    if base in _TEXT_TYPES:
        return "text"
    return ""


def is_key_shaped(column_name: str) -> bool:
    name = str(column_name or "").strip().upper()
    if not name or _VALUE_SHAPED.search(name):
        return False
    return bool(_KEY_SUFFIX.search(name))


def candidate_pairs(tables: Iterable[Mapping], *,
                    declared: Iterable[tuple] = (),
                    max_pairs: int = 120) -> list:
    """Column pairs worth the price of two probe queries, best first.

    ``tables``: [{schema, name, node_id, value_score, columns:
    [{name, data_type}]}] — already restricted by the caller to populated,
    non-shadow objects. ``declared``: {(schema_a, table_a, col_a,
    schema_b, table_b, col_b)} for every declared FK pair, in both
    orders — the miner answers only where the schema is silent.

    Two candidate classes, in priority order:
    1. SAME-NAME: the same key-shaped column name on two tables
       (BUSINESS_UNIT, VOUCHER_ID — the PeopleSoft idiom).
    2. KEY-SUFFIX: differently named, type-compatible, both key-shaped
       (INVOICE_NO vs INV_NBR — the custom-schema idiom). To keep the
       candidate count from exploding quadratically, the non-identical
       pairing additionally requires a shared alphabetic trigram
       (INV/VOU/...) between the names.
    """
    declared_set = {tuple(str(part).upper() for part in entry)
                    for entry in declared or ()}
    prepared = []
    for table in tables or ():
        columns = []
        for column in table.get("columns") or ():
            name = str(column.get("name") or "").strip().upper()
            family = _type_family(column.get("data_type"))
            if not name or not family or _VALUE_SHAPED.search(name):
                continue
            columns.append((name, family, bool(_KEY_SUFFIX.search(name))))
        prepared.append((table, columns))

    def emit(left, lcol, right, rcol, priority):
        key = (str(left.get("schema") or "").upper(),
               str(left.get("name") or "").upper(), lcol[0],
               str(right.get("schema") or "").upper(),
               str(right.get("name") or "").upper(), rcol[0])
        if key in declared_set:
            return None
        return {
            "priority": priority,
            "left": {"schema": left.get("schema"), "table": left.get("name"),
                     "node_id": left.get("node_id"), "column": lcol[0]},
            "right": {"schema": right.get("schema"),
                      "table": right.get("name"),
                      "node_id": right.get("node_id"), "column": rcol[0]},
        }

    out = []
    for i, (left, lcols) in enumerate(prepared):
        for right, rcols in prepared[i + 1:]:
            if left.get("node_id") == right.get("node_id"):
                continue
            for lcol in lcols:
                for rcol in rcols:
                    if lcol[1] != rcol[1]:
                        continue
                    if lcol[0] == rcol[0]:
                        if not lcol[2]:
                            continue
                        pair = emit(left, lcol, right, rcol, 0)
                    else:
                        if not (lcol[2] and rcol[2]):
                            continue
                        if not _shares_trigram(lcol[0], rcol[0]):
                            continue
                        pair = emit(left, lcol, right, rcol, 1)
                    if pair is not None:
                        out.append(pair)
    out.sort(key=lambda p: (
        p["priority"],
        -(_score_of(p["left"], tables) + _score_of(p["right"], tables)),
        p["left"]["table"] or "", p["left"]["column"] or "",
        p["right"]["table"] or "", p["right"]["column"] or ""))
    return out[:max_pairs]


def _score_of(side: Mapping, tables) -> float:
    for table in tables:
        if table.get("node_id") == side.get("node_id"):
            try:
                return float(table.get("value_score") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _shares_trigram(a: str, b: str) -> bool:
    grams = {a[i:i + 3] for i in range(len(a) - 2)
             if a[i:i + 3].isalpha()}
    return any(b[i:i + 3] in grams for i in range(len(b) - 2))


def classify_overlap(sampled: int, contained: int, *,
                     min_sample: int = 20) -> tuple:
    """(confidence, overlap_pct) for one probe direction, or ("", 0.0).

    Below ``min_sample`` distinct values nothing is claimed: three
    matching status codes prove only that both tables have status codes.
    """
    if sampled < min_sample or sampled <= 0:
        return "", 0.0
    pct = contained / sampled
    if pct >= 0.98:
        return "likely", round(pct, 4)
    if pct >= 0.90:
        return "possible", round(pct, 4)
    return "", round(pct, 4)


def bounded_select(dialect: str, inner: str, cap: int) -> str:
    """``inner`` capped to ``cap`` rows in the current dialect's syntax.

    SQL Server has no LIMIT; emitting one produced invalid T-SQL from
    both new query builders (review finding). The OFFSET/FETCH form
    needs an ORDER BY, and (SELECT NULL) is the standard no-order anchor.
    """
    d = str(dialect or "").lower()
    n = max(int(cap), 1)
    if d == "oracle":
        return f"{inner} FETCH FIRST {n} ROWS ONLY"
    if d == "sqlserver":
        return (f"{inner} ORDER BY (SELECT NULL) "
                f"OFFSET 0 ROWS FETCH NEXT {n} ROWS ONLY")
    return f"{inner} LIMIT {n}"


def probe_containment(db, child: Mapping, parent: Mapping, *,
                      sample_rows: int = 100) -> dict:
    """One direction: are child's values present among parent's?

    Two queries whose READS are bounded on the inside, not just whose
    outputs are capped (review finding: 'SELECT DISTINCT col ... FETCH
    FIRST 100' must read the whole table before Oracle's stop key when
    the column is low-cardinality — the cap has to sit under the
    DISTINCT, not over it):
    1. DISTINCT over a bounded inner read of the child column.
    2. COUNT DISTINCT over a bounded inner read of the parent rows
       matching the sampled values, bound as named IN-list parameters —
       values transit the connection and are never retained.
    A sample below classify_overlap's minimum is returned as-is; callers
    skip the reverse probe rather than spend two more queries on a
    result the classifier will refuse anyway.
    """
    dialect = str(getattr(db, "dialect", "")).lower()
    # Identifiers come from the catalog, which read them from the
    # database's own dictionary — but they are still interpolated into
    # SQL, so they pass the same bare-identifier gate the profiler's
    # sample branch uses. Anything unusual is skipped, never quoted.
    for part in (child.get("schema"), child.get("table"), child.get("column"),
                 parent.get("schema"), parent.get("table"),
                 parent.get("column")):
        if part and not _SAFE_IDENT.fullmatch(str(part)):
            return {"sampled": 0, "contained": 0,
                    "skipped": f"unsafe identifier {str(part)[:40]!r}"}
    child_col = str(child["column"])
    child_table = f'{child["schema"]}.{child["table"]}' \
        if child.get("schema") else str(child["table"])
    parent_col = str(parent["column"])
    parent_table = f'{parent["schema"]}.{parent["table"]}' \
        if parent.get("schema") else str(parent["table"])
    # Caps are inlined as validated integers, not bound: whether a bind
    # is legal inside FETCH FIRST varies by driver version, and a
    # sampling cap is configuration, not data. The inner read is bounded
    # at 50x the sample so a repetitive column still yields distincts
    # without the read ever being unbounded.
    cap = max(int(sample_rows), 1)
    inner = bounded_select(
        dialect,
        f"SELECT {child_col} AS v FROM {child_table} "
        f"WHERE {child_col} IS NOT NULL",
        cap * 50)
    rows, _ = db.query(
        f"SELECT DISTINCT v FROM ({inner}) s",
        {}, max_rows=cap)
    values = [row.get("v") for row in rows if row.get("v") is not None]
    if not values:
        return {"sampled": 0, "contained": 0}
    binds = {f"v{i}": value for i, value in enumerate(values)}
    placeholders = ",".join(f":v{i}" for i in range(len(values)))
    parent_inner = bounded_select(
        dialect,
        f"SELECT {parent_col} AS v FROM {parent_table} "
        f"WHERE {parent_col} IN ({placeholders})",
        len(values) * 50)
    counted, _ = db.query(
        f"SELECT COUNT(DISTINCT v) AS n FROM ({parent_inner}) s",
        binds, max_rows=1)
    contained = int((counted[0].get("n") if counted else 0) or 0)
    return {"sampled": len(values), "contained": contained}
