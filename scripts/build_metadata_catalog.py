#!/usr/bin/env python3
"""Build the offline metadata intelligence catalog.

    python scripts/build_metadata_catalog.py
    python scripts/build_metadata_catalog.py --source default,warehouse
    python scripts/build_metadata_catalog.py --out /secure/path/catalog.db

The builder reads database and PeopleTools catalogs with SELECT/PRAGMA only.
It never samples source rows.  A new SQLite artifact is built beside the
target and atomically replaces it only after a usable snapshot is complete;
on failure the prior artifact remains intact.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config                            # noqa: E402
from pstb.db import Database                                   # noqa: E402
from pstb.metadata import (MetadataBuildLimits, MetadataError, # noqa: E402
                           build_catalog, catalog_path)
from pstb.sources import SourceRegistry                        # noqa: E402


def _say(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only metadata catalog for this deployment.")
    parser.add_argument("--config", default="", help="path to config.yaml")
    parser.add_argument("--out", default="",
                        help="artifact path (default: beside config.yaml)")
    parser.add_argument(
        "--source", default="",
        help="comma-separated configured sources (default: all)")
    parser.add_argument(
        "--peopletools-source", default="default",
        help="source carrying PSRECDEFN etc. (default: default)")
    parser.add_argument("--max-objects", type=int, default=None)
    parser.add_argument("--max-fields", type=int, default=None)
    parser.add_argument("--max-indexes", type=int, default=None)
    parser.add_argument("--max-constraints", type=int, default=None)
    parser.add_argument("--max-constraint-columns", type=int, default=None)
    parser.add_argument("--max-dependencies", type=int, default=None)
    parser.add_argument("--max-peopletools-rows", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config or None)
    primary = Database(cfg)
    registry = SourceRegistry(cfg, primary)
    requested = [value.strip() for value in args.source.split(",")
                 if value.strip()] or registry.names()
    unknown = sorted(set(requested) - set(registry.names()))
    if unknown:
        print("Unknown source(s): " + ", ".join(unknown) + ". Available: " +
              ", ".join(registry.names()))
        primary.close()
        return 2
    if args.peopletools_source not in requested and args.peopletools_source != "none":
        # Point at the fix that KEEPS the artifact whole. Offering "none" as
        # a co-equal escape sent people down the one path that silently
        # halves their catalog: --source rebuilds the whole file, so
        # dropping the PeopleTools source drops PeopleSoft from it.
        print(
            f"--peopletools-source {args.peopletools_source!r} is not in "
            f"--source ({', '.join(requested)}).\n"
            "--source REPLACES the whole artifact — it is not an incremental "
            "refresh — so name every source you want in it, e.g.\n"
            f"    --source {','.join(registry.names())}\n"
            "Use --peopletools-source none only to build native database "
            "structure WITHOUT any PeopleTools layer; the resulting catalog "
            "contains only the sources named above.")
        primary.close()
        return 2
    # A narrower --source is legitimate (diagnosing one connection), but the
    # result is a smaller catalog, not a patched one. Say so before the write
    # rather than leaving it to be discovered through a search that finds
    # nothing — the artifact is replaced atomically and looks complete.
    dropped = [name for name in registry.names() if name not in requested]
    if dropped:
        print(f"NOTE: --source names {', '.join(requested)}; the rebuilt "
              f"catalog will NOT contain {', '.join(dropped)}. Run without "
              "--source to index every configured source.")

    overrides = {}
    for arg_name, limit_name in (
            ("max_objects", "max_objects"),
            ("max_fields", "max_fields"),
            ("max_indexes", "max_indexes"),
            ("max_constraints", "max_constraints"),
            ("max_constraint_columns", "max_constraint_columns"),
            ("max_dependencies", "max_dependencies"),
            ("max_peopletools_rows", "max_peopletools_rows"),
            ("page_size", "query_page_size")):
        value = getattr(args, arg_name)
        if value is not None:
            overrides[limit_name] = value
    try:
        limits = MetadataBuildLimits.from_config(
            cfg.metadata_catalog, **overrides)
    except MetadataError as exc:
        print(f"Invalid metadata catalog limits: {exc}")
        primary.close()
        return 2

    out = Path(args.out) if args.out else catalog_path(cfg)
    opened = []
    try:
        for name in requested:
            opened.append((name, registry.get(name)))
        _say("sources: " + ", ".join(requested), args.quiet)
        _say(f"artifact: {out}", args.quiet)
        _say(
            f"limits: {limits.max_objects:,} objects/source; "
            f"{limits.max_fields:,} columns/source; "
            f"{limits.max_indexes:,} indexes/source; "
            f"{limits.max_constraints:,} constraints/source; "
            f"{limits.max_constraint_columns:,} constraint columns/source; "
            f"{limits.max_dependencies:,} dependencies/source; "
            f"{limits.max_peopletools_rows:,} rows/PeopleTools layer; "
            f"{limits.query_page_size:,}/page", args.quiet)
        started = time.perf_counter()
        info = build_catalog(
            out, opened, limits=limits,
            peopletools_source=args.peopletools_source)
        elapsed = time.perf_counter() - started
    except Exception as exc:  # preserve the old artifact; builder does cleanup
        print(f"Metadata catalog was NOT replaced: {exc}")
        print("The prior artifact, if any, remains readable.")
        return 1
    finally:
        closed = set()
        for _name, database in opened:
            if id(database) not in closed:
                database.close()
                closed.add(id(database))
        if id(primary) not in closed:
            primary.close()

    print(f"wrote {int(info['nodes']):,} metadata facts and "
          f"{int(info['edges']):,} relationships to {out} "
          f"in {elapsed:.1f}s")
    print("search: " + ("full text" if info["fts"] == "yes"
                        else "substring fallback"))
    if info.get("partial") == "yes":
        print("PARTIAL: a configured source/layer limit or read error was "
              "recorded. Run describe_metadata_catalog for exact coverage.")
    if info.get("degraded"):
        print("DEGRADED sources: " + info["degraded"])
    print("The artifact contains structural metadata only, not source rows or "
          "financial evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
