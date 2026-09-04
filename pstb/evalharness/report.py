"""Fixed-string reporting for the provable-answers harness.

Two jobs, both deliberately dull. build_summary turns scored rows into
the one artifact the harness is allowed to keep: verdicts, counts and
tracked-pack ids, nothing else. The row schema is closed and enforced by
refusal -- a row carrying an "answer", a "question", a "calls" list, or
any key this module does not know is rejected with ValueError rather
than stripped, because a summary that silently drops text channels today
will silently forward them after the next refactor. Values are held to
the closed vocabularies too, so the only free strings that can ride
through are slug-shaped case ids and the contract's own problem notes.

render_stdout prints the counts in fixed template strings, failures
first, no adjectives, no color. Every number is recomputed here from the
rows alone; the template never echoes a verdict enum token raw, so a new
verdict cannot leak into stdout unlabeled -- it simply is not counted
until this module learns its line. The closing paragraph states the
instrument's scope verbatim and never varies.
"""
from __future__ import annotations

import copy
import re

from .scoring import (JOINT_CLASSES, LEXICON_VERSION, PSTB_VERDICTS,
                      RAW_VERDICTS, SCORING_VERSION)

HARNESS_VERSION = "provable_answers_v1"

_KINDS = ("figure", "verdict", "policy", "trap")
_REQUIRED_KEYS = ("id", "kind", "pstb_verdict", "raw_verdict", "joint",
                  "figure_counts", "seconds")
_OPTIONAL_KEYS = ("problems", "refusal_pattern")

# Tracked-pack ids are slugs. An "id" with spaces is how an entity name
# would ride into the persistable file, so the shape itself is the guard.
_ID_SHAPE = re.compile(r"^[A-Za-z0-9._:@+-]{1,80}$")


def _fail(ident, message: str) -> None:
    raise ValueError(f"result row {ident!r}: {message}")


