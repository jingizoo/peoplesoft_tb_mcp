"""Partitioned execution: break one slow aggregate into indexed slices,
run the slices, merge the partials mechanically.

"Top 20 customers across all business units" is one GROUP BY over millions
of rows, and on an instance whose indexes lead with BUSINESS_UNIT the direct
query is a full scan that dies at the query timeout — while the same query
FOR ONE UNIT is an index range scan that returns in seconds. The model even
proposes the per-unit strategy when the direct query fails; what it lacked
was a mechanical way to run N slices and combine them without N model
rounds, retyped rows and hand re-ranking.

So the contract lives here. The caller writes the query for ONE partition,
with a :partition bind where the slice value goes; names the values (or
where to get them); and the engine runs every slice — concurrently, through
the session pool — and merges the partials with the correct combiner per
aggregate: SUM of SUMs, SUM of COUNTs, MAX of MAXes, MIN of MINs. ORDER BY
and FETCH FIRST are stripped from the slices and applied ONCE after the
merge, because a top-20 of each slice is not the top-20 of the whole.

The honest limits are refusals, not surprises:
  - AVG and DISTINCT aggregates do not merge from partials (an average of
    averages is wrong); rewrite as SUM and COUNT.
  - a query with no GROUP BY has nothing to merge; run it directly.
  - partitioning only helps when the partition column is INDEXED — that
    judgment belongs to explain_query, and the caller is told so.
"""
from __future__ import annotations

import re


class PartitionError(RuntimeError):
    pass


MAX_PARTITIONS = 200
_AGG_RE = re.compile(
    r"^\s*(SUM|COUNT|MIN|MAX|AVG)\s*\((.*)\)\s+AS\s+([A-Za-z_]\w*)\s*$",
    re.IGNORECASE | re.DOTALL)
_KEY_RE = re.compile(
    r"^\s*(?:[A-Za-z_][\w.]*)(?:\s+AS\s+([A-Za-z_]\w*))?\s*$",
    re.IGNORECASE)
_ORDER_RE = re.compile(
    r"\bORDER\s+BY\s+([A-Za-z_]\w*)\s*(ASC|DESC)?\s*"
    r"(?:FETCH\s+FIRST\s+(\d+)\s+ROWS?\s+ONLY\s*)?$",
    re.IGNORECASE)


def _split_select_items(select_list: str) -> list:
    """Split the SELECT list on top-level commas (parens respected)."""
    items, depth, current = [], 0, []
    for ch in select_list:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        items.append("".join(current))
    return [i.strip() for i in items if i.strip()]


def parse_mergeable(sql: str) -> dict:
    """Classify a grouped aggregate so its partials can be combined.

    Returns {"keys": [aliases...], "aggs": {alias: combiner}, "chunk_sql":
    sql-without-order, "order": (alias, desc, fetch_n)|None} or raises with
    the reason partitioning cannot be done safely.
    """
    m = re.search(r"(?is)^\s*SELECT\s+(.*?)\s+FROM\s", sql)
    if not m:
        raise PartitionError("could not read the SELECT list")
    if not re.search(r"(?i)\bGROUP\s+BY\b", sql):
        raise PartitionError(
            "partitioned execution merges GROUPED aggregates; this query has "
            "no GROUP BY. Run it directly, or add the grouping.")
    keys: list = []
    aggs: dict = {}
    for item in _split_select_items(m.group(1)):
        agg = _AGG_RE.match(item)
        if agg:
            func = agg.group(1).upper()
            inner = agg.group(2)
            alias = agg.group(3)
            if func == "AVG":
                raise PartitionError(
                    "AVG does not merge from partials (an average of "
                    "averages is wrong). Rewrite as SUM(x) AS s, COUNT(*) "
                    "AS n and divide after.")
            if re.match(r"(?is)^\s*DISTINCT\b", inner):
                raise PartitionError(
                    f"{func}(DISTINCT ...) does not merge from partials — "
                    "the same value can appear in several partitions.")
            aggs[alias] = {"SUM": "sum", "COUNT": "sum",
                           "MIN": "min", "MAX": "max"}[func]
            continue
        key = _KEY_RE.match(item)
        if not key:
            raise PartitionError(
                f"cannot classify {item!r} in the SELECT list — every item "
                "must be a plain grouped column (aliased) or "
                "SUM/COUNT/MIN/MAX(...) AS alias.")
        alias = key.group(1) or item.strip().split(".")[-1]
        keys.append(alias.lower())
    if not aggs:
        raise PartitionError("no mergeable aggregates in the SELECT list")
    order = None
    om = _ORDER_RE.search(sql)
    chunk_sql = sql
    if om:
        alias = om.group(1).lower()
        known = set(keys) | {a.lower() for a in aggs}
        if alias not in known:
            raise PartitionError(
                f"ORDER BY {om.group(1)} does not name a SELECT alias, so "
                "it cannot be applied after the merge.")
        order = (alias,
                 (om.group(2) or "ASC").upper() == "DESC",
                 int(om.group(3)) if om.group(3) else None)
        chunk_sql = sql[:om.start()].rstrip()
    return {"keys": keys, "aggs": {a.lower(): c for a, c in aggs.items()},
            "chunk_sql": chunk_sql, "order": order}


def merge_partials(parsed: dict, partial_rows: list) -> list:
    """Combine slice rows into whole-query rows, then order and fetch."""
    combined: dict = {}
    for row in partial_rows:
        key = tuple(row.get(k) for k in parsed["keys"])
        slot = combined.get(key)
        if slot is None:
            combined[key] = dict(row)
            continue
        for alias, how in parsed["aggs"].items():
            value = row.get(alias)
            if value is None:
                continue
            current = slot.get(alias)
            if current is None:
                slot[alias] = value
            elif how == "sum":
                slot[alias] = current + value
            elif how == "max":
                slot[alias] = max(current, value)
            elif how == "min":
                slot[alias] = min(current, value)
    rows = list(combined.values())
    if parsed["order"]:
        alias, desc, fetch_n = parsed["order"]
        rows.sort(key=lambda r: (r.get(alias) is None, r.get(alias)),
                  reverse=desc)
        if fetch_n:
            rows = rows[:fetch_n]
    return rows
