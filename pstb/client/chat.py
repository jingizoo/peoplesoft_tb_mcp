"""Terminal chat client: connects to the TB MCP server over stdio, exposes its
tools to the chosen LLM (Ollama, Gemini on Vertex AI, or Claude on the
Anthropic API), and runs the agent loop.

Usage:
    python -m pstb.client.chat                       # REPL, provider from config
    python -m pstb.client.chat --provider gemini
    python -m pstb.client.chat --provider claude
    python -m pstb.client.chat --ask "Does the trial balance balance for period 6?"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Config, load_config
from ..guards import (
    FINANCIAL_EVIDENCE_TOOLS,
    POLICY_EVIDENCE_TOOLS,
    ScopeConflict,
    apply_request_scope,
    attribution_caveat,
    evidence_intent,
    misattributed_figures,
    wants_all_business_units,
    financial_tool_domains,
    is_policy_tool,
    normalize_request_scope,
    promises_tool_call,
    spans_business_units,
    units_named_in,
    question_financial_domains,
    filter_scope_payload,
    tool_result_status,
    unit_access_block,
    rate_caveat,
    rate_findings,
    ungrounded_figures,
    unevidenced_verdict,
)
from ..qlog import QuestionLog
from .llm_base import (
    PROVIDERS,
    LLMProvider,
    LLMResponse,
    ToolResult,
    ToolSpec,
)
from .prompt import system_prompt

MAX_TOOL_ROUNDS = 10
MAX_NUDGES = 2
MAX_TOOL_RESULT_CHARS = 24_000  # mutable via set_tool_result_limit()

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


# Providers whose context window is measured in hundreds of thousands of
# tokens, so a whole tool result fits and truncating one only loses rows
# the answer might need.
WIDE_WINDOW_PROVIDERS = frozenset({"gemini", "claude"})


def tool_result_limit(cfg: Config, provider_name: str) -> int:
    """Size tool results to the model. Local 8B models drown past ~24k chars;
    Gemini 2.5 Pro and Claude Opus 5 both have 1M-token windows and chain
    better when they see whole results instead of truncated rows."""
    explicit = int(getattr(cfg.llm, "max_tool_result_chars", 0) or 0)
    if explicit:
        return explicit
    return 120_000 if provider_name in WIDE_WINDOW_PROVIDERS else 24_000


def set_tool_result_limit(cfg: Config, provider_name: str) -> None:
    """Set the PROCESS default (terminal client, tests). The GUI passes a
    per-turn limit instead: mutating a module global from a request handler
    let one browser session's provider decide another session's truncation,
    and a 120k limit applied to a local model is a context overflow."""
    global MAX_TOOL_RESULT_CHARS
    MAX_TOOL_RESULT_CHARS = tool_result_limit(cfg, provider_name)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_provider(name: str, cfg: Config, tools: list[ToolSpec]) -> LLMProvider:
    from ..memory import SiteMemory
    prompt = system_prompt(cfg, memory=SiteMemory(cfg.resolve_path(
        getattr(cfg.tools, "site_memory", "site_memory.json"))),
        provider=name)
    if name == "gemini":
        from .llm_gemini import GeminiVertexProvider

        return GeminiVertexProvider(cfg, prompt, tools)
    if name == "claude":
        from .llm_claude import ClaudeProvider

        return ClaudeProvider(cfg, prompt, tools)
    if name == "ollama":
        from .llm_ollama import OllamaProvider

        return OllamaProvider(cfg, prompt, tools)
    raise SystemExit(
        f"Unknown provider {name!r} — use 'ollama', 'gemini' or 'claude'")


def _field(obj, *names, default=None):
    """Read the first attribute present. MCP 1.x uses camelCase (inputSchema,
    isError, structuredContent); 2.x renamed them to snake_case."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def _lean_schema(node):
    """Strip schema keys the model cannot act on.

    FastMCP derives a "title" for every parameter — "title": "Business Unit"
    sitting beside the key `business_unit`. It restates the name with a space
    in it and nothing reads it. Across 70 tools that is 7,412 characters,
    about 1,850 tokens, of a fixed prompt that has to fit beside the tool
    results. Measured: 37% of all schema bytes.
    """
    if isinstance(node, dict):
        return {k: _lean_schema(v) for k, v in node.items()
                if k not in ("title", "$schema")}
    if isinstance(node, list):
        return [_lean_schema(v) for v in node]
    return node


def tool_specs(listed) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=t.name,
            description=t.description or "",
            schema=_lean_schema(
                _field(t, "input_schema", "inputSchema", default={}) or {}),
        )
        for t in listed.tools
    ]


