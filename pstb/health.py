"""One table's health, with every dependency handed in.

The health engine reads a table's DATA -- null samples, duplicate
groups, orphan probes -- which is why the operator's record exclusions
bind it exactly as they bind a query. It lived inside server.py behind
three module globals; the continuous ticker needs the same engine
without the MCP surface, so the globals became parameters and nothing
else moved. Counts and percentages only: no row value leaves the
database through this module.
"""
from __future__ import annotations

from .db import DbError


SECTIONS = frozenset({"nulls", "duplicates", "orphans"})


def table_health(table: str, *, source_name: str, catalog,
                 get_db, exclusions,
                 sections: frozenset = SECTIONS,
                 declared_keys_only: bool = False) -> dict:
    """The health engine behind get_table_health -- and the ticker.

    Extracted from server.py with its dependencies INJECTED so a
    background loop can run it without the MCP module's globals:
    ``catalog`` is the source's offline artifact, ``get_db`` a LAZY
    thunk to the bound Database (called only after resolution and the
    exclusion refusal, preserving the chat tool's failure order), and
    ``exclusions`` the operator-veto index, read fresh on every call.

    ``sections`` gates the three query-bearing blocks. The ticker runs
    {"duplicates"} only: sampled null rates and sampled orphan probes
    are nondeterministic run to run, and a diff store turns any
    fluctuating metric into worsened/improved churn. ``declared_keys_only``
    additionally disables the heuristic key fallback, which manufactures
    standing false exceptions on legitimately multi-row stage tables.
    Defaults preserve the chat tool bit for bit.
    """
    from . import relmine

    name = source_name
    # limit=60, not the default: the shared context budget also feeds
    # dependencies and mappings, and a well-connected table starved the
    # column list to empty -- which silently skipped every check while
    # looking like a clean bill (review finding).
    context = catalog.context(table, source=name, limit=60)
    if not isinstance(context, dict) or context.get("found") is not True \
            or context.get("ambiguous"):
        return {
            "source_database": name,
            "error": str((context or {}).get("detail")
                         or "the catalog did not resolve one exact object"),
            "candidates": (context or {}).get("candidates") or [],
        }
    subject = context.get("subject") or {}
    schema = str(subject.get("schema") or "")
    physical = str(subject.get("physical_object") or "")
    kind = str(subject.get("kind") or "").lower()
    useful = context.get("usefulness") or {}
    qualified = f"{schema}.{physical}" if schema else physical

    for part in (schema, physical):
        if part and not relmine._SAFE_IDENT.fullmatch(part):
            return {"source_database": name,
                    "error": f"unsafe identifier {part[:40]!r}"}

    # Operator record exclusions bind here exactly as they bind join_path:
    # a health probe reads the record's DATA, which is precisely what an
    # exclusion forbids (review finding).
    excluded_ids = {str(row.get("object_id") or "")
                    for row in exclusions.for_source(name)}
    excluded_names = {str(row.get("object") or "").upper()
                      for row in exclusions.for_source(name)}
    if (str(subject.get("object_id") or "") in excluded_ids
            or physical.upper() in excluded_names):
        return {"source_database": name,
                "error": (f"{physical} is excluded from this source by an "
                          "operator decision; health checks read its data "
                          "and are refused for the same reason as queries")}

    db = get_db()
    dialect = str(getattr(db, "dialect", "")).lower()

    def bounded(inner: str, cap: int) -> str:
        return relmine.bounded_select(dialect, inner, cap)

    caveats: list[str] = []
    estimate = useful.get("row_estimate")
    # FAIL CLOSED on size: the duplicate probe is a whole-table GROUP BY,
    # and an unknown estimate is not permission -- it is the absence of
    # permission. On the target deployment most tables have never been
    # analyzed, so treating "unknown" as "small" would have made the 2M
    # gate decorative (review blocker).
    measured_size = (isinstance(estimate, (int, float))
                     and not isinstance(estimate, bool)
                     and estimate >= 0)
    exact_ok = measured_size and estimate <= 2_000_000
    if not exact_ok:
        caveats.append(
            (f"the profiler estimates {int(estimate):,} rows; "
             if measured_size else
             "the table's size is not established by any statistic; ")
            + "exact duplicate counting was skipped to avoid a full "
            "scan — null rates and orphan checks are sampled either way")

    # ---- columns worth checking: key-shaped first, then declared order
    columns = []
    for column in context.get("columns") or []:
        cname = str(column.get("name") or "")
        if cname and relmine._SAFE_IDENT.fullmatch(cname):
            columns.append(cname)
    key_first = sorted(
        columns, key=lambda c: (not relmine.is_key_shaped(c),
                                columns.index(c)))[:12]

    # ---- null rates over a bounded sample (one query, never a scan)
    null_rates: list[dict] = []
    sampled_rows = 0
    if "nulls" in sections and not key_first:
        caveats.append(
            "the catalog returned no readable columns for this object, so "
            "null rates and duplicate checks were skipped -- not passed")
    if "nulls" in sections and key_first and kind in ("table", "view"):
        selects = ", ".join(
            f"COUNT({c}) AS nn_{i}" for i, c in enumerate(key_first))
        try:
            rows, _ = db.query(
                f"SELECT COUNT(*) AS n, {selects} FROM "
                f"({bounded(f'SELECT * FROM {qualified}', 5000)}) t",
                {}, max_rows=1)
            row = rows[0] if rows else {}
            sampled_rows = int(row.get("n") or 0)
            for i, column in enumerate(key_first):
                non_null = int(row.get(f"nn_{i}") or 0)
                nulls = max(sampled_rows - non_null, 0)
                null_rates.append({
                    "column": column, "nulls": nulls,
                    "null_pct": (round(nulls / sampled_rows, 4)
                                 if sampled_rows else None)})
        except DbError as exc:
            caveats.append(f"null-rate sample failed: {exc}")
    if sampled_rows >= 5000:
        caveats.append(
            f"null rates are measured over {sampled_rows:,} rows the "
            "database happened to return first (no ordering is imposed), "
            "not the full population")
    elif sampled_rows:
        caveats.append(
            f"null rates are measured over all {sampled_rows:,} rows -- "
            "the sample covered the whole table")

    # ---- duplicate candidate keys (exact but bounded; skipped when large)
    duplicates: dict = {"checked": False}
    if "duplicates" in sections:
        unique_cols: list[str] = []
        basis = ""
        for index in context.get("indexes") or []:
            if index.get("unique") and index.get("columns"):
                declared = [str(c) for c in index["columns"]]
                if len(declared) > 8:
                    # Never truncate a declared key: grouping by a PREFIX of a
                    # unique index manufactures duplicate alarms on data the
                    # database itself enforces as unique (review blocker --
                    # PS_JRNL_LN's five-column key read as thousands of
                    # "duplicates"). An unusually wide key is skipped and
                    # named instead.
                    duplicates = {
                        "checked": False,
                        "key_columns": declared,
                        "basis": f"declared unique index {index.get('name')}",
                        "skipped": (f"the unique key has {len(declared)} "
                                    "columns; grouping by fewer would report "
                                    "false duplicates"),
                    }
                    break
                unique_cols = declared
                basis = f"declared unique index {index.get('name')}"
                break
        if (not declared_keys_only and not unique_cols
                and not duplicates.get("skipped")):
            unique_cols = [c for c in key_first
                           if relmine.is_key_shaped(c)][:3]
            basis = "heuristic key-shaped columns"
        unique_cols = [c for c in unique_cols
                       if relmine._SAFE_IDENT.fullmatch(c)]
        if basis.startswith("declared") and len(unique_cols) != len(
                [str(c) for i2 in (context.get("indexes") or [])
                 if i2.get("name") == basis.split()[-1]
                 for c in (i2.get("columns") or [])]):
            # An expression column failed the identifier gate: the remaining
            # prefix is NOT the key. Skip rather than group by part of it.
            duplicates = {"checked": False, "key_columns": unique_cols,
                          "basis": basis,
                          "skipped": "the unique index contains expression "
                                     "columns; a partial grouping would report "
                                     "false duplicates"}
            unique_cols = []
        if unique_cols and kind == "table" and exact_ok:
            group = ", ".join(unique_cols)
            try:
                rows, _ = db.query(
                    "SELECT COUNT(*) AS g FROM ("
                    + bounded(
                        f"SELECT 1 AS x FROM {qualified} GROUP BY {group} "
                        "HAVING COUNT(*) > 1", 51)
                    + ") d", {}, max_rows=1)
                groups = int((rows[0].get("g") if rows else 0) or 0)
                duplicates = {
                    "checked": True, "key_columns": unique_cols,
                    "basis": basis,
                    "duplicate_groups": ("50+" if groups > 50 else groups)}
            except DbError as exc:
                duplicates = {"checked": False, "key_columns": unique_cols,
                              "basis": basis, "error": str(exc)}

    # ---- referential integrity along known relationships
    relationships: list[dict] = []
    if "orphans" in sections:
        ring = catalog.relationships_of(qualified, source=name)
        for edge in (ring.get("relationships") or []):
            if len(relationships) >= 3:
                break
            if edge.get("direction") != "references":
                continue
            pairs = edge.get("column_pairs") or []
            if not pairs:
                continue
            target = edge.get("next") or {}
            relationship = edge.get("relationship")
            entry = {
                "parent": ".".join(p for p in (target.get("schema"),
                                               target.get("object")) if p),
                "relationship": relationship,
                "confidence": edge.get("confidence"),
            }
            # A multi-pair edge used to be silently skipped -- which was every
            # merged mined edge and every composite declared key, i.e. most of
            # the graph this tool exists to spend (review finding). Mined
            # pairs are independent single-column containments, so the
            # best-measured one is probed and named. A composite declared key
            # is NOT independent per column -- probing one column undercounts
            # real orphans -- so it is listed and skipped with the reason.
            if relationship == "value_overlap":
                measurements = edge.get("measurements") or []
                best = max(measurements,
                           key=lambda m: m.get("overlap_pct") or 0)                 if measurements else None
                probe_pair = ({"left": best.get("column"),
                               "right": best.get("referenced_column")}
                              if best else
                              {"left": pairs[0].get("left_column"),
                               "right": pairs[0].get("right_column")})
                if len(pairs) > 1:
                    entry["probed_pair"] = (
                        f"{probe_pair['left']} -> {probe_pair['right']} "
                        f"(best-measured of {len(pairs)} pairs)")
            elif len(pairs) != 1:
                entry["via"] = ", ".join(
                    f"{pair.get('left_column')} -> {pair.get('right_column')}"
                    for pair in pairs)
                entry["skipped"] = (
                    "composite declared key: probing one column of a "
                    f"{len(pairs)}-column key would undercount real orphans, "
                    "so nothing is claimed")
                relationships.append(entry)
                continue
            else:
                probe_pair = {"left": pairs[0].get("left_column"),
                              "right": pairs[0].get("right_column")}
            left = str(probe_pair["left"] or "")
            right = str(probe_pair["right"] or "")
            entry.setdefault("via", f"{left} -> {right}")
            if relationship == "value_overlap":
                entry["caveat"] = (
                    "this relationship is MEASURED from value containment, "
                    "not declared; treat orphan counts as evidence to "
                    "investigate, not as a broken constraint")
            parent_id = str((target.get("object") or "")).upper()
            if (str(target.get("id") or "") in excluded_ids
                    or parent_id in excluded_names):
                entry["skipped"] = (
                    "the parent record is excluded from this source by an "
                    "operator decision; its data is not probed")
                relationships.append(entry)
                continue
            try:
                probe = relmine.probe_containment(
                    db,
                    {"schema": schema, "table": physical, "column": left},
                    {"schema": target.get("schema"),
                     "table": target.get("object"), "column": right},
                    sample_rows=200)
                sampled = int(probe.get("sampled") or 0)
                contained = int(probe.get("contained") or 0)
                # DISTINCT-VALUE vocabulary, deliberately: the probe samples
                # distinct values of the child column, so 7/200 is a share of
                # VALUES, not of rows -- one orphaned value may be one row or
                # ten thousand (review finding).
                entry["sampled_distinct"] = sampled
                entry["orphaned_distinct"] = max(sampled - contained, 0)
                entry["orphan_value_pct"] = (
                    round((sampled - contained) / sampled, 4)
                    if sampled else None)
                entry["rate_note"] = (
                    "rates are over distinct sampled values of the child "
                    "column, not rows")
            except DbError as exc:
                entry["error"] = str(exc)
            relationships.append(entry)

    return {
        "source_database": name,
        "object": {"schema": schema, "name": physical, "kind": kind},
        "profile": {
            "liveness": useful.get("liveness"),
            "row_estimate": useful.get("row_estimate"),
            "value_score": useful.get("value_score"),
            "caveat": useful.get("caveat") or "",
        },
        "null_rates": null_rates,
        "sampled_rows": sampled_rows,
        "duplicate_keys": duplicates,
        "relationships": relationships,
        "caveats": caveats,
        "note": (
            "Counts and percentages only — no row values leave the "
            "database. Sampled figures are labeled; they support an "
            "investigation, never a completeness conclusion."),
    }
