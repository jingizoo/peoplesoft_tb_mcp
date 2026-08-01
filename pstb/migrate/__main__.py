"""CLI for the record-porting pipeline — the same steps the MCP server
exposes, for operators who prefer a terminal (or a cron job re-running
reconcile overnight).

    python -m pstb.migrate discover [--mode prefix|oprid|both] [--limit N]
    python -m pstb.migrate plan [REC ...] [--mode ...]
    python -m pstb.migrate show REC
    python -m pstb.migrate emit [--out DIR]
    python -m pstb.migrate verify-build
    python -m pstb.migrate reconcile [REC ...]
    python -m pstb.migrate mark REC STATUS [--note TEXT]
    python -m pstb.migrate status
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ..config import load_config
from ..db import Database, DbError
from ..sources import SourceRegistry
from .pipeline import MigratePipeline
from .spec import STATUSES, MigrateError


def _build() -> MigratePipeline:
    cfg = load_config(os.environ.get("PSTB_CONFIG"))
    return MigratePipeline(cfg, SourceRegistry(cfg, Database(cfg)))


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pstb.migrate",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="list candidate custom records on 9.1")
    d.add_argument("--mode", default="", choices=["", "prefix", "oprid", "both"])
    d.add_argument("--limit", type=int, default=0)

    pl = sub.add_parser("plan", help="closure + classification against 9.2")
    pl.add_argument("seeds", nargs="*", help="seed records (default: discovery)")
    pl.add_argument("--mode", default="", choices=["", "prefix", "oprid", "both"])

    sh = sub.add_parser("show", help="one record: both shapes + diff")
    sh.add_argument("recname")

    em = sub.add_parser("emit", help="write App Designer / Data Mover artifacts")
    em.add_argument("--out", default="")

    sub.add_parser("verify-build", help="check built tables in 9.2")

    rc = sub.add_parser("reconcile", help="row counts + numeric sums, both sides")
    rc.add_argument("recnames", nargs="*")

    mk = sub.add_parser("mark", help="record a manual step")
    mk.add_argument("recname")
    mk.add_argument("status", choices=list(STATUSES))
    mk.add_argument("--note", default="")

    sub.add_parser("status", help="plan progress summary")

    a = p.parse_args(argv)
    try:
        pipe = _build()
        if a.cmd == "discover":
            out = pipe.discover(mode=a.mode, limit=a.limit)
        elif a.cmd == "plan":
            out = pipe.plan(seed_records=a.seeds or None, mode=a.mode)
        elif a.cmd == "show":
            out = pipe.show_record(a.recname)
        elif a.cmd == "emit":
            out = pipe.emit(out_dir=a.out)
        elif a.cmd == "verify-build":
            out = pipe.verify_build()
        elif a.cmd == "reconcile":
            out = pipe.reconcile(recnames=a.recnames or None)
        elif a.cmd == "mark":
            out = pipe.mark(a.recname, a.status, a.note)
        else:
            out = pipe.status()
    except (MigrateError, DbError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
