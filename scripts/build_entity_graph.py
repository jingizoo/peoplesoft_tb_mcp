#!/usr/bin/env python3
"""Build the entity graph: who deals with whom.

    python scripts/build_entity_graph.py                # 12 months, default
    python scripts/build_entity_graph.py --months 24
    python scripts/build_entity_graph.py --dry-run
    python scripts/build_entity_graph.py --out /tmp/eg.db

Customers, suppliers, products and business units, plus the transaction
flows between them, aggregated once so question time is a local index read.

REFRESH CADENCE differs from the process graph, which is why this is its own
script and its own file. The process graph changes when someone CUSTOMIZES
the instance — a new page, a new record — so it is rebuilt after a project.
This one changes when people TRADE, so it is rebuilt nightly. Running them
on the same schedule would either leave this stale or rebuild that for
nothing.

    0 3 * * *  cd /opt/pstb && .venv/bin/python scripts/build_entity_graph.py --quiet

WHAT IT WRITES, AND WHAT IT DOES NOT. Company and product names, ids, unit
codes, transaction counts, dates, and aggregated amounts. No bank accounts,
no tax identifiers, no addresses, no contacts, and no row-level detail. The
file is written 0600 and is git-ignored, because it is derived business data
for one deployment.

Amounts in it are a WEIGHT for ranking, taken at build time. Oracle stays
the record; every tool that reads this stamps its answers with the build
date and names the live tool that confirms a figure.

Exit codes: 0 built, 1 nothing harvested, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import entitygraph as eg                              # noqa: E402
from pstb.config import load_config                             # noqa: E402
from pstb.db import Database                                    # noqa: E402
from pstb.engine import TBEngine                                # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the customer/product/supplier entity graph.")
    ap.add_argument("--config", default="", help="path to config.yaml")
    ap.add_argument("--out", default="",
                    help=f"output file (default: {eg.DEFAULT_FILENAME} "
                         "beside the config)")
    ap.add_argument("--months", type=int, default=12,
                    help="transaction window in months (default 12)")
    ap.add_argument("--as-of", default="",
                    help="window end date, YYYY-MM-DD (default today)")
    ap.add_argument("--dry-run", action="store_true",
                    help="harvest and report, write nothing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.months < 1:
        print("--months must be at least 1")
        return 2

    cfg = load_config(args.config or None)
    out = Path(args.out) if args.out else eg.graph_path(cfg)
    engine = TBEngine(Database(cfg), cfg)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    log(f"backend {cfg.db.backend} -> {out}")
    started = time.perf_counter()
    harvest = eg.harvest_entities(engine, months=args.months,
                                  as_of_date=args.as_of)
    took = time.perf_counter() - started

    kinds: dict = {}
    for node in harvest.nodes.values():
        kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
    for kind, n in sorted(kinds.items()):
        log(f"  {kind:16} {n:6} actors")
    flows: dict = {}
    for (_s, _d, kind), e in harvest.edges.items():
        flows[kind] = flows.get(kind, 0) + max(len(e.get("slices") or {}), 1)
    for kind, n in sorted(flows.items()):
        log(f"  {kind:16} {n:6} flows")
    for note in harvest.notes:
        log(f"      - {note}")

    if not harvest.nodes:
        print("\nNothing could be harvested. The graph was NOT written.\n"
              "Check the read-only grants: PS_BI_HDR and PS_CUSTOMER carry "
              "most of it.")
        return 1
    if args.dry_run:
        print(f"\ndry run: {len(harvest.nodes)} actors would be written "
              f"to {out}")
        return 0

    info = eg.write_graph(out, harvest, meta={
        "backend": cfg.db.backend,
        "build_seconds": round(took, 1),
    })
    print(f"\nwrote {info['actors']} actors / {info['flows']} flows to {out}")
    print(f"window: {info['window_start']} to {info['as_of']} "
          f"({info['window_months']} months), built in {round(took, 1)}s")
    if info.get("degraded"):
        print("DEGRADED: a source could not be read; the notes above say "
              "which, and the graph has a hole there.")
    print("\nAsk the agent an actor question now, e.g. "
          '"which customers buy LIC-SAAS", "what is our customer '
          'concentration", "how is ACME connected to Summit Machining".')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
