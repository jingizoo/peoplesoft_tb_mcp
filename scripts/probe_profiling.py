#!/usr/bin/env python3
"""Measure what the profiler will have to work with, on the real instance.

Everything the ranking does is calibrated on numbers this deployment does
not have locally: how many objects there are, how many have ever been
analyzed, how wide the copies problem actually is. The bundled sample has
63 objects and no backup tables, so tuning against it would be tuning
against a fiction.

So this asks, and reports. It is READ ONLY:

* every query reads the Oracle data dictionary, never a business table;
* no business table is ever counted or scanned -- row counts come from
  NUM_ROWS, which is already in the dictionary, because a count sweep
  across a mismanaged schema is the kind of thing a reporting account
  loses its grant for;
* nothing is written anywhere, including the catalog.

Most sections are cheap dictionary lookups. TWO ARE NOT, and they are
marked EXPENSIVE where they appear: the PL/SQL sections aggregate over
ALL_SOURCE, which holds one row per line of stored code, and the last one
pattern-matches every one of those lines. They are placed last so that
everything above them has already printed if they are slow.

A query that TIMES OUT reports through the same `UNREADABLE:` slot as one
the account may not read. If a line here says UNREADABLE, check the text:
a privilege error names the object, a timeout does not. They are not the
same finding and the remedies are opposites.

Run it on the work box and send back the output:

    python scripts/probe_profiling.py                 # the primary
    python scripts/probe_profiling.py --source p2go
    python scripts/probe_profiling.py --skip-expensive

Nothing printed is a value from a business table, and no line of stored
source is printed anywhere -- only counts, ratios, and object names.

Three of the reported figures decide what gets built next -- see COVERAGE,
the PLSQL ratio, and GRANTS.
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
    ap.add_argument("--skip-expensive", action="store_true",
                    help="omit the ALL_SOURCE aggregates and the shape scan")
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
    # ORA-01489 on the first attempt: LISTAGG of every column name
    # overflows 4000 characters on a schema with 3.19M columns. Hashing
    # instead cannot overflow. SUM is commutative, so the signature is
    # order-independent for free -- and collisions only ever produce a
    # CANDIDATE here, which is then read by eye.
    rows, err = _rows(
        db,
        "SELECT NCOLS, SIG, COUNT(*) AS N, "
        "  SUBSTR(LISTAGG(TABLE_NAME, ', ') WITHIN GROUP "
        "         (ORDER BY TABLE_NAME), 1, 300) AS NAMES "
        "FROM (SELECT OWNER, TABLE_NAME, COUNT(*) AS NCOLS, "
        "        SUM(ORA_HASH(COLUMN_NAME || ':' || DATA_TYPE)) AS SIG "
        f"      FROM ALL_TAB_COLUMNS {where} GROUP BY OWNER, TABLE_NAME) "
        "GROUP BY NCOLS, SIG HAVING COUNT(*) > 1 "
        "ORDER BY COUNT(*) DESC FETCH FIRST 20 ROWS ONLY", params, cap=20)
    if err:
        # The inner LISTAGG is now over table names within one signature
        # group, not every column in the schema, so it is far shorter --
        # but a group with hundreds of members can still overflow.
        rows, err = _rows(
            db,
            "SELECT NCOLS, SIG, COUNT(*) AS N, MIN(TABLE_NAME) AS NAMES "
            "FROM (SELECT OWNER, TABLE_NAME, COUNT(*) AS NCOLS, "
            "        SUM(ORA_HASH(COLUMN_NAME || ':' || DATA_TYPE)) AS SIG "
            f"      FROM ALL_TAB_COLUMNS {where} GROUP BY OWNER, TABLE_NAME) "
            "GROUP BY NCOLS, SIG HAVING COUNT(*) > 1 "
            "ORDER BY COUNT(*) DESC FETCH FIRST 20 ROWS ONLY", params, cap=20)
    if err:
        print(f"  UNREADABLE: {err}")
    elif not rows:
        print("  none -- no two tables in this schema share a column signature")
    else:
        total = sum(int(r.get("n") or 0) for r in rows)
        print(f"  {len(rows)} shapes shown, covering {total} tables")
        print("  (a shape is a column-count + hash of its columns; names are")
        print("   what the marker list has to recognise)")
        for row in rows:
            print(f"  x{row.get('n'):<4} {row.get('ncols'):>3} cols  "
                  f"{str(row.get('names') or '')[:130]}")

    # --------------------------------------------------------- GRANTS
    # Everything above works on a plain read-only account. These two do
    # not always, and they are what phase two would want: real data
    # recency, and what anyone actually queries.
    _section("GRANTS  (phase two signals -- expected to fail on a bare account)")
    for label, sql in (
        ("ALL_TAB_MODIFICATIONS", "SELECT COUNT(*) FROM ALL_TAB_MODIFICATIONS"),
        ("DBA_TAB_MODIFICATIONS", "SELECT COUNT(*) FROM DBA_TAB_MODIFICATIONS"),
        ("V$SQL", "SELECT COUNT(*) FROM V$SQL"),
        ("DBA_HIST_SQLSTAT", "SELECT COUNT(*) FROM DBA_HIST_SQLSTAT"),
        ("ALL_DEPENDENCIES", f"SELECT COUNT(*) FROM ALL_DEPENDENCIES {where}"),
        # Stored-source reach. ALL_SOURCE unscoped answers a different
        # question from ALL_SOURCE scoped: whether this account can see
        # ANY owner's code, which separates "no grant" from "no code
        # here". DBA_SOURCE is the remedy to ask for when it cannot.
        ("DBA_SOURCE", "SELECT COUNT(*) FROM DBA_SOURCE WHERE ROWNUM <= 1"),
        # Reach, not breadth: COUNT(DISTINCT OWNER) here was a full
        # unscoped scan of every line of stored code on the instance --
        # sitting in GRANTS, which --skip-expensive does not gate. The
        # grants question is only "can this account see ANY other
        # owner's source"; the per-owner breadth stays in PLSQL OWNERS,
        # which is marked expensive and skippable.
        ("ALL_SOURCE (any owner)",
         "SELECT COUNT(*) FROM ALL_SOURCE WHERE ROWNUM <= 1"),
        ("ALL_IDENTIFIERS",
         "SELECT COUNT(*) FROM ALL_IDENTIFIERS WHERE ROWNUM <= 1"),
        ("ALL_STATEMENTS",
         "SELECT COUNT(*) FROM ALL_STATEMENTS WHERE ROWNUM <= 1"),
    ):
        value, err = _one(db, sql, params if "ALL_DEPENDENCIES" in sql else {})
        print(f"  {label:<24} {'readable, ' + str(value) if not err else 'no: ' + err}")

    # ---------------------------------------------------------- PLSQL
    # The go/no-go for harvesting stored code. A custom schema writes its
    # real joins and its load lineage into packages, but only if this
    # account can SEE them: ALL_OBJECTS and ALL_SOURCE are both privilege
    # filtered, and a reporting account with no EXECUTE typically sees a
    # package's existence and not its body. So the ratio of the first two
    # numbers is the whole question, and a low one is a PRIVILEGE GAP,
    # not evidence that this schema has no PL/SQL.
    unit_types = ("PACKAGE BODY", "PROCEDURE", "FUNCTION", "TRIGGER",
                  "TYPE BODY")
    type_list = ", ".join(f"'{t}'" for t in unit_types)
    and_where = f"{where} AND " if where else "WHERE "

    _section("PLSQL  (is there stored source, and may this account read it)")
    visible, err_v = _one(
        db,
        f"SELECT COUNT(*) FROM ALL_OBJECTS {and_where}"
        f"OBJECT_TYPE IN ({type_list})", params)
    print(f"  units visible in ALL_OBJECTS    "
          f"{visible if not err_v else 'UNREADABLE: ' + err_v}")
    # One indexed probe per unit, never a scan of every line: ALL_SOURCE
    # is one row per LINE of stored code, so COUNT over its DISTINCT
    # units reads the whole schema's source to answer a question about
    # unit COUNTS -- on a large instance that cannot finish inside any
    # sane per-query timeout (measured: it did not, at 180s). EXISTS on
    # LINE = 1 touches one indexed row per visible unit instead.
    readable, err_r = _one(
        db,
        f"SELECT COUNT(*) FROM ALL_OBJECTS O {and_where}"
        f"O.OBJECT_TYPE IN ({type_list}) "
        "AND EXISTS (SELECT 1 FROM ALL_SOURCE S "
        "WHERE S.OWNER = O.OWNER AND S.NAME = O.OBJECT_NAME "
        "AND S.TYPE = O.OBJECT_TYPE AND S.LINE = 1)", params)
    print(f"  units with readable source      "
          f"{readable if not err_r else 'UNREADABLE: ' + err_r}")
    if not err_v and not err_r and visible:
        share = 100.0 * (int(readable or 0) / int(visible))
        print(f"  ^ {share:.1f}% readable -- THE GO/NO-GO NUMBER. Near zero "
              "means this")
        print("    account sees the objects and not their text, which is a "
              "grant to")
        print("    ask for (SELECT on DBA_SOURCE), not an absence of code.")

    if args.skip_expensive:
        print("  (--skip-expensive: the ALL_SOURCE aggregates were not run)")
    else:
        _section("PLSQL VOLUME  (EXPENSIVE -- aggregates over one row per line)")
        rows, err = _rows(
            db,
            "SELECT TYPE, COUNT(*) AS LINES, COUNT(DISTINCT NAME) AS OBJECTS "
            f"FROM ALL_SOURCE {where} GROUP BY TYPE ORDER BY 2 DESC",
            params, cap=30)
        if err:
            print(f"  by type: UNREADABLE: {err}")
        else:
            for row in rows:
                print(f"  {str(row.get('type') or ''):<16} "
                      f"{row.get('lines'):>9} lines  "
                      f"{row.get('objects'):>6} objects")
            print("  ^ says from the instance whether TRIGGER bodies live "
                  "here at all")

        rows, err = _rows(
            db,
            "SELECT OWNER, NAME, TYPE, COUNT(*) AS LINES FROM ALL_SOURCE "
            f"{where} GROUP BY OWNER, NAME, TYPE ORDER BY 4 DESC "
            "FETCH FIRST 10 ROWS ONLY", params, cap=10)
        print("  largest programs:")
        if err:
            print(f"    UNREADABLE: {err}")
        for row in rows:
            print(f"    {row.get('lines'):>7} lines  "
                  f"{row.get('owner')}.{row.get('name')} "
                  f"({row.get('type')})")

        value, err = _one(
            db, f"SELECT MAX(LENGTH(TEXT)) FROM ALL_SOURCE {where}", params)
        # A blank source line is stored as NULL ('' IS NULL in Oracle), so
        # this maximum skips them -- it sizes the fetch buffer, and blank
        # lines cost nothing to fetch.
        print(f"  widest line (chars)             "
              f"{value if not err else 'UNREADABLE: ' + err}")

        value, err = _one(
            db,
            "SELECT COUNT(DISTINCT OWNER||'.'||NAME) FROM ALL_SOURCE "
            f"{and_where}LINE <= 5 AND LOWER(TEXT) LIKE '%wrapped%'", params)
        print(f"  programs that look wrapped      "
              f"{value if not err else 'UNREADABLE: ' + err}")
        print("  ^ wrapped bodies are ciphertext: visible, readable, and "
              "useless")

        value, err = _one(
            db,
            "SELECT COUNT(*) FROM (SELECT OWNER, NAME, TYPE FROM "
            f"ALL_DEPENDENCIES {and_where}TYPE IN ({type_list}) "
            "AND REFERENCED_TYPE IN "
            "('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM') "
            "GROUP BY OWNER, NAME, TYPE "
            "HAVING COUNT(DISTINCT REFERENCED_OWNER||'.'||"
            "REFERENCED_NAME) >= 2)", params)
        print(f"  programs touching 2+ objects    "
              f"{value if not err else 'UNREADABLE: ' + err}")
        print("  ^ what a ranked harvest would actually queue")

        # ------------------------------------------------- PLSQL OWNERS
        # Deliberately UNSCOPED. The package that loads a custom schema is
        # very often owned by a neighbouring account, and a scoped probe
        # would report zero while the code sits one owner away.
        _section("PLSQL OWNERS  (EXPENSIVE, and UNSCOPED on purpose)")
        rows, err = _rows(
            db,
            "SELECT OWNER, COUNT(*) AS LINES, COUNT(DISTINCT NAME) AS OBJECTS "
            "FROM ALL_SOURCE GROUP BY OWNER ORDER BY 2 DESC "
            "FETCH FIRST 20 ROWS ONLY", {}, cap=20)
        if err:
            print(f"  UNREADABLE: {err}")
        for row in rows:
            print(f"  {row.get('lines'):>9} lines  "
                  f"{row.get('objects'):>6} objects  "
                  f"{str(row.get('owner') or '')[:60]}")
        print("  ^ if the volume is next door, `schemas` is pointed at the "
              "wrong owner")

        # -------------------------------------------------- PLSQL SHAPE
        _section("PLSQL SHAPE  (MOST EXPENSIVE -- pattern-matches every line)")
        value, err = _one(
            db,
            "SELECT COUNT(DISTINCT OWNER||'.'||NAME) FROM ALL_SOURCE "
            f"{and_where}REGEXP_LIKE(TEXT, "
            "'(^|[^A-Za-z_])JOIN[^A-Za-z_]', 'i')", params)
        print(f"  programs containing a JOIN      "
              f"{value if not err else 'UNREADABLE: ' + err}")
        print("  ^ a harvest can only accept an explicit JOIN ... ON. Legacy")
        print("    PL/SQL joins in the WHERE clause with (+), which is NOT")
        print("    extractable safely. If this number is near zero, the "
              "feature")
        print("    would read a great deal and find nothing -- do not build "
              "it.")

    print()
    print("Nothing was written, no business table was read, and no line of")
    print("stored source was printed -- only counts, ratios and names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
