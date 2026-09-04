"""The tracked corpus: cases.json plus the trap pack, resolved and checked.

cases.json is the answerable suite and it is READ-ONLY here — the trap
pack borrows from it, never edits it. A trap that wants an existing
case's exact wording says ``"import": "<case id>"`` and gets that
case's question/scope/expect copied in at load time, so the two can
never drift apart; a trap that tries to import AND carry its own copy
of any of those keys is rejected, because a silently shadowed question
is exactly the drift the import exists to prevent.

Every runnable trap must name a ``twin`` — the answerable sibling the
report prints beside it, so refusal-happiness has nowhere to hide (a
model that "passes" a trap by refusing everything fails its twin).
Twins, probes, kinds and entity specs are all validated here, at load,
with errors that name the offending id: a malformed pack dies before a
single model runs, not halfway through a report.

The ``kinds`` block declares — never derives — what each answerable
case is: ``figure`` (a substantive amount is the answer), ``verdict``
(a Coupa-tie-style conclusion), or ``policy``. Deriving kind from the
expect block is how a vacuous grounded-pass happens; declaring it makes
the classification reviewable data. The returned map additionally
carries each runnable trap id as kind ``trap`` so one lookup covers the
whole corpus.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .probes import validate_probe
from .runner import _load_cases

_CASE_KINDS = ("figure", "verdict", "policy")
_IMPORTED_KEYS = ("question", "scope", "expect")


def _trap_error(trap_id: str, why: str) -> ValueError:
    return ValueError(f"traps.json: trap {trap_id!r} {why}")


def _resolve_trap(raw: dict, by_id: dict, runnable_ids: set) -> dict:
    tid = str(raw.get("id") or "")
    trap = copy.deepcopy(raw)
    if str(trap.get("kind") or "") != "trap":
        raise _trap_error(tid, 'must declare kind "trap"')
    trap_kind = str(trap.get("trap_kind") or "")
    if not trap_kind:
        raise _trap_error(tid, "needs a trap_kind")

    source_id = str(trap.pop("import", "") or "")
    if source_id:
        shadowed = [key for key in _IMPORTED_KEYS if key in trap]
        if shadowed:
            raise _trap_error(
                tid, f"imports {source_id!r} but also carries its own "
                     f"{shadowed} — the import exists so these cannot drift")
        source = by_id.get(source_id)
        if source is None:
            raise _trap_error(
                tid, f"imports unknown or unrunnable case {source_id!r}")
        for key in _IMPORTED_KEYS:
            if key in source:
                trap[key] = copy.deepcopy(source[key])

    if not str(trap.get("question") or "").strip():
        raise _trap_error(tid, "has no question")

    twin = str(trap.get("twin") or "")
    if twin not in runnable_ids:
        raise _trap_error(
            tid, f"names twin {twin!r}, which is not a runnable case id")

    must = trap.get("must_name")
    if not (isinstance(must, list) and must
            and all(isinstance(name, str) and name.strip() for name in must)):
        raise _trap_error(tid, "needs a non-empty must_name list of strings")

    probe = trap.get("validity_probe")
    if probe is not None:
        validate_probe(probe, owner=tid)
    elif trap_kind != "unsupported_domain":
        # Only a hole declared in code (guards.UNSUPPORTED_DOMAIN_REASONS)
        # is its own proof. Every other premise must be machine-checked
        # this run, or the trap can rot into scoring a correct answer.
        raise _trap_error(tid, f"of kind {trap_kind!r} needs a validity_probe")

    if trap_kind == "entity_confusion":
        for name in ("required_figure", "poison"):
            spec = trap.get(name)
            if not (isinstance(spec, dict)
                    and str(spec.get("tool") or "").strip()
                    and str(spec.get("path") or "").strip()):
                raise _trap_error(
                    tid, f"needs {name} as {{tool, path}} payload-path spec")
            args = spec.get("args")
            if args is not None and not (
                    isinstance(args, dict) and args
                    and all(isinstance(key, str) for key in args)):
                raise _trap_error(
                    tid, f"{name}.args must be a non-empty dict of "
                         "argument filters when present")
    if trap_kind == "wiki_poison":
        spec = trap.get("poison")
        if not (isinstance(spec, dict)
                and str(spec.get("figure") or "").strip()):
            raise _trap_error(tid, 'needs poison as {"figure": "<literal>"}')
    return trap


def load_corpus(root: Path) -> dict:
    """Load evals/cases.json + evals/traps.json under ``root``, validated.

    Returns {"cases", "traps", "kinds", "skipped"}: the runnable
    answerable cases exactly as runner._load_cases produces them, the
    resolved runnable trap dicts, the declared kind map (cases as
    declared, runnable traps added as "trap"), and every skipped entry
    as {"id", "reason"}. Any structural problem raises ValueError
    naming the offending id.
    """
    root = Path(root)
    cases_path = root / "evals" / "cases.json"
    traps_path = root / "evals" / "traps.json"

    cases, case_skips = _load_cases(cases_path)
    runnable_ids = {case["id"] for case in cases}
    by_id = {case["id"]: case for case in cases}
    all_case_ids = {
        str(case.get("id") or "")
        for case in json.loads(
            cases_path.read_text(encoding="utf-8")).get("cases", [])
    }

    data = json.loads(traps_path.read_text(encoding="utf-8"))
    traps: list = []
    skipped: list = list(case_skips)
    seen: set = set()
    for raw in data.get("traps", []):
        if not isinstance(raw, dict):
            raise ValueError("traps.json: trap entries must be objects")
        tid = str(raw.get("id") or "")
        if not tid:
            raise ValueError("traps.json: a trap entry has no id")
        if tid in seen:
            raise _trap_error(tid, "appears twice")
        if tid in all_case_ids:
            raise _trap_error(tid, "collides with a cases.json id")
        seen.add(tid)
        if raw.get("skip"):
            skipped.append({"id": tid,
                            "reason": str(raw.get("_todo") or "skipped")})
            continue
        traps.append(_resolve_trap(raw, by_id, runnable_ids))

    kinds_block = data.get("kinds")
    if not isinstance(kinds_block, dict):
        raise ValueError("traps.json: missing the kinds block")
    for cid, kind in kinds_block.items():
        if cid not in all_case_ids:
            raise ValueError(
                f"traps.json: kinds block names {cid!r}, which is not a "
                f"cases.json id")
        if kind not in _CASE_KINDS:
            raise ValueError(
                f"traps.json: kinds[{cid!r}] is {kind!r}; must be one of "
                f"{_CASE_KINDS}")
    unclassified = sorted(runnable_ids - set(kinds_block))
    if unclassified:
        raise ValueError(
            "traps.json: kinds block leaves runnable cases unclassified: "
            + ", ".join(unclassified))

    kinds = dict(kinds_block)
    for trap in traps:
        kinds[trap["id"]] = "trap"
    return {"cases": cases, "traps": traps, "kinds": kinds,
            "skipped": skipped}
