"""Learning loop over the question log.

logs/questions.jsonl records every turn with failure flags, tool timings
and thumbs-down feedback. This module turns that stream into the backlog
decision the log was created for: WHAT should be optimized or built next.
Every suggestion is a deterministic rule over the records — counts and
thresholds, not model judgment — so the same log always yields the same
report and a suggestion can be traced back to the turns that caused it.

Run it:  python -m pstb.qlog_report [logs/questions.jsonl]
The GUI Diagnostics tab renders the same report via /api/question-report.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

SLOW_MS = 30_000
ERROR_RATE = 0.25
MIN_CALLS = 3


def _norm_question(q: str) -> str:
    """Collapse a question to its shape so repeats cluster: numbers, unit
    codes and punctuation out, keywords kept in order."""
    q = re.sub(r"[a-z]{2}\d{3}|\d+", "#", (q or "").lower())
    return " ".join(re.findall(r"[a-z#]+", q))


def load(path: str | Path) -> tuple[list[dict], dict]:
    turns, feedback = [], {}
    p = Path(path)
    if not p.exists():
        return turns, feedback
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn write must not kill the report
        if rec.get("type") == "turn":
            turns.append(rec)
        elif rec.get("type") == "feedback" and rec.get("verdict") == "bad":
            feedback[rec.get("turn_id")] = rec
    return turns, feedback


def analyze(path: str | Path) -> dict:
    turns, feedback = load(path)
    bad_ids = set(feedback)
    failed = [t for t in turns
              if t.get("failed") or t.get("turn_id") in bad_ids]
    flags = Counter(f for t in turns for f in t.get("flags", []))
    if bad_ids:
        flags["user_bad"] = len([t for t in turns
                                 if t.get("turn_id") in bad_ids])

    tools: dict[str, dict] = {}
    for t in turns:
        for c in t.get("tools", []):
            s = tools.setdefault(str(c.get("tool")), {
                "tool": str(c.get("tool")), "calls": 0, "errors": 0,
                "max_ms": 0, "slow_calls": 0})
            s["calls"] += 1
            if not c.get("ok", True):
                s["errors"] += 1
            ms = c.get("ms")
            if isinstance(ms, (int, float)):
                s["max_ms"] = max(s["max_ms"], int(ms))
                if ms > SLOW_MS:
                    s["slow_calls"] += 1
    tool_rows = sorted(tools.values(),
                       key=lambda s: (-s["errors"], -s["max_ms"]))

    clusters: dict[str, dict] = {}
    for t in failed:
        key = _norm_question(t.get("question", ""))
        c = clusters.setdefault(key, {"times": 0, "example": "", "last": ""})
        c["times"] += 1
        c["example"] = t.get("question", "")[:140]
        c["last"] = str(t.get("ts", ""))[:16]
    repeats = sorted((c for c in clusters.values() if c["times"] >= 2),
                     key=lambda c: -c["times"])

    suggestions: list[str] = []
    for s in tool_rows:
        if s["calls"] >= MIN_CALLS and s["errors"] / s["calls"] >= ERROR_RATE:
            suggestions.append(
                f"{s['tool']}: {s['errors']} of {s['calls']} calls failed — "
                "read the errors in the log and fix or shape-adapt the tool")
        if s["slow_calls"]:
            suggestions.append(
                f"{s['tool']}: {s['slow_calls']} call(s) over {SLOW_MS//1000}s "
                f"(worst {s['max_ms']:,} ms) — index or partition candidate; "
                "run the Diagnostics tab and show the DBA summary")
    if flags.get("no_tool_calls", 0) >= 2:
        suggestions.append(
            f"{flags['no_tool_calls']} data-sounding questions were answered "
            "without any tool call — a routing gap; compare them against "
            "docs/QUESTIONS.md and extend the tool descriptions or packs")
    for c in repeats:
        suggestions.append(
            f"asked {c['times']}x and failed: \"{c['example']}\" — the "
            "strongest candidate for a curated tool or playbook")
    if flags.get("gave_up", 0) >= 2:
        suggestions.append(
            f"{flags['gave_up']} answers gave up outright — check whether "
            "the wiki corpus or a module pack covers those subjects")

    recent_failed = [{
        "ts": str(t.get("ts", ""))[:16],
        "question": t.get("question", "")[:140],
        "flags": t.get("flags", [])
        + (["user_bad"] if t.get("turn_id") in bad_ids else []),
        "tools": [str(x.get("tool")) for x in t.get("tools", [])],
    } for t in failed[-15:]][::-1]

    return {
        "turns": len(turns), "failed": len(failed),
        "first_ts": str(turns[0].get("ts", ""))[:16] if turns else "",
        "last_ts": str(turns[-1].get("ts", ""))[:16] if turns else "",
        "flags": dict(flags),
        "tools": tool_rows,
        "repeat_failures": repeats,
        "recent_failed": recent_failed,
        "suggestions": suggestions,
    }


def report_text(r: dict) -> str:
    lines = [
        f"Question log: {r['turns']} turns"
        + (f" ({r['first_ts']} .. {r['last_ts']})" if r["turns"] else "")
        + f", {r['failed']} failed or thumbed down",
    ]
    if r["flags"]:
        lines.append("flags: " + ", ".join(
            f"{k}={v}" for k, v in sorted(r["flags"].items())))
    if r["suggestions"]:
        lines.append("")
        lines.append("What to do next, in order:")
        lines += [f"  {i}. {s}" for i, s in enumerate(r["suggestions"], 1)]
    else:
        lines.append("nothing actionable — no failures, errors or repeats")
    if r["recent_failed"]:
        lines.append("")
        lines.append("Recent failed turns:")
        for t in r["recent_failed"]:
            lines.append(f"  [{t['ts']}] ({','.join(t['flags'])}) "
                         f"{t['question']}")
    return "\n".join(lines)


if __name__ == "__main__":
    _path = sys.argv[1] if len(sys.argv) > 1 else "logs/questions.jsonl"
    print(report_text(analyze(_path)))
