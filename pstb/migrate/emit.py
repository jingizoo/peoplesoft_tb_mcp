"""Emit the artifacts the delivered tools consume.

The pipeline's write boundary is the filesystem. Definitions move through an
Application Designer project an operator copies 9.1 -> 9.2 and Builds; data
moves through Data Mover scripts; drift records get a mapping script that a
human (or the LLM, then a human) finishes. Nothing here executes against
either database.

Artifacts, in run order:
  README.txt              the runbook for this emission
  01_project_records.txt  record names for the App Designer project
  02_export_records.dms   Data Mover, run against 9.1: EXPORT each table
  03_import_records.dms   Data Mover, run against 9.2 AFTER Build: REPLACE_DATA
  04_reconcile.sql        reference count/sum probes (the pipeline also runs
                          these live via `reconcile`)
  05_staging_ddl.sql      landing tables in the 9.1 shape (staging mode only)
  convert/<REC>.sql       mapping-driven INSERT-SELECT for every record whose
                          shape differs — custom drift and delivered
                          conversion alike
  resolved_mappings.json  every column decision and risk, for review
  plan.json / plan.md     the machine and human forms of the plan
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .catalog import RecordCatalog
from .convert import MEASURED_CODES, insert_select, staging_ddl
from .spec import (BLOCKER, BUILD_AND_LOAD, DATA_DMS, DATA_MAPPED_SQL,
                   DELIVERED_CONVERT, DELIVERED_MISSING, DRIFT_REVIEW,
                   LOAD_ONLY, PlanItem, UNKNOWN_SOURCE)

_RUNBOOK = """\
PeopleSoft 9.1 -> 9.2 record port — generated {when}

Write policy: this pipeline NEVER writes to either database. Apply
definitions with Application Designer, move data with Data Mover, and let
the pipeline verify the result read-only.

Order of operations
  1. 01_project_records.txt — in App Designer on 9.1: create a project and
     Insert > Definitions into Project for each record listed. Copy Project
     to File; then on 9.2, Copy Project from File.
     Drifted records (marked DRIFT) need a manual definition merge in 9.2 —
     see drift/<REC>.sql headers and plan.md for the field-level diff.
  2. Build (Create/Alter Tables + Create Indexes/Views/Triggers) in App
     Designer on 9.2, from that project. Then run: pipeline verify-build.
  3. 02_export_records.dms — Data Mover against 9.1.
  4. 03_import_records.dms — Data Mover against 9.2. REPLACE_DATA deletes
     then inserts, so re-running is safe. Run ONLY after step 2 verified.
  5. convert/<REC>.sql — records whose shape differs between releases. Run
     `pipeline preflight` FIRST: it counts, on the real 9.1 data, the rows
     each mapping would truncate, round, overflow, or collide. Resolve every
     [blocker] comment in the file, then execute one record at a time.
     In staging mode, run 05_staging_ddl.sql on 9.2 and land the 9.1 extract
     into those tables before running the transforms.
  6. pipeline reconcile — live row counts and numeric sums on both sides,
     following renames and honouring each mapping's row filter.

