#!/usr/bin/env python3
"""Measure what the profiler will have to work with, on the real instance.

Everything the ranking does is calibrated on numbers this deployment does
not have locally: how many objects there are, how many have ever been
analyzed, how wide the copies problem actually is. The bundled sample has
63 objects and no backup tables, so tuning against it would be tuning
against a fiction.

So this asks, and reports. It is READ ONLY and it is cheap:

* every query reads the Oracle data dictionary, never a business table;
* nothing is counted with COUNT(*) -- NUM_ROWS is already in the
  dictionary, and a count sweep across a mismanaged schema is the kind of
  thing a reporting account loses its grant for;
* nothing is written anywhere, including the catalog.

Run it on the work box and send back the output:

    python scripts/probe_profiling.py                 # the primary
    python scripts/probe_profiling.py --source p2go

Two of the reported figures decide whether phase two is worth building at
all -- see COVERAGE and GRANTS at the end of the output.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pstb.config import load_config                      # noqa: E402
from pstb.db import Database, DbError                    # noqa: E402
from pstb.sources import SourceRegistry                  # noqa: E402


def _rows(db, sql, params=None, cap=200):
    try:
        rows, _ = db.query(sql, params or {}, max_rows=cap)
        return rows, ""
    except DbError as exc:
        return [], str(exc).strip().splitlines()[0][:200]


def _one(db, sql, params=None):
    rows, err = _rows(db, sql, params, cap=1)
    if err:
        return None, err
    if not rows:
        return None, "no rows"
    return list(rows[0].values())[0], ""


def _section(title):
    print()
    print(title)
    print("-" * len(title))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--source", default="", help="registry source (default: primary)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config) if args.config else load_config()
    registry = SourceRegistry(cfg, Database(cfg))
    name = registry.resolve_name(args.source)
    db = registry.get(args.source)

    if db.dialect != "oracle":
        print(f"source {name!r} is {db.dialect}, not oracle -- this probe "
              "reads the Oracle data dictionary and has nothing to say "
              "about other backends.")
        return 2

    owners = tuple(o for o in (getattr(db.cfg.db, "schemas", None) or
                               [getattr(db.cfg.db, "schema", "")]) if o)
    print(f"source        : {name}")
    print(f"schemas       : {', '.join(owners) or '(current user)'}")

    scope, params = "", {}
    if owners:
        binds = []
        for i, owner in enumerate(owners):
            params[f"o{i}"] = owner.upper()
            binds.append(f":o{i}")
        scope = f"OWNER IN ({','.join(binds)})"
    where = f"WHERE {scope}" if scope else ""

    # ---------------------------------------------------------- SCALE
    _section("SCALE  (how much there is to rank)")
    for label, sql in (
        ("tables", f"SELECT COUNT(*) FROM ALL_TABLES {where}"),
        ("views", f"SELECT COUNT(*) FROM ALL_VIEWS {where}"),
        ("columns", f"SELECT COUNT(*) FROM ALL_TAB_COLUMNS {where}"),
    ):
        value, err = _one(db, sql, params)
        print(f"  {label:<10} {value if err == '' else 'UNREADABLE: ' + err}")

    # ------------------------------------------------------- COVERAGE
    # The single most important number here. Liveness comes from NUM_ROWS,
    # and NUM_ROWS is NULL until someone analyzes the table. If most of
    # this schema has never been analyzed, most objects rank on a prior
    # rather than a measurement, and phase two has to earn liveness some
    # other way (DBA_TAB_MODIFICATIONS, or a sampled probe).
    _section("COVERAGE  (can liveness be measured at all)")
    value, err = _one(
        db,
        "SELECT COUNT(*) FROM ALL_TAB_STATISTICS "
        f"{where}{' AND ' if where else 'WHERE '}"
        "PARTITION_NAME IS NULL AND OBJECT_TYPE='TABLE' "
        "AND NUM_ROWS IS NOT NULL", params)
    print(f"  tables with a row estimate      {value if not err else 'UNREADABLE: ' + err}")
    value, err = _one(
        db,
        "SELECT COUNT(*) FROM ALL_TAB_STATISTICS "
        f"{where}{' AND ' if where else 'WHERE '}"
        "PARTITION_NAME IS NULL AND OBJECT_TYPE='TABLE' "
        "AND NUM_ROWS = 0", params)
    print(f"  tables measured EMPTY           {value if not err else 'UNREADABLE: ' + err}")
    value, err = _one(
        db,
        "SELECT COUNT(*) FROM ALL_TAB_STATISTICS "
        f"{where}{' AND ' if where else 'WHERE '}"
        "PARTITION_NAME IS NULL AND OBJECT_TYPE='TABLE' "
        "AND LAST_ANALYZED > SYSDATE - 365", params)
    print(f"  analyzed in the last year       {value if not err else 'UNREADABLE: ' + err}")
    value, err = _one(
        db,
        "SELECT COUNT(*) FROM ALL_TAB_COL_STATISTICS "
        f"{where}{' AND ' if where else 'WHERE '}NUM_DISTINCT IS NOT NULL",
        params)
    print(f"  columns with distinct counts    {value if not err else 'UNREADABLE: ' + err}")

    # --------------------------------------------------------- COPIES
    # How big the shadow-table problem really is, measured the same way
    # the detector measures it: identical column signature first, name
    # second. Reported as candidates only -- this prints names so the
    # markers actually in use here can be read off, since the built-in
    # list was written from experience elsewhere.
    _section("COPIES  (identically shaped tables, worst offenders first)")
    rows, err = _rows(
        db,
        "SELECT SIGNATURE, COUNT(*) AS N, "
        "LISTAGG(TABLE_NAME, ', ') WITHIN GROUP (ORDER BY TABLE_NAME) AS NAMES "
        "FROM (SELECT OWNER, TABLE_NAME, LISTAGG(COLUMN_NAME || ':' || "
        "DATA_TYPE, '|') WITHIN GROUP (ORDER BY COLUMN_NAME) AS SIGNATURE "
        f"FROM ALL_TAB_COLUMNS {where} GROUP BY OWNER, TABLE_NAME) "
        "GROUP BY SIGNATURE HAVING COUNT(*) > 1 "
        "ORDER BY COUNT(*) DESC FETCH FIRST 15 ROWS ONLY", params, cap=15)
    if err:
        print(f"  UNREADABLE: {err}")
        print("  (LISTAGG overflows past 4000 chars on very wide tables; if "
              "that is the error, say so and this becomes a client-side pass)")
    elif not rows:
        print("  none -- no two tables in this schema share a column signature")
    else:
        for row in rows:
            names = str(row.get("names") or "")
            print(f"  x{row.get('n'):<3} {names[:150]}")

    # --------------------------------------------------------- GRANTS
    # Everything above works on a plain read-only account. These two do
    # not always, and they are what phase two would want: real data
    # recency, and what anyone actually queries.
    _section("GRANTS  (phase two signals -- expected to fail on a bare account)")
    for label, sql in (
        ("DBA_TAB_MODIFICATIONS", "SELECT COUNT(*) FROM DBA_TAB_MODIFICATIONS"),
        ("V$SQL", "SELECT COUNT(*) FROM V$SQL"),
        ("DBA_HIST_SQLSTAT", "SELECT COUNT(*) FROM DBA_HIST_SQLSTAT"),
        ("ALL_DEPENDENCIES", f"SELECT COUNT(*) FROM ALL_DEPENDENCIES {where}"),
    ):
        value, err = _one(db, sql, params if "ALL_DEPENDENCIES" in sql else {})
        print(f"  {label:<24} {'readable, ' + str(value) if not err else 'no: ' + err}")

    print()
    print("Nothing was written and no business table was read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