def _truncate_json(text: str, limit: int) -> str:
    """Trim an oversized tool result without cutting JSON mid-object: drop whole
    rows from the 'rows' list and record how many were dropped."""
    if len(text) <= limit:
        return text
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:limit] + "\n...[truncated]"
    # Trim whichever top-level list dominates the payload ('rows' for
    # run_sql, 'customers'/'items' for aging, 'stuck_invoices' for billing...).
    # Trimming only 'rows' meant AR payloads fell through to a raw character
    # cut that produced invalid JSON and severed totals/gl_tie — the model
    # then summed visible customers itself and fabricated a total.
    list_key, rows = None, None
    if isinstance(payload, dict):
        best = 0
        for k, v in payload.items():
            if isinstance(v, list) and v:
                size = len(json.dumps(v, default=str))
                if size > best:
                    best, list_key, rows = size, k, v
    if not isinstance(rows, list) or not rows:
        return text[:limit] + "\n...[truncated]"
    kept = rows
    while kept and len(json.dumps({**payload, list_key: kept}, default=str)) > limit:
        kept = kept[: max(1, len(kept) * 3 // 4)]
        if len(kept) == 1:
            break
    payload[list_key] = kept
    payload["rows_omitted_for_context"] = len(rows) - len(kept)
    payload["note_truncation"] = (
        f"{len(rows) - len(kept)} '{list_key}' entr(ies) withheld to fit context; "
        "totals above cover the full result set."
    )
    out = json.dumps(payload, default=str)
    return out if len(out) <= limit else out[:limit] + "\n...[truncated]"


_ID_ARGS = ("cust_id", "customer", "customer_id", "vendor_id", "account",
            "invoice", "invoice_id")


def _observed_next_steps(name: str, parsed: dict, question: str,
                         unit: str, seen: set, already=()) -> list:
    """Turn this result's own figures into machine-visible next steps.

    The suggestion rules already read tool payloads and produce a finding
    plus the tool that answers it — but only the human saw them, as chips
    under the answer. So the model would report an aging and stop, while
    the machinery beside it had already worked out that one customer's
    cash was sitting unapplied and which tool would show it.

    These are pointers, not findings: every figure in them is quoted from
    the payload they came out of, so they add no new number to ground.
    Capped at three, deduplicated across the turn, and skipped entirely on
    any failure — a next step is never worth an answer.
    """
    try:
        from ..suggest import _subject, suggestions_for
        out = []
        for s in suggestions_for([(name, parsed)], question=question,
                                 business_unit=unit):
            if s["question"] in seen:
                continue
            # suggest.py suppresses "you already ran that" within a single
            # payload; it cannot see the OTHER calls of the same turn. So a
            # turn that ran the 360 for C1004 and an aging got told, by the
            # aging, to go and run the 360 for C1004.
            if (s["answered_by"], _subject(s["question"])) in already:
                continue
            seen.add(s["question"])
            out.append({"finding": s["because"], "ask": s["question"],
                        "tool": s["answered_by"]})
            if len(out) >= 3:
                break
        return out
    except Exception:                       # noqa: BLE001
        return []


def _compact_args(args: dict, limit: int = 90) -> str:
    """Readable one-line tool arguments for the trail.

    Empty strings, zeros and False are the tool DEFAULTS — printing them
    ("as_of_date":"", "customer_id":"", "detail":false ...) buried the two
    values that actually mattered and wrapped the terminal. Show only what
    was really set.
    """
    parts = []
    for key, value in (args or {}).items():
        if value in ("", 0, None, False, [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(
            value, default=str)
        if len(text) > 40:
            text = text[:40] + "…"
        parts.append(f"{key}={text}")
    line = ", ".join(parts)
    if len(line) > limit:
        line = line[:limit] + "…"
    return line


async def call_mcp_tool(session: ClientSession, name: str, args: dict,
                        limit: int = 0) -> str:
    try:
        res = await session.call_tool(name, arguments=args)
        chunks = [c.text for c in res.content if getattr(c, "text", None)]
        if chunks:
            text = "\n".join(chunks)
        else:
            structured = _field(res, "structured_content", "structuredContent", default={})
            text = json.dumps(structured or {}, default=str)
        if _field(res, "is_error", "isError", default=False):
            text = f"TOOL ERROR: {text}"
    except Exception as e:
        text = f"TOOL ERROR: {type(e).__name__}: {e}"
    return _truncate_json(text, limit or MAX_TOOL_RESULT_CHARS)


class ResultRefError(RuntimeError):
    pass


def _resolve_path(payload, path: str) -> list:
    """Follow a dot path ("accounts", "rows[].account") into a payload and
    return the scalar values it reaches."""
    current = [payload]
    for segment in (path or "").split("."):
        if not segment:
            raise ResultRefError(f"empty segment in field path {path!r}")
        is_list = segment.endswith("[]")
        key = segment[:-2] if is_list else segment
        nxt = []
        for node in current:
            if isinstance(node, dict) and key in node:
                value = node[key]
                if is_list:
                    if not isinstance(value, list):
                        raise ResultRefError(
                            f"{key!r} is not a list in this result")
                    nxt.extend(value)
                else:
                    nxt.append(value)
        current = nxt
    out = []
    for value in current:
        if isinstance(value, list):
            out.extend(v for v in value
                       if isinstance(v, (str, int, float))
                       and not isinstance(v, bool))
        elif isinstance(value, (str, int, float)) \
                and not isinstance(value, bool):
            out.append(value)
    return out


def resolve_result_refs(args, turn_results: dict):
    """Replace {"from_result": "rN", "field": "..."} with the actual values.

    This is how a chain hands values from one tool to the next WITHOUT the
    model retyping them. The model wires references — "the accounts from
    r2" — and the client substitutes the real list straight out of the
    stored result, the same way the request scope is injected. Forty
    accounts arrive as forty accounts, not thirty-nine and a transposition;
    the number guard protects answers, and this protects arguments.
    """
    if isinstance(args, dict):
        if "from_result" in args:
            rid = str(args.get("from_result") or "")
            if rid not in turn_results:
                have = ", ".join(sorted(turn_results)) or "none yet"
                raise ResultRefError(
                    f"result {rid!r} does not exist (available: {have}). "
                    "Produce the set in one round, then reference it in the "
                    "NEXT round — results in the same batch have no id yet.")
            field = str(args.get("field") or "")
            values = _resolve_path(turn_results[rid], field)
            if not values:
                keys = ", ".join(sorted(turn_results[rid])[:12])
                raise ResultRefError(
                    f"field {field!r} reached no values in {rid} "
                    f"(top-level keys: {keys}). Fix the path — e.g. "
                    "'accounts' or 'rows[].account'.")
            return values
        return {k: resolve_result_refs(v, turn_results)
                for k, v in args.items()}
    if isinstance(args, list):
        return [resolve_result_refs(v, turn_results) for v in args]
    return args


def _blocked_result(reason: str, next_step: str) -> str:
    """Provider-neutral function response for a call stopped by a guard."""
    return json.dumps({
        "error": reason,
        "evidence_gate": "blocked",
        "next_step": next_step,
    })


def _evidence_nudge(intent: str, db_ok: bool, relevant_financial_db_ok: bool,
                    policy_ok: bool, policy_attempted: bool = False,
                    db_problem: str = "", user_question: str = "") -> str:
    """Return the next required evidence phase, or an empty string."""
    if intent == "data" and not db_ok:
        detail = f" The previous database result failed: {db_problem}" if db_problem else ""
        return (
            "Evidence gate: answer this data question from PeopleSoft, not the "
            f"wiki. Call the most specific database tool now.{detail}"
        )
    if intent == "mixed" and not relevant_financial_db_ok:
        detail = f" The previous database result failed: {db_problem}" if db_problem else ""
        return (
            "Evidence gate: this question combines a financial fact with a "
            "policy decision. Call the relevant PeopleSoft financial tool now. "
            f"Do not call the wiki until that result succeeds.{detail}"
        )
    if intent in ("policy", "mixed") and not policy_ok and not policy_attempted:
        return (
            "Evidence gate: retrieve the actual policy passage with wiki_lookup "
            f"now using the user's exact question: {user_question!r}. "
            "Do not search for generic phrases such as 'evidence gate policy', "
            "and do not answer from memory, wiki titles, or links."
        )
    return ""


async def agent_turn(provider: LLMProvider, session: ClientSession,
                     user_text: str, qlog=None, surface: str = "terminal",
                     scope: dict | None = None,
                     tool_observer: Callable | None = None,
                     tool_started: Callable | None = None,
                     prior_payloads: list | None = None,
                     result_limit: int = 0,
                     access=None,
                     allow_raw_sql: bool = True,
                     known_units=()) -> str:
    """Run one model turn with deterministic source ordering.

    ``scope`` is an optional, user-validated request scope. Concrete values are
    injected into financial tools that support them; a model attempt to change
    one is rejected before reaching MCP. Scope discovery itself is never
    constrained, so ``list_financial_scopes`` can still answer "all BUs".
    """
    request_scope = normalize_request_scope(scope)
    intent = evidence_intent(user_text)
    # A question that NAMES two units crosses them just as surely as one
    # that says "across all business units" — and that shape was invisible
    # here, so the scope lock pinned the chip's unit and the model answered
    # about one of the two. Naming a unit that is not the selected one
    # counts too: the user's words are the newer instruction.
    selected_unit = str((request_scope or {}).get("business_unit") or "")
    crosses_units = spans_business_units(user_text, known_units, selected_unit)
    bu_override = crosses_units
    turn_results: dict = {}
    # One pointer per turn, not one per tool that noticed the same
    # thing: aging and the 360 both spot an unapplied deposit.
    observed_seen: set = set()
    # (tool, id) this turn already ran, so a pointer never sends the model
    # back for something it has.
    observed_asked: set = set()
    required_financial_domains = question_financial_domains(user_text)
    # Tell the provider whether this question should open with a tool call.
    # The Gemini provider forces function-calling mode ANY on that first
    # turn (greedy-decoded), which makes "answered from memory without
    # looking" structurally impossible for tool-needing questions. Other
    # providers simply carry the attribute.
    provider.expect_tool_call = intent != "general"
    financial_fact_required = (
        intent == "data" and bool(required_financial_domains)
    )
    # Tell the model the shape of the question BEFORE it routes, not after
    # a wasted round. Only the curated ranking tool takes business_unit=ALL,
    # so a genuine crossing is one grouped query — and firing a single-unit
    # tool first is exactly what this stops.
    asked = user_text
    if crosses_units and intent != "general":
        named = units_named_in(user_text, known_units)
        which = (", ".join(named) if len(named) > 1
                 else "every business unit the question covers")
        asked = (
            f"{user_text}\n\n[Routing note: this question spans business "
            f"units ({which}). The selected scope does NOT pin it. Answer it "
            "with ONE call that returns every unit together — a grouped "
            "run_sql with BUSINESS_UNIT in the SELECT and GROUP BY, or "
            "business_unit=\"ALL\" on a curated tool that accepts it. Do "
            "not call a single-unit tool once per unit, and do not answer "
            "for one unit only.]")
    resp: LLMResponse = await asyncio.to_thread(provider.send_user, asked)
    logged_calls: list[dict] = []
    rounds = 0
    hit_limit = False
    nudges = 0
    sql_remedy_pending = False
    db_ok = False
    covered_financial_domains: set[str] = set()
    policy_ok = False
    policy_attempted = False
    scope_blocked = False
    last_db_problem = ""
    last_policy_problem = ""
    turn_payloads: list = []

    def has_relevant_financial_evidence() -> bool:
        if required_financial_domains:
            return required_financial_domains.issubset(
                covered_financial_domains
            )
        # An anaphoric mixed question ("Is that within policy?") must re-query
        # at least one curated financial source. Do not rely on stale model
        # history to supply the data side of a verdict.
        return bool(covered_financial_domains)

    for _ in range(MAX_TOOL_ROUNDS):
        if not resp.tool_calls:
            relevant_financial_db_ok = has_relevant_financial_evidence()
            data_ok = (
                relevant_financial_db_ok if financial_fact_required else db_ok
            )
            # A failed run_sql whose error LISTED the real columns is a
            # solved problem the model abandoned: retrying with those
            # columns is one round, wandering to a different tool rarely
            # answers the question that needed ad-hoc SQL. It outranks the
            # generic evidence nudge and shares its MAX_NUDGES bound.
            if nudges < MAX_NUDGES and sql_remedy_pending:
                nudges += 1
                sql_remedy_pending = False
                resp = await asyncio.to_thread(
                    provider.send_user,
                    "Your run_sql failed, but the error message listed the "
                    "REAL columns of every table you referenced. Rewrite the "
                    "query using only those columns and call run_sql again "
                    "now — do not switch to a different tool.",
                )
                continue
            needed = _evidence_nudge(
                intent,
                data_ok,
                relevant_financial_db_ok,
                policy_ok,
                policy_attempted,
                last_db_problem,
                user_text,
            )
            if nudges < MAX_NUDGES and needed:
                nudges += 1
                resp = await asyncio.to_thread(provider.send_user, needed)
                continue
            # Promised a tool call but made none — continue rather than
            # handing the user an unfulfilled intention.
            if nudges < MAX_NUDGES and promises_tool_call(resp.text):
                nudges += 1
                resp = await asyncio.to_thread(
                    provider.send_user,
                    "You stated you would call a tool but did not. Issue that "
                    "tool call now, then answer. Do not describe what you will "
                    "do — do it.",
                )
                continue
            break
        rounds += 1
        if resp.text.strip():
            print(f"{DIM}{resp.text.strip()}{RESET}")

        # A model may emit DB and wiki calls in one batch, and each call is
        # dead time — an Oracle query and a Confluence fetch have no reason
        # to wait for one another. Calls run CONCURRENTLY in two phases:
        # database tools first, wiki tools after, because the mixed-intent
        # gate must decide the wiki's fate from the database's OUTCOME
        # ("wiki only after financial evidence succeeds"), which does not
        # exist until the first phase completes. Within a phase everything
        # overlaps; bookkeeping then runs sequentially in the model's
        # original call order, so observers, logs and transcripts read
        # exactly as they did when execution was serial.
        indexed_calls = list(enumerate(resp.tool_calls))
        results_by_index: dict[int, ToolResult] = {}

        async def run_call(index: int, call) -> tuple:
            """Decide blocked-or-not, then execute. No shared-state writes:
            everything the sequential loop mutated is returned and applied
            by the bookkeeping pass, so concurrent calls cannot race."""
            effective_args = dict(call.args or {})
            blocked = ""
            next_step = ""
            was_scope_block = False
            # Row security first: a unit this person was never granted is
            # refused before the scope lock even looks at it, because the
            # scope lock's question ("does this match what you selected")
            # has no opinion about units you could never select.
            denied = unit_access_block(call.name, effective_args, access,
                                       allow_raw_sql=allow_raw_sql)
            if denied:
                blocked = denied
                next_step = (
                    "Ask about a business unit this user is authorised for, "
                    "or ask the PeopleSoft security administrator for access.")
            elif (
                surface == "gui"
                and not request_scope
                and not is_policy_tool(call.name)
                and call.name != "list_financial_scopes"
            ):
                blocked = (
                    f"{call.name} requires a user-selected PeopleSoft scope "
                    "in the GUI"
                )
                next_step = (
                    "Ask the user to choose a business unit and ledger before "
                    "calling a financial or database tool."
                )
                was_scope_block = True
            elif intent == "data" and is_policy_tool(call.name):
                blocked = (
                    f"{call.name} is not allowed for a data-only question; "
                    "wiki text cannot substitute for PeopleSoft data"
                )
                next_step = "Call the relevant PeopleSoft database tool."
            elif (
                intent == "mixed"
                and is_policy_tool(call.name)
                and not has_relevant_financial_evidence()
            ):
                blocked = (
                    f"{call.name} is blocked until a PeopleSoft financial tool "
                    "returns successful evidence"
                )
                next_step = (
                    "Call the relevant PeopleSoft financial tool; retry the "
                    "wiki only after it succeeds."
                )
            if not blocked:
                try:
                    effective_args = apply_request_scope(
                        call.name, effective_args, request_scope,
                        allow_bu_override=bu_override,
                    )
                except ScopeConflict as e:
                    blocked = f"REQUEST_SCOPE_CONFLICT: {e}"
                    next_step = (
                        "Use the request scope exactly, or ask the user to "
                        "change the scope before retrying."
                    )
            if not blocked:
                try:
                    effective_args = resolve_result_refs(
                        effective_args, turn_results)
                except ResultRefError as e:
                    blocked = f"RESULT_REF: {e}"
                    next_step = (
                        "Correct the from_result reference and call again."
                    )
            # RAG relevance is a server-side invariant, not a model preference.
            # The model may paraphrase a nudge as a useless query such as
            # "evidence gate policy"; always retrieve passages for what the
            # user actually asked. Other lookup controls (page/passages limits)
            # remain intact.
            if (
                not blocked
                and call.name == "wiki_lookup"
                and intent in {"policy", "mixed"}
            ):
                effective_args = {
                    key: value
                    for key, value in effective_args.items()
                    if key in {"question", "max_pages", "max_passages"}
                }
                effective_args["question"] = user_text

            arg_preview = _compact_args(effective_args)
            marker = "⊘" if blocked else "⚙"
            print(f"{DIM}  {marker} {call.name}({arg_preview}){RESET}")
            if tool_started is not None:
                # Fires when the call is DISPATCHED, not when it returns —
                # the difference between a UI that says "working…" and one
                # that says "running get_trial_balance, 40s so far".
                try:
                    tool_started(call.name, arg_preview, bool(blocked))
                except Exception:
                    pass

            started = time.perf_counter()
            out = (
                _blocked_result(blocked, next_step)
                if blocked
                else await call_mcp_tool(session, call.name, effective_args,
                                        limit=result_limit)
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            # The catalog comes back from a server that does not know who
            # asked; narrow it here, where we do.
            out = filter_scope_payload(call.name, out, access)
            return (index, call, effective_args, blocked, out, elapsed_ms,
                    was_scope_block)

        db_phase = [(i, c) for i, c in indexed_calls
                    if not is_policy_tool(c.name)]
        wiki_phase = [(i, c) for i, c in indexed_calls
                      if is_policy_tool(c.name)]
        for phase in (db_phase, wiki_phase):
            if not phase:
                continue
            outcomes = await asyncio.gather(
                *(run_call(i, c) for i, c in phase))
            # What this batch already answered, before any of it is drained
            # — the first result must not be told to go and fetch the
            # fourth, which the model requested in the same breath.
            for _o in outcomes:
                for _k in _ID_ARGS:
                    _v = (_o[2] or {}).get(_k)
                    if isinstance(_v, str) and _v.strip():
                        observed_asked.add((_o[1].name, _v.strip()))
            for (index, call, effective_args, blocked, out, elapsed_ms,
                 was_scope_block) in sorted(outcomes, key=lambda o: o[0]):
                if was_scope_block:
                    scope_blocked = True
                ok, problem = tool_result_status(call.name, out)
                if tool_observer is not None:
                    tool_observer(
                        call.name, dict(effective_args), out, elapsed_ms, ok
                    )
                if is_policy_tool(call.name):
                    if call.name in POLICY_EVIDENCE_TOOLS:
                        policy_attempted = True
                        if ok:
                            policy_ok = True
                        else:
                            last_policy_problem = (problem or blocked
                                                   or "wiki lookup failed")
                else:
                    if ok:
                        db_ok = True
                        if call.name == "run_sql":
                            sql_remedy_pending = False
                        if call.name in FINANCIAL_EVIDENCE_TOOLS:
                            covered = financial_tool_domains(call.name)
                            if call.name == "run_sql":
                                # Ad-hoc SQL has no fixed domain: a successful
                                # SELECT the user's question routed to IS the
                                # financial evidence for that question. Without
                                # this, correct run_sql answers were replaced by
                                # a false "could not obtain a PeopleSoft result".
                                covered = required_financial_domains or {"adhoc"}
                            covered_financial_domains.update(covered)
                    else:
                        last_db_problem = (problem or blocked
                                           or "database tool failed")
                        if (call.name == "run_sql"
                                and "has columns:" in
                                str(problem or blocked or out or "")):
                            sql_remedy_pending = True
                logged_calls.append({
                    "tool": call.name,
                    "ok": ok,
                    **({"error": (problem or blocked or out)[:240]}
                       if not ok else {}),
                })
                if ok:
                    # Successful results get an id (r1, r2, ...) the model
                    # can REFERENCE in later rounds instead of retyping the
                    # values — see resolve_result_refs.
                    try:
                        parsed = json.loads(out)
                        if isinstance(parsed, dict):
                            rid = f"r{len(turn_results) + 1}"
                            turn_results[rid] = parsed
                            parsed["result_id"] = rid
                            steps = _observed_next_steps(
                                call.name, parsed, user_text,
                                str(effective_args.get("business_unit") or ""),
                                observed_seen, observed_asked)
                            if steps:
                                parsed["observed_next_steps"] = steps
                                parsed["observed_next_steps_note"] = (
                                    "Computed from the figures in THIS "
                                    "result. Pointers, not new facts — do "
                                    "not restate a finding as if a tool "
                                    "reported it separately. Follow one "
                                    "only if it answers what was asked.")
                            out = json.dumps(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    # Tagged with the producing tool: the number
                    # guard asks whether a figure exists, and the
                    # attribution guard asks which system said so.
                    turn_payloads.append((call.name, out))
                results_by_index[index] = ToolResult(
                    call_id=call.id, name=call.name, content=out
                )
        results = [results_by_index[i] for i in range(len(resp.tool_calls))]
        resp = await asyncio.to_thread(provider.send_tool_results, results)
    else:
        hit_limit = True
    answer = resp.text or ("(stopped: too many tool rounds)" if hit_limit
                           else "(no response)")

    relevant_financial_db_ok = has_relevant_financial_evidence()
    gate_replaced_answer = False
    if scope_blocked:
        answer = (
            "Choose a PeopleSoft business unit and ledger before I query "
            "financial data. No configured default scope was used."
        )
        gate_replaced_answer = True
    elif intent == "mixed" and not relevant_financial_db_ok:
        answer = (
            "I could not obtain successful PeopleSoft financial evidence, so "
            "I cannot decide this question. The wiki was not used as a "
            "substitute for the missing financial result."
            + (f" Database detail: {last_db_problem}" if last_db_problem else "")
        )
        gate_replaced_answer = True
    elif intent == "mixed" and not policy_ok:
        answer = (
            "The PeopleSoft data was retrieved, but no verified wiki passage "
            "was available, so I cannot decide whether the result satisfies "
            "the requested rule."
            + (f" Wiki detail: {last_policy_problem}"
               if last_policy_problem else "")
        )
        gate_replaced_answer = True
    elif intent == "data" and not (
        relevant_financial_db_ok if financial_fact_required else db_ok
    ):
        answer = (
            "I could not obtain a successful PeopleSoft result for this "
            "question. No wiki content was used in its place."
        )
        gate_replaced_answer = True
    elif intent == "policy" and not policy_ok:
        answer = (
            "I could not retrieve a verified policy passage from the wiki, so "
            "I cannot answer this policy question from memory."
            + (f" Wiki detail: {last_policy_problem}"
               if last_policy_problem else "")
        )
        gate_replaced_answer = True

    # MECHANICAL number grounding. The prompt forbids inventing figures and
    # the verdict guard catches unevidenced judgements, but neither makes a
    # fabricated amount IMPOSSIBLE to state. Every figure in the answer must
    # appear in a tool payload from this turn; if one does not, the answer is
    # refused rather than shown. Numbers have been fabricated by 8B models in
    # this exact product ($1,234,567.89 alongside balanced=true), so this is
    # the difference between "told not to" and "cannot".
    if not gate_replaced_answer and turn_payloads:
        # Ground against RECENT turns too. A follow-up legitimately restates
        # a figure the conversation already fetched — the model can see that
        # prior tool result in its own history — and withholding
        # 21,334,221.84 as "invented" because it came from the PREVIOUS
        # turn taught a user that nothing works. Prior payloads ground
        # figures only; they never satisfy this turn's evidence gates.
        invented = ungrounded_figures(
            answer, list(turn_payloads) + list(prior_payloads or []))
        if invented:
            answer = (
                "I withheld that answer: it stated "
                + ", ".join(invented[:3])
                + (" and others" if len(invented) > 3 else "")
                + ", which does not appear in any tool result from this turn. "
                "Every figure must come from the database. Ask again and I "
                "will requery rather than restate."
            )
            gate_replaced_answer = True
            logged_calls.append({"tool": "_number_guard", "ok": False,
                                 "error": "ungrounded figures: "
                                          + ", ".join(invented[:5])})

    # A compliance verdict needs a rule AND a figure. If one side is missing,
    # say so rather than letting a half-grounded judgement stand.
    used = {c["tool"] for c in logged_calls if c["ok"]}
    missing = "" if gate_replaced_answer else unevidenced_verdict(answer, used)
    if missing:
        if True:
            answer += (
                f"\n\n[unverified verdict: this turn never retrieved {missing}, "
                "so the compliance judgement above is not fully evidenced. Ask "
                "again for both the rule and the balance.]"
            )
            logged_calls.append({"tool": "_verdict_guard", "ok": False,
                                 "error": f"verdict without {missing}"})

    # Rates get a caveat, never a withhold. A percentage passes the figure
    # guard untouched (see guards._FIGURE_EXEMPT), so "the standard rate is
    # 18%" — recalled from training data and presented as this company's
    # configured rate — has been reaching users with nothing objecting.
    # Grounding here is declared-only: a tool said "this is a percent", or
    # the user typed it. Nothing is derived by dividing payload numbers.
    #
    # Deliberately NOT recorded in logged_calls: qlog.log_turn flags a turn
    # as FAILED when any call carries ok=False, and this clause is designed
    # to fire routinely on healthy turns. Marking them failures would
    # poison the question-log learning loop that decides what to build
    # next. The clause is visible in the answer text, which is the record.
    if not gate_replaced_answer:
        caveat = rate_caveat(
            rate_findings(answer,
                          list(turn_payloads) + list(prior_payloads or []),
                          user_text))
        if caveat:
            answer += "\n\n" + caveat

    # Every figure above is already proven to exist in a tool result. This
    # asks the next question: WHICH tool. A Coupa commitment quoted as a
    # ledger balance is grounded and wrong, and a balance typed into a
    # Confluence page anyone can edit is grounded and worse — wiki passages
    # are tool payloads like any other.
    #
    # A caveat, never a withhold, and not recorded in logged_calls, for the
    # same two reasons the rate caveat is not: reading prose to decide what
    # a sentence claimed is arguable, and a healthy turn must not be logged
    # as a failure.
    if not gate_replaced_answer:
        attribution = attribution_caveat(misattributed_figures(
            answer, list(turn_payloads) + list(prior_payloads or []),
            intent))
        if attribution:
            answer += "\n\n" + attribution

    if qlog is not None:
        turn_id = qlog.log_turn(surface=surface, provider=provider.name,
                                question=user_text, calls=logged_calls,
                                rounds=rounds, answer=answer,
                                hit_round_limit=hit_limit)
        agent_turn.last_turn_id = turn_id  # for the GUI feedback button
    return answer


async def run(args: argparse.Namespace) -> int:
    cfg_path = args.config or os.environ.get("PSTB_CONFIG") or str(_repo_root() / "config.yaml")
    cfg = load_config(cfg_path)
    provider_name = (args.provider or cfg.llm.provider).lower()
    if args.model:
        setattr(cfg.llm, {"ollama": "ollama_model", "claude": "claude_model"}
                .get(provider_name, "gemini_model"), args.model)

    env = dict(os.environ)
    env["PSTB_CONFIG"] = str(Path(cfg_path).resolve())
    env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env.get("PYTHONPATH", "")
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "pstb.server"], env=env
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = tool_specs(await session.list_tools())
            set_tool_result_limit(cfg, provider_name)
            try:
                provider = build_provider(provider_name, cfg, tools)
            except (RuntimeError, SystemExit) as e:
                # Setup problems (missing project, no credentials, package not
                # installed) are user-fixable; show the fix, not a traceback.
                print(f"\nCannot start the {provider_name} provider:\n  {e}\n", file=sys.stderr)
                if provider_name == "gemini":
                    print(
                        "Gemini on Vertex AI needs, in order:\n"
                        "  1. gcloud CLI installed  (macOS: brew install --cask google-cloud-sdk)\n"
                        "  2. gcloud auth application-default login\n"
                        "  3. GOOGLE_CLOUD_PROJECT=<project-id> in .env\n"
                        "  4. Vertex AI API enabled on that project\n"
                        "See docs/SETUP.md for the full walkthrough.",
                        file=sys.stderr,
                    )
                # Return rather than raise: an exception here would surface as an
                # anyio ExceptionGroup traceback and bury the message above.
                return 1
            qlog = QuestionLog(getattr(cfg.tools, "question_log", ""), cfg.root)
            banner = (
                f"{BOLD}PeopleSoft TB agent{RESET} — {provider.name}:{provider.model} | "
                f"{len(tools)} tools | BU {cfg.defaults.business_unit}, ledger {cfg.defaults.ledger}"
            )
            print(banner)

            if args.show_tools:
                for t in tools:
                    first = (t.description or "").strip().splitlines()[0] if t.description else ""
                    print(f"  - {t.name}: {first}")

            if args.ask:
                print(f"\n{BOLD}you>{RESET} {args.ask}")
                answer = await agent_turn(provider, session, args.ask,
                                          qlog=qlog, surface="terminal")
                print(f"\n{answer}")
                return 0

            print("Type a question "
                  "( /tools /reset /provider ollama|gemini|claude /quit )")
            while True:
                try:
                    q = (await asyncio.to_thread(input, f"\n{BOLD}you>{RESET} ")).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not q:
                    continue
                if q in ("/quit", "/exit", "/q"):
                    break
                if q == "/reset":
                    provider.reset()
                    print("(history cleared)")
                    continue
                if q == "/tools":
                    for t in tools:
                        print(f"  - {t.name}")
                    continue
                if q.startswith("/provider"):
                    parts = q.split()
                    if len(parts) == 2 and parts[1] in PROVIDERS:
                        try:
                            provider = build_provider(parts[1], cfg, tools)
                            # Re-size tool results for the NEW provider: an 8B
                            # local model drowns past ~24k chars, and Gemini
                            # or Claude can take 120k — keeping the old limit
                            # gives the wrong behavior in both directions.
                            set_tool_result_limit(cfg, parts[1])
                            print(f"(switched to {provider.name}:{provider.model} — history reset)")
                        except (RuntimeError, SystemExit) as e:
                            print(f"(cannot switch: {e})")
                    else:
                        print(f"usage: /provider {'|'.join(PROVIDERS)}")
                    continue
                try:
                    answer = await agent_turn(provider, session, q,
                                              qlog=qlog, surface="terminal")
                except RuntimeError as e:
                    print(f"\n[provider error] {e}")
                    continue
                print(f"\n{answer}")
            return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="PeopleSoft TB chat agent")
    ap.add_argument("--provider", choices=sorted(PROVIDERS),
                    help="override config.llm.provider")
    ap.add_argument("--model", help="override the model name for the chosen provider")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--ask", help="ask one question and exit")
    ap.add_argument("--show-tools", action="store_true", help="print tool list at startup")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args)) or 0)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
