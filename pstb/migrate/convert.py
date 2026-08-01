"""Column-level mapping between a 9.1 table and its reshaped 9.2 counterpart.

Delivered records change shape across releases: columns appear, disappear,
get renamed, get longer or shorter, gain or lose decimals, and — the
dangerous one — the key set changes. This module resolves, for every physical
column of the 9.2 table, where its value comes from, and records the risk
that carries.

Resolution order per target column:
  1. an operator override (`from`, `expr`, or `default` in the overrides file)
  2. the same-named source column                              -> direct
  3. nothing -> PeopleSoft's type default, flagged as synthesized

Nothing here executes SQL. It reads two RecordDefs and an overrides file and
produces a reviewable mapping plus the INSERT-SELECT text that implements it.
Overrides are operator-authored config (same trust level as config.yaml) and
their expressions reach the generated SQL verbatim — that is what makes
arbitrary conversions possible, and why the file belongs under review.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .spec import (BLOCKER, INFO, PS_TYPE_DEFAULT, WARNING, MigrateError,
                   RecordDef, type_family)

# Mapping kinds.
DIRECT = "direct"          # same column name on both sides
RENAMED = "renamed"        # operator mapped a differently-named source column
EXPRESSION = "expression"  # operator supplied SQL
DEFAULTED = "defaulted"    # no source: PeopleSoft type default
OVERRIDE_DEFAULT = "override_default"  # no source: operator-supplied constant

# Risk codes. Risks are (severity, code, message). The code matters because
# some risks are PREDICTIONS the pre-flight probes then MEASURE against real
# data — "this column may truncate" is answered by "…in 3 rows". A measured
# risk must not also count as an unmeasured blocker, or a mapping whose
# probes all came back clean would still report as blocked.
R_TYPE_FAMILY = "type_family"
R_TRUNCATION = "truncation"
R_ROUNDING = "rounding"
R_OVERFLOW = "overflow"
R_KEY_FLAG = "key_flag"
R_KEY_SET = "key_set_change"
R_UNSOURCED_KEY = "unsourced_key"
R_UNSOURCED = "unsourced_column"
R_BAD_OVERRIDE = "bad_override"
R_DROPPED = "dropped_columns"
R_OPERATOR_EXPR = "operator_expression"

# Codes the pre-flight probes count on real data.
MEASURED_CODES = frozenset({R_TRUNCATION, R_ROUNDING, R_OVERFLOW, R_KEY_SET})


@dataclass
class ColumnMap:
    target_column: str
    source_expr: str
    kind: str
    fieldtype: int | None = None
    risks: list = field(default_factory=list)   # (severity, code, message)
    is_key: bool = False

    @property
    def sourced(self) -> bool:
        """True when a real 9.1 column feeds this target column."""
        return self.kind in (DIRECT, RENAMED)

    def to_dict(self) -> dict:
        return {"target_column": self.target_column,
                "source_expr": self.source_expr, "kind": self.kind,
                "is_key": self.is_key,
                "risks": [{"severity": s, "code": c, "message": m}
                          for s, c, m in self.risks]}


@dataclass
class RecordMapping:
    recname: str
    source_table: str
    target_table: str
    columns: list = field(default_factory=list)      # ColumnMap
    dropped_source: list = field(default_factory=list)
    rename_suggestions: dict = field(default_factory=dict)
    where: str = ""
    risks: list = field(default_factory=list)        # record-level (severity, msg)

    def risk_counts(self) -> dict:
        counts: dict = {}
        for sev, _, _ in self.all_risks():
            counts[sev] = counts.get(sev, 0) + 1
        return counts

    def all_risks(self) -> list:
        out = list(self.risks)
        for c in self.columns:
            out.extend(c.risks)
        return out

    @property
    def blocked(self) -> bool:
        return any(sev == BLOCKER for sev, _, _ in self.all_risks())

    def unmeasured_blockers(self) -> list:
        """Blockers pre-flight cannot settle by counting rows — a type-family
        change, a key column with no source, a broken override. These stand
        regardless of what the data looks like; the measurable ones are left
        to the probes so a clean count is allowed to clear them."""
        return [(sev, code, msg) for sev, code, msg in self.all_risks()
                if sev == BLOCKER and code not in MEASURED_CODES]

    def sourced_pairs(self) -> list:
        """(target_column, source_column) for columns fed by a real 9.1
        column — what reconciliation can legitimately compare."""
        return [(c.target_column, c.source_expr) for c in self.columns
                if c.sourced]

    def to_dict(self) -> dict:
        return {
            "recname": self.recname,
            "source_table": self.source_table,
            "target_table": self.target_table,
            "where": self.where,
            "columns": [c.to_dict() for c in self.columns],
            "dropped_source": self.dropped_source,
            "rename_suggestions": self.rename_suggestions,
            "record_risks": [{"severity": s, "code": c, "message": m}
                             for s, c, m in self.risks],
            "risk_counts": self.risk_counts(),
            "blocked": self.blocked,
            "unmeasured_blockers": [
                {"code": c, "message": m}
                for _, c, m in self.unmeasured_blockers()],
        }


# ---- overrides file ------------------------------------------------------
def load_overrides(path: Path) -> dict:
    """Operator-authored mapping overrides, {} when the file does not exist.

    Shape:
      {"PS_VOUCHER": {
          "where": "BUSINESS_UNIT = 'US001'",
          "columns": {
              "NEW_NAME":  {"from": "OLD_NAME"},
              "CONVERTED": {"expr": "TO_NUMBER(OLD_TEXT)"},
              "ADDED_92":  {"default": "'N'"}}}}
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text() or "{}")
    except ValueError as e:
        raise MigrateError(f"Mapping overrides at {p} are not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise MigrateError(f"Mapping overrides at {p} must be a JSON object.")
    return {str(k).upper(): v for k, v in data.items()}


def _physical(rec: RecordDef) -> dict:
    """Columns that physically exist — PSRECFIELD rows without a PSDBFIELD
    row carry no column, so they must never enter a column list."""
    return {f.fieldname.upper(): f for f in rec.fields if f.fieldtype is not None}


def suggest_renames(src_only: list, tgt_only: list, src_fields: dict,
                    tgt_fields: dict) -> dict:
    """Rename candidates for 9.2-only columns: same type family first, then
    name similarity. Suggestions only — a wrong auto-rename silently moves
    data into the wrong column, so nothing here is ever applied."""
    out: dict = {}
    for t in sorted(tgt_only):
        tf = tgt_fields[t]
        scored = []
        for s in src_only:
            sf = src_fields[s]
            if type_family(sf.fieldtype) != type_family(tf.fieldtype):
                continue
            ratio = difflib.SequenceMatcher(None, s, t).ratio()
            scored.append((round(ratio, 3), s))
        scored.sort(reverse=True)
        if scored:
            out[t] = [{"source_column": s, "similarity": r}
                      for r, s in scored[:3]]
    return out


def _pair_risks(tgt_name: str, sf, tf) -> list:
    """What moving sf's values into tf can cost."""
    risks = []
    sfam, tfam = type_family(sf.fieldtype), type_family(tf.fieldtype)
    if sfam != tfam:
        risks.append((BLOCKER, R_TYPE_FAMILY,
                      f"{tgt_name}: type family {sfam} -> {tfam}. Supply an "
                      f"explicit conversion via an 'expr' override; an "
                      f"implicit cast will fail or corrupt values."))
    elif tfam == "char" and tf.length < sf.length:
        risks.append((WARNING, R_TRUNCATION,
                      f"{tgt_name}: character length {sf.length} -> "
                      f"{tf.length}; longer values truncate. Pre-flight "
                      f"counts the rows at risk."))
    elif tfam == "num":
        if tf.decimalpos < sf.decimalpos:
            risks.append((WARNING, R_ROUNDING,
                          f"{tgt_name}: decimals {sf.decimalpos} -> "
                          f"{tf.decimalpos}; values round."))
        if (tf.length - tf.decimalpos) < (sf.length - sf.decimalpos):
            risks.append((WARNING, R_OVERFLOW,
                          f"{tgt_name}: integer digits "
                          f"{sf.length - sf.decimalpos} -> "
                          f"{tf.length - tf.decimalpos}; large values overflow."))
    if sf.is_key != tf.is_key:
        risks.append((INFO, R_KEY_FLAG,
                      f"{tgt_name}: key flag {sf.is_key} -> {tf.is_key}."))
    return risks


def build_mapping(recname: str, src: RecordDef, tgt: RecordDef,
                  overrides: dict | None = None) -> RecordMapping:
    ov = (overrides or {}).get(recname.upper(), {}) or {}
    col_ov = {str(k).upper(): v for k, v in (ov.get("columns") or {}).items()}
    skip = {str(c).upper() for c in (ov.get("skip_columns") or [])}

    sfields, tfields = _physical(src), _physical(tgt)
    mapping = RecordMapping(recname=recname.upper(),
                            source_table=src.table_name,
                            target_table=tgt.table_name,
                            where=str(ov.get("where") or "").strip())

    used_source: set = set()
    for tname in sorted(tfields):
        tf = tfields[tname]
        if tname in skip:
            continue
        rule = col_ov.get(tname) or {}
        cm = ColumnMap(target_column=tname, source_expr="", kind=DEFAULTED,
                       fieldtype=tf.fieldtype, is_key=tf.is_key)

        if "expr" in rule:
            cm.source_expr = str(rule["expr"])
            cm.kind = EXPRESSION
            cm.risks.append((INFO, R_OPERATOR_EXPR,
                             f"{tname}: operator expression, not verified "
                             f"by the pipeline."))
        elif "from" in rule:
            sname = str(rule["from"]).upper()
            sf = sfields.get(sname)
            if sf is None:
                cm.risks.append((BLOCKER, R_BAD_OVERRIDE,
                                 f"{tname}: override maps from {sname}, which "
                                 f"does not exist on the 9.1 table."))
                cm.source_expr = sname
            else:
                cm.source_expr, cm.kind = sname, RENAMED
                used_source.add(sname)
                cm.risks.extend(_pair_risks(tname, sf, tf))
        elif "default" in rule:
            cm.source_expr = str(rule["default"])
            cm.kind = OVERRIDE_DEFAULT
        elif tname in sfields:
            cm.source_expr, cm.kind = tname, DIRECT
            used_source.add(tname)
            cm.risks.extend(_pair_risks(tname, sfields[tname], tf))
        else:
            cm.source_expr = PS_TYPE_DEFAULT.get(tf.fieldtype, "NULL")
            cm.kind = DEFAULTED
            cm.risks.append((BLOCKER if tf.is_key else WARNING,
                             R_UNSOURCED_KEY if tf.is_key else R_UNSOURCED,
                             f"{tname}: new in 9.2 with no 9.1 source; "
                             f"defaulting to {cm.source_expr}."
                             + (" It is a KEY column, so every row would get "
                                "the same value and collide — map it or "
                                "supply an expression."
                                if tf.is_key else "")))
        mapping.columns.append(cm)

    src_only = sorted(set(sfields) - used_source - set(tfields))
    mapping.dropped_source = src_only
    if src_only:
        mapping.risks.append((WARNING, R_DROPPED,
                              "9.1 columns with no home in 9.2: "
                              + ", ".join(src_only)
                              + ". Confirm each is genuinely dropped rather "
                                "than renamed."))
    tgt_unmapped = [c.target_column for c in mapping.columns
                    if c.kind == DEFAULTED]
    if src_only and tgt_unmapped:
        mapping.rename_suggestions = suggest_renames(
            src_only, tgt_unmapped, sfields, tfields)

    # Key-set change is the risk that silently destroys rows: fewer keys in
    # 9.2 means distinct 9.1 rows collapse onto one key.
    skeys = {f.fieldname for f in src.fields if f.is_key and f.fieldtype is not None}
    tkeys = {c.target_column for c in mapping.columns if c.is_key}
    if skeys != tkeys:
        lost = sorted(skeys - tkeys)
        mapping.risks.append((
            WARNING if not lost else BLOCKER, R_KEY_SET,
            f"Key set changed: 9.1 {sorted(skeys)} -> 9.2 {sorted(tkeys)}."
            + (f" 9.1 keys {lost} are no longer keys, so rows distinct only "
               f"by them collide. Pre-flight counts the collisions."
               if lost else "")))
    return mapping


# ---- SQL generation ------------------------------------------------------
def source_reference(mapping: RecordMapping, via: str, dblink: str,
                     staging_prefix: str) -> str:
    """Where the transform reads 9.1 rows from: a database link, or a staging
    table the extract was landed into on the 9.2 side."""
    if via == "dblink":
        return f"{mapping.source_table}@{dblink}"
    if via == "staging":
        return f"{staging_prefix}{mapping.source_table}"
    raise MigrateError(f"migrate.convert_via must be dblink|staging, got {via!r}")


def insert_select(mapping: RecordMapping, via: str = "dblink",
                  dblink: str = "SOURCE91",
                  staging_prefix: str = "STG_") -> str:
    """The INSERT-SELECT that implements the mapping, with every risk restated
    as a comment above the statement it applies to."""
    lines = [
        f"-- {mapping.recname}: 9.1 -> 9.2 data conversion.",
        f"-- source {mapping.source_table}  ->  target {mapping.target_table}",
    ]
    if mapping.blocked:
        lines += [
            "--",
            "-- !! BLOCKED: resolve the blocker risks below before running.",
            "--    Risks marked (measurable) are counted against real 9.1 "
            "data by preflight;",
            "--    a zero count clears them. The rest stand on their own.",
        ]
    for sev, code, msg in mapping.all_risks():
        tag = " (measurable)" if code in MEASURED_CODES else ""
        lines.append(f"-- [{sev}:{code}{tag}] {msg}")
    if mapping.dropped_source:
        lines.append("-- dropped 9.1 columns: " + ", ".join(mapping.dropped_source))
    for tcol, cands in sorted(mapping.rename_suggestions.items()):
        best = ", ".join(f"{c['source_column']} ({c['similarity']})"
                         for c in cands)
        lines.append(f"-- rename candidates for {tcol}: {best}")
    lines.append("--")

    src_ref = source_reference(mapping, via, dblink, staging_prefix)
    targets = [c.target_column for c in mapping.columns]
    selects = []
    for c in mapping.columns:
        if c.kind in (DEFAULTED, OVERRIDE_DEFAULT):
            selects.append(f"{c.source_expr:<28} /* {c.kind} {c.target_column} */")
        elif c.kind == EXPRESSION:
            selects.append(f"{c.source_expr:<28} /* expr -> {c.target_column} */")
        elif c.kind == RENAMED:
            selects.append(f"{c.source_expr:<28} /* -> {c.target_column} */")
        else:
            selects.append(c.source_expr)
    lines.append(f"INSERT INTO {mapping.target_table} (")
    lines.append("    " + ",\n    ".join(targets))
    lines.append(") SELECT")
    lines.append("    " + ",\n    ".join(selects))
    lines.append(f"FROM {src_ref}")
    if mapping.where:
        lines.append(f"WHERE {mapping.where}")
    lines.append(";")
    return "\n".join(lines) + "\n"


def staging_ddl(mapping: RecordMapping, src: RecordDef,
                staging_prefix: str, dialect: str = "oracle") -> str:
    """CREATE TABLE for the staging landing table, in the 9.1 shape.

    The staging path exists because Data Mover's EXPORT/IMPORT pairs matching
    shapes: land the 9.1 rows unchanged, then let the generated INSERT-SELECT
    do the reshaping inside 9.2 where it can be reviewed and re-run.
    """
    name = f"{staging_prefix}{mapping.source_table}"
    if dialect == "oracle" and len(name) > 30:
        head = (f"-- NOTE: {name} exceeds 30 characters; shorten "
                f"migrate.staging_prefix on Oracle releases before 12.2.\n")
    else:
        head = ""
    cols = []
    for f in src.fields:
        if f.fieldtype is None:
            continue
        fam = type_family(f.fieldtype)
        if fam == "char":
            t = f"VARCHAR2({max(f.length, 1)})" if dialect == "oracle" else "TEXT"
        elif fam == "num":
            t = f"NUMBER({max(f.length, 1)},{f.decimalpos})" if dialect == "oracle" else "REAL"
        elif fam in ("date", "datetime", "time"):
            t = "DATE" if dialect == "oracle" else "TEXT"
        else:
            t = "BLOB"
        cols.append(f"    {f.fieldname:<20} {t}")
    return head + f"CREATE TABLE {name} (\n" + ",\n".join(cols) + "\n);\n"
