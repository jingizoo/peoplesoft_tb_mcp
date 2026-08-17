#!/usr/bin/env python3
"""Start, resume and review deterministic controller workflows.

Examples:
  .venv/bin/python scripts/workflow.py list-specs
  .venv/bin/python scripts/workflow.py start month_end_close --bu US001 --fy 2026 --period 6
  .venv/bin/python scripts/workflow.py run <workflow-id>
  .venv/bin/python scripts/workflow.py review <workflow-id> accept --revision 3
  .venv/bin/python scripts/workflow.py status <workflow-id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable, human-reviewed PeopleSoft control workflows")
    parser.add_argument(
        "--state-dir", default="",
        help="checkpoint directory (default: <deployment>/logs/workflows)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-specs", help="show available workflow definitions")
    sub.add_parser("list", help="show saved workflow runs")

    start = sub.add_parser("start", help="start a workflow without running it")
    start.add_argument("workflow")
    start.add_argument("--bu", default="")
    start.add_argument("--ledger", default="")
    start.add_argument("--fy", type=int, default=0)
    start.add_argument("--period", type=int, default=0)

    status = sub.add_parser("status", help="read one checkpoint")
    status.add_argument("workflow_id")

    run = sub.add_parser("run", help="run the pending deterministic phase")
    run.add_argument("workflow_id")

    review = sub.add_parser("review", help="accept, rerun or cancel a phase")
    review.add_argument("workflow_id")
    review.add_argument("decision", choices=("accept", "rerun", "cancel"))
    review.add_argument(
        "--revision", type=int, required=True,
        help="exact revision displayed by the latest run/status response")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    from pstb.config import load_config
    from pstb.workflows import WorkflowError, WorkflowStore, list_workflow_specs

    cfg = load_config(os.environ.get("PSTB_CONFIG"))
    state_dir = (Path(args.state_dir) if args.state_dir
                 else cfg.root / "logs" / "workflows")
    store = WorkflowStore(state_dir)
    try:
        if args.command == "list-specs":
            result = list_workflow_specs()
        elif args.command == "list":
            result = store.list_workflows()
        elif args.command == "start":
            result = store.start(
                args.workflow, business_unit=args.bu, ledger=args.ledger,
                fiscal_year=args.fy, period=args.period)
        elif args.command == "status":
            result = store.get(args.workflow_id)
        elif args.command == "review":
            result = store.review(
                args.workflow_id, args.decision,
                expected_revision=args.revision)
        else:
            from pstb.db import Database
            from pstb.engine import TBEngine
            from pstb.playbooks import PlaybookRunner
            database = Database(cfg)
            try:
                runner = PlaybookRunner(TBEngine(database, cfg))
                result = store.run_next(args.workflow_id, runner)
            finally:
                database.close()
    except WorkflowError as exc:
        print(f"workflow: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
