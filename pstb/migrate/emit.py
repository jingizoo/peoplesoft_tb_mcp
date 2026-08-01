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
  drift/<REC>.sql         reviewed mapping template per drifted record
  plan.json / plan.md     the machine and human forms of the plan
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .catalog import RecordCatalog
from .spec import (BUILD_AND_LOAD, DATA_DMS, DATA_MAPPED_SQL,
                   DELIVERED_MISSING, DRIFT_REVIEW, LOAD_ONLY, PlanItem,
                   UNKNOWN_SOURCE)

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
  5. drift/<REC>.sql — complete the column mappings, review, execute via
     your DBA (db link or staging extract), one record at a time.
  6. pipeline reconcile — live row counts and numeric sums on both sides.

Delivered records are never copied — 9.2 already ships its own. Records
flagged delivered_missing block their dependents until retargeted.
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def emit_all(items: list, out_dir: Path, source: RecordCatalog) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "drift").mkdir(exist_ok=True)

    project = [i for i in items if i.needs_definition_port]
    dms = sorted(i.recname for i in items if i.data_plan == DATA_DMS)
    drift = [i for i in items if i.classification == DRIFT_REVIEW]
    blockers = [i for i in items
                if i.classification in (DELIVERED_MISSING, UNKNOWN_SOURCE)]

    files = {
        "README.txt": _RUNBOOK.format(when=_now()),
        "01_project_records.txt": _project_list(project),
        "02_export_records.dms": _export_dms(dms),
        "03_import_records.dms": _import_dms(dms),
        "04_reconcile.sql": _reconcile_sql(items, source),
        "plan.json": json.dumps({"generated_utc": _now(),
                                 "records": [i.to_dict() for i in items]},
                                indent=2),
        "plan.md": _plan_md(items),
    }
    for it in drift:
        files[f"drift/{it.recname}.sql"] = _drift_sql(it, source)

    for rel, content in files.items():
        (out / rel).write_text(content)

    return {
        "out_dir": str(out),
        "files": sorted(files),
        "project_records": len(project),
        "dms_records": len(dms),
        "drift_records": len(drift),
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


def _reconcile_sql(items: list, source: RecordCatalog) -> str:
    lines = [
        "-- Reference reconciliation probes (the pipeline runs these live via",
        "-- `reconcile`; keep this file for DBA spot checks). Run each",
        "-- statement on BOTH instances and compare.",
    ]
    for it in sorted(items, key=lambda i: i.recname):
        if it.data_plan not in (DATA_DMS, DATA_MAPPED_SQL):
            continue
        rec = source.record(it.recname)
        if rec is None or not rec.has_table:
            continue
        t = rec.table_name
        lines.append(f"SELECT '{it.recname}' AS RECNAME, COUNT(*) AS ROW_COUNT FROM {t};")
        for col in rec.numeric_fields():
            lines.append(
                f"SELECT '{it.recname}.{col}' AS PROBE, SUM({col}) AS TOTAL FROM {t};")
    return "\n".join(lines) + "\n"


def _drift_sql(item: PlanItem, source: RecordCatalog) -> str:
    rec = source.record(item.recname)
    diff = item.shape_diff or {}
    tgt_only = diff.get("target_only", [])
    src_only = diff.get("source_only", [])
    changed = diff.get("changed", [])
    lines = [
        f"-- {item.recname}: exists in both instances with different shapes.",
        "-- Finish this mapping, have it reviewed, then load via DB link or a",
        "-- staged extract. A straight Data Mover import would misalign.",
        "--",
        f"-- 9.2-only columns (need a default or a sourced value): {', '.join(tgt_only) or 'none'}",
        f"-- 9.1-only columns (dropped or renamed — decide which): {', '.join(src_only) or 'none'}",
    ]
    lines += [f"-- changed: {c}" for c in changed]
    if rec is None:
        lines.append("-- source definition unavailable; complete by hand")
        return "\n".join(lines) + "\n"
    common = [f.fieldname for f in rec.fields
              if f.fieldname not in set(src_only)]
    t = rec.table_name
    cols_out = common + [c for c in tgt_only]
    sel = [c for c in common]
    sel += [f"/* TODO default for 9.2-only {c} */ NULL" for c in tgt_only]
    lines.append(f"INSERT INTO {t} (")
    lines.append("    " + ",\n    ".join(cols_out))
    lines.append(") SELECT")
    lines.append("    " + ",\n    ".join(sel))
    lines.append(f"FROM {t}@SOURCE91  -- TODO: your 9.1 db link or staging table")
    lines.append(";")
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
