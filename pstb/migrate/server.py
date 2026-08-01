"""MCP server exposing the 9.1 -> 9.2 record-porting pipeline over stdio.

Run:  python -m pstb.migrate.server

Same contract as pstb.server: stdio carries the protocol, diagnostics go to
stderr, every tool returns a dict and never raises into the agent loop. The
chat client (Gemini or Ollama) — or any MCP host — drives the cycle:

    migrate_discover -> migrate_plan -> [review, migrate_show_record ...]
    -> migrate_emit -> operator applies in App Designer / Data Mover
    -> migrate_verify_build -> migrate_reconcile -> migrate_status

All tools are read-only against both databases; changes land only through
the emitted artifacts an operator applies with the delivered tools.
"""
from __future__ import annotations

import os
import sys
import threading

try:  # mcp SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

from ..config import load_config
from ..db import Database, DbError
from ..sources import SourceRegistry
from .pipeline import MigratePipeline
from .spec import MigrateError

cfg = load_config(os.environ.get("PSTB_CONFIG"))
mcp = FastMCP("peoplesoft-migrate")

_lock = threading.Lock()
_pipeline: MigratePipeline | None = None


def _get_pipeline() -> MigratePipeline:
    """Built lazily so the server starts (and lists tools) even before
    migrate.source is configured — the error then arrives as a readable tool
    result instead of a dead process."""
    global _pipeline
    with _lock:
        if _pipeline is None:
            primary = Database(cfg)
            _pipeline = MigratePipeline(cfg, SourceRegistry(cfg, primary))
        return _pipeline


def _safe(fn_name: str, /, **kw) -> dict:
    try:
        fn = getattr(_get_pipeline(), fn_name)
        return fn(**kw)
    except (MigrateError, DbError) as e:
        return {"error": str(e)}
    except Exception as e:  # keep the agent loop alive on unexpected failures
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def migrate_discover(mode: str = "", limit: int = 0) -> dict:
    """List candidate custom records on the 9.1 source. mode: 'prefix' (site
    naming standard), 'oprid' (LASTUPDOPRID <> PPLSOFT), or 'both' (default
    from config). Both signals are heuristics — treat the result as a review
    list, and re-run with different modes to see what each signal adds."""
    return _safe("discover", mode=mode, limit=limit)


@mcp.tool()
def migrate_plan(seed_records: str = "", mode: str = "") -> dict:
    """Build the port plan: dependency closure over the seeds (subrecords,
    audit + related-language records, prompt tables, view references), then
    classify every record against 9.2. seed_records: comma-separated list;
    empty = every discovered custom record. Classifications: build_and_load,
    build_definition, load_only, already_present, drift_review, delivered_ok,
    delivered_missing, unknown_source. The plan persists in the state db and
    survives restarts; re-planning refreshes it without losing progress."""
    seeds = [s for s in (seed_records or "").replace("\n", ",").split(",")
             if s.strip()]
    return _safe("plan", seed_records=seeds or None, mode=mode)


@mcp.tool()
def migrate_show_record(recname: str) -> dict:
    """One record in full: 9.1 shape, 9.2 shape, field-level diff, view SQL
    (for views), and current pipeline state. Use before deciding what to do
    about a drift_review or delivered_missing record."""
    return _safe("show_record", recname=recname)


@mcp.tool()
def migrate_emit(out_dir: str = "") -> dict:
    """Write the apply artifacts from the current plan: App Designer project
    record list, Data Mover export/import scripts, per-record drift mapping
    SQL, reconcile probes, plan.json/plan.md and a runbook README. Files only
    — nothing executes against either database."""
    return _safe("emit", out_dir=out_dir)


@mcp.tool()
def migrate_verify_build(recname: str = "") -> dict:
    """After App Designer Build on 9.2: confirm each planned table physically
    exists with the expected columns. Marks verified records built_verified.
    (recname parameter reserved; currently checks the whole plan.)"""
    return _safe("verify_build")


@mcp.tool()
def migrate_reconcile(recnames: str = "") -> dict:
    """After data load: compare row counts and numeric-column sums between
    9.1 and 9.2 for every record with a data plan (or a comma-separated
    subset). Clean records are marked reconciled. Sums catch partial loads
    and zeroed columns that row counts alone would miss."""
    names = [s.strip() for s in (recnames or "").split(",") if s.strip()]
    return _safe("reconcile", recnames=names or None)


@mcp.tool()
def migrate_mark(recname: str, status: str, note: str = "") -> dict:
    """Record a manual step: definitions_exported after the App Designer
    project copy, data_loaded after running the import, blocked (with a note)
    when a record needs a human decision. Statuses: planned,
    definitions_exported, built_verified, data_loaded, reconciled, blocked."""
    return _safe("mark", recname=recname, status=status, note=note)


@mcp.tool()
def migrate_status() -> dict:
    """Plan progress: record counts by classification and by lifecycle
    status, and where the state db lives."""
    return _safe("status")


def main() -> None:
    print(
        f"[pstb.migrate] MCP server starting — source={cfg.migrate.source or '(unset)'}, "
        f"target={cfg.migrate.target or 'primary db'}, "
        f"prefixes={','.join(cfg.migrate.custom_prefixes)}",
        file=sys.stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
