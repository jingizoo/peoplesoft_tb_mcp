"""Learning loop over the question log.

logs/questions.jsonl records every turn with failure flags, tool timings,
runtime-grounding facts, categorized feedback, and operator review state.
This module turns that stream into the backlog
decision the log was created for: WHAT should be optimized or built next.
Every suggestion is a deterministic rule over the records — counts and
thresholds, not model judgment — so the same log always yields the same
report and a suggestion can be traced back to the turns that caused it.

Run it:  python -m pstb.qlog_report [logs/questions.jsonl]
The GUI Diagnostics tab renders the same report via /api/question-report.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path

from .qlog import (
    DEFAULT_LOG_BACKUPS, FEEDBACK_CATEGORIES, FEEDBACK_VERDICTS,
    REVIEW_STATUSES, QuestionLog,
)
from .quality import (
    GROUNDEDNESS_STATUSES, QUALITY_COUNT_KEYS, QUALITY_REASON_CODES,
    RUNTIME_GROUNDING_BASIS,
)

SLOW_MS = 30_000
ERROR_RATE = 0.25
MIN_CALLS = 3
MIN_QUALITY_SAMPLE = 20
REVIEW_QUEUE_LIMIT = 100

_ACTIVE_REVIEW_STATUSES = frozenset({
    "open", "triaged", "eval_added", "fix_in_progress", "fixed",
})

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm_question(q: str) -> str:
    """Collapse a question to its shape so repeats cluster: numbers, unit
    codes and punctuation out, keywords kept in order."""
    q = re.sub(r"[a-z]{2}\d{3}|\d+", "#", (q or "").lower())
    return " ".join(re.findall(r"[a-z#]+", q))


def _safe_token(value: object, *, limit: int = 80) -> str:
    token = str(value or "")[:limit]
    return token if _SAFE_TOKEN.fullmatch(token) else ""


def _log_paths(path: str | Path) -> list[Path]:
    """Return rotated logs oldest-to-newest, followed by the active log.

    Rotation moves the active file to ``.1`` and shifts older generations
    upward, so the largest numeric suffix is the oldest.  Ignore non-numeric
    lookalikes and links: the report is reachable from the GUI and must never
    turn a configured log path into an arbitrary-file reader.
    """
    active = Path(path)
    rotated: list[tuple[int, Path]] = []
    try:
        candidates = active.parent.glob(f"{active.name}.*")
        for candidate in candidates:
            suffix = candidate.name[len(active.name) + 1:]
            if (suffix.isdigit()
                    and 1 <= int(suffix) <= DEFAULT_LOG_BACKUPS):
                rotated.append((int(suffix), candidate))
    except OSError:
        pass
    ordered = [candidate for _, candidate in sorted(
        rotated, key=lambda item: item[0], reverse=True)]
    ordered.append(active)
    return ordered


def _records(path: str | Path | QuestionLog) -> dict:
    """Load the append-only stream and join each record type by turn id.

    A crash during append can leave one torn final line, and a copied rotation
    can temporarily expose the same line in two generations.  Loading in log
    order and replacing by turn id makes the newest complete record win without
    double-counting the turn.
    """
    turns: dict[str, dict] = {}
    turn_order: list[str] = []
    feedback: dict[str, dict] = {}
    quality: dict[str, dict] = {}
    reviews: dict[str, dict] = {}

    def consume(rec: object) -> None:
        if not isinstance(rec, dict):
            return
        turn_id = _safe_token(rec.get("turn_id"), limit=64)
        if not turn_id:
            return
        kind = rec.get("type")
        if kind == "turn":
            if turn_id not in turns:
                turn_order.append(turn_id)
            turns[turn_id] = rec
        elif kind == "feedback":
            verdict = str(rec.get("verdict") or "")
            if verdict in FEEDBACK_VERDICTS:
                feedback[turn_id] = rec
        elif kind == "quality":
            quality[turn_id] = rec
        elif kind == "review":
            status = str(rec.get("status") or "")
            if status in REVIEW_STATUSES:
                reviews[turn_id] = rec

    if isinstance(path, QuestionLog):
        # The GUI passes its live QuestionLog so reads share the writer's
        # startup-pinned directory identity. A post-start symlink swap then
        # fails closed instead of redirecting the operator dashboard.
        for rec in path.retained_records():
            consume(rec)
    else:
        for candidate in _log_paths(path):
            fd = -1
            try:
                before = candidate.lstat()
                if not stat.S_ISREG(before.st_mode):
                    continue
                fd = os.open(
                    candidate,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise OSError("question log is not a regular file")
                # O_NOFOLLOW is unavailable on a few supported platforms.
                # Prove the entry and descriptor still identify one file.
                if (before.st_dev, before.st_ino) != (
                        opened.st_dev, opened.st_ino):
                    raise OSError("question log changed while opening")
                handle = os.fdopen(fd, "r", encoding="utf-8")
                fd = -1
            except (FileNotFoundError, OSError):
                if fd >= 0:
                    os.close(fd)
                continue
            with handle:
                for line in handle:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a torn write must not kill the report
                    consume(rec)
    return {
        "turns": [turns[turn_id] for turn_id in turn_order],
        "feedback": feedback,
        "quality": quality,
        "reviews": reviews,
    }


def load(path: str | Path) -> tuple[list[dict], dict]:
    """Backward-compatible turn/bad-feedback loader.

    New report consumers use the richer joined stream internally; callers of
    the old helper still receive exactly the original two-item shape.
    """
    records = _records(path)
    bad = {turn_id: rec for turn_id, rec in records["feedback"].items()
           if rec.get("verdict") == "bad"}
    return records["turns"], bad


def _source_name(turn: dict) -> str:
    scope = turn.get("scope") if isinstance(turn.get("scope"), dict) else {}
    value = str(turn.get("source_database") or scope.get("source")
                or "default").strip() or "default"
    return _safe_token(value, limit=64) or "unknown"


def _tool_rows(turns: list[dict], source: str) -> list[dict]:
    tools: dict[str, dict] = {}
    for turn in turns:
        for call in turn.get("tools", []):
            if not isinstance(call, dict):
                continue
            name = _safe_token(call.get("tool"), limit=80)
            if not name:
                continue
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


def _feedback_categories(rec: object) -> list[str]:
    row = rec if isinstance(rec, dict) else {}
    raw = row.get("categories")
    if not isinstance(raw, list):
        raw = [row.get("category")] if row.get("category") else []
    return sorted({str(value) for value in raw
                   if str(value) in FEEDBACK_CATEGORIES})


def _groundedness(rec: object) -> dict:
    """Select the public mechanical-quality contract from one record."""
    row = rec if isinstance(rec, dict) else {}
    # A status without the audited runtime basis is not a mechanical result.
    # Treat legacy, tampered, or future-incompatible records as unscored rather
    # than silently mixing unlike rubrics in one pass rate.
    if str(row.get("basis") or "") != RUNTIME_GROUNDING_BASIS:
        return {}
    raw = row.get("groundedness")
    raw = raw if isinstance(raw, dict) else {}
    status = str(raw.get("status") or "")
    if status not in GROUNDEDNESS_STATUSES:
        return {}
    reasons = raw.get("reason_codes")
    reasons = reasons if isinstance(reasons, list) else []
    counts = raw.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    safe_counts = {}
    for key in QUALITY_COUNT_KEYS:
        value = counts.get(key)
        if (isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= 1_000_000):
            safe_counts[key] = value
    basis = RUNTIME_GROUNDING_BASIS
    rubric = _safe_token(
        raw.get("rubric_version") or row.get("rubric_version"), limit=80)
    return {
        "status": status,
        "reason_codes": sorted({str(value) for value in reasons
                                if str(value) in QUALITY_REASON_CODES}),
        "counts": safe_counts,
        **({"basis": basis} if basis else {}),
        **({"rubric_version": rubric} if rubric else {}),
    }


def _rate(numerator: int, denominator: int):
    return (round(numerator / denominator, 4) if denominator else None)


def _safe_tool_names(turn: dict) -> list[str]:
    names = []
    for call in turn.get("tools", []):
        if not isinstance(call, dict):
            continue
        name = _safe_token(call.get("tool"), limit=80)
        if name and name not in names:
            names.append(name)
    return names[:20]


def _quality_summary(turns: list[dict], feedback: dict, quality: dict,
                     reviews: dict, source: str) -> tuple[dict, list[dict]]:
    groundedness = Counter()
    grounding_reasons = Counter()
    grounding_counts = Counter()
    bases = Counter()
    rubrics = Counter()
    feedback_counts = Counter()
    category_counts = Counter()
    relevance_counts = Counter()
    review_counts = Counter()
    trend: dict[str, dict] = {}
    queue = []

    for turn in turns:
        turn_id = _safe_token(turn.get("turn_id"), limit=64)
        if not turn_id:
            continue
        day = str(turn.get("ts") or "")[:10]
        bucket = None
        if _SAFE_DATE.fullmatch(day):
            bucket = trend.setdefault(day, {
                "date": day, "turns": 0,
                "groundedness_assessed": 0,
                "groundedness_scored": 0,
                "groundedness_passed": 0,
                "groundedness_blocked": 0,
                "groundedness_unknown": 0,
                "groundedness_not_applicable": 0,
                "feedback_good": 0, "feedback_bad": 0,
                "user_rated_relevance_assessed": 0,
                "user_rated_relevance_relevant": 0,
                "user_rated_relevance_not_relevant": 0,
            })
            bucket["turns"] += 1

        observed = _groundedness(quality.get(turn_id))
        status = observed.get("status", "")
        if status:
            groundedness[status] += 1
            if bucket is not None:
                bucket["groundedness_scored"] += 1
                bucket[f"groundedness_{status}"] += 1
                if status in {"passed", "blocked"}:
                    bucket["groundedness_assessed"] += 1
            grounding_reasons.update(observed.get("reason_codes") or [])
            grounding_counts.update(observed.get("counts") or {})
            if observed.get("basis"):
                bases[observed["basis"]] += 1
            if observed.get("rubric_version"):
                rubrics[observed["rubric_version"]] += 1

        fb = feedback.get(turn_id)
        verdict = (str(fb.get("verdict") or "")
                   if isinstance(fb, dict) else "")
        categories = _feedback_categories(fb)
        if verdict in FEEDBACK_VERDICTS:
            feedback_counts[verdict] += 1
            category_counts.update(categories)
            if verdict == "good":
                relevance_counts["relevant"] += 1
            elif "not_relevant" in categories:
                relevance_counts["not_relevant"] += 1
            if bucket is not None:
                bucket[f"feedback_{verdict}"] += 1
                if verdict == "good":
                    bucket["user_rated_relevance_assessed"] += 1
                    bucket["user_rated_relevance_relevant"] += 1
                elif "not_relevant" in categories:
                    bucket["user_rated_relevance_assessed"] += 1
                    bucket["user_rated_relevance_not_relevant"] += 1

        review = reviews.get(turn_id)
        review_status = (str(review.get("status") or "")
                         if isinstance(review, dict) else "")
        # ``unknown`` is an honest limitation of the mechanical rubric for
        # policy/structural prose, not a failure. Only a hard block, bad user
        # feedback, or explicit operator review enters the work queue.
        candidate = (verdict == "bad" or status == "blocked"
                     or review_status in REVIEW_STATUSES)
        if candidate:
            if review_status not in REVIEW_STATUSES:
                review_status = "open"
            review_counts[review_status] += 1
            event_times = [str(turn.get("ts") or "")]
            for event in (fb, quality.get(turn_id), review):
                if isinstance(event, dict):
                    event_times.append(str(event.get("ts") or ""))
            updated_ts = max((value for value in event_times if value),
                             default="")
            queue.append({
                "ts": updated_ts[:16],
                "turn_id": turn_id,
                "source_database": source,
                "groundedness": status or "not_scored",
                "grounding_reasons": observed.get("reason_codes") or [],
                "feedback": verdict or "none",
                "feedback_categories": categories,
                "review_status": review_status,
                "tools": _safe_tool_names(turn),
            })

    assessed = groundedness["passed"] + groundedness["blocked"]
    feedback_total = feedback_counts["good"] + feedback_counts["bad"]
    relevance_negative = relevance_counts["not_relevant"]
    relevance_positive = relevance_counts["relevant"]
    relevance_assessed = relevance_positive + relevance_negative
    for bucket in trend.values():
        bucket["groundedness_unscored"] = max(
            bucket["turns"] - bucket["groundedness_scored"], 0)
        bucket["groundedness_rate"] = _rate(
            bucket["groundedness_passed"],
            bucket["groundedness_assessed"])
        bucket["user_rated_relevance_rate"] = _rate(
            bucket["user_rated_relevance_relevant"],
            bucket["user_rated_relevance_assessed"])

    quality_records = sum(groundedness.values())
    return ({
        "minimum_sample": MIN_QUALITY_SAMPLE,
        "groundedness": {
            "records": quality_records,
            "unscored": max(len(turns) - quality_records, 0),
            "coverage_rate": _rate(quality_records, len(turns)),
            "assessed": assessed,
            "passed": groundedness["passed"],
            "blocked": groundedness["blocked"],
            "unknown": groundedness["unknown"],
            "not_applicable": groundedness["not_applicable"],
            "pass_rate": _rate(groundedness["passed"], assessed),
            "sample_warning": assessed < MIN_QUALITY_SAMPLE,
            "reason_counts": dict(sorted(grounding_reasons.items())),
            "counts": dict(sorted(grounding_counts.items())),
            "basis_counts": dict(sorted(bases.items())),
            "rubric_versions": dict(sorted(rubrics.items())),
        },
        "feedback": {
            "responses": feedback_total,
            "good": feedback_counts["good"],
            "bad": feedback_counts["bad"],
            "unreviewed": max(len(turns) - feedback_total, 0),
            "helpfulness": _rate(feedback_counts["good"], feedback_total),
            "sample_warning": feedback_total < MIN_QUALITY_SAMPLE,
            "category_counts": dict(sorted(category_counts.items())),
        },
        # This is intentionally human-labelled relevance only. A bad vote for
        # slowness or incompleteness is not silently reclassified as irrelevant.
        "user_rated_relevance": {
            "assessed": relevance_assessed,
            "relevant": relevance_positive,
            "not_relevant": relevance_negative,
            "rate": _rate(relevance_positive, relevance_assessed),
            "sample_warning": relevance_assessed < MIN_QUALITY_SAMPLE,
        },
        "review_status_counts": dict(sorted(review_counts.items())),
        "trends": [trend[key] for key in sorted(trend)],
    }, queue)


def _source_summary(source: str, turns: list[dict], bad_ids: set,
                    feedback: dict, quality: dict,
                    reviews: dict) -> tuple[dict, list[dict]]:
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
            if not isinstance(call, dict):
                continue
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

    quality_summary, review_queue = _quality_summary(
        turns, feedback, quality, reviews, source)
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
        "quality": quality_summary,
    }, review_queue


def analyze(path: str | Path | QuestionLog) -> dict:
    records = _records(path)
    turns = records["turns"]
    feedback = records["feedback"]
    quality = records["quality"]
    reviews = records["reviews"]
    bad_ids = {turn_id for turn_id, rec in feedback.items()
               if rec.get("verdict") == "bad"}
    failed = [t for t in turns
              if t.get("failed") or t.get("turn_id") in bad_ids]
    flags = Counter(f for t in turns for f in t.get("flags", []))
    if bad_ids:
        flags["user_bad"] = len([t for t in turns
                                 if t.get("turn_id") in bad_ids])

    turns_by_source: dict[str, list[dict]] = {}
    for turn in turns:
        turns_by_source.setdefault(_source_name(turn), []).append(turn)
    sources = {}
    review_queue = []
    for source, source_turns in sorted(turns_by_source.items()):
        summary, queued = _source_summary(
            source, source_turns, bad_ids, feedback, quality, reviews)
        sources[source] = summary
        review_queue.extend(queued)
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
        review_counts = ((summary.get("quality") or {}).get(
            "review_status_counts") or {})
        active_reviews = sum(int(review_counts.get(status) or 0)
                             for status in _ACTIVE_REVIEW_STATUSES)
        if active_reviews:
            suggestions.append(
                f"{prefix}{active_reviews} answer review item(s) remain active "
                "— triage, add an eval, fix, and verify them")
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
        "tools": _safe_tool_names(t),
    } for t in failed[-15:]][::-1]

    # Newest first within each lifecycle group, but unfinished work always
    # stays ahead of verified/dismissed history even when that history is newer.
    review_queue.sort(key=lambda item: (item.get("ts", ""),
                                        item.get("turn_id", "")),
                      reverse=True)
    review_queue.sort(
        key=lambda item: item.get("review_status")
        not in _ACTIVE_REVIEW_STATUSES)
    review_queue_total = len(review_queue)
    review_queue_active_total = sum(
        item.get("review_status") in _ACTIVE_REVIEW_STATUSES
        for item in review_queue)

    return {
        "turns": len(turns), "failed": len(failed),
        "first_ts": str(turns[0].get("ts", ""))[:16] if turns else "",
        "last_ts": str(turns[-1].get("ts", ""))[:16] if turns else "",
        "flags": dict(flags),
        "tools": tool_rows,
        "sources": sources,
        "repeat_failures": repeats,
        "recent_failed": recent_failed,
        "review_queue": review_queue[:REVIEW_QUEUE_LIMIT],
        "review_queue_total": review_queue_total,
        "review_queue_active_total": review_queue_active_total,
        "review_queue_truncated": review_queue_total > REVIEW_QUEUE_LIMIT,
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
        quality_summary = summary.get("quality") or {}
        grounded = quality_summary.get("groundedness") or {}
        lines.append(
            "  mechanical quality coverage: "
            f"{grounded.get('records', 0)}/{summary.get('turns', 0)} scored, "
            f"unscored={grounded.get('unscored', summary.get('turns', 0))}")
        if grounded.get("records"):
            lines.append(
                "  mechanical groundedness: "
                f"{grounded.get('passed', 0)}/"
                f"{grounded.get('assessed', 0)} assessed passed, "
                f"blocked={grounded.get('blocked', 0)}, "
                f"unknown={grounded.get('unknown', 0)}, "
                f"N/A={grounded.get('not_applicable', 0)}")
        feedback_summary = quality_summary.get("feedback") or {}
        if feedback_summary.get("responses"):
            lines.append(
                "  user feedback: "
                f"good={feedback_summary.get('good', 0)}, "
                f"bad={feedback_summary.get('bad', 0)}, "
                f"responses={feedback_summary.get('responses', 0)}")
        relevance = quality_summary.get("user_rated_relevance") or {}
        if relevance.get("assessed"):
            lines.append(
                "  user-rated relevance proxy: "
                f"{relevance.get('relevant', 0)}/"
                f"{relevance.get('assessed', 0)} relevant")
        review_counts = quality_summary.get("review_status_counts") or {}
        if review_counts:
            lines.append("  review status: " + ", ".join(
                f"{key}={value}" for key, value in sorted(
                    review_counts.items())))
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
    queue_total = int(r.get("review_queue_total") or 0)
    if queue_total:
        lines.append(
            f"review queue: showing {len(r.get('review_queue') or [])} of "
            f"{queue_total} item(s)"
            + (" (truncated)" if r.get("review_queue_truncated") else ""))
    if r["suggestions"]:
        lines.append("")
        lines.append("What to do next, in order:")
        lines += [f"  {i}. {s}" for i, s in enumerate(r["suggestions"], 1)]
    elif r.get("failed"):
        lines.append(
            f"{r.get('failed', 0)} failed or negatively rated turn(s) "
            "observed below the repeat/tool-error suggestion thresholds — "
            "inspect Recent failed turns")
    else:
        lines.append(
            "nothing actionable — no active review or threshold-triggered "
            "failure pattern")
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
