#!/usr/bin/env python3
"""Build the offline metadata intelligence catalog.

    python scripts/build_metadata_catalog.py
    python scripts/build_metadata_catalog.py --source default,warehouse
    python scripts/build_metadata_catalog.py --out /secure/path/catalog.db

The builder reads database and PeopleTools catalogs with SELECT/PRAGMA only.
It never samples source rows.  Each configured database receives a separate
SQLite artifact containing its metadata nodes and relationship edges.  Every
artifact is built beside its target and atomically replaces only that source's
prior file after a usable snapshot is complete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config                            # noqa: E402
from pstb.db import Database                                   # noqa: E402
from pstb.metadata import (MetadataBuildLimits, MetadataError, # noqa: E402
                           build_catalog, catalog_path,
                           write_build_status)
from pstb.sources import SourceRegistry                        # noqa: E402


def _say(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, flush=True)


def _prior_snapshot_id(path: Path) -> str:
    """Read only the prior artifact identity; an unreadable file is none."""
    if not path.is_file():
        return ""
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM meta WHERE key='snapshot_id'"
            ).fetchone()
            value = str(row[0] if row else "")
            return value if len(value) <= 128 else ""
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return ""


def _record_build_status(path: Path, record: dict) -> bool:
    """Persist the acceptance state without ever modifying the artifact."""
    try:
        write_build_status(path, record)
    except (MetadataError, OSError) as exc:
        print("WARNING: metadata build status could not be recorded "
              f"for {record.get('source_database')}: {type(exc).__name__}")
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only metadata catalog for this deployment.")
    parser.add_argument("--config", default="", help="path to config.yaml")
    parser.add_argument("--out", default="",
                        help="artifact path (only with exactly one --source)")
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
        # Preserve the explicit opt-out: a source other than the configured
        # PeopleTools source is native structure only, and the operator must
        # say that deliberately rather than silently assuming PeopleTools is
        # absent.  The wording retains the old operational keyword while
        # making the new per-source replacement scope precise.
        print(
            f"--peopletools-source {args.peopletools_source!r} is not in "
            f"--source ({', '.join(requested)}).\n"
            "--source REPLACES the whole artifact for each named source — "
            "it never patches another source's file. To rebuild the "
            "PeopleTools source too, name it, e.g.\n"
            f"    --source {','.join(registry.names())}\n"
            "Use --peopletools-source none only to build native database "
            "structure WITHOUT any PeopleTools layer; every unmentioned "
            "source artifact remains unchanged.")
        primary.close()
        return 2
    if args.out and len(requested) != 1:
        print("--out can be used only with exactly one --source; otherwise "
              "one path would collapse isolated source artifacts together.")
        primary.close()
        return 2
    # State the isolation boundary even on a diagnostic one-source refresh.
    # Existing runbooks looked for "will NOT contain" before a narrow build;
    # it now describes the selected file, while also saying the other files
    # are preserved.
    dropped = [name for name in registry.names() if name not in requested]
    if dropped:
        print(f"NOTE: --source names {', '.join(requested)}; the rebuilt "
              f"source artifact will NOT contain {', '.join(dropped)}. "
              "Their separate artifacts remain unchanged; run without "
              "--source to refresh every configured source.")

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

    _say("sources: " + ", ".join(requested), args.quiet)
    _say(
        f"limits: {limits.max_objects:,} objects/source; "
        f"{limits.max_fields:,} columns/source; "
        f"{limits.max_indexes:,} indexes/source; "
        f"{limits.max_constraints:,} constraints/source; "
        f"{limits.max_constraint_columns:,} constraint columns/source; "
        f"{limits.max_dependencies:,} dependencies/source; "
        f"{limits.max_peopletools_rows:,} rows/PeopleTools layer; "
        f"{limits.query_page_size:,}/page", args.quiet)

    failures = []
    built = []
    opened_dbs = [primary]
    try:
        for name in requested:
            out = (Path(args.out) if args.out else catalog_path(cfg, name))
            _say(f"{name} artifact: {out}", args.quiet)
            database = registry.get(name)
            schemas = list(getattr(database, "allowed_schemas", ()) or ())
            if schemas:
                rendered = [f"{schemas[0]} (default)", *schemas[1:]]
                _say(f"{name} schemas: {', '.join(rendered)}", args.quiet)
            if all(id(database) != id(opened) for opened in opened_dbs):
                opened_dbs.append(database)
            build_run_id = uuid.uuid4().hex
            attempted_at = dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0).isoformat().replace("+00:00", "Z")
            previous_snapshot_id = _prior_snapshot_id(out)
            pending_status = {
                "source_database": name,
                "build_run_id": build_run_id,
                "attempted_at": attempted_at,
                "published": False,
                "status": "building",
                "previous_snapshot_id": previous_snapshot_id,
                "schema_coverage": {
                    "default": schemas[0] if schemas else "",
                    "configured": schemas,
                    "object_counts": {schema: 0 for schema in schemas},
                    "missing": schemas,
                    "complete": False,
                },
            }
            if not _record_build_status(out, pending_status):
                exc = MetadataError(
                    "The refresh attempt could not be durably recorded; "
                    "the prior artifact was not touched")
                failures.append((name, exc))
                print(f"Metadata catalog for {name} was NOT rebuilt: {exc}")
                continue
            started = time.perf_counter()
            try:
                info = build_catalog(
                    out, [(name, database)], limits=limits,
                    peopletools_source=(
                        name if args.peopletools_source == name else "none"))
                elapsed = time.perf_counter() - started
            except Exception as exc:  # atomic builder preserves this source
                _record_build_status(out, {
                    "source_database": name,
                    "build_run_id": build_run_id,
                    "attempted_at": attempted_at,
                    "published": False,
                    "status": "failed",
                    "previous_snapshot_id": previous_snapshot_id,
                    "failure_category": (
                        "metadata_unavailable"
                        if isinstance(exc, MetadataError) else "build_error"),
                    "schema_coverage": {
                        "default": schemas[0] if schemas else "",
                        "configured": schemas,
                        "object_counts": {schema: 0 for schema in schemas},
                        "missing": schemas,
                        "complete": False,
                    },
                })
                failures.append((name, exc))
                print(f"Metadata catalog for {name} was NOT replaced: {exc}")
                print(f"The prior {name} artifact, if any, remains readable.")
                continue
            built.append((name, out, info, elapsed))
            print(f"wrote {int(info['nodes']):,} metadata facts and "
                  f"{int(info['edges']):,} relationships for {name} to "
                  f"{out} in {elapsed:.1f}s")
            print("search: " + ("full text" if info["fts"] == "yes"
                                else "substring fallback"))
            if info.get("partial") == "yes":
                print(f"PARTIAL {name}: a configured layer limit or read "
                      "error was recorded. Run describe_metadata_catalog "
                      f"with source={name!r} for exact coverage.")
            try:
                coverage = json.loads(info.get("schema_coverage") or "{}") \
                    .get(name, {})
            except (TypeError, ValueError):
                coverage = {}
            status_recorded = _record_build_status(out, {
                "source_database": name,
                "build_run_id": build_run_id,
                "attempted_at": attempted_at,
                "published": True,
                "status": ("partial" if info.get("partial") == "yes"
                           else "complete"),
                "snapshot_id": info.get("snapshot_id", ""),
                "previous_snapshot_id": previous_snapshot_id,
                "schema_coverage": coverage,
            })
            if not status_recorded:
                exc = MetadataError(
                    "The artifact was published, but its matching build "
                    "status could not be recorded; deployment acceptance "
                    "must remain failed until a clean refresh")
                failures.append((name, exc))
                print(f"STATUS INCOMPLETE {name}: {exc}")
            if coverage.get("missing"):
                print(
                    f"MISSING SCHEMAS {name}: "
                    + ", ".join(coverage["missing"])
                    + ". Verify the service/PDB, exact owner names, and "
                    "normal-session catalog grants."
                )
            if info.get("degraded"):
                print("DEGRADED sources: " + info["degraded"])
    finally:
        closed = set()
        for database in opened_dbs:
            if id(database) not in closed:
                database.close()
                closed.add(id(database))
        if id(primary) not in closed:  # defensive for alternate registries
            primary.close()
    if built:
        print("Each artifact contains structural metadata and relationship "
              "edges only, not source rows or financial evidence.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