Delivered records: with migrate.delivered_data = skip (the default) they are
never copied — 9.2 ships its own. With "convert" their data DOES move, and
this bypasses both 9.2's delivered content and Oracle's delivered conversion
programs; confirm each converted record is not one 9.2 already populates.
Records flagged delivered_missing block their dependents until retargeted.
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def emit_all(items: list, out_dir: Path, source: RecordCatalog,
             mappings: dict | None = None, convert_via: str = "dblink",
             dblink: str = "SOURCE91", staging_prefix: str = "STG_") -> dict:
    """mappings: recname -> RecordMapping for every record whose data needs
    reshaping (custom drift and delivered conversion). Absent means the plan
    has no shape mismatches to convert."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mappings = mappings or {}

    project = [i for i in items if i.needs_definition_port]
    dms = sorted(i.recname for i in items if i.data_plan == DATA_DMS)
    converts = [i for i in items if i.data_plan == DATA_MAPPED_SQL]
    blockers = [i for i in items
                if i.classification in (DELIVERED_MISSING, UNKNOWN_SOURCE)]

    files = {
        "README.txt": _RUNBOOK.format(when=_now()),
        "01_project_records.txt": _project_list(project),
        "02_export_records.dms": _export_dms(dms),
        "03_import_records.dms": _import_dms(dms),
        "04_reconcile.sql": _reconcile_sql(items, source, mappings),
        "plan.json": json.dumps({"generated_utc": _now(),
                                 "records": [i.to_dict() for i in items]},
                                indent=2),
        "plan.md": _plan_md(items),
    }

    mapped, blocked_mappings = [], []
    if converts:
        (out / "convert").mkdir(exist_ok=True)
    staging_parts = []
    for it in converts:
        m = mappings.get(it.recname)
        if m is None:
            files[f"convert/{it.recname}.sql"] = (
                f"-- {it.recname}: no mapping could be resolved (source or "
                f"target definition unavailable). Complete by hand.\n")
            continue
        files[f"convert/{it.recname}.sql"] = insert_select(
            m, via=convert_via, dblink=dblink, staging_prefix=staging_prefix)
        mapped.append(it.recname)
        if m.blocked:
            blocked_mappings.append({
                "recname": it.recname,
                "blockers": [{"code": code, "message": msg,
                              "measurable_by_preflight": code in MEASURED_CODES}
                             for sev, code, msg in m.all_risks()
                             if sev == BLOCKER]})
        if convert_via == "staging":
            rec = source.record(it.recname)
            if rec is not None:
                staging_parts.append(staging_ddl(
                    m, rec, staging_prefix,
                    dialect="oracle" if source.db.dialect == "oracle"
                    else source.db.dialect))
    if staging_parts:
        files["05_staging_ddl.sql"] = (
            "-- Landing tables in the 9.1 shape. Create these on 9.2, load\n"
            "-- the 9.1 extract into them unchanged, then run convert/*.sql.\n\n"
            + "\n".join(staging_parts))
    if mappings:
        files["resolved_mappings.json"] = json.dumps(
            {"generated_utc": _now(), "convert_via": convert_via,
             "mappings": {k: v.to_dict() for k, v in sorted(mappings.items())}},
            indent=2)

    for rel, content in files.items():
        (out / rel).write_text(content)

    return {
        "out_dir": str(out),
        "files": sorted(files),
        "project_records": len(project),
        "dms_records": len(dms),
        "convert_records": len(mapped),
        "blocked_mappings": blocked_mappings,
        "blockers": [
            {"recname": b.recname, "classification": b.classification}
            for b in blockers
        ],
    }


def _project_list(items: list) -> str:
    lines = [
        "# App Designer project contents — one record per line.",
        "# Includes subrecords, audit and related-language records, prompt",
        "# tables and view dependencies discovered by the closure. Records",
        "# already present and identical in 9.2 are deliberately absent.",
    ]
    for it in sorted(items, key=lambda i: i.recname):
        tag = "DRIFT — merge by hand in 9.2" if it.classification == DRIFT_REVIEW \
            else it.rectype_name
        lines.append(f"{it.recname:<30} # {tag}")
    return "\n".join(lines) + "\n"


def _export_dms(recnames: list) -> str:
    lines = [
        "-- Data Mover: run against the 9.1 SOURCE.",
        "-- One .dat carries every straight-copy record.",
        "SET LOG mig91_export.log;",
        "SET OUTPUT mig91_records.dat;",
    ]
    lines += [f"EXPORT {r};" for r in recnames]
    return "\n".join(lines) + "\n"


def _import_dms(recnames: list) -> str:
    lines = [
        "-- Data Mover: run against the 9.2 TARGET, only after App Designer",
        "-- Build succeeded and verify-build passed.",
        "-- REPLACE_DATA deletes target rows then inserts: idempotent, and it",
        "-- fails loudly (rather than misaligning) if a table was not built.",
        "SET LOG mig92_import.log;",
        "SET INPUT mig91_records.dat;",
        "SET COMMIT 10000;",
    ]
    lines += [f"REPLACE_DATA {r};" for r in recnames]
    return "\n".join(lines) + "\n"


def _reconcile_sql(items: list, source: RecordCatalog,
                   mappings: dict | None = None) -> str:
    """Spot-check probes for a DBA. Where a mapping renames a column or filters
    rows, the two sides are written with their own column names and the
    source carries the same WHERE — otherwise the comparison is meaningless."""
    mappings = mappings or {}
    lines = [
        "-- Reference reconciliation probes (the pipeline runs these live via",
        "-- `reconcile`; keep this file for DBA spot checks). Run the 9.1",
        "-- statement on the source and the 9.2 one on the target, compare.",
    ]
    for it in sorted(items, key=lambda i: i.recname):
        if it.data_plan not in (DATA_DMS, DATA_MAPPED_SQL):
            continue
        rec = source.record(it.recname)
        if rec is None or not rec.has_table:
            continue
        m = mappings.get(it.recname)
        t = rec.table_name
        tgt_t = m.target_table if m else t
        where = f" WHERE {m.where}" if (m and m.where) else ""
        lines.append(f"-- {it.recname}")
        lines.append(f"SELECT '{it.recname}' AS RECNAME, COUNT(*) AS ROW_COUNT "
                     f"FROM {t}{where};   -- 9.1")
        lines.append(f"SELECT '{it.recname}' AS RECNAME, COUNT(*) AS ROW_COUNT "
                     f"FROM {tgt_t};   -- 9.2")
        pairs = m.sourced_pairs() if m else [(c, c) for c in rec.numeric_fields()]
        numeric = set(rec.numeric_fields())
        for tcol, scol in pairs:
            if scol not in numeric:
                continue
            lines.append(f"SELECT SUM({scol}) AS TOTAL FROM {t}{where};   "
                         f"-- 9.1 {it.recname}.{scol}")
            lines.append(f"SELECT SUM({tcol}) AS TOTAL FROM {tgt_t};   "
                         f"-- 9.2 {it.recname}.{tcol}")
    return "\n".join(lines) + "\n"


def _plan_md(items: list) -> str:
    order = {BUILD_AND_LOAD: 0, DRIFT_REVIEW: 1, LOAD_ONLY: 2}
    lines = [
        "# Record port plan",
        "",
        "| Record | Type | Classification | Data | Rows (9.1) | Entered plan via |",
        "|---|---|---|---|---:|---|",
    ]
    for it in sorted(items, key=lambda i: (order.get(i.classification, 9),
                                           i.recname)):
        via = "; ".join(it.via[:3]) + ("; …" if len(it.via) > 3 else "")
        rows = "" if it.row_count is None else str(it.row_count)
        lines.append(f"| {it.recname} | {it.rectype_name} | "
                     f"{it.classification} | {it.data_plan} | {rows} | {via} |")
    noted = [i for i in items if i.notes]
    if noted:
        lines += ["", "## Notes", ""]
        for it in sorted(noted, key=lambda i: i.recname):
            for n in it.notes:
                lines.append(f"- **{it.recname}** — {n}")
    return "\n".join(lines) + "\n"
