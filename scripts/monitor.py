#!/usr/bin/env python3
"""Run playbooks on a schedule and report what CHANGED since last time.

    .venv/bin/python scripts/monitor.py                    # run + diff + report
    .venv/bin/python scripts/monitor.py --playbook receivables_health
    .venv/bin/python scripts/monitor.py --bu US001 --ledger ACTUALS
    .venv/bin/python scripts/monitor.py --quiet            # only if something changed
    .venv/bin/python scripts/monitor.py --history          # what has been recorded

This turns the agent from something people remember to open into something
that tells them when to look. A playbook answers "what is true now"; running
it nightly and diffing against yesterday answers the question a controller
actually has: "what changed while I wasn't watching".

No language model is involved. The comparison is a deterministic diff of
playbook verdicts, so the report cannot drift, hallucinate, or need review —
and it runs on a box with no LLM configured at all.

Snapshots live in logs/monitor/<playbook>-<bu>-<ledger>.json (latest) with an
append-only history beside them. Exit codes are for schedulers:
    0  nothing changed
    1  something changed (new findings, or findings that cleared)
    2  the run itself failed
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOT_DIR = ROOT / "logs" / "monitor"


def _key(playbook: str, bu: str, ledger: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_"
                   for c in f"{playbook}-{bu}-{ledger}")
    return safe


def _load_previous(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _step_map(run: dict) -> dict:
    return {s["step"]: s for s in (run.get("steps") or [])}


def diff_runs(previous: dict, current: dict) -> dict:
    """What changed between two playbook runs.

    Deliberately reports movement in BOTH directions: a finding that cleared
    is as worth knowing as a new one, and a check that stopped running is the
    most important of all — it means the next verdict is not comparable.
    """
    before, after = _step_map(previous), _step_map(current)
    new_findings, resolved, still_open, stopped_running, started_running = (
        [], [], [], [], [])

    for step_id, now in after.items():
        was = before.get(step_id)
        if was is None:
            if now["status"] != "ok":
                new_findings.append(now)
            continue
        if was["status"] == now["status"]:
            if now["status"] == "attention":
                entry = dict(now)
                if was.get("headline") != now.get("headline"):
                    entry["previous_headline"] = was.get("headline")
                still_open.append(entry)
            continue
        if now["status"] == "attention":
            new_findings.append({**now, "previous_status": was["status"]})
        elif now["status"] == "skipped":
            stopped_running.append({**now, "previous_status": was["status"]})
        elif was["status"] == "attention":
            resolved.append({**now, "previous_headline": was.get("headline")})
        elif was["status"] == "skipped":
            started_running.append(now)

    disappeared = [was for step_id, was in before.items() if step_id not in after]
    changed = bool(new_findings or resolved or stopped_running
                   or started_running or disappeared)
    # A headline that moved on an already-open finding is a change worth
    # seeing (suspense grew from 15,000 to 40,000) even though the status is
    # the same.
    moved = [s for s in still_open if "previous_headline" in s]
    return {
        "changed": changed or bool(moved),
        "verdict_before": previous.get("verdict"),
        "verdict_after": current.get("verdict"),
        "verdict_changed": (previous.get("verdict") != current.get("verdict")
                            if previous else False),
        "new_findings": new_findings,
        "resolved": resolved,
        "moved": moved,
        "still_open": [s for s in still_open if "previous_headline" not in s],
        "stopped_running": stopped_running,
        "started_running": started_running,
        "steps_disappeared": disappeared,
        "first_run": not previous,
    }


def render(current: dict, delta: dict) -> str:
    lines = []
    scope = (f"{current['business_unit']} / {current['ledger']} / "
             f"FY{current['fiscal_year']} P{current['period']}")
    lines.append(f"{current['title']} — {scope}")
    lines.append(f"  verdict: {current['verdict']} ({current['summary']})")
    if delta["first_run"]:
        lines.append("  first run — recording a baseline, nothing to compare")
    elif delta["verdict_changed"]:
        lines.append(f"  verdict CHANGED: {delta['verdict_before']} -> "
                     f"{delta['verdict_after']}")

    def block(title: str, rows: list, show_previous: bool = False) -> None:
        if not rows:
            return
        lines.append(f"\n  {title}:")
        for row in rows:
            lines.append(f"    - {row['title']}: {row['headline']}")
            if show_previous and row.get("previous_headline"):
                lines.append(f"        was: {row['previous_headline']}")

    block("NEW findings", delta["new_findings"])
    block("Findings that MOVED", delta["moved"], show_previous=True)
    block("RESOLVED since last run", delta["resolved"], show_previous=True)
    # A check that stopped running is the loudest signal here: the verdict is
    # no longer comparable to yesterday's.
    block("Checks that STOPPED RUNNING (verdict not comparable)",
          delta["stopped_running"])
    block("Checks that started running again", delta["started_running"])
    if delta["still_open"]:
        lines.append(f"\n  Unchanged open findings: "
                     f"{len(delta['still_open'])}")
    if not delta["changed"] and not delta["first_run"]:
        lines.append("\n  Nothing changed since the last run.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run playbooks on a schedule and report what changed")
    ap.add_argument("--playbook", default="close_readiness")
    ap.add_argument("--bu", default="")
    ap.add_argument("--ledger", default="")
    ap.add_argument("--fy", type=int, default=0)
    ap.add_argument("--period", type=int, default=0)
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when nothing changed (for cron)")
    ap.add_argument("--history", action="store_true",
                    help="show recorded runs instead of running one")
    ap.add_argument("--json", default="", help="write the delta here")
    args = ap.parse_args()

    from pstb.config import load_config
    from pstb.db import Database
    from pstb.engine import TBEngine
    from pstb.playbooks import PlaybookRunner
    from pstb.version import label as build_label

    cfg = load_config(os.environ.get("PSTB_CONFIG"))
    engine = TBEngine(Database(cfg), cfg)
    runner = PlaybookRunner(engine)

    try:
        current = runner.run(args.playbook, business_unit=args.bu,
                             ledger=args.ledger, fiscal_year=args.fy,
                             period=args.period)
    except Exception as e:
        print(f"monitor: run failed — {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    key = _key(args.playbook, current["business_unit"], current["ledger"])
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    latest = SNAPSHOT_DIR / f"{key}.json"
    history = SNAPSHOT_DIR / f"{key}.history.jsonl"

    if args.history:
        if not history.exists():
            print(f"no history yet at {history}")
            return 0
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            print(f"{rec.get('ran_at')}  {rec.get('verdict'):<17} "
                  f"attention={rec.get('attention_count')} "
                  f"skipped={rec.get('skipped_count')}"
                  + ("  CHANGED" if rec.get("changed") else ""))
        return 0

    previous = _load_previous(latest)
    delta = diff_runs(previous, current)
    current["ran_at"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    current["build"] = build_label()

    latest.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ran_at": current["ran_at"], "verdict": current["verdict"],
            "attention_count": current["attention_count"],
            "skipped_count": current["skipped_count"],
            "changed": delta["changed"], "build": current["build"],
        }) + "\n")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"current": current, "delta": delta}, indent=2), encoding="utf-8")

    if not (args.quiet and not delta["changed"]):
        print(render(current, delta))
    return 1 if delta["changed"] else 0


if __name__ == "__main__":
    sys.exit(main())
