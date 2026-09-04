#!/usr/bin/env python3
"""Graded evaluation of real questions against the real stack.

    .venv/bin/python scripts/eval.py                  # Finance cases
    .venv/bin/python scripts/eval.py --suite p2go     # P2Go silo cases
    .venv/bin/python scripts/eval.py --suite all      # separate scores
    .venv/bin/python scripts/eval.py --case ar-aging  # one case
    .venv/bin/python scripts/eval.py --from-qlog      # seed cases from failures
    .venv/bin/python scripts/eval.py --provider gemini
    .venv/bin/python scripts/eval.py --provider claude

Why this exists: the suites pin SQL and engine behavior, but MODEL behavior —
which tool it picks, whether it refuses, whether it answers from the wiki when
it should query the ledger — was previously only reasoned about. A prompt edit
that fixes one routing problem can silently break another. This replays a fixed set
of questions through the actual MCP server and asserts deterministic
properties of the turn, so a change to the prompt, the model, or the tool
docstrings is MEASURED rather than hoped.

Assertions are deliberately structural, never "does this read well":
  any_tool           at least one of these tools was called
  all_tools           every listed tool succeeded at least once
  ordered_tools       listed tools succeeded in this order (other calls okay)
  not_tool           none of these tools was called
  allowed_tools      the complete callable profile; [] means no tool calls
  tool_args_contain  a call carried these argument values
  tool_args_by_tool  a named successful call carried these argument values
  failed_tools       the named guard/tool refusal occurred
  tool_result_fields all paths for one tool match in the same successful call
  tool_result_sets   one successful named call has exactly these path values
  tool_result_values_within all observed path values stay in an allowlist
  tool_result_path_equal two paths in one successful call are equal
  answer_contains    the answer text contains each string
  answer_lacks       the answer contains none of these strings
  not_refused        the answer is not one of the guard refusals

Exit code 0 when every case passes, 1 otherwise — so it can gate a merge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FINANCE_CASES = ROOT / "evals" / "cases.json"
P2GO_CASES = ROOT / "evals" / "p2go_cases.json"
# Backward-compatible name used by the qlog seeder and a few focused tests.
CASES = FINANCE_CASES
EVAL_PENDING = ROOT / "logs" / "eval-pending.json"

# The grading engine lives in pstb.evalharness.runner now (the provable-
# answers harness is its second consumer); everything imports back so this
# script's CLI, behavior, and test-visible names are unchanged.
from pstb.evalharness.runner import (_REFUSALS, _case_source,           # noqa: F401,E402
                                     _decoded_result, _grade,           # noqa: F401
                                     _load_cases, _replace_values,      # noqa: F401
                                     _result_observation,               # noqa: F401
                                     _result_values, _run_case,         # noqa: F401
                                     _runtime_profile, _runtime_scope,  # noqa: F401
                                     _write_private_json)               # noqa: F401

_SAFE_OBJECT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


def _discover_p2go_values_from_catalog(
        path: Path, default_schema: str,
        secondary_schemas: tuple[str, ...]) -> dict[str, str]:
    """Choose real, structural-only examples without querying source rows."""
    if not path.is_file():
        return {}
    default = str(default_schema or "").upper()
    secondaries = tuple(str(item).upper() for item in secondary_schemas
                        if str(item).strip())
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id,schema_name,name FROM nodes WHERE source='p2go' "
            "AND kind IN ('table','view') ORDER BY schema_name,name,id"
        ).fetchall()
        rows = [row for row in rows
                if _SAFE_OBJECT_NAME.fullmatch(str(row["name"] or ""))
                and _SAFE_OBJECT_NAME.fullmatch(
                    str(row["schema_name"] or ""))]
        names: dict[str, set[str]] = {}
        for row in rows:
            names.setdefault(str(row["name"]), set()).add(
                str(row["schema_name"]).upper())
        alias_counts: dict[str, int] = {}
        if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='aliases'").fetchone():
            alias_counts = {
                str(row["alias_upper"]): int(row["n"])
                for row in con.execute(
                    "SELECT alias_upper,COUNT(DISTINCT node_id) AS n "
                    "FROM aliases WHERE source='p2go' GROUP BY alias_upper")
            }

        values: dict[str, str] = {}
        unique_default = next((row for row in rows
                               if str(row["schema_name"]).upper() == default
                               and len(names[str(row["name"])]) == 1), None)
        if alias_counts:
            unique_default = next((
                row for row in rows
                if str(row["schema_name"]).upper() == default
                and alias_counts.get(str(row["name"]).upper(), 0) == 1
            ), None)
        if unique_default is not None:
            values["P2GO_DEFAULT_OBJECT"] = str(unique_default["name"])
            values["P2GO_DEFAULT_SCHEMA"] = default

        secondary = next((row for row in rows
                          if str(row["schema_name"]).upper() in secondaries),
                         None)
        if secondary is not None:
            schema = str(secondary["schema_name"]).upper()
            name = str(secondary["name"])
            values["P2GO_SECONDARY_SCHEMA"] = schema
            values["P2GO_SECONDARY_OBJECT"] = f"{schema}.{name}"
            values["P2GO_SECONDARY_NAME"] = name

        ambiguous = next((
            (name, schemas) for name, schemas in sorted(names.items())
            if default in schemas and any(s in schemas for s in secondaries)
        ), None)
        if ambiguous is not None:
            name, schemas = ambiguous
            other = next(schema for schema in secondaries if schema in schemas)
            values.update({
                "P2GO_AMBIGUOUS_OBJECT": name,
                "P2GO_AMBIGUOUS_DEFAULT_SCHEMA": default,
                "P2GO_AMBIGUOUS_SECONDARY_SCHEMA": other,
            })

        # Native view lineage is object -> object. A native FK is
        # object -> constraint -> referenced object. Normalize both shapes to
        # one direct pair for the join_path acceptance case.
        cross = con.execute(
            "SELECT S.schema_name AS from_schema,S.name AS from_name,"
            "T.schema_name AS to_schema,T.name AS to_name "
            "FROM edges E JOIN nodes S ON S.id=E.src "
            "JOIN nodes T ON T.id=E.dst "
            "WHERE E.kind='view_depends_on' AND S.source='p2go' "
            "AND T.source='p2go' AND UPPER(S.schema_name)<>UPPER(T.schema_name) "
            "UNION ALL "
            "SELECT S.schema_name,S.name,T.schema_name,T.name "
            "FROM edges O JOIN nodes S ON S.id=O.src "
            "JOIN edges F ON F.src=O.dst "
            "JOIN nodes T ON T.id=F.dst "
            "WHERE O.kind='object_has_constraint' "
            "AND F.kind='foreign_key_references_object' "
            "AND S.source='p2go' AND T.source='p2go' "
            "AND UPPER(S.schema_name)<>UPPER(T.schema_name) "
            "ORDER BY from_schema,from_name,to_schema,to_name LIMIT 1"
        ).fetchone()
        if cross is not None:
            from_schema = str(cross["from_schema"]).upper()
            to_schema = str(cross["to_schema"]).upper()
            allowed = {default, *secondaries}
            if from_schema in allowed and to_schema in allowed:
                values.update({
                    "P2GO_CROSS_FROM":
                        f"{from_schema}.{cross['from_name']}",
                    "P2GO_CROSS_TO": f"{to_schema}.{cross['to_name']}",
                    "P2GO_CROSS_FROM_SCHEMA": from_schema,
                    "P2GO_CROSS_TO_SCHEMA": to_schema,
                    "P2GO_CROSS_FROM_NAME": str(cross["from_name"]),
                    "P2GO_CROSS_TO_NAME": str(cross["to_name"]),
                })
        return values
    except sqlite3.DatabaseError:
        return {}
    finally:
        if "con" in locals():
            con.close()


def _discover_p2go_values(cfg) -> dict[str, str]:
    try:
        from pstb.metadata import source_catalog_path

        source = cfg.sources["p2go"]
        schemas = tuple(getattr(source, "schemas", None) or [])
        default = str(getattr(source, "schema", "") or
                      (schemas[0] if schemas else ""))
        secondary = tuple(schema for schema in schemas
                          if str(schema).upper() != default.upper())
        return _discover_p2go_values_from_catalog(
            source_catalog_path(cfg, "p2go"), default, secondary)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return {}


def _load_suite_cases(suite: str, values: dict[str, str] | None = None
                     ) -> tuple[list, list]:
    """Partition legacy and dedicated packs before any score is computed."""
    legacy, _legacy_skipped = _load_cases(FINANCE_CASES)
    if suite == "finance":
        return ([case for case in legacy
                 if _case_source(case) in ("", "default")], [])
    if suite != "p2go":
        raise ValueError(f"unknown eval suite {suite!r}")
    dedicated, skipped = _load_cases(P2GO_CASES, values)
    inherited = [case for case in legacy if _case_source(case) == "p2go"]
    return inherited + dedicated, skipped


async def _main(args) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from pstb.config import load_config

    cfg = load_config(os.environ.get("PSTB_CONFIG") or str(ROOT / "config.yaml"))
    provider_name = (args.provider or cfg.llm.provider).lower()
    suite_names = (["finance", "p2go"] if args.suite == "all"
                   else [args.suite])
    suites = []
    for suite_name in suite_names:
        cases, skipped = _load_suite_cases(
            suite_name,
            _discover_p2go_values(cfg) if suite_name == "p2go" else None,
        )
        if args.case:
            cases = [case for case in cases if case["id"] == args.case]
            skipped = [item for item in skipped if item["id"] == args.case]
        suites.append((suite_name, cases, skipped))
    if args.case and not any(cases or skipped for _, cases, skipped in suites):
        print(f"no case named {args.case!r} in suite {args.suite!r}")
        return 1
    if args.case and not any(cases for _, cases, _ in suites):
        for suite_name, _cases, skipped in suites:
            for item in skipped:
                print(f"[{suite_name} N/A] {item['id']}: {item['reason']}")
        print("The requested case did not run; N/A is not a passing score.")
        return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Point the wiki at the eval fixture, not the bundled demo pages. The
    # evidence gate correctly refuses demo content as support for a policy
    # answer, which made every policy case unpassable here — a permanently
    # red case teaches you to stop reading the eval. The fixture pages are
    # ordinary policy documents, so these cases test the real path.
    fixture = ROOT / "evals" / "wiki"
    if fixture.is_dir():
        env["PSTB_WIKI_PROVIDER"] = "localdocs"
        env["PSTB_WIKI_LOCALDOCS_PATH"] = str(fixture)
        cfg.wiki.provider = "localdocs"
        cfg.wiki.localdocs_path = str(fixture)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "pstb.server"], env=env)

    # Name which prompt was measured, so a pasted result is self-describing
    # and an A/B pair cannot be mixed up after the fact.
    skills = "off" if args.no_skills else "on"
    total = sum(len(cases) for _, cases, _ in suites)
    print(f"eval: {total} runnable case(s) · provider={provider_name} · "
          f"skills={skills} · backend={cfg.db.backend}")
    results = []
    summaries = {}
    not_applicable = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for suite_name, cases, skipped in suites:
                label = "Finance" if suite_name == "finance" else "P2Go"
                not_applicable[suite_name] = skipped
                print(f"\n{label} suite ({len(cases)} runnable, "
                      f"{len(skipped)} N/A)")
                suite_results = []
                for item in skipped:
                    print(f"  [N/A ] {item['id']:<26}       {item['reason']}")
                for case in cases:
                    try:
                        res = await _run_case(
                            session, cfg, provider_name, case,
                            skills=not args.no_skills)
                    except Exception as e:  # crash is failure, not a stop
                        res = {"id": case["id"], "answer": "", "calls": [],
                               "seconds": 0, "problems": [
                                   f"raised {type(e).__name__}: {e}"]}
                    res["suite"] = suite_name
                    results.append(res)
                    suite_results.append(res)
                    mark = "PASS" if not res["problems"] else "FAIL"
                    print(
                        f"  [{mark}] {res['id']:<26} {res['seconds']:>5.1f}s  "
                        f"{[c['tool'] for c in res['calls']]}")
                    for problem in res["problems"]:
                        print(f"         - {problem}")
                failed = [result for result in suite_results
                          if result["problems"]]
                summaries[suite_name] = {
                    "passed": len(suite_results) - len(failed),
                    "run": len(suite_results),
                    "failed": len(failed),
                    "not_applicable": len(skipped),
                }

    print("\nSource-separated scores")
    for suite_name, summary in summaries.items():
        label = "Finance" if suite_name == "finance" else "P2Go"
        print(f"  {label:<8} {summary['passed']}/{summary['run']} passed"
              + (f" · {summary['not_applicable']} N/A"
                 if summary["not_applicable"] else ""))
    failed = [r for r in results if r["problems"]]
    if args.json:
        _write_private_json(Path(args.json), {
            "summary_by_source": summaries,
            "not_applicable_by_source": not_applicable,
            "results": results,
        })
        print(f"detail written to {args.json}")
    if failed:
        print("\nFailures are routing/behavior regressions — compare against the "
              "last run before blaming the model.")
    return 1 if failed else 0


def _seed_from_qlog(path: str) -> int:
    """Write flagged questions to an ignored owner-only review queue.

    Real failures are the best eval material; this turns the backlog into
    regression candidates instead of anecdotes. It deliberately never edits a
    tracked eval pack: a human must redact and promote a reviewed case.
    """
    from pstb.qlog import redact_private_text
    from pstb.qlog_report import _records
    from pstb.quality import RUNTIME_GROUNDING_BASIS

    log = Path(path)
    if not log.exists():
        print(f"no question log at {log}")
        return 1
    packs = {
        "finance": json.loads(FINANCE_CASES.read_text(encoding="utf-8")),
        "p2go": json.loads(P2GO_CASES.read_text(encoding="utf-8")),
    }
    try:
        raw_pending = json.loads(EVAL_PENDING.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        raw_pending = {}
    pending = {
        "finance": list(raw_pending.get("finance") or [])
        if isinstance(raw_pending, dict) else [],
        "p2go": list(raw_pending.get("p2go") or [])
        if isinstance(raw_pending, dict) else [],
    }
    known = {
        (suite, str(case.get("question") or ""))
        for suite, pack in packs.items()
        for case in pack.get("cases", []) if isinstance(case, dict)
    }
    known.update(
        (suite, str(case.get("question") or ""))
        for suite, cases in pending.items() for case in cases
        if isinstance(case, dict)
    )
    joined = _records(log)
    entries = joined["turns"]
    latest_feedback = joined["feedback"]
    latest_quality = joined["quality"]
    latest_reviews = joined["reviews"]
    bad_ids = {
        turn_id for turn_id, entry in latest_feedback.items()
        if entry.get("verdict") == "bad"
    }
    blocked_ids = {
        turn_id for turn_id, entry in latest_quality.items()
        if entry.get("basis") == RUNTIME_GROUNDING_BASIS
        and isinstance(entry.get("groundedness"), dict)
        and entry["groundedness"].get("status") == "blocked"
    }
    added = {"finance": 0, "p2go": 0}
    for entry in entries:
        turn_id = str(entry.get("turn_id") or "")
        review = latest_reviews.get(turn_id) or {}
        if review.get("status") in {"verified", "dismissed"}:
            continue
        if (entry.get("type") != "turn"
                or not (entry.get("failed")
                        or turn_id in bad_ids
                        or turn_id in blocked_ids)):
            continue
        question = redact_private_text(entry.get("question"), limit=8_000)
        source = str(entry.get("source_database") or
                     (entry.get("scope") or {}).get("source") or
                     "default").strip() or "default"
        suite = "p2go" if source == "p2go" else "finance"
        if not question or (suite, question) in known:
            continue
        raw_scope = entry.get("scope") if isinstance(
            entry.get("scope"), dict) else {}
        scope = {"source": source}
        if suite == "finance":
            for key in ("business_unit", "ledger", "fiscal_year", "period"):
                if raw_scope.get(key) not in (None, ""):
                    scope[key] = raw_scope[key]
        known.add((suite, question))
        flags = list(entry.get("flags") or [])
        if turn_id in bad_ids:
            flags.append("user_bad")
            feedback = latest_feedback.get(turn_id) or {}
            categories = feedback.get("categories")
            if isinstance(categories, list):
                flags.extend(
                    f"feedback_{category}" for category in categories
                    if category in {
                        "not_relevant", "unsupported_claim", "wrong_number",
                        "wrong_source", "incomplete", "too_slow", "other",
                    }
                )
        if turn_id in blocked_ids:
            flags.append("grounding_blocked")
        pending[suite].append({
            "id": f"qlog-{entry.get('turn_id', added[suite])}",
            "question": question,
            "scope": scope,
            "skip": True,
            "expect": {"_todo": "fill in the correct behavior, then remove skip",
                       "_observed_flags": flags},
        })
        added[suite] += 1
    if any(added.values()):
        _write_private_json(EVAL_PENDING, pending)
    print(
        f"added {added['finance']} Finance and {added['p2go']} P2Go pending "
        f"case(s) from {log} to {EVAL_PENDING}; review/redact private "
        "identifiers and explicitly promote approved cases into the tracked "
        "source pack"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Graded evals for the finance agent")
    ap.add_argument("--case", default="", help="run a single case by id")
    ap.add_argument("--suite", choices=("finance", "p2go", "all"),
                    default="finance",
                    help="source-specific pack; 'all' still reports separate "
                         "Finance and P2Go scores")
    ap.add_argument("--provider", default="",
                    help="ollama | gemini | claude")
    ap.add_argument("--json", default="", help="write detailed results here")
    ap.add_argument("--no-skills", action="store_true",
                    help="drop the provider's worked-example block, so the "
                         "same suite can be run with and without it")
    ap.add_argument("--from-qlog", nargs="?", const="logs/questions.jsonl",
                    default="", help="write an ignored owner-only pending "
                                     "review queue from the failure log")
    args = ap.parse_args()
    if args.from_qlog:
        return _seed_from_qlog(args.from_qlog)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
