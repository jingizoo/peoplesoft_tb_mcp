#!/usr/bin/env python3
"""What business-unit security looks like from where this app is standing.

    .venv/bin/python scripts/diagnose_bu_security.py
    .venv/bin/python scripts/diagnose_bu_security.py --user FIN_US001

Run this on the HOST before switching security.enabled on. Every question
it answers is one that cannot be answered from here: whether this site
keeps unit security by user or by permission list, whether the read-only
reporting account can SELECT those records at all, and what a real user ID
resolves to. Guessing any of it produces an app that shows one site
everything and the next site nothing.

Exit code 1 when security is switched on and could not be read — so it can
gate a deployment rather than being something someone remembers to look at.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Report PeopleSoft business-unit security readability")
    ap.add_argument("--user", default="",
                    help="resolve one user ID and print its units")
    ap.add_argument("--config", default="", help="path to config.yaml")
    args = ap.parse_args()

    from pstb.config import load_config
    from pstb.db import Database, DbError
    from pstb.security import (CLASS_UNIT_RECORDS, OPERATOR_RECORD,
                               ROW_SECURITY_FIELD, USER_UNIT_RECORDS,
                               RowSecurity, SecurityError)

    cfg = load_config(args.config or str(ROOT / "config.yaml"))
    db = Database(cfg)
    sec = RowSecurity(db, cfg)

    print(f"\n  backend {cfg.db.backend}"
          f"{f' · schema {cfg.db.schema}' if cfg.db.schema else ''}")
    print(f"  security.enabled: {cfg.security.enabled}")
    print(f"  privileged_users: "
          f"{', '.join(cfg.security.privileged_users) or '(none)'}")
    print(f"  on_unavailable:   {cfg.security.on_unavailable}\n")

    # 1. Which records can this account actually read?
    print("  Records this account can read:")
    readable = {}
    for record, key in ((OPERATOR_RECORD, "OPRID"),) + USER_UNIT_RECORDS \
            + CLASS_UNIT_RECORDS:
        try:
            cols = {c.upper() for c in db.columns(record)}
        except Exception as e:
            cols = set()
            print(f"    {record:<18} ERROR {type(e).__name__}: {e}")
            continue
        readable[record] = cols
        if not cols:
            print(f"    {record:<18} not readable (missing, or no SELECT "
                  f"grant for {cfg.db.oracle_user or 'this account'})")
            continue
        has_key = key in cols
        has_bu = "BUSINESS_UNIT" in cols
        note = []
        if record == OPERATOR_RECORD:
            note.append("ROWSECCLASS present"
                        if ROW_SECURITY_FIELD in cols
                        else "no ROWSECCLASS — class-based security "
                             "cannot be resolved")
        else:
            note.append(f"{key} {'yes' if has_key else 'MISSING'}")
            note.append(f"BUSINESS_UNIT {'yes' if has_bu else 'MISSING'}")
        print(f"    {record:<18} {len(cols):>3} columns · {', '.join(note)}")

    # 2. What did discovery settle on?
    record, key, kind = sec.source_record()
    print()
    if record:
        print(f"  Security source: {record} keyed on {key} ({kind}-level)")
        if kind == "class":
            print(f"    users reach it through {OPERATOR_RECORD}."
                  f"{ROW_SECURITY_FIELD}")
    else:
        print("  Security source: NONE FOUND.")
        print("    Nothing to enforce with. If this site keeps unit security")
        print("    in a custom record, set security.unit_record and")
        print("    security.unit_key in config.yaml.")

    # 3. How much is in it, and does it look populated?
    if record:
        try:
            rows, _ = db.query(
                f"SELECT COUNT(*) AS n FROM {db.prefix}{record}", {},
                max_rows=1)
            total = rows[0].get("n") if rows else 0
            units, _ = db.query(
                f"SELECT DISTINCT BUSINESS_UNIT AS bu FROM "
                f"{db.prefix}{record}", {}, max_rows=50)
            names = sorted(str(r.get("bu") or "").strip()
                           for r in units if str(r.get("bu") or "").strip())
            print(f"    {total} row(s), {len(names)} distinct unit(s)"
                  + (f": {', '.join(names[:12])}"
                     f"{'…' if len(names) > 12 else ''}" if names else ""))
            if not total:
                print("    EMPTY — every user would resolve to no units. "
                      "That is a real PeopleSoft state, but check it is the "
                      "intended one before switching security on.")
        except DbError as e:
            print(f"    could not count rows: {e}")

    # 4. Resolve a real user, which is the only end-to-end proof.
    if args.user:
        print(f"\n  Resolving {args.user.upper()}:")
        was = cfg.security.enabled
        cfg.security.enabled = True          # answer the real question
        sec.invalidate()
        try:
            access = sec.access_for(args.user)
            print(f"    {access.describe()}")
            print(f"    source: {access.source or '(none)'}")
            print(f"    {access.detail}")
        except SecurityError as e:
            print(f"    REFUSED: {e}")
        finally:
            cfg.security.enabled = was

    # 5. The verdict, and it gates.
    print()
    if not cfg.security.enabled:
        print("  security.enabled is false: no restriction is applied and "
              "every signed-in session sees every unit.")
        print("  Switch it on in config.yaml (or from /console) once the "
              "records above look right.\n")
        return 0
    if not record:
        print("  FAIL: security is ON and no source record is readable, so "
              "every user would be refused all data.")
        print("  Grant SELECT on the security record, name a custom one in "
              "config.yaml, or set security.enabled: false.\n")
        return 1
    print("  OK: security is on and readable.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
