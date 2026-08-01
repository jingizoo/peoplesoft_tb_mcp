"""Dependency closure: from seed records to everything they need to work.

A record is never portable alone. Its subrecords must exist to build it, its
audit and related-language records to save through it, its prompt (edit)
tables for every field-level lookup, and — for views — every record named in
the view SQL. Missing any of these surfaces in 9.2 as a broken component
weeks later, so the closure is computed up front and each record carries the
provenance of WHY it entered the plan.

Custom dependencies are traversed recursively. Delivered dependencies are
recorded but not traversed: their subtree is Oracle's, and walking it would
pull half the delivered system into the plan through one generic prompt.
"""
from __future__ import annotations

import re

from .catalog import RecordCatalog
from .spec import MigrateError

# FROM/JOIN targets in view SQL. PeopleSoft views reference tables as
# PS_<RECNAME> (or a custom SQLTABLENAME); bare recnames appear in %Table()
# style meta-SQL. Regex over SQL is a HINT — every hit is verified against
# PSRECDEFN before it can enter the plan.
_VIEW_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_$#]+)", re.I)
_META_TABLE_RE = re.compile(r"%Table\(\s*([A-Za-z0-9_]+)\s*\)", re.I)


def _view_candidates(sql_text: str) -> set:
    names = set()
    for m in _VIEW_REF_RE.finditer(sql_text):
        t = m.group(1).upper()
        names.add(t[3:] if t.startswith("PS_") else t)
    for m in _META_TABLE_RE.finditer(sql_text):
        names.add(m.group(1).upper())
    return {n for n in names if n and not n.isdigit()}


def expand(catalog: RecordCatalog, seeds: list, max_records: int = 2000) -> dict:
    """recname -> sorted provenance list (e.g. ["seed", "subrec:Z_INV_HDR"]).

    Seeds and every custom record found along the way are expanded; delivered
    records stop the walk. Deterministic order (FIFO over sorted deps) so two
    runs over the same metadata produce the same plan.
    """
    provenance: dict = {}
    queue: list = []

    def note(recname: str, via: str) -> bool:
        key = recname.upper().strip()
        if not key:
            return False
        first_time = key not in provenance
        provenance.setdefault(key, set()).add(via)
        return first_time

    for s in seeds:
        if note(s, "seed"):
            queue.append(s.upper().strip())

    while queue:
        if len(provenance) > max_records:
            raise MigrateError(
                f"Dependency closure exceeded migrate.max_records "
                f"({max_records}). A generic prompt table is probably "
                "dragging in the delivered system — review the seeds, or "
                "raise the limit deliberately.")
        name = queue.pop(0)
        rec = catalog.record(name)
        if rec is None:
            continue  # classified as unknown_source later
        deps: list = []
        for sub in rec.subrecords:
            deps.append((sub, f"subrec:{name}"))
        if rec.audit_recname:
            deps.append((rec.audit_recname, f"audit:{name}"))
        if rec.rellang_recname:
            deps.append((rec.rellang_recname, f"rellang:{name}"))
        for f in rec.fields:
            et = f.edittable.upper()
            # %EDITTABLE and friends are resolved at runtime from derived
            # state — there is no static record to port.
            if et and not et.startswith("%"):
                deps.append((et, f"prompt:{name}.{f.fieldname}"))
        if rec.rectype in (1, 5, 6):  # SQL / dynamic / query views
            text = catalog.view_sql(name)
            if text:
                for cand in sorted(_view_candidates(text)):
                    if cand != name and catalog.record_exists(cand):
                        deps.append((cand, f"view:{name}"))
        for dep, via in sorted(deps):
            first_time = note(dep, via)
            # Delivered records are boundary nodes: recorded, not traversed.
            if first_time and catalog.is_custom(dep):
                queue.append(dep.upper())

    return {k: sorted(v) for k, v in provenance.items()}