def _checked_row(row) -> dict:
    """One validated, freshly built copy of a result row.

    Rebuilding instead of copying is the point: a key that is not in the
    closed schema has no path into the output, and an unknown key raises
    so the caller learns about the leak instead of trusting a filter.
    """
    if not isinstance(row, dict):
        raise ValueError(
            f"result row must be a dict, got {type(row).__name__}")
    ident = row.get("id")
    for key in row:
        if key not in _REQUIRED_KEYS and key not in _OPTIONAL_KEYS:
            _fail(ident, f"key {key!r} is outside the closed row schema; "
                         "the summary refuses text channels rather than "
                         "forwarding them")
    for key in _REQUIRED_KEYS:
        if key not in row:
            _fail(ident, f"missing required key {key!r}")

    if not isinstance(ident, str) or not _ID_SHAPE.match(ident):
        _fail(ident, "id must be a tracked-pack slug "
                     "(letters, digits, ._:@+- only)")
    if row["kind"] not in _KINDS:
        _fail(ident, f"unknown case kind {row['kind']!r}")
    if row["pstb_verdict"] not in PSTB_VERDICTS:
        _fail(ident, f"unknown pstb verdict {row['pstb_verdict']!r}")
    if row["raw_verdict"] not in RAW_VERDICTS:
        _fail(ident, f"unknown raw verdict {row['raw_verdict']!r}")
    if row["joint"] not in JOINT_CLASSES:
        _fail(ident, f"unknown joint class {row['joint']!r}")

    counts = row["figure_counts"]
    if (not isinstance(counts, dict)
            or set(counts) != {"pstb", "raw"}
            or any(isinstance(value, bool) or not isinstance(value, int)
                   or value < 0 for value in counts.values())):
        _fail(ident, "figure_counts must be exactly "
                     "{'pstb': int, 'raw': int} with counts >= 0")
    seconds = row["seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        _fail(ident, "seconds must be a number")

    clean = {
        "id": ident,
        "kind": row["kind"],
        "pstb_verdict": row["pstb_verdict"],
        "raw_verdict": row["raw_verdict"],
        "joint": row["joint"],
        "figure_counts": {"pstb": int(counts["pstb"]),
                          "raw": int(counts["raw"])},
        "seconds": float(seconds),
    }
    if "problems" in row:
        problems = row["problems"]
        if (not isinstance(problems, (list, tuple))
                or any(not isinstance(item, str) for item in problems)):
            _fail(ident, "problems must be a list of strings")
        clean["problems"] = list(problems)
    if "refusal_pattern" in row:
        if not isinstance(row["refusal_pattern"], bool):
            _fail(ident, "refusal_pattern must be a bool")
        clean["refusal_pattern"] = row["refusal_pattern"]
    return clean


def build_summary(*, results, meta) -> dict:
    """The persistable dict: verdicts and counts, never words.

    Rows go in validated and rebuilt (see _checked_row); the trap_invalid
    and refusal_pattern id lists are derived here so a consumer of the
    file never has to re-scan rows to find the instrument's own caveats.
    meta is harness-built (backend, sample_db, per-arm providers) and is
    copied through as given.
    """
    meta = meta or {}
    cases = [_checked_row(row) for row in (results or [])]
    return {
        "harness": HARNESS_VERSION,
        "scoring": SCORING_VERSION,
        "lexicon": LEXICON_VERSION,
        "backend": str(meta.get("backend") or ""),
        "sample_db": bool(meta.get("sample_db")),
        "providers": copy.deepcopy(meta.get("providers") or {}),
        "cases": cases,
        "trap_invalid": [row["id"] for row in cases
                         if row["pstb_verdict"] == "trap_invalid"],
        "refusal_pattern": [row["id"] for row in cases
                            if row.get("refusal_pattern")],
    }


def _provider_token(info) -> str:
    name = str((info or {}).get("name") or "?")
    model = str((info or {}).get("model") or "")
    return f"{name}/{model}" if model else name


def _tally(rows, verdict: str) -> int:
    return sum(1 for row in rows if row.get("pstb_verdict") == verdict)


def render_stdout(summary, results) -> str:
    """The fixed-template stdout report, counted from the rows alone.

    Failures lead; comparisons follow; the scope paragraph closes and
    never changes. Line by line:

    - the failure list carries every row whose joint class fails the run
      (pstb_failed and unscoreable both exit nonzero, so both are named
      up front rather than left to the exit code);
    - "declared-hole" counts a trap row scored structural_pass -- the
      unsupported-domain trap graded on its declared reason string.
      scoring_v1 folds most declared holes into informed not-found (the
      reason substring rides must_name), so this count stays 0 until the
      vocabulary distinguishes them; it is derived, never invented;
    - "fabricated" is claimed only for traps whose premise survived this
      run (neither trap_invalid nor unscoreable); every other stated
      figure from the raw arm is merely "unverifiable".
    """
    summary = summary or {}
    rows = list(results or [])
    figure_rows = [row for row in rows if row.get("kind") == "figure"]
    trap_rows = [row for row in rows if row.get("kind") == "trap"]

    failures = [str(row.get("id") or "") for row in rows
                if row.get("joint") in ("pstb_failed", "unscoreable")]
    invalid = _tally(trap_rows, "trap_invalid")
    valid = len(trap_rows) - invalid
    flagged = sum(1 for row in trap_rows if row.get("refusal_pattern"))

    raw_stated = [row for row in rows
                  if row.get("raw_verdict") == "stated_figures"]
    fabricated = sum(
        1 for row in raw_stated
        if row.get("kind") == "trap"
        and row.get("pstb_verdict") not in ("trap_invalid", "unscoreable"))
    unverifiable = len(raw_stated) - fabricated
    abstained = sum(1 for row in rows
                    if row.get("raw_verdict") == "abstained")
    prose = sum(1 for row in rows
                if row.get("raw_verdict") == "unverifiable_prose")

    head = (f"{summary.get('harness') or HARNESS_VERSION} "
            f"[{summary.get('scoring') or SCORING_VERSION}, "
            f"{summary.get('lexicon') or LEXICON_VERSION}] "
            f"backend {summary.get('backend') or '?'}")
    if summary.get("sample_db"):
        head += ", sample database"
    providers = summary.get("providers") or {}
    arm_bits = [f"pstb={_provider_token(providers.get('pstb'))}",
                f"raw={_provider_token(providers.get('raw'))}"]
    variant = str((providers.get("raw") or {}).get("prompt_variant") or "")
    if variant:
        arm_bits.append(f"prompt-variant={variant}")

    lines = [
        head,
        "arms: " + " ".join(arm_bits),
        "",
        "pstb failures (list first, by id): "
        + (", ".join(failures) if failures else "none"),
        f"Figure cases ({len(figure_rows)}): "
        f"proved {_tally(figure_rows, 'proved')}, "
        f"no-figures {_tally(figure_rows, 'no_figures')}, "
        f"ungrounded {_tally(figure_rows, 'ungrounded')}, "
        f"structural {_tally(figure_rows, 'structural_fail')}, "
        f"refused {_tally(figure_rows, 'refused')}.",
        f"Traps ({valid} valid, {invalid} invalid): "
        f"informed not-found {_tally(trap_rows, 'informed_notfound')}, "
        f"declared-hole {_tally(trap_rows, 'structural_pass')}, "
        f"guard-withheld {_tally(trap_rows, 'guard_withheld')},",
        f"  blind refusal {_tally(trap_rows, 'blind_refusal')} "
        f"({flagged} flagged refusal_pattern via twins), "
        f"stated a figure {_tally(trap_rows, 'stated_figure')} (FAIL).",
        f"Raw arm [{summary.get('lexicon') or LEXICON_VERSION}]: "
        f"fabricated on validated traps {fabricated}, "
        f"unverifiable figures {unverifiable},",
        f"  abstained (lexicon) {abstained}, "
        f"unverifiable prose {prose}.",
        "",
        "This instrument measures one property: whether a stated figure "
        "traces to the governed",
        "system of record, on this deployment, under the tool-free "
        "condition. It does not",
        "measure fluency, and it does not measure a model given exported "
        "data.",
    ]
    return "\n".join(lines)
