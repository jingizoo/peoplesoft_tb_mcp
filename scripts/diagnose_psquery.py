#!/usr/bin/env python3
"""Why PSQuery execution is or is not live on THIS host.

    .venv/bin/python scripts/diagnose_psquery.py
    .venv/bin/python scripts/diagnose_psquery.py --query AP_AGING_BY_VENDOR

Six things must all be true before run_ps_query reaches a real instance,
and when one is missing the connector falls back to FIXTURES rather than
failing. That fallback is deliberate — the demo must work with no
credentials — but it means a half-configured site gets sample rows that
look real, and the only tell is `mode: fixtures` inside the payload.

This checks each precondition in order and names the exact fix for the
first one that is unmet. It reads; it never writes. The password is never
printed, only whether one is set.

Exit code 0 when execution is live, 1 otherwise.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, NO, HM = "  [ok]  ", "  [--]  ", "  [??]  "


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="",
                    help="also try executing this query (read-only)")
    args = ap.parse_args()

    from pstb.config import load_config
    from pstb.connectors import ConnectorError
    from pstb.connectors.psquery_api import from_config, rest_base
    from pstb.db import Database
    from pstb.engine import TBEngine
    from pstb.psquery import QueryCatalog

    cfg = load_config(os.environ.get("PSTB_CONFIG")
                      or str(ROOT / "config.yaml"))
    api = cfg.ps_api
    problems: list[str] = []

    print("PSQuery / Query Access Service — configuration check\n")

    # 1. The switch.
    if api.enabled:
        print(OK + "ps_api.enabled: true")
    else:
        print(NO + "ps_api.enabled is false")
        problems.append(
            "Set ps_api.enabled: true in config.yaml. Until then every "
            "run_ps_query returns FIXTURE rows (the payload says "
            "mode: fixtures) so the demo works with no credentials.")

    # 2. Credentials. The user is disclosed on purpose — it explains the
    #    row set. The password never is.
    user = os.environ.get("PSFT_QAS_USER", api.user or "")
    has_pw = bool(os.environ.get("PSFT_QAS_PASSWORD", api.password or ""))
    if user:
        print(OK + f"PSFT_QAS_USER: {user}")
    else:
        print(NO + "PSFT_QAS_USER is not set")
        problems.append(
            "Add PSFT_QAS_USER to .env — a PeopleSoft user, not a database "
            "account. Queries run AS this user and return what their "
            "permission lists allow, which is why figures may legitimately "
            "differ from the curated tools.")
    print((OK if has_pw else NO)
          + f"PSFT_QAS_PASSWORD: {'set' if has_pw else 'not set'}")
    if not has_pw:
        problems.append("Add PSFT_QAS_PASSWORD to .env (chmod 600 the file).")

    # 3. The gateway, discovered from the site's own IB catalog.
    configured = (api.target_location or "").strip()
    discovered, source = "", ""
    if configured:
        discovered, source = configured, "ps_api.target_location"
    else:
        try:
            cat = QueryCatalog(TBEngine(Database(cfg), cfg))
            found = cat.integration_endpoints()
            discovered = (found.get("target_location") or "").strip()
            source = found.get("target_source", "PSIBSVCSETUP")
            if not found.get("catalog_readable", True):
                print(HM + "IB operation catalog is not readable — on Oracle "
                           "a missing table and a missing SELECT grant raise "
                           "the same ORA-00942")
        except Exception as e:
            print(HM + f"IB discovery failed: {type(e).__name__}: "
                       f"{str(e)[:80]}")
    if discovered:
        print(OK + f"gateway: {discovered}")
        print(f"         (from {source})")
    else:
        print(NO + "no gateway discovered and none configured")
        problems.append(
            "Set ps_api.target_location in config.yaml to the REST "
            "listening connector, e.g. "
            "http://<host>:<port>/PSIGW/RESTListeningConnector — or grant "
            "SELECT on PSIBSVCSETUP so it can be discovered.")

    base = rest_base(discovered)
    if base and base != discovered:
        print(OK + f"REST base derived: {base}")
    elif base:
        print(OK + f"REST base: {base}")

    node = os.environ.get("PSFT_QAS_NODE", "PSFT_FS")
    print(OK + f"node: {node}"
          + ("" if "PSFT_QAS_NODE" in os.environ else "   (default)"))
    if "PSFT_QAS_NODE" not in os.environ:
        print("         set PSFT_QAS_NODE in .env if your site differs — "
              "a wrong node is the usual first 404")

    # 4. What the connector actually resolved to.
    qas = from_config(cfg, discovered)
    live = qas.mode != "fixtures"
    print((OK if live else NO) + f"connector mode: {qas.mode}")
    if not live:
        print("\n" + "-" * 62)
        print("NOT LIVE — run_ps_query is answering from bundled fixtures.")
        for i, p in enumerate(problems or ["Unknown: enabled, credentials "
                                           "and gateway all look present."],
                              1):
            print(f"\n  {i}. {p}")
        print("\nThe curated database tools are unaffected either way.")
        sys.exit(1)

    # 5. Reach it. QAS_LISTQUERIES is on the read-only allowlist.
    print("\ncontacting the service (read-only)...")
    try:
        out = qas.execute(args.query or "", {}) if args.query else None
    except ConnectorError as e:
        if args.query:
            print(NO + "execute failed:\n")
            print("    " + str(e).replace("\n", "\n    "))
            sys.exit(1)
        out = None
    if args.query and out:
        print(OK + f"{args.query}: {out['row_count']} row(s), "
                   f"executed as {out['executed_as']}")
        print(f"         columns: {', '.join(out['columns'][:8])}")
    elif not args.query:
        print("         pass --query <NAME> to execute one end to end; "
              "find a name with search_ps_queries")
    print("\nLIVE. Results reflect the PeopleSoft user's permission lists, "
          "so they may\nlegitimately differ from the curated tools, which "
          "read through the database\naccount and bypass row-level "
          "security. Say so if the figures disagree.")


if __name__ == "__main__":
    main()
