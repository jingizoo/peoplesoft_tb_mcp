#!/usr/bin/env python3
"""Graded evaluation of real questions against the real stack.

    .venv/bin/python scripts/eval.py                  # run every case
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
  not_tool           none of these tools was called
  tool_args_contain  a call carried these argument values
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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = ROOT / "evals" / "cases.json"

# Phrases the guards use when they withhold an answer. A case that expects a
# real answer must not match one of these.
_REFUSALS = (
    "I withheld that answer",
    "could not obtain a successful PeopleSoft result",
    "could not retrieve a verified policy passage",
    "Choose a PeopleSoft business unit",
    "cannot decide whether the result satisfies",
)


def _load_cases() -> list:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return [c for c in data.get("cases", []) if not c.get("skip")]


def _grade(case: dict, answer: str, calls: list) -> list:
    """Return a list of failure strings; empty means the case passed."""
    expect = case.get("expect") or {}
    called = [c.get("tool") for c in calls]
    problems = []

    want_any = expect.get("any_tool")
    if want_any and not any(t in called for t in want_any):
        problems.append(f"expected one of {want_any}, called {called or 'nothing'}")
    # A tool that was CALLED but ERRORED is not evidence the feature works.
    # PR #34 shipped run_playbook whose module was never imported: the eval
    # saw the call, the model narrated the error, and the case passed.
    #
    # But the bar is "at least one call SUCCEEDED", not "no call failed".
    # Recovering from a bad argument is the loop working as designed — a model
    # that guessed source="PeopleSoft", read the error listing the real source
    # names and immediately retried has demonstrated exactly the behaviour we
    # want, and failing it here trains the harness to punish resilience.
    if want_any:
        attempts = [c for c in calls if c.get("tool") in want_any]
        if attempts and not any(c.get("ok") for c in attempts):
            problems.append(
                "every call to the expected tool(s) failed: "
                + ", ".join(sorted({c["tool"] for c in attempts})))

    for banned in expect.get("not_tool") or []:
        if banned in called:
            problems.append(f"called {banned}, which this question does not need")

    wanted_args = expect.get("tool_args_contain") or {}
    if wanted_args:
        def _arg_matches(actual, expected) -> bool:
            # A structured argument (a pivot spec, a partition spec) matches
            # when the expected string names one of its keys or appears in
            # its JSON — "pivot": "row_field" means "a pivot spec carrying
            # row_field was passed", not string equality against a dict.
            if isinstance(actual, dict):
                return str(expected) in actual or str(expected) in json.dumps(actual)
            if isinstance(actual, list):
                return str(expected) in json.dumps(actual)
            return str(actual) == str(expected)

        hit = any(
            all(_arg_matches(c.get("args", {}).get(k), v)
                for k, v in wanted_args.items())
            for c in calls
        )
        if not hit:
            problems.append(
                f"no call carried {wanted_args}; args seen: "
                f"{[c.get('args') for c in calls]}"
            )

    for needle in expect.get("answer_contains") or []:
        if needle.lower() not in (answer or "").lower():
            problems.append(f"answer missing {needle!r}")

    for banned in expect.get("answer_lacks") or []:
        if banned.lower() in (answer or "").lower():
            problems.append(f"answer contains {banned!r}")

    if expect.get("not_refused"):
        for refusal in _REFUSALS:
            if refusal.lower() in (answer or "").lower():
                problems.append(f"answer was refused: {refusal!r}")
                break
    return problems


async def _run_case(session, cfg, provider_name: str, case: dict,
                    skills: bool = True) -> dict:
    from pstb.client.chat import agent_turn, tool_specs
    from pstb.client.prompt import system_prompt

    tools = tool_specs(await session.list_tools())
    # The PROVIDER decides what the prompt contains — Gemini gets the
    # worked-example block, the local model does not. Building the prompt
    # without it measured the small prompt against whichever model was
    # asked, so "--provider gemini" was never testing what Gemini is
    # actually sent. A harness that exercises a different code path than
    # the product gives false assurance; that is the whole reason this
    # file exists.
    prompt = system_prompt(cfg, surface="gui",
                           provider=provider_name if skills else "")
    scope = case.get("scope") or {}
    if scope:
        prompt += (
            "\n\n## Active scope selected by the user\n"
            f"- Business unit: {scope.get('business_unit')}\n"
            f"- Ledger: {scope.get('ledger')}\n"
            f"- Fiscal year: {scope.get('fiscal_year') or 'any'}\n"
            f"- Period: {scope.get('period') or 'any'}\n"
        )
    if provider_name == "gemini":
        from pstb.client.llm_gemini import GeminiVertexProvider as P
    elif provider_name == "claude":
        from pstb.client.llm_claude import ClaudeProvider as P
    else:
        from pstb.client.llm_ollama import OllamaProvider as P
    provider = P(cfg, prompt, tools)

    seen: list = []

    def observe(name, args, out, ms, ok):
        seen.append({"tool": name, "args": dict(args or {}), "ok": ok, "ms": ms})

    started = time.time()
    answer = await agent_turn(
        provider, session, case["question"], surface="gui",
        scope=scope or None, tool_observer=observe,
    )
    return {
        "id": case["id"],
        "answer": answer,
        "calls": seen,
        "seconds": round(time.time() - started, 1),
        "problems": _grade(case, answer, seen),
    }


async def _main(args) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from pstb.config import load_config

    cfg = load_config(os.environ.get("PSTB_CONFIG") or str(ROOT / "config.yaml"))
    provider_name = (args.provider or cfg.llm.provider).lower()
    cases = _load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"no case named {args.case!r}")
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
    print(f"eval: {len(cases)} case(s) · provider={provider_name} · "
          f"skills={skills} · backend={cfg.db.backend}\n")
    results = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for case in cases:
                try:
                    res = await _run_case(session, cfg, provider_name,
                                         case, skills=not args.no_skills)
                except Exception as e:  # a crash is a failure, not a stop
                    res = {"id": case["id"], "answer": "", "calls": [],
                           "seconds": 0, "problems": [f"raised {type(e).__name__}: {e}"]}
                results.append(res)
                mark = "PASS" if not res["problems"] else "FAIL"
                print(f"  [{mark}] {res['id']:<26} {res['seconds']:>5.1f}s  "
                      f"{[c['tool'] for c in res['calls']]}")
                for problem in res["problems"]:
                    print(f"         - {problem}")

    failed = [r for r in results if r["problems"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"detail written to {args.json}")
    if failed:
        print("\nFailures are routing/behavior regressions — compare against the "
              "last run before blaming the model.")
    return 1 if failed else 0


def _seed_from_qlog(path: str) -> int:
    """Append flagged questions from the live log as pending cases.

    Real failures are the best eval material; this turns the backlog into
    regressions instead of anecdotes. Seeded cases start with skip=true so a
    human decides what the correct behavior actually is.
    """
    log = Path(path)
    if not log.exists():
        print(f"no question log at {log}")
        return 1
    data = json.loads(CASES.read_text(encoding="utf-8"))
    known = {c["question"] for c in data["cases"]}
    added = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "turn" or not entry.get("failed"):
            continue
        question = entry.get("question") or ""
        if not question or question in known:
            continue
        known.add(question)
        data["cases"].append({
            "id": f"qlog-{entry.get('turn_id', added)}",
            "question": question,
            "scope": {},
            "skip": True,
            "expect": {"_todo": "fill in the correct behavior, then remove skip",
                       "_observed_flags": entry.get("flags", [])},
        })
        added += 1
    CASES.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"added {added} pending case(s) from {log}; edit evals/cases.json "
          "to state the expected behavior and remove skip")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Graded evals for the finance agent")
    ap.add_argument("--case", default="", help="run a single case by id")
    ap.add_argument("--provider", default="",
                    help="ollama | gemini | claude")
    ap.add_argument("--json", default="", help="write detailed results here")
    ap.add_argument("--no-skills", action="store_true",
                    help="drop the provider's worked-example block, so the "
                         "same suite can be run with and without it")
    ap.add_argument("--from-qlog", nargs="?", const="logs/questions.jsonl",
                    default="", help="seed pending cases from the failure log")
    args = ap.parse_args()
    if args.from_qlog:
        return _seed_from_qlog(args.from_qlog)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
