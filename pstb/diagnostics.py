"""Site health diagnostics, runnable from the GUI.

The slowness stories on a real instance ("it worked before", "close
readiness times out") almost always resolve to two database facts nobody
can see from the chat window: optimizer statistics that predate the last
big load, and hot tables whose indexes don't lead with the filtered
columns. The shell script (scripts/diagnose_db.py) reports both, but a
box operator who never opens a shell never sees it. This module runs the
same checks behind one GUI click and renders a copy-paste summary the
operator can hand to a DBA verbatim.

Speed contract: quick_checks() touches CATALOG views only (ALL_TABLES,
index metadata) — never the multi-million-row data tables — so it is safe
to run on demand. input_timings() deliberately runs the real
close-readiness playbook to measure the slow inputs; it is behind its own
explicit button in the GUI and never runs on page load.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

KEY_TABLES = ["PS_LEDGER", "PS_JRNL_HEADER", "PS_JRNL_LN", "PS_ITEM",
              "PS_BI_HDR", "PS_VOUCHER", "PS_PROJ_RESOURCE"]

STALE_DAYS = 45  # stats older than a typical close cycle are suspect


def stats_age(db) -> dict:
    """Optimizer-statistics freshness for the hot tables (Oracle only)."""
    if db.dialect != "oracle":
        return {"available": False,
                "note": "Optimizer statistics are an Oracle concept; this "
                        "connection is " + db.dialect + " (sample mode). "
                        "Run this panel against the real instance."}
    owner = (db.cfg.db.schema or "").strip().rstrip(".").upper()
    binds = {f"t{i}": t for i, t in enumerate(KEY_TABLES)}
    where = "TABLE_NAME IN (" + ",".join(":" + k for k in binds) + ")"
    if owner:
        where += " AND OWNER = :o"
        binds["o"] = owner
    rows, _ = db.query(
        f"SELECT TABLE_NAME AS t, NUM_ROWS AS n, "
        f"TO_CHAR(LAST_ANALYZED, 'YYYY-MM-DD') AS analyzed "
        f"FROM ALL_TABLES WHERE {where}", binds, max_rows=len(KEY_TABLES))
    today = dt.date.today()
    out = []
    for r in sorted(rows, key=lambda x: str(x.get("t"))):
        analyzed = r.get("analyzed")
        age = None
        if analyzed:
            try:
                age = (today - dt.date.fromisoformat(str(analyzed))).days
            except ValueError:
                pass
        out.append({
            "table": str(r["t"]),
            "stats_rows": r.get("n"),
            "last_analyzed": analyzed,
            "age_days": age,
            "stale": analyzed is None or (age is not None
                                          and age > STALE_DAYS),
        })
    return {"available": True, "tables": out, "stale_days": STALE_DAYS}


def index_summary(db) -> dict:
    """Index names and ordered column lists for the hot tables.

    The optimizer can only use an index whose LEADING columns appear in the
    predicates — this list is what lets anyone reason about a slow query
    without DBA access.
    """
    out = []
    for t in KEY_TABLES:
        if not db.columns(t):
            continue  # record not present at this site
        idx = db.indexes(t)
        out.append({"table": t, "indexes": idx,
                    "note": "" if idx else
                    "no indexes visible — every filtered query on this "
                    "table is a full scan"})
    return {"tables": out}


def input_timings(engine) -> dict:
    """Measure what close readiness actually waits on, by running it."""
    from pstb.ar import ARBilling
    from pstb.playbooks import PlaybookRunner

    t0 = time.perf_counter()
    res = PlaybookRunner(engine, ARBilling(engine)).run("close_readiness")
    total_ms = int((time.perf_counter() - t0) * 1000)
    inputs = [{"name": k, "ms": int(v), "slow": v > 30_000}
              for k, v in sorted((res.get("input_timings_ms") or {}).items(),
                                 key=lambda kv: -kv[1])]
    bal = next((s for s in res.get("steps", [])
                if s.get("step") == "balance"), None)
    probes = [{"name": k, "ms": int(v)}
              for k, v in sorted(((bal or {}).get("detail", {})
                                  .get("probe_timings_ms") or {}).items(),
                                 key=lambda kv: -kv[1])]
    return {"total_ms": total_ms, "verdict": res.get("verdict"),
            "inputs": inputs, "probes": probes}


def _dba_summary(db, stats: dict, indexes: dict,
                 timings: dict | None) -> str:
    """Plain text an operator can paste to a DBA unchanged."""
    owner = ((db.cfg.db.schema or "").strip().rstrip(".").upper()
             if db.dialect == "oracle" else "")
    lines = [
        "PeopleSoft finance agent - database health summary "
        f"({dt.date.today().isoformat()})",
        f"Connection: {db.dialect}"
        + (f", schema {owner}" if owner else ""),
        "",
        "1) Optimizer statistics on hot tables",
    ]
    if not stats.get("available"):
        lines.append("   " + stats.get("note", "not available"))
    else:
        gather = []
        for r in stats["tables"]:
            age = ("never analyzed" if r["last_analyzed"] is None
                   else f"analyzed {r['last_analyzed']}"
                        + (f" ({r['age_days']} days ago)"
                           if r["age_days"] is not None else ""))
            flag = "  << STALE" if r["stale"] else ""
            lines.append(f"   {r['table']:<18} stats rows="
                         f"{r['stats_rows'] if r['stats_rows'] is not None else '?':>12} "
                         f" {age}{flag}")
            if r["stale"]:
                gather.append(r["table"])
        if gather:
            lines.append("")
            lines.append("   Requested action - gather statistics "
                         "(plans are built on these row counts):")
            for t in gather:
                lines.append(
                    "   EXEC DBMS_STATS.GATHER_TABLE_STATS("
                    + (f"ownname=>'{owner}', " if owner else "")
                    + f"tabname=>'{t}', cascade=>TRUE);")
    lines += ["", "2) Indexes on hot tables (leading columns decide "
                  "whether a filter can use them)"]
    for t in indexes.get("tables", []):
        if t["indexes"]:
            for ix in t["indexes"]:
                lines.append(f"   {t['table']:<18} {ix['name']}"
                             f" ({', '.join(ix['columns'])})"
                             + (" UNIQUE" if ix.get("unique") else ""))
        else:
            lines.append(f"   {t['table']:<18} {t['note']}")
    if timings:
        lines += ["", "3) Close-readiness inputs, slowest first "
                      f"(one full run: {timings['total_ms']:,} ms)"]
        for i in timings["inputs"]:
            lines.append(f"   {i['ms']:>8,} ms  {i['name']}"
                         + ("   << SLOW" if i["slow"] else ""))
        for p in timings["probes"]:
            lines.append(f"   {p['ms']:>8,} ms    integrity/{p['name']}")
    return "\n".join(lines)


def run(db, engine, include_timings: bool = False) -> dict[str, Any]:
    """All sections, each independently fault-isolated: one broken catalog
    view must not blank the whole panel."""
    out: dict[str, Any] = {"backend": db.dialect, "problems": []}
    for key, fn in (("stats", lambda: stats_age(db)),
                    ("indexes", lambda: index_summary(db))):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    timings = None
    if include_timings:
        try:
            timings = input_timings(engine)
            out["timings"] = timings
        except Exception as e:
            out["timings"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    if out.get("stats", {}).get("available"):
        for r in out["stats"]["tables"]:
            if r["stale"]:
                out["problems"].append(
                    f"{r['table']}: statistics "
                    + ("never gathered" if r["last_analyzed"] is None
                       else f"{r['age_days']} days old")
                    + " — the optimizer is planning on stale row counts")
    for t in out.get("indexes", {}).get("tables", []):
        if not t["indexes"]:
            out["problems"].append(f"{t['table']}: {t['note']}")
    if timings:
        for i in timings["inputs"]:
            if i["slow"]:
                out["problems"].append(
                    f"close-readiness input {i['name']}: {i['ms']:,} ms")
    try:
        out["dba_summary"] = _dba_summary(
            db, out.get("stats", {}), out.get("indexes", {}), timings)
    except Exception as e:
        out["dba_summary"] = f"summary unavailable: {type(e).__name__}"
    return out
