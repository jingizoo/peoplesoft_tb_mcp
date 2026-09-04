"""Validity probes: the machine check that a trap's premise still holds.

A trap only counts against anyone after THIS run proved its premise
through the live guarded engine — the vendor really is absent, the
fiscal year really holds no rows, the look-alike customer really
resolves. A probe that fails does not soften the trap to a warning; it
invalidates the case outright, because a caveat that fires on a correct
answer is worse than a miss.

The grammar is deliberately tiny. ``expect`` is the string "empty" or
"exists", judged from the payload's own count/collection fields, or a
one-clause path test ``{"path": ..., "contains"|"lacks": ...}`` walked
with the SAME dotted-path walker the grading contract uses
(runner._result_values) — one walker, no second dialect. Anything the
grammar cannot judge is valid=False with a reason, never a shrug that
lets the trap run.

Tool errors are read conservatively. "No such journal" satisfies an
emptiness expectation because the engine itself said not-found; an auth
failure, a timeout, or any other error is "probe errored" and the trap
is invalid — an outage must never be scored as a vendor that does not
exist.
"""
from __future__ import annotations

import json

from .runner import _decoded_result, _result_values

# Error texts that legitimately MEAN "nothing there". Anything else —
# auth, timeout, bad argument — is an outage, not an absence.
_NOT_FOUND_MARKERS = (
    "not found", "no such", "does not exist", "no rows", "no data",
)

# Payload keys that carry the primary collection when no count field
# does. Closed on purpose: judging emptiness from an unknown key shape
# is how a metadata list ("group_by": ["ACCOUNT"]) becomes evidence.
_COLLECTION_KEYS = (
    "rows", "customers", "vendors", "journals", "scopes", "items",
    "results", "matches", "candidates",
)


def validate_probe(probe, *, owner: str = "") -> None:
    """Reject any probe outside the grammar, naming the owning trap.

    corpus.py calls this at load time so a malformed pack dies before a
    single model runs; run_validity_probe assumes a validated shape.
    """
    label = f" (trap {owner!r})" if owner else ""
    if not isinstance(probe, dict):
        raise ValueError(f"validity_probe must be an object{label}")
    if not str(probe.get("tool") or "").strip():
        raise ValueError(f"validity_probe needs a tool name{label}")
    args = probe.get("args", {})
    if not isinstance(args, dict):
        raise ValueError(f"validity_probe args must be an object{label}")
    expect = probe.get("expect")
    if isinstance(expect, str):
        if expect not in ("empty", "exists"):
            raise ValueError(
                f"validity_probe expect must be 'empty', 'exists', or a "
                f"path clause; got {expect!r}{label}")
        return
    if isinstance(expect, dict):
        if not str(expect.get("path") or "").strip():
            raise ValueError(
                f"validity_probe path clause needs a path{label}")
        clauses = [key for key in ("contains", "lacks") if key in expect]
        if len(clauses) != 1:
            raise ValueError(
                f"validity_probe path clause needs exactly one of "
                f"'contains'/'lacks'; got {clauses!r}{label}")
        return
    raise ValueError(f"validity_probe expect is missing or malformed{label}")


def resolve_path_values(calls, tool: str, path: str,
                        args_contain=None) -> list:
    """Every value a dotted path yields from SUCCESSFUL calls to a tool.

    Failed calls are ignored entirely: an error payload is not evidence,
    and PR #34 already taught this codebase what happens when a call
    that merely happened is treated as a call that worked. The path
    grammar is runner._result_values verbatim ("customers[].total"),
    applied to each matching call's decoded result in call order.

    ``args_contain`` narrows to calls whose arguments carry every given
    key/value (string-compared). An entity trap's required figure must
    come from the call SCOPED to that entity -- an unfiltered call's
    grand total answers a different question, and counting it would let
    a wrong answer match a right one.
    """
    values: list = []
    for call in calls or []:
        if call.get("ok") is not True:
            continue
        if str(call.get("tool") or "") != str(tool or ""):
            continue
        if args_contain:
            call_args = call.get("args") or {}
            if not all(str(call_args.get(key)) == str(value)
                       for key, value in args_contain.items()):
                continue
        values.extend(_result_values(call.get("_result"), path))
    return values


