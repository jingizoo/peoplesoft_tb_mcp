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
_SAFE_OBJECT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


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


def _case_source(case: dict) -> str:
    scope = case.get("scope")
    if not isinstance(scope, dict):
        return ""
    return str(scope.get("source") or "").strip()


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

    def _result_values(payload, path: str) -> list:
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
