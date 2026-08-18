"""Deterministic, privacy-safe answer-quality signals.

The runtime can prove a deliberately narrow set of grounding properties: a
data answer used successful evidence, source-bound results matched the selected
database, substantive figures appeared in tool results, and compliance
verdicts retrieved both sides of their evidence.  It cannot prove that every
sentence of arbitrary prose is semantically entailed.  The statuses below keep
that distinction explicit instead of turning "no guard fired" into a universal
groundedness claim.

Only the returned status, bounded counts, reason codes and basis are suitable
for persistence.  Questions, answers, tool payloads, SQL and object names must
never be added to this contract.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


RUNTIME_GROUNDING_BASIS = "runtime_evidence_guards_v1"

GROUNDEDNESS_STATUSES = frozenset({
    "passed", "blocked", "unknown", "not_applicable",
})

QUALITY_REASON_CODES = frozenset({
    "guarded_response",
    "missing_evidence",
    "no_evidence",
    "source_mismatch",
    "source_misattribution",
    "tool_error",
    "ungrounded_figure",
    "unverified_verdict",
})

QUALITY_COUNT_KEYS = (
    "evidence_calls",
    "successful_evidence_calls",
    "failed_evidence_calls",
    "unsupported_figure_count",
    "unverified_verdict_count",
    "source_mismatch_count",
    "source_misattribution_count",
)

MAX_QUALITY_COUNT = 1_000_000
MAX_REASON_CODES = 16


def _count(value: object) -> int:
    """Return a small non-negative count without accepting bool as an int."""
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return min(max(value, 0), MAX_QUALITY_COUNT)


def safe_reason_codes(values: object) -> list[str]:
    """Select the bounded public reason-code vocabulary in stable order."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return sorted({
        str(value) for value in values
        if str(value) in QUALITY_REASON_CODES
    })[:MAX_REASON_CODES]


def safe_groundedness(value: object) -> dict:
    """Defensively reduce a groundedness result at a persistence boundary."""
    raw = value if isinstance(value, Mapping) else {}
    status = str(raw.get("status") or "unknown")
    if status not in GROUNDEDNESS_STATUSES:
        status = "unknown"
    raw_counts = raw.get("counts")
    raw_counts = raw_counts if isinstance(raw_counts, Mapping) else {}
    return {
        "status": status,
        "reason_codes": safe_reason_codes(raw.get("reason_codes")),
        "counts": {
            key: _count(raw_counts.get(key)) for key in QUALITY_COUNT_KEYS
        },
    }


def runtime_groundedness(
    *,
    intent: str,
    evidence_calls: int,
    successful_evidence_calls: int,
    failed_evidence_calls: int,
    guard_blocked: bool = False,
    missing_evidence: bool = False,
    unsupported_figures: Iterable[object] = (),
    unverified_verdict: bool = False,
    source_mismatch_count: int = 0,
    source_misattribution_count: int = 0,
) -> dict:
    """Summarize the runtime evidence guards without retaining their inputs.

    ``passed`` is intentionally limited to data/mixed turns that ended with a
    successful evidence call and no hard grounding guard.  Policy, technical
    and structural prose remains ``unknown`` because numeric/source checks do
    not establish full semantic entailment.  A general answer with no evidence
    requirement is ``not_applicable`` rather than a vacuous pass.

    A recovered tool error is counted but does not by itself make the final
    answer blocked.  The final guard/evidence state decides that, matching the
    agent loop's ability to correct an argument and retry successfully.
    """
    try:
        unsupported_count = len(tuple(unsupported_figures or ()))
    except TypeError:
        unsupported_count = 0
    counts = {
        "evidence_calls": _count(evidence_calls),
        "successful_evidence_calls": _count(successful_evidence_calls),
        "failed_evidence_calls": _count(failed_evidence_calls),
        "unsupported_figure_count": _count(unsupported_count),
        "unverified_verdict_count": 1 if unverified_verdict else 0,
        "source_mismatch_count": _count(source_mismatch_count),
        "source_misattribution_count": _count(
            source_misattribution_count),
    }

    reasons: set[str] = set()
    if guard_blocked:
        reasons.add("guarded_response")
    if missing_evidence:
        reasons.add("missing_evidence")
    if counts["unsupported_figure_count"]:
        reasons.add("ungrounded_figure")
    if counts["unverified_verdict_count"]:
        reasons.add("unverified_verdict")
    if counts["source_mismatch_count"]:
        reasons.add("source_mismatch")
    if counts["source_misattribution_count"]:
        reasons.add("source_misattribution")
    if (counts["failed_evidence_calls"]
            and not counts["successful_evidence_calls"]):
        reasons.add("tool_error")

    hard_block = bool(
        guard_blocked
        or missing_evidence
        or counts["unsupported_figure_count"]
        or counts["unverified_verdict_count"]
        or (counts["source_mismatch_count"]
            and not counts["successful_evidence_calls"])
    )
    normalized_intent = str(intent or "").strip().lower()
    if hard_block:
        status = "blocked"
    elif normalized_intent == "general" and not counts["evidence_calls"]:
        status = "not_applicable"
    elif not counts["successful_evidence_calls"]:
        status = "unknown"
        reasons.add("no_evidence")
    elif counts["source_misattribution_count"]:
        # Attribution reads prose and is intentionally heuristic. It is strong
        # enough to prevent a deterministic pass, but not strong enough to
        # label the whole answer blocked.
        status = "unknown"
    elif normalized_intent in {"data", "mixed"}:
        status = "passed"
    else:
        # Structural, policy and technical prose can contain non-numeric claims
        # that these runtime guards cannot entail mechanically.
        status = "unknown"

    return safe_groundedness({
        "status": status,
        "reason_codes": reasons,
        "counts": counts,
    })
