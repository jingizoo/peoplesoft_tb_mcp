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
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "turn":
            turns.append(rec)
        elif rec.get("type") == "feedback" and rec.get("verdict") == "bad":
            feedback[rec.get("turn_id")] = rec
    return turns, feedback


def _source_name(turn: dict) -> str:
    scope = turn.get("scope") if isinstance(turn.get("scope"), dict) else {}
    return str(turn.get("source_database") or scope.get("source")
               or "default").strip() or "default"


def _tool_rows(turns: list[dict], source: str) -> list[dict]:
    tools: dict[str, dict] = {}
    for turn in turns:
        for call in turn.get("tools", []):
            name = str(call.get("tool"))
            stats = tools.setdefault(name, {
                "source_database": source,
                "tool": name,
                "calls": 0,
                "errors": 0,
                "max_ms": 0,
                "slow_calls": 0,
                "refusal_categories": Counter(),
                "completeness": Counter(),
                "source_verification": Counter(),
                "target_owners": set(),
            })
            stats["calls"] += 1
            if not call.get("ok", True):
                stats["errors"] += 1
            ms = call.get("ms")
            if isinstance(ms, (int, float)):
                stats["max_ms"] = max(stats["max_ms"], int(ms))
                if ms > SLOW_MS:
                    stats["slow_calls"] += 1
            category = str(call.get("refusal_category") or "")
            if category:
                stats["refusal_categories"][category] += 1
            completeness = call.get("result_completeness")
            if isinstance(completeness, dict):
                status = str(completeness.get("status") or "unknown")
                stats["completeness"][status] += 1
            verified = call.get("result_source_verified")
            stats["source_verification"][
                "verified" if verified is True
                else "mismatch" if verified is False else "not_declared"
            ] += 1
            stats["target_owners"].update(
                str(owner) for owner in (call.get("target_owners") or []))
    rows = []
    for stats in tools.values():
        stats["refusal_categories"] = dict(stats["refusal_categories"])
        stats["completeness"] = dict(stats["completeness"])
        stats["source_verification"] = dict(
            stats["source_verification"])
        stats["target_owners"] = sorted(stats["target_owners"])
        rows.append(stats)
    return sorted(rows, key=lambda row: (
        -row["errors"], -row["max_ms"], row["tool"]))


def _source_summary(source: str, turns: list[dict], bad_ids: set) -> dict:
    failed = [turn for turn in turns
              if turn.get("failed") or turn.get("turn_id") in bad_ids]
    flags = Counter(flag for turn in turns for flag in turn.get("flags", []))
    flags["user_bad"] = sum(
        turn.get("turn_id") in bad_ids for turn in turns)
    if not flags["user_bad"]:
        del flags["user_bad"]

    scopes: list[dict] = []
    for turn in turns:
        scope = turn.get("scope") if isinstance(turn.get("scope"), dict) else {}
        observed = {
            key: scope[key] for key in (
                "business_unit", "ledger", "fiscal_year", "period")
            if scope.get(key) not in (None, "")
        }
        if observed and observed not in scopes:
            scopes.append(observed)

    contexts = [turn.get("source_context") for turn in turns
                if isinstance(turn.get("source_context"), dict)]
    source_context = contexts[-1] if contexts else {
        "canonical_source": source}

    catalog = {}
    relationships = {
        "calls": 0,
        "found": 0,
        "not_found": 0,
        "confidence": Counter(),
        "evidence_class": Counter(),
    }
    for turn in turns:
        for call in turn.get("tools", []):
            if isinstance(call.get("catalog"), dict):
                catalog = call["catalog"]
            relation = call.get("relationship_path")
            if not isinstance(relation, dict):
                continue
            relationships["calls"] += 1
            if relation.get("found") is True:
                relationships["found"] += 1
            elif relation.get("found") is False:
                relationships["not_found"] += 1
            for confidence in relation.get("confidence") or []:
                relationships["confidence"][str(confidence)] += 1
            for evidence in relation.get("evidence_class") or []:
                relationships["evidence_class"][str(evidence)] += 1
    relationships["confidence"] = dict(relationships["confidence"])
    relationships["evidence_class"] = dict(
        relationships["evidence_class"])

    return {
        "source_database": source,
        "turns": len(turns),
        "failed": len(failed),
        "flags": dict(flags),
        "source_context": source_context,
        "scopes": scopes[:20],
        "scopes_truncated": len(scopes) > 20,
        "tools": _tool_rows(turns, source),
        "catalog": catalog,
        "relationship_paths": relationships,
    }