def _call_text(res) -> str:
    """One text body from an MCP call result, the way the chat loop reads it."""
    chunks = [c.text for c in getattr(res, "content", []) or []
              if getattr(c, "text", None)]
    if chunks:
        text = "\n".join(chunks)
    else:
        structured = (getattr(res, "structured_content", None)
                      or getattr(res, "structuredContent", None) or {})
        text = json.dumps(structured or {}, default=str)
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        text = f"TOOL ERROR: {text}"
    return text


def _emptiness(payload):
    """(is_empty, has_content, reason) judged from the payload's own fields.

    Count fields ("count", "row_count") are the first authority; the
    closed collection-key list is the fallback. Both None means the
    grammar cannot judge this payload — the caller reports that as an
    invalid probe rather than guessing.
    """
    if isinstance(payload, list):
        return (not payload, bool(payload),
                f"list payload with {len(payload)} entries")
    if not isinstance(payload, dict):
        return None, None, f"payload is {type(payload).__name__}, not judgeable"
    counts = [(key, payload[key]) for key in ("count", "row_count")
              if isinstance(payload.get(key), (int, float))
              and not isinstance(payload.get(key), bool)]
    collections = [(key, payload[key]) for key in _COLLECTION_KEYS
                   if isinstance(payload.get(key), list)]
    if not counts and not collections:
        return None, None, ("no count or known collection field to judge "
                            "emptiness from")
    empty = (all(value == 0 for _, value in counts)
             and all(not value for _, value in collections))
    has = (any(value > 0 for _, value in counts)
           or any(value for _, value in collections))
    seen = ", ".join(f"{key}={value if not isinstance(value, list) else len(value)}"
                     for key, value in counts + collections)
    return empty, has, seen


def _evaluate(payload, expect) -> dict:
    """Judge a decoded SUCCESS payload against one expect clause."""
    if isinstance(expect, str):
        empty, has, reason = _emptiness(payload)
        if empty is None:
            return {"valid": False, "reason": f"probe unjudgeable: {reason}"}
        if expect == "empty":
            return ({"valid": True, "reason": f"empty as expected ({reason})"}
                    if empty else
                    {"valid": False,
                     "reason": f"expected empty, found content ({reason})"})
        return ({"valid": True, "reason": f"content exists ({reason})"}
                if has else
                {"valid": False,
                 "reason": f"expected content, found none ({reason})"})
    path = str(expect.get("path") or "")
    values = [str(value) for value in _result_values(payload, path)]
    shown = ", ".join(values[:8]) or "<nothing>"
    if "contains" in expect:
        needle = str(expect["contains"])
        if needle in values:
            return {"valid": True,
                    "reason": f"{path} contains {needle!r}"}
        return {"valid": False,
                "reason": f"{path} lacks {needle!r}; resolved: {shown}"}
    needle = str(expect["lacks"])
    if not values:
        # A vacuous "lacks" over nothing would validate a trap premise
        # from a payload that drifted away from the path entirely.
        return {"valid": False,
                "reason": f"{path} resolved no values; cannot prove it "
                          f"lacks {needle!r}"}
    if needle in values:
        return {"valid": False,
                "reason": f"{path} contains {needle!r}; resolved: {shown}"}
    return {"valid": True, "reason": f"{path} lacks {needle!r}"}


async def run_validity_probe(session, probe: dict) -> dict:
    """Run one probe through the live MCP session; {"valid", "reason"}.

    The probe calls the SAME guarded tool surface the agent uses, so a
    premise is proven by the engine, not by a side query the deployment
    would never serve. valid=True means the trap premise holds this run.
    """
    tool = str(probe.get("tool") or "")
    args = dict(probe.get("args") or {})
    expect = probe.get("expect")
    try:
        res = await session.call_tool(tool, arguments=args)
        text = _call_text(res)
    except Exception as exc:                              # noqa: BLE001
        text = f"TOOL ERROR: {type(exc).__name__}: {exc}"
    payload = _decoded_result(text)
    if isinstance(payload, dict) and payload.get("error"):
        error = str(payload["error"])
        lowered = error.lower()
        if expect == "empty" and any(marker in lowered
                                     for marker in _NOT_FOUND_MARKERS):
            return {"valid": True,
                    "reason": f"tool reported not-found: {error[:200]}"}
        return {"valid": False, "reason": f"probe errored: {error[:200]}"}
    return _evaluate(payload, expect)
