#!/usr/bin/env python3
"""Time each database step the trial-balance tools perform, so a slow or hanging
query can be pinned to a specific statement.

    .venv/bin/python scripts/diagnose_db.py
    .venv/bin/python scripts/diagnose_db.py --bu 10000 --ledger ACTUALS --fy 2024 --period 6

Each step prints its elapsed time and row count. The first step that takes
minutes is the one to index or narrow. Use --sql to print the statement.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import queries as q  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402


PROBLEMS: list = []


def timed(label: str, fn, show_sql: str = "", expect_rows: bool = False,
          remedy: str = "") -> object:
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        print(f"  [FAIL {ms:8.0f} ms] {label}\n      {type(e).__name__}: {str(e)[:200]}")
        if remedy:
            print(f"      -> {remedy}")
        PROBLEMS.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
        if show_sql:
            print("      SQL:\n" + "\n".join("        " + l for l in show_sql.splitlines()))
        return None
    ms = (time.perf_counter() - t0) * 1000
    n = len(out[0]) if isinstance(out, tuple) else (len(out) if hasattr(out, "__len__") else 1)
    flag = "  <-- SLOW" if ms > 5000 else ""
    if expect_rows and not n:
        # An empty discovery step is a misconfiguration, not a pass. Reporting
        # [ok] here is how a completely wrong scope reached the chat UI.
        print(f"  [EMPTY{ms:8.0f} ms] {label} — 0 rows")
        if remedy:
            print(f"      -> {remedy}")
        PROBLEMS.append(f"{label}: returned no rows")
        return out
    print(f"  [ok   {ms:8.0f} ms] {label} ({n} rows){flag}")
    if show_sql:
        print("      SQL:\n" + "\n".join("        " + l for l in show_sql.splitlines()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Time the trial-balance database steps")
    ap.add_argument("--bu", default="")
    ap.add_argument("--ledger", default="")
    ap.add_argument("--fy", type=int, default=0)
    ap.add_argument("--period", type=int, default=0)
    ap.add_argument("--sql", action="store_true", help="print each statement")
    args = ap.parse_args()

    # Resolve config from the REPO, not the current directory: running
    # `cd scripts && python diagnose_db.py` otherwise silently loads the
    # built-in defaults (backend=sqlite) and reports the wrong database.
    cfg = load_config(os.environ.get('PSTB_CONFIG')
                      or str(ROOT / 'config.yaml'))
    db = Database(cfg)
    engine = TBEngine(db, cfg)
    bu = args.bu or cfg.defaults.business_unit
    led = args.ledger or cfg.defaults.ledger
    p = db.prefix

    from pstb.version import label as _build_label
    print(f"build: {_build_label()}")
    print(f"backend={cfg.db.backend} schema={cfg.db.schema or '(none)'} "
          f"views={cfg.db.use_views} timeout={cfg.db.query_timeout_seconds}s")
    print(f"scope: BU={bu} ledger={led} fy={args.fy or 'current'} period={args.period or 'current'}\n")

    print("1. Connectivity")
    probe = "SELECT 1 AS ok FROM DUAL" if db.dialect == "oracle" else "SELECT 1 AS ok"
    if timed("connect + trivial select", lambda: db.query(probe, {}, max_rows=1)) is None:
        print("\nCannot reach the database. Check ORACLE_DSN / ORACLE_USER / "
              "ORACLE_PASSWORD in .env and network access to the host.")
        return 1

    print("\n2. Dimension tables (should be milliseconds)")
    timed("PS_BUS_UNIT_TBL_GL",
          lambda: db.query(q.business_units(db), {}, max_rows=50),
          expect_rows=True,
          remedy="No GL business units visible — check db.schema and SELECT grants.")
    timed("PS_SET_CNTRL_REC (setid)",
          lambda: db.query(q.setid_for(db), {"bu": bu, "recname": "GL_ACCOUNT_TBL"}, max_rows=1))
    timed("PS_CAL_DETP_TBL (calendar)",
          lambda: db.query(q.cal_periods(db),
                           {"setid": cfg.defaults.setid, "cal": cfg.defaults.calendar_id,
                            "fy": args.fy or 2025}, max_rows=20))
    timed("PS_GL_ACCOUNT_TBL (effective-dated)",
          lambda: db.query(q.accounts_search(db, "", {"setid": cfg.defaults.setid,
                                                      "qd": "%", "qa": "%"}),
                           {"setid": cfg.defaults.setid, "qd": "%", "qa": "%"}, max_rows=50))

    print("\n3. Ledger existence checks (must be fast — these run on 'no data')")
    timed("BU exists",
          lambda: db.query(db.exists_sql(f"SELECT 1 FROM {p}PS_LEDGER WHERE BUSINESS_UNIT = :bu"),
                           {"bu": bu}, max_rows=1),
          expect_rows=True,
          remedy=f"business unit {bu!r} has no ledger rows — fix "
                 "defaults.business_unit in config.yaml or pass --bu.")
    timed("BU+ledger exists",
          lambda: db.query(db.exists_sql(
              f"SELECT 1 FROM {p}PS_LEDGER WHERE BUSINESS_UNIT = :bu AND LEDGER = :led"),
              {"bu": bu, "led": led}, max_rows=1),
          expect_rows=True,
          remedy=f"ledger {led!r} has no rows for {bu!r} — fix defaults.ledger "
                 "in config.yaml or pass --ledger.")

    print("\n4. The trial-balance aggregate (the expensive one)")
    fy, per = args.fy, args.period
    if not fy or not per:
        try:
            cur = engine.resolve_period("")
            fy = fy or cur["fiscal_year"]
            per = per or cur["period"]
        except Exception:
            fy, per = fy or 2025, per or 12
    params: dict = {"bu": bu, "ledger": led, "fy": fy, "maxper": per,
                    "setid": engine.resolve_setid(bu)}
    sql = q.tb_period_sums(db, extras=[], include_adj=False,
                           adj_periods=cfg.defaults.adjustment_periods,
                           dept="", currency="", account="", params=params,
                           base_currency=engine.base_currency_for(bu))
    timed(f"TB aggregate FY{fy} P{per}", lambda: db.query(sql, params, max_rows=100_000),
          sql if args.sql else "")

    print("\n5. Same aggregate, one account only (isolates scan vs. volume)")
    params2: dict = {"bu": bu, "ledger": led, "fy": fy, "maxper": per,
                     "setid": engine.resolve_setid(bu)}
    sql2 = q.tb_period_sums(db, extras=[], include_adj=False,
                            adj_periods=cfg.defaults.adjustment_periods,
                            dept="", currency="", account="1%", params=params2,
                            base_currency=engine.base_currency_for(bu))
    timed("TB aggregate, accounts 1%", lambda: db.query(sql2, params2, max_rows=100_000))

    print("\n6. Record-shape audit (every record the tools touch, one pass)")
    try:
        audit = engine.audit_record_shapes()
        for t in audit["unreadable"]:
            print(f"   [WARN] {t}: not readable (missing table or grants)")
            PROBLEMS.append(f"{t}: not readable")
        for t, cols in audit["missing_required"].items():
            print(f"   [FAIL] {t}: missing REQUIRED column(s) {', '.join(cols)}")
            PROBLEMS.append(f"{t}: missing required {', '.join(cols)}")
        for t, cols in audit["missing_optional"].items():
            print(f"   [note] {t}: no {', '.join(cols)} here — the tools adapt "
                  "and disclose it")
        if audit["clean"]:
            print(f"   [ok]   {len(audit['clean'])} record(s) match the "
                  "reference shape exactly")
    except Exception as e:
        print(f"   shape audit failed: {e}")

    print("\n6b. AR / Billing record shapes (what the curated tools adapt to)")
    try:
        from pstb.ar import ARBilling
        arb = ARBilling(engine)
        for t in ("PS_ITEM", "PS_CUSTOMER", "PS_BI_HDR", "PS_INTFC_BI"):
            cols = sorted(arb._cols(t))
            print(f"   {t}: " + (", ".join(cols) if cols
                                 else "NOT READABLE (missing table or grants)"))
        shape = arb._item_shape()
        print(f"   -> item dating column: {shape['date'] or '(none; DUE_DT only)'}"
              f" | due: {shape['due'] or '-'}"
              f" | dispute: {shape['dispute'] or '-'}"
              f" | currency: {shape['currency'] or '-'}")
        for n in shape["notes"]:
            print(f"   note: {n}")
    except Exception as e:
        print(f"   AR shape check failed: {e}")

    print("""
Reading this:
  - Step 4 slow but step 5 fast    -> volume; narrow the scope or add an index
                                      on PS_LEDGER (BUSINESS_UNIT, LEDGER,
                                      FISCAL_YEAR, ACCOUNTING_PERIOD, ACCOUNT).
  - Steps 4 and 5 both slow        -> the ledger scan itself; check that the
                                      delivered PSALEDGER index exists and that
                                      statistics are current.
  - Step 3 slow                    -> no usable index on BUSINESS_UNIT.
  - Step 2 slow                    -> schema/grants problem, not volume.
  - Anything fails with a timeout  -> raise db.query_timeout_seconds, or narrow.
""")
    db.close()
    if PROBLEMS:
        print("\nPRE-FLIGHT FAILED — fix these before using the agent:")
        for pb in PROBLEMS:
            print(f"  - {pb}")
        return 1
    print("\nPre-flight OK: scope resolves, records readable, no empty discovery steps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
