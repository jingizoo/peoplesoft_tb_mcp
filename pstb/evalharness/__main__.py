"""python -m pstb.evalharness — the provable-answers harness.

Two arms, one question, no model grading anything. The pstb arm runs
the real guarded loop; the raw arm gets the same question with a
checked-in neutral prompt and no tools. Every verdict traces to a pure
function in scoring.py, every trap premise is machine-validated this
run before it may count against anyone, and the harness refuses to
emit ANY report if its own canary triples stop scoring correctly.

Exit is nonzero on pstb failures, unscoreable cases, canary failure,
or a harness-integrity error — never on raw-arm outcomes: the raw arm
is the thing being measured, not the gate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from . import arms, corpus, probes, report, scoring          # noqa: E402
from .runner import _write_private_json                       # noqa: E402

# Overridable so the sabotage suite can point one CI run at transcripts
# built to be caught. Never read outside --ci.
FIXTURES = Path(os.environ.get("PSTB_PROVABLE_FIXTURES")
                or ROOT / "evals" / "fixtures" / "provable")
RAW_PROMPTS = ROOT / "evals" / "raw_prompts.json"


def _raw_prompt(variant: str, scope: dict) -> str:
    data = json.loads(RAW_PROMPTS.read_text(encoding="utf-8"))
    template = data[variant]
    return template.format(
        business_unit=scope.get("business_unit") or "US001",
        ledger=scope.get("ledger") or "ACTUALS",
        fiscal_year=scope.get("fiscal_year") or "2026",
        period=scope.get("period") or "6")


def _question_figures(question: str) -> list:
    from pstb.guards import substantive_figures
    return substantive_figures(question)


def _entity_facts(trap: dict, calls: list) -> dict:
    """Resolve the poison / required-figure paths against THIS run.

    Unresolvable (tool never called, payload shape drifted) stays None
    -- an integrity signal the scorer maps to "unscoreable", never a
    pass."""
    from pstb.guards import _numeric_key
    out: dict = {}
    for name in ("required_figure", "poison"):
        spec = trap.get(name)
        values = (probes.resolve_path_values(
            calls, str(spec.get("tool") or ""),
            str(spec.get("path") or ""),
            args_contain=spec.get("args"))
            if isinstance(spec, dict) else [])
        out[name] = ([_numeric_key(str(v)) for v in values]
                     if values else None)
    return out


def _figure_stated(answer_figures: list, keys) -> bool:
    from pstb.guards import _numeric_key
    stated = {_numeric_key(str(f)) for f in answer_figures}
    stated |= {k.lstrip("-") for k in stated}
    return any(k in stated or k.lstrip("-") in stated for k in keys)


def _wiki_poison_facts(trap: dict, answer_figures: list,
                       calls: list) -> dict:
    from pstb.guards import (_labels_for, _numeric_key,
                             tagged_payload_numbers)
    figure = str((trap.get("poison") or {}).get("figure") or "")
    if not figure:
        return {"poison_hit": None, "poison_wiki_only": None}
    hit = _figure_stated(answer_figures, {_numeric_key(figure)})
    tagged = tagged_payload_numbers(
        [(c.get("tool"), c.get("_result"))
         for c in calls if c.get("ok")])
    labels = _labels_for(figure, tagged)
    wiki_only = bool(labels) and labels <= {"wiki"}
    return {"poison_hit": hit,
            "poison_wiki_only": (wiki_only if labels else None)}


async def _score_case(session, cfg, args, case: dict, kind: str,
                      report_dir: Path, ci_fixture: dict | None) -> dict:
    from pstb.guards import substantive_figures

    question = str(case.get("question") or "")
    q_figures = _question_figures(question)
    probe_valid: bool | None = None
    probe_reason = ""
    if kind == "trap":
        probe = case.get("validity_probe")
        if isinstance(probe, dict):
            outcome = await probes.run_validity_probe(session, probe)
            probe_valid = bool(outcome.get("valid"))
            probe_reason = str(outcome.get("reason") or "")
        elif case.get("trap_kind") == "unsupported_domain":
            probe_valid = True        # the hole is declared in code
    errored = False
    facts: dict = {"poison_hit": None, "required_hit": None,
                   "poison_wiki_only": None}
    try:
        factory = None
        if ci_fixture is not None:
            steps = ci_fixture["steps"]
            factory = (lambda prompt, tools, observed:
                       arms.TemplatedScriptedProvider(steps, observed))
        pstb_run = await arms.run_pstb_arm(
            session, cfg, args.provider, case,
            report_dir=report_dir, provider_factory=factory)
    except arms.HarnessIntegrityError:
        raise
    except Exception as exc:                      # noqa: BLE001
        errored = True
        pstb_run = {"id": case.get("id"), "answer": "", "calls": [],
                    "payloads": [], "grade_problems": [
                        f"raised {type(exc).__name__}: {exc}"],
                    "guard_withheld": False, "groundedness_status": "",
                    "seconds": 0.0}
    answer = pstb_run["answer"]
    figures = substantive_figures(answer, exclude=q_figures)
    from pstb.guards import ungrounded_figures
    recomputed = ungrounded_figures(answer, pstb_run["payloads"])

    trap_kind = str(case.get("trap_kind") or "")
    if kind == "trap" and trap_kind == "entity_confusion" and not errored:
        resolved = _entity_facts(case, pstb_run["calls"])
        required = resolved.get("required_figure")
        poison = resolved.get("poison")
        facts["required_hit"] = (
            _figure_stated(figures, required)
            if required is not None else None)
        facts["poison_hit"] = (
            _figure_stated(figures, poison)
            if poison is not None else None)
    if kind == "trap" and trap_kind == "wiki_poison" and not errored:
        facts.update(_wiki_poison_facts(case, figures, pstb_run["calls"]))

    pstb_verdict = scoring.score_pstb(
        kind=kind, case=case, answer=answer, figures=figures,
        grade_problems=pstb_run["grade_problems"],
        recomputed_ungrounded=recomputed,
        guard_withheld=pstb_run["guard_withheld"],
        groundedness_status=pstb_run["groundedness_status"],
        probe_valid=probe_valid,
        poison_hit=facts["poison_hit"],
        required_hit=facts["required_hit"],
        poison_wiki_only=facts["poison_wiki_only"],
        errored=errored)

    if ci_fixture is not None:
        raw_answer = str(ci_fixture.get("raw_answer") or "")
        raw_meta = {"provider": "fixture", "model": "canned",
                    "seconds": 0.0}
    else:
        scope = case.get("scope") or {}
        raw = arms.run_raw_arm(
            cfg, args.raw_provider,
            _raw_prompt(args.raw_prompt_variant, scope), question)
        raw_answer, raw_meta = raw["answer"], raw
    raw_verdict = scoring.score_raw(
        kind=kind, answer=raw_answer, question=question,
        probe_valid=probe_valid)

    row = {
        "id": str(case.get("id") or ""),
        "kind": kind,
        "pstb_verdict": pstb_verdict,
        "raw_verdict": raw_verdict,
        "joint": scoring.joint_class(pstb_verdict, raw_verdict),
        "figure_counts": {"pstb": len(figures),
                          "raw": len(scoring.casual_figures(raw_answer))
                          + len(substantive_figures(
                              raw_answer, exclude=q_figures))},
        "seconds": round(float(pstb_run["seconds"])
                         + float(raw_meta.get("seconds") or 0.0), 1),
    }
    detail = {
        **row,
        "answer": answer,
        "raw_answer": raw_answer,
        "calls": [{k: v for k, v in c.items() if k != "_result"}
                  for c in pstb_run["calls"]],
        "probe": {"valid": probe_valid, "reason": probe_reason},
        "twin": case.get("twin") or "",
    }
    return {"row": row, "detail": detail}


def _flag_refusal_patterns(rows: list, details: list) -> None:
    """F9: a blind refusal whose twin ALSO refused is a pattern, not
    a discrimination -- flagged, never silently celebrated."""
    by_id = {row["id"]: row for row in rows}
    for detail in details:
        row = by_id.get(detail["id"])
        if row is None or row["pstb_verdict"] != "blind_refusal":
            continue
        twin = by_id.get(str(detail.get("twin") or ""))
        if twin is not None and twin["pstb_verdict"] in (
                "refused", "blind_refusal"):
            row["refusal_pattern"] = True


async def _run(args) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from pstb.config import load_config

    scoring.self_check()          # the canary: no report from a broken scorer

    cfg = load_config(os.environ.get("PSTB_CONFIG")
                      or str(ROOT / "config.yaml"))
    if not args.provider:
        args.provider = cfg.llm.provider
    body = corpus.load_corpus(ROOT)
    kinds = body["kinds"]
    selected: list[tuple[dict, str]] = []
    if args.suite in ("answerable", "all"):
        selected += [(case, kinds.get(case["id"], "policy"))
                     for case in body["cases"]]
    if args.suite in ("traps", "all"):
        selected += [(trap, "trap") for trap in body["traps"]]
    if args.case:
        selected = [(c, k) for c, k in selected if c.get("id") == args.case]
        if not selected:
            print(f"no case named {args.case!r}")
            return 1

    ci_fixtures: dict = {}
    if args.ci:
        # Scripted transcripts are exact: a mid-turn nudge would demand
        # a response the transcript does not carry and fail the case as
        # an error. CI measures the guards and the scorer, not the
        # nudge loop, so it runs nudge-free -- box mode never touches
        # this.
        from pstb.client import chat as _chat
        _chat.MAX_NUDGES = 0
        for path in sorted(FIXTURES.glob("*.json")):
            if path.name.startswith("raw_"):
                continue
            ci_fixtures[path.stem] = json.loads(
                path.read_text(encoding="utf-8"))
        selected = [(c, k) for c, k in selected
                    if c.get("id") in ci_fixtures]

    report_dir = ROOT / "logs" / "provable"
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    fixture = ROOT / "evals" / "wiki"
    if fixture.is_dir():
        env["PSTB_WIKI_PROVIDER"] = "localdocs"
        env["PSTB_WIKI_LOCALDOCS_PATH"] = str(fixture)
        cfg.wiki.provider = "localdocs"
        cfg.wiki.localdocs_path = str(fixture)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "pstb.server"], env=env)

    rows, details = [], []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for case, kind in selected:
                outcome = await _score_case(
                    session, cfg, args, case, kind, report_dir,
                    ci_fixtures.get(str(case.get("id") or "")))
                rows.append(outcome["row"])
                details.append(outcome["detail"])
                mark = outcome["row"]["pstb_verdict"]
                print(f"  [{mark:>18}] {outcome['row']['id']:<28} "
                      f"raw={outcome['row']['raw_verdict']}")

    _flag_refusal_patterns(rows, details)
    meta = {
        "backend": cfg.db.backend,
        "sample_db": "sample" in str(
            getattr(cfg.db, "sqlite_path", "") or "").lower()
        or cfg.db.backend == "sqlite",
        "providers": {
            "pstb": {"name": args.provider,
                     "model": "transcript" if args.ci else ""},
            "raw": {"name": ("fixture" if args.ci
                             else args.raw_provider),
                    "model": "",
                    "prompt_variant": args.raw_prompt_variant},
        },
    }
    summary = report.build_summary(results=rows, meta=meta)
    print(report.render_stdout(summary, rows))
    if args.summary:
        _write_private_json(Path(args.summary), summary)
    if args.json:
        _write_private_json(Path(args.json), {"summary": summary,
                                              "details": details})
        print(f"detail written to {args.json}")
    bad = [row for row in rows
           if row["joint"] in ("pstb_failed", "unscoreable")]
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="provable-answers harness: pstb vs a raw model")
    ap.add_argument("--provider", default="")
    ap.add_argument("--raw-provider", default="")
    ap.add_argument("--suite", choices=("answerable", "traps", "all"),
                    default="all")
    ap.add_argument("--case", default="")
    ap.add_argument("--raw-prompt-variant", choices=("a", "b", "c"),
                    default="a")
    ap.add_argument("--json", default="")
    ap.add_argument("--summary", default="")
    ap.add_argument("--ci", action="store_true",
                    help="scripted transcripts + canned raw fixtures; "
                         "no LLM anywhere")
    args = ap.parse_args(argv)
    if not args.ci and not args.raw_provider:
        ap.error("--raw-provider is required outside --ci: the raw arm "
                 "is named explicitly, never defaulted")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
