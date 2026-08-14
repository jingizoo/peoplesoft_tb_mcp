#!/usr/bin/env python3
"""Build the process graph: how work flows through THIS installation.

    python scripts/build_process_graph.py            # build beside config.yaml
    python scripts/build_process_graph.py --dry-run  # report, write nothing
    python scripts/build_process_graph.py --only peopletools,record_map
    python scripts/build_process_graph.py --out /tmp/pg.db
    python scripts/build_process_graph.py --row-limit 150000 --max-nodes 250000

Run it once after deployment and again whenever the instance is customized —
a new component, a new page, a new custom record, a rewritten procedure. It
reads metadata and structure only, never balances, and it writes ONE SQLite
file that the agent then reads in milliseconds.

Why offline. Harvesting PeopleTools metadata is minutes of catalog reads on a
real instance. Doing it per question would put that on the critical path of a
chat turn, which is the cost this project protects hardest. Build is slow and
rare; read is fast and constant.

Safe to re-run: the graph is built beside the target and renamed over it, so
a rebuild never leaves a half-written file for the running GUI to read.

Exit codes: 0 built (possibly with degraded/partial sources), 1 harvest or
safe build failed and the prior graph was preserved, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import procgraph as pg                                # noqa: E402
from pstb.config import load_config                             # noqa: E402
from pstb.db import Database                                    # noqa: E402
from pstb.engine import TBEngine                                # noqa: E402

ALL_SOURCES = ("peopletools", "record_map", "joins", "scopes", "wiki",
               "site_memory")


def _say(msg: str) -> None:
    print(msg, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the process knowledge graph for this deployment.")
    ap.add_argument("--config", default="", help="path to config.yaml")
    ap.add_argument("--out", default="",
                    help=f"output file (default: {pg.DEFAULT_FILENAME} "
                         "beside the config)")
    ap.add_argument("--only", default="",
                    help="comma-separated subset of: " + ", ".join(ALL_SOURCES))
    ap.add_argument("--skip", default="", help="sources to leave out")
    ap.add_argument("--dry-run", action="store_true",
                    help="harvest and report, write nothing")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--row-limit", type=int, default=None,
        help="override every PeopleTools catalog row limit for this build")
    ap.add_argument("--page-size", type=int, default=None,
                    help="catalog rows fetched per keyset page")
    ap.add_argument("--max-nodes", type=int, default=None,
                    help="maximum nodes in the finished artifact")
    ap.add_argument("--max-edges", type=int, default=None,
                    help="maximum edges in the finished artifact")
    ap.add_argument("--memory-mb", type=int, default=None,
                    help="estimated build-memory budget in MiB")
    args = ap.parse_args(argv)

    chosen = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    skipped = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    unknown = ({*chosen, *skipped}) - set(ALL_SOURCES)
    if unknown:
        _say(f"unknown source(s): {', '.join(sorted(unknown))}\n"
             f"known: {', '.join(ALL_SOURCES)}")
        return 2
    sources = [s for s in (chosen or ALL_SOURCES) if s not in skipped]
    if not sources:
        _say("every source was skipped; nothing to build.")
        return 2

    cfg = load_config(args.config or None)
    overrides = {}
    if args.row_limit is not None:
        overrides.update({name: args.row_limit for name in (
            "max_records", "max_pages", "max_page_fields",
            "max_components", "max_navigation", "max_queries")})
    for arg_name, limit_name in (
            ("page_size", "query_page_size"),
            ("max_nodes", "max_nodes"), ("max_edges", "max_edges"),
            ("memory_mb", "memory_budget_mb")):
        if getattr(args, arg_name) is not None:
            overrides[limit_name] = getattr(args, arg_name)
    try:
        limits = pg.GraphBuildLimits.from_config(cfg.process_graph,
                                                 **overrides)
    except pg.ProcessGraphError as e:
        _say(f"invalid process graph limits: {e}")
        return 2
    out = Path(args.out) if args.out else pg.graph_path(cfg)
    db = Database(cfg)
    engine = TBEngine(db, cfg)

    def log(msg: str) -> None:
        if not args.quiet:
            _say(msg)

    log(f"backend {cfg.db.backend} -> {out}")
    log("limits: catalog rows up to "
        f"{max(limits.max_records, limits.max_pages, limits.max_page_fields, limits.max_components, limits.max_navigation, limits.max_queries):,}; "
        f"{limits.max_nodes:,} nodes; {limits.max_edges:,} edges; "
        f"{limits.memory_budget_mb:,} MiB")
    harvests = []
    timings = {}

    # The curated map first: it defines the record universe the join and wiki
    # harvesters work over. Computed even when record_map is NOT one of the
    # chosen sources — otherwise `--only wiki` quietly has nothing to search
    # for and reports a clean zero, which reads as "the wiki has nothing".
    curated = pg.harvest_record_map(engine)
    records = sorted({n["name"] for n in curated.nodes.values()
                      if n["kind"] in ("record", "setup")})
    modules = sorted({n["name"] for n in curated.nodes.values()
                      if n["kind"] == "module"})
    if "record_map" in sources:
        harvests.append(curated)
        log(f"  record_map    {len(curated.nodes):7,} nodes  "
            f"{len(curated.edges):7,} edges")

    for name, fn in (("peopletools",
                      lambda: pg.harvest_peopletools(db, limits)),
                     ("scopes", lambda: pg.harvest_scopes(engine)),
                     ("joins", lambda: pg.harvest_joins(engine, records)),
                     ("wiki", lambda: _wiki(cfg, records, modules)),
                     ("site_memory", lambda: _memory(cfg))):
        if name not in sources:
            continue
        t = time.perf_counter()
        try:
            h = fn()
        except Exception as e:                          # noqa: BLE001
            h = pg.Harvest(name)
            h.note(f"the {name} harvester failed outright "
                   f"({type(e).__name__}: {e}).", ok=False)
        timings[name] = time.perf_counter() - t
        harvests.append(h)
        log(f"  {name:<13} {len(h.nodes):7,} nodes  "
            f"{len(h.edges):7,} edges"
            + ("   PARTIAL" if h.partial else
               "" if h.ok else "   DEGRADED"))
        for note in h.notes:
            log(f"      - {note}")

    total_nodes = len({nid for h in harvests for nid in h.nodes})
    if not total_nodes:
        _say("\nNothing could be harvested. The graph was NOT written.\n"
             "Check the read-only grants: PSRECDEFN and PSPNLFIELD are the "
             "two that carry most of it.")
        db.close()
        return 1

    if args.dry_run:
        total_edges = len({key for h in harvests for key in h.edges})
        _say(f"\ndry run: approximately {total_nodes:,} nodes / "
             f"{total_edges:,} edges would be written to {out}")
        if any(h.partial for h in harvests):
            _say("PARTIAL: at least one configured catalog limit was reached; "
                 "the notes above identify it.")
        db.close()
        return 0

    try:
        info = pg.write_graph(out, harvests, meta={
            "backend": cfg.db.backend,
            "build_seconds": round(sum(timings.values()), 1),
        }, limits=limits)
    except Exception as e:                              # noqa: BLE001
        _say(f"\nProcess graph was NOT replaced: {e}")
        _say("The prior artifact, if any, remains readable.")
        db.close()
        return 1
    _say(f"\nwrote {info['nodes']} nodes / {info['edges']} edges to {out}")
    _say(f"seed search: {'full text' if info['fts'] == 'yes' else 'substring'}")
    if info.get("degraded"):
        _say(f"DEGRADED sources: {info['degraded']} — the graph is usable "
             "but has holes; the notes above say where.")
    if info.get("partial") == "yes":
        _say("PARTIAL graph: one or more configured catalog limits were "
             "reached. Run describe_process_graph for structured limit hits.")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    db.close()
    _say("\nAsk the agent a process question now, e.g. "
         '"how do we do invoicing", "what maintains customer credit".')
    return 0


def _wiki(cfg, records, modules):
    from pstb.wiki import make_wiki
    return pg.harvest_wiki(make_wiki(cfg), records, modules)


def _memory(cfg):
    from pstb.memory import SiteMemory
    return pg.harvest_memory(SiteMemory(cfg.resolve_path("site_memory.json")))


if __name__ == "__main__":
    raise SystemExit(main())