def analyze(path: str | Path) -> dict:
    turns, feedback = load(path)
    bad_ids = set(feedback)
    failed = [t for t in turns
              if t.get("failed") or t.get("turn_id") in bad_ids]
    flags = Counter(f for t in turns for f in t.get("flags", []))
    if bad_ids:
        flags["user_bad"] = len([t for t in turns
                                 if t.get("turn_id") in bad_ids])

    turns_by_source: dict[str, list[dict]] = {}
    for turn in turns:
        turns_by_source.setdefault(_source_name(turn), []).append(turn)
    sources = {
        source: _source_summary(source, source_turns, bad_ids)
        for source, source_turns in sorted(turns_by_source.items())
    }
    # Compatibility list for existing consumers, but every row carries its
    # source.  No cross-source failure rate is calculated from this list.
    tool_rows = [tool for summary in sources.values()
                 for tool in summary["tools"]]

    clusters: dict[str, dict] = {}
    for t in failed:
        source = _source_name(t)
        key = f"{source}\0{_norm_question(t.get('question', ''))}"
        c = clusters.setdefault(key, {
            "source_database": source,
            "times": 0,
            "last": "",
            "turn_ids": [],
        })
        c["times"] += 1
        c["last"] = str(t.get("ts", ""))[:16]
        c["turn_ids"].append(str(t.get("turn_id") or "")[:32])
    repeats = sorted((c for c in clusters.values() if c["times"] >= 2),
                     key=lambda c: -c["times"])

    suggestions: list[str] = []
    for source, summary in sources.items():
        prefix = f"[{source}] "
        for stats in summary["tools"]:
            if (stats["calls"] >= MIN_CALLS
                    and stats["errors"] / stats["calls"] >= ERROR_RATE):
                categories = ", ".join(
                    f"{name}={count}" for name, count in sorted(
                        stats["refusal_categories"].items()))
                suggestions.append(
                    f"{prefix}{stats['tool']}: {stats['errors']} of "
                    f"{stats['calls']} calls failed"
                    + (f" ({categories})" if categories else "")
                    + " — fix or shape-adapt this source-specific path")
            if stats["slow_calls"]:
                suggestions.append(
                    f"{prefix}{stats['tool']}: {stats['slow_calls']} call(s) "
                    f"over {SLOW_MS//1000}s (worst {stats['max_ms']:,} ms) — "
                    "index or partition candidate for this source")
        no_tool = summary["flags"].get("no_tool_calls", 0)
        if no_tool >= 2:
            suggestions.append(
                f"{prefix}{no_tool} data-sounding questions were answered "
                "without any tool call — a source-specific routing gap")
        gave_up = summary["flags"].get("gave_up", 0)
        if gave_up >= 2:
            suggestions.append(
                f"{prefix}{gave_up} answers gave up outright — inspect that "
                "workspace's evidence coverage")
    for c in repeats:
        suggestions.append(
            f"[{c['source_database']}] asked {c['times']}x and failed: "
            "the same private question shape — inspect its local turn IDs "
            "before choosing a curated tool or playbook")

    recent_failed = [{
        "ts": str(t.get("ts", ""))[:16],
        "turn_id": str(t.get("turn_id") or "")[:32],
        "source_database": _source_name(t),
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
        "sources": sources,
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
    for source, summary in sorted((r.get("sources") or {}).items()):
        lines.append(
            f"source {source}: {summary['turns']} turns, "
            f"{summary['failed']} failed or thumbed down")
        context = summary.get("source_context") or {}
        schemas = context.get("schema_allowlist") or []
        if schemas:
            lines.append(
                "  schema boundary: "
                + ", ".join(str(schema) for schema in schemas)
                + (f" (default {context.get('default_schema')})"
                   if context.get("default_schema") else ""))
        if summary.get("scopes"):
            rendered = []
            for scope in summary["scopes"][:5]:
                rendered.append("/".join(str(scope[key]) for key in (
                    "business_unit", "ledger", "fiscal_year", "period")
                    if scope.get(key) not in (None, "")))
            lines.append("  Finance scopes: " + "; ".join(rendered))
        catalog = summary.get("catalog") or {}
        if catalog:
            latest = catalog.get("latest_build") or {}
            lines.append(
                f"  catalog: {catalog.get('status', 'unknown')}"
                + (f", snapshot {str(catalog.get('snapshot_id'))[:12]}"
                   if catalog.get("snapshot_id") else "")
                + (f", refresh {latest.get('status')}"
                   if latest.get("status") else "")
                + (", not published"
                   if latest.get("published") is False else ""))
        relation = summary.get("relationship_paths") or {}
        if relation.get("calls"):
            lines.append(
                f"  relationships: {relation.get('found', 0)} found, "
                f"{relation.get('not_found', 0)} not found")
        for tool in summary.get("tools") or []:
            verification = ",".join(
                f"{key}={value}" for key, value in sorted(
                    (tool.get("source_verification") or {}).items()))
            completeness = ",".join(
                f"{key}={value}" for key, value in sorted(
                    (tool.get("completeness") or {}).items()))
            lines.append(
                f"  tool {tool.get('tool')}: calls={tool.get('calls', 0)}, "
                f"errors={tool.get('errors', 0)}, "
                f"max={tool.get('max_ms', 0)}ms"
                + (f", source[{verification}]" if verification else "")
                + (f", completeness[{completeness}]"
                   if completeness else ""))
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
            lines.append(f"  [{t['ts']}] [{t.get('source_database', 'default')}] "
                         f"({','.join(t['flags'])}) "
                         f"turn={t.get('turn_id', '-')}")
    return "\n".join(lines)


if __name__ == "__main__":
    _path = sys.argv[1] if len(sys.argv) > 1 else "logs/questions.jsonl"
    print(report_text(analyze(_path)))
