"""The graded-eval engine, lifted from scripts/eval.py so harnesses can share it.

scripts/eval.py grew a second consumer (the provable-answers harness),
and the importlib hack tests/test_multi_schema_eval_pack.py documents
was the cost of these functions living in a script. They moved here
VERBATIM -- the script imports them back, so its behavior, its CLI, and
every existing assertion key are unchanged. Grading stays deliberately
structural (tools called, arguments carried, result paths, refusal
phrases): never "does this read well".
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FINANCE_CASES = ROOT / "evals" / "cases.json"
P2GO_CASES = ROOT / "evals" / "p2go_cases.json"
# Backward-compatible name used by the qlog seeder and focused tests.
CASES = FINANCE_CASES

# Phrases the guards use when they withhold an answer. A case that expects a
# real answer must not match one of these.
_REFUSALS = (
    "I withheld that answer",
    "could not obtain a successful PeopleSoft result",
    "could not retrieve a verified policy passage",
    "Choose a PeopleSoft business unit",
    "cannot decide whether the result satisfies",
)


_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _write_private_json(path: Path, payload: object) -> None:
    """Atomically write sensitive local eval evidence as owner-only.

    Eval output intentionally contains model answers and tool arguments. It
    is not the sanitized observability stream and must not follow a symlink or
    inherit a group/world-readable umask.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".building",
            dir=str(target.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        final_fd = os.open(
            target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(final_fd, 0o600)
        finally:
            os.close(final_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


def _replace_values(value, values: dict[str, str]):
    if isinstance(value, dict):
        return {key: _replace_values(item, values)
                for key, item in value.items() if key != "requires"}
    if isinstance(value, list):
        return [_replace_values(item, values) for item in value]
    if isinstance(value, str):
        return _PLACEHOLDER.sub(
            lambda match: str(values[match.group(1)]), value)
    return value


def _load_cases(path: Path = CASES, values: dict[str, str] | None = None
                ) -> tuple[list, list]:
    """Load one source-specific pack and report fixture-dependent N/As.

    P2Go object names are installation data, not universal constants. The
    runner discovers safe structural examples from that source's own offline
    artifact and expands them into the case pack. A database with no declared
    cross-schema FK (or no duplicate name) reports that scenario as N/A rather
    than pretending a made-up object is a passing evaluation.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    supplied = values or {}
    cases, skipped = [], []
    for case in data.get("cases", []):
        if case.get("skip"):
            continue
        required = [str(item) for item in case.get("requires") or []]
        missing = [name for name in required if not supplied.get(name)]
        if missing:
            skipped.append({
                "id": case.get("id") or "unnamed",
                "reason": "no catalog fixture for " + ", ".join(missing),
            })
            continue
        cases.append(_replace_values(case, supplied))
    return cases, skipped


def _case_source(case: dict) -> str:
    scope = case.get("scope")
    if not isinstance(scope, dict):
        return ""
    return str(scope.get("source") or "").strip()


def _result_values(payload, path: str) -> list:
    """Walk a dotted path ("rows[].amount") through a tool payload.

    Module-level on purpose: the provable-answers harness resolves its
    poison and required-figure paths with exactly the grammar the
    grading contract uses -- one walker, no second dialect."""
    current = [payload]
    for segment in str(path or "").split("."):
        if not segment:
            return []
        many = segment.endswith("[]")
        key = segment[:-2] if many else segment
        nxt = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                continue
            value = item[key]
            if many:
                if isinstance(value, list):
                    nxt.extend(value)
            else:
                nxt.append(value)
        current = nxt
    return current


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

    successful_calls = [c for c in calls if c.get("ok") is True]
    successful = [c.get("tool") for c in successful_calls]
    want_all = expect.get("all_tools") or []
    missing = [tool for tool in want_all if tool not in successful]
    if missing:
        problems.append(
            f"expected every tool {want_all} to succeed; missing {missing}; "
            f"successful calls were {successful or 'nothing'}")

    for tool in expect.get("failed_tools") or []:
        if not any(c.get("tool") == tool and c.get("ok") is False
                   for c in calls):
            problems.append(
                f"expected {tool} to be refused/fail, but no failed call "
                f"was observed; calls were {called or 'nothing'}")

    if "allowed_tools" in expect:
        allowed = set(expect.get("allowed_tools") or [])
        unexpected = sorted({tool for tool in called if tool not in allowed})
        if unexpected:
            problems.append(
                f"called tools outside the closed profile: {unexpected}")

    # Every generic database/catalog tool must identify the selected source,
    # including explicit Finance/default.  Argument pinning stops the model
    # asking for another database; this independently catches a stale worker,
    # copied artifact, or wrapper regression returning one.
    from pstb.guards import SOURCE_PROVENANCE_TOOLS
    expected_source = _case_source(case) or "default"
    for call in successful_calls:
        if call.get("tool") not in SOURCE_PROVENANCE_TOOLS:
            continue
        result = call.get("_result")
        actual = (str(result.get("source_database") or "").strip()
                  if isinstance(result, dict) else "")
        if actual != expected_source:
            problems.append(
                f"successful {call.get('tool')} returned source_database="
                f"{actual or '<missing>'!r}; selected source is "
                f"{expected_source!r}")

    ordered = expect.get("ordered_tools") or []
    if ordered:
        cursor = 0
        for tool in successful:
            if cursor < len(ordered) and tool == ordered[cursor]:
                cursor += 1
        if cursor != len(ordered):
            problems.append(
                f"expected successful tool sequence {ordered}; successful "
                f"calls were {successful or 'nothing'}")

    for banned in expect.get("not_tool") or []:
        if banned in called:
            problems.append(f"called {banned}, which this question does not need")

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

    def _args_match(call: dict, wanted: dict) -> bool:
        return all(_arg_matches(call.get("args", {}).get(key), value)
                   for key, value in wanted.items())

    wanted_args = expect.get("tool_args_contain") or {}
    if wanted_args:
        expected_tools = set(want_any or want_all or [])
        hit = any(
            _args_match(c, wanted_args)
            for c in successful_calls
            if not expected_tools or c.get("tool") in expected_tools
        )
        if not hit:
            problems.append(
                f"no successful expected call carried {wanted_args}; "
                f"args seen: {[c.get('args') for c in successful_calls]}"
            )

    # Source-bound evals use the named form. Arguments on a failed discovery
    # call must not satisfy the assertion while an unrelated successful call
    # supplies the result being graded.
    for tool, wanted in (expect.get("tool_args_by_tool") or {}).items():
        matches = [call for call in calls
                   if call.get("tool") == tool
                   and call.get("ok") is True
                   and _args_match(call, wanted)]
        if not matches:
            problems.append(
                f"no successful {tool} call carried {wanted}; calls seen: "
                f"{[c.get('args') for c in calls if c.get('tool') == tool]}")

    for tool, wanted in (
            expect.get("failed_tool_args_by_tool") or {}).items():
        matches = [call for call in calls
                   if call.get("tool") == tool
                   and call.get("ok") is False
                   and _args_match(call, wanted)]
        if not matches:
            problems.append(
                f"no failed {tool} call carried {wanted}; calls seen: "
                f"{[c.get('args') for c in calls if c.get('tool') == tool]}")

    named_args = expect.get("tool_args_by_tool") or {}

    def _result_candidates(tool: str) -> list[dict]:
        candidates = [call for call in successful_calls
                      if call.get("tool") == tool]
        wanted = named_args.get(tool)
        if isinstance(wanted, dict):
            candidates = [call for call in candidates
                          if _args_match(call, wanted)]
        return candidates

    # Result assertions are one evidence contract, not independent searches.
    # A healthy field from call A, the configured-owner set from call B, and
    # a matching snapshot id from call C must never combine into a pass.
    # Group every rule kind by tool and require one successful (and, when
    # declared, argument-matching) call to satisfy the entire conjunction.
    result_rules: dict[str, list[tuple[str, dict]]] = {}
    for key, kind in (
        ("tool_result_fields", "field"),
        ("tool_result_sets", "set"),
        ("tool_result_values_within", "within"),
        ("tool_result_path_equal", "path_equal"),
    ):
        for rule in expect.get(key) or []:
            tool = str(rule.get("tool") or "")
            result_rules.setdefault(tool, []).append((kind, rule))

    def _result_rule_satisfied(call: dict, kind: str, rule: dict) -> bool:
        payload = call.get("_result")
        if kind == "field":
            values = _result_values(payload, str(rule.get("path") or ""))
            return any(value == rule.get("equals") for value in values)
        if kind == "set":
            values = _result_values(payload, str(rule.get("path") or ""))
            expected = rule.get("equals")
            expected = expected if isinstance(expected, list) else []
            return (sorted(values, key=repr)
                    == sorted(expected, key=repr))
        if kind == "within":
            values = _result_values(payload, str(rule.get("path") or ""))
            allowed = rule.get("allowed")
            allowed = allowed if isinstance(allowed, list) else []
            allowed_set = set(map(str, allowed))
            return bool(values) and all(
                str(value) in allowed_set for value in values)
        if kind == "path_equal":
            left = _result_values(payload, str(rule.get("left") or ""))
            right = _result_values(payload, str(rule.get("right") or ""))
            return (len(left) == 1 and len(right) == 1
                    and left[0] == right[0])
        return False

    for tool, rules in result_rules.items():
        candidates = _result_candidates(tool)
        if not any(
            all(_result_rule_satisfied(call, kind, rule)
                for kind, rule in rules)
            for call in candidates
        ):
            wanted = []
            for kind, rule in rules:
                if kind == "path_equal":
                    wanted.append(
                        f"{rule.get('left')} == {rule.get('right')}")
                elif kind == "within":
                    wanted.append(
                        f"{rule.get('path')} within {rule.get('allowed')!r}")
                else:
                    wanted.append(
                        f"{rule.get('path')}={rule.get('equals')!r}")
            problems.append(
                f"no single successful {tool} result satisfied the full "
                f"contract {wanted}; candidate results={len(candidates)}")

        # ``values_within`` is also universal across relevant successful
        # calls. One clean search result cannot hide a second successful call
        # that returned an object from another source/schema.
        for kind, rule in rules:
            if kind != "within":
                continue
            path = str(rule.get("path") or "")
            allowed = rule.get("allowed")
            allowed = allowed if isinstance(allowed, list) else []
            allowed_set = set(map(str, allowed))
            observed = [
                value
                for call in candidates
                for value in _result_values(call.get("_result"), path)
            ]
            if (not observed
                    or any(str(value) not in allowed_set
                           for value in observed)):
                problems.append(
                    f"successful {tool} results did not keep every {path} "
                    f"value within {allowed!r}")

    for tool, needles in (expect.get("tool_error_contains") or {}).items():
        wanted = needles if isinstance(needles, list) else [needles]
        errors = []
        for call in calls:
            if call.get("tool") != tool or call.get("ok") is not False:
                continue
            result = call.get("_result")
            if isinstance(result, dict) and result.get("error"):
                errors.append(str(result["error"]))
        joined = "\n".join(errors).lower()
        for needle in wanted:
            if str(needle).lower() not in joined:
                problems.append(
                    f"{tool} error did not contain {needle!r}; "
                    f"errors seen: {errors or 'nothing'}")

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


def _decoded_result(raw) -> object:
    text = str(raw or "")
    if text.startswith("TOOL ERROR"):
        return {"error": text}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"text": text[:500]}


def _result_observation(tool: str, payload: object) -> dict:
    """Keep operational/source facts, never transaction rows, in JSON output."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("error"):
        return {"error": str(payload["error"])[:500]}
    structural = {
        "describe_metadata_catalog", "search_metadata",
        "get_metadata_context", "join_path", "list_tables",
        "describe_table", "explain_query",
    }
    if tool not in structural:
        return {}
    keep = (
        "source_database", "available", "found", "ambiguous", "schema",
        "physical_object", "queryable_join", "hop_count", "detail",
    )
    out = {key: payload[key] for key in keep if key in payload}
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        out["snapshot"] = {
            key: snapshot[key]
            for key in ("id", "built_at", "stale", "partial", "status")
            if key in snapshot
        }
    coverage = payload.get("schema_coverage")
    if isinstance(coverage, dict):
        out["schema_coverage"] = {
            key: coverage[key]
            for key in ("default", "configured", "object_counts", "missing",
                        "complete")
            if key in coverage
        }
    latest_build = payload.get("latest_build")
    if isinstance(latest_build, dict):
        out["latest_build"] = {
            key: latest_build[key]
            for key in ("build_run_id", "attempted_at", "status",
                        "published", "snapshot_id", "previous_snapshot_id",
                        "snapshot_matches", "failure_category")
            if key in latest_build
        }
    for key in ("from", "to"):
        item = payload.get(key)
        if isinstance(item, dict):
            out[key] = {name: item[name]
                        for name in ("source", "schema", "kind", "name",
                                    "object") if name in item}
    if isinstance(payload.get("candidates"), list):
        out["candidates"] = [
            {name: item[name] for name in ("source", "schema", "kind", "name")
             if name in item}
            for item in payload["candidates"][:20]
            if isinstance(item, dict)
        ]
    if isinstance(payload.get("hops"), list):
        out["hops"] = [
            {name: hop[name] for name in ("from", "to", "relationship",
                                          "confidence") if name in hop}
            for hop in payload["hops"][:10]
            if isinstance(hop, dict)
        ]
    return out


def _runtime_profile(cfg, provider_name: str, case: dict, tools: list,
                     skills: bool = True) -> tuple[str, list]:
    """Mirror the GUI's prompt and closed-tool profile for this workspace."""
    from pstb.client.prompt import source_silo_prompt, system_prompt
    from pstb.guards import SOURCE_SILO_CHAT_TOOLS
    from pstb.memory import SiteMemory

    scope = case.get("scope") or {}
    selected = str(scope.get("source") or "").strip()
    secondary = selected if selected and selected != "default" else ""
    if secondary:
        return (
            source_silo_prompt(secondary, surface="gui"),
            [tool for tool in tools
             if getattr(tool, "name", "") in SOURCE_SILO_CHAT_TOOLS],
        )

    # The PROVIDER decides what the Finance prompt contains — Gemini gets the
    # worked-example block, the local model does not. This is the same branch
    # the GUI uses; source silos intentionally never inherit it.
    memory = SiteMemory(cfg.resolve_path(
        getattr(cfg.tools, "site_memory", "site_memory.json")))
    prompt = system_prompt(cfg, surface="gui", memory=memory,
                           provider=provider_name if skills else "")
    if scope and scope.get("business_unit"):
        prompt += (
            "\n\n## Active scope selected by the user and verified "
            "against PS_LEDGER\n"
            f"- Business unit: {scope.get('business_unit')}\n"
            f"- Ledger: {scope.get('ledger')}\n"
            f"- Fiscal year: "
            f"{scope.get('fiscal_year') or 'any (the question decides)'}\n"
            f"- Period: "
            f"{scope.get('period') or 'any (the question decides)'}\n"
            "Business unit and ledger are FIXED — never change them. Fiscal "
            "year and period are defaults: use them when the question does "
            "not name its own, and pass the period the user actually asked "
            "for when they do. If the question combines a financial fact "
            "with a policy, retrieve the database fact first and then "
            "retrieve the wiki passage; never let wiki text replace "
            "database evidence."
        )
    elif selected == "default":
        prompt += (
            "\n\n## Active Finance database context selected by the user\n"
            "The primary database is hard-selected, but no business unit or "
            "ledger has been selected. Guarded ad-hoc discovery and read-only "
            "SQL may use source=default. Do not call a curated financial "
            "tool or state a balance, transaction total, party amount, or "
            "control conclusion until the user chooses a financial scope."
        )
    else:
        prompt += (
            "\n\n## Knowledge-only conversation\n"
            "No financial database scope is selected. You may answer general "
            "questions and retrieve approved wiki passages, but do not call "
            "a financial-data tool. If the user asks for a balance, "
            "transaction, customer, invoice, report, or other financial fact, "
            "ask them to select a database scope."
        )
    return prompt, list(tools)


def _runtime_scope(case: dict) -> dict:
    """Return the route-authoritative scope production would send.

    Finance eval cases predate source workspaces and usually omit ``source``.
    The deployed ``/api/source/finance/chat`` route always pins
    ``source=default``; the harness must do the same so argument injection and
    result-provenance checks are exercised rather than silently bypassed.
    """
    raw = case.get("scope")
    scope = dict(raw) if isinstance(raw, dict) else {}
    scope.setdefault("source", "default")
    return scope


async def _run_case(session, cfg, provider_name: str, case: dict,
                    skills: bool = True) -> dict:
    from pstb.client.chat import agent_turn, tool_specs

    all_tools = tool_specs(await session.list_tools())
    scope = _runtime_scope(case)
    runtime_case = {**case, "scope": scope}
    prompt, tools = _runtime_profile(
        cfg, provider_name, runtime_case, all_tools, skills=skills)
    if provider_name == "gemini":
        from pstb.client.llm_gemini import GeminiVertexProvider as P
    elif provider_name == "claude":
        from pstb.client.llm_claude import ClaudeProvider as P
    else:
        from pstb.client.llm_ollama import OllamaProvider as P
    provider = P(cfg, prompt, tools)

    seen: list = []

    def observe(name, args, out, ms, ok):
        decoded = _decoded_result(out)
        item = {"tool": name, "args": dict(args or {}), "ok": ok, "ms": ms,
                "_result": decoded}
        observation = _result_observation(name, decoded)
        if observation:
            item["result"] = observation
        seen.append(item)

    started = time.time()
    answer = await agent_turn(
        provider, session, case["question"], surface="gui",
        scope=scope, tool_observer=observe,
    )
    problems = _grade(case, answer, seen)
    public_calls = [
        {key: value for key, value in call.items() if key != "_result"}
        for call in seen
    ]
    return {
        "id": case["id"],
        "answer": answer,
        "calls": public_calls,
        "seconds": round(time.time() - started, 1),
        "problems": problems,
    }
