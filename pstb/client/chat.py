"""Terminal chat client: connects to the TB MCP server over stdio, exposes its
tools to the chosen LLM (Ollama or Gemini on Vertex AI), and runs the agent loop.

Usage:
    python -m pstb.client.chat                       # REPL, provider from config
    python -m pstb.client.chat --provider gemini
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
    evidence_intent,
    financial_tool_domains,
    is_policy_tool,
    normalize_request_scope,
    promises_tool_call,
    question_financial_domains,
    tool_result_status,
    ungrounded_figures,
    unevidenced_verdict,
)
from ..qlog import QuestionLog
from .llm_base import LLMProvider, LLMResponse, ToolResult, ToolSpec
from .prompt import system_prompt

MAX_TOOL_ROUNDS = 10
MAX_NUDGES = 2
MAX_TOOL_RESULT_CHARS = 24_000  # mutable via set_tool_result_limit()

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def set_tool_result_limit(cfg: Config, provider_name: str) -> None:
    """Size tool results to the model. Local 8B models drown past ~24k chars;
    Gemini 2.5 Pro has a 1M-token window and chains better when it sees whole
    results instead of truncated rows."""
    global MAX_TOOL_RESULT_CHARS
    explicit = int(getattr(cfg.llm, "max_tool_result_chars", 0) or 0)
    MAX_TOOL_RESULT_CHARS = explicit or (120_000 if provider_name == "gemini" else 24_000)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_provider(name: str, cfg: Config, tools: list[ToolSpec]) -> LLMProvider:
    prompt = system_prompt(cfg)
    if name == "gemini":
        from .llm_gemini import GeminiVertexProvider

        return GeminiVertexProvider(cfg, prompt, tools)
    if name == "ollama":
        from .llm_ollama import OllamaProvider

        return OllamaProvider(cfg, prompt, tools)
    raise SystemExit(f"Unknown provider {name!r} — use 'ollama' or 'gemini'")


def _field(obj, *names, default=None):
    """Read the first attribute present. MCP 1.x uses camelCase (inputSchema,
    isError, structuredContent); 2.x renamed them to snake_case."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def tool_specs(listed) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=t.name,
            description=t.description or "",
            schema=_field(t, "input_schema", "inputSchema", default={}) or {},
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


async def call_mcp_tool(session: ClientSession, name: str, args: dict) -> str:
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
    return _truncate_json(text, MAX_TOOL_RESULT_CHARS)


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
                     tool_observer: Callable | None = None) -> str:
    """Run one model turn with deterministic source ordering.

    ``scope`` is an optional, user-validated request scope. Concrete values are
    injected into financial tools that support them; a model attempt to change
    one is rejected before reaching MCP. Scope discovery itself is never
    constrained, so ``list_financial_scopes`` can still answer "all BUs".
    """
    request_scope = normalize_request_scope(scope)
    intent = evidence_intent(user_text)
    required_financial_domains = question_financial_domains(user_text)
    financial_fact_required = (
        intent == "data" and bool(required_financial_domains)
    )
    resp: LLMResponse = await asyncio.to_thread(provider.send_user, user_text)
    logged_calls: list[dict] = []
    rounds = 0
    hit_limit = False
    nudges = 0
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

        # A model may emit DB and wiki calls in one parallel batch. Execute all
        # non-wiki calls first; only a successful financial result opens the
        # wiki phase for a mixed question. Return results in the model's
        # original call order so Gemini/Ollama transcripts remain valid.
        indexed_calls = list(enumerate(resp.tool_calls))
        execution_order = (
            sorted(indexed_calls, key=lambda item: is_policy_tool(item[1].name))
            if intent == "mixed" else indexed_calls
        )
        results_by_index: dict[int, ToolResult] = {}
        for index, call in execution_order:
            effective_args = dict(call.args or {})
            blocked = ""
            next_step = ""
            relevant_financial_db_ok = has_relevant_financial_evidence()
            if (
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
                scope_blocked = True
            elif intent == "data" and is_policy_tool(call.name):
                blocked = (
                    f"{call.name} is not allowed for a data-only question; "
                    "wiki text cannot substitute for PeopleSoft data"
                )
                next_step = "Call the relevant PeopleSoft database tool."
            elif (
                intent == "mixed"
                and is_policy_tool(call.name)
                and not relevant_financial_db_ok
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
                        call.name, effective_args, request_scope
                    )
                except ScopeConflict as e:
                    blocked = f"REQUEST_SCOPE_CONFLICT: {e}"
                    next_step = (
                        "Use the request scope exactly, or ask the user to "
                        "change the scope before retrying."
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

            started = time.perf_counter()
            out = (
                _blocked_result(blocked, next_step)
                if blocked
                else await call_mcp_tool(session, call.name, effective_args)
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
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
                    last_db_problem = problem or blocked or "database tool failed"
            logged_calls.append({
                "tool": call.name,
                "ok": ok,
                **({"error": (problem or blocked or out)[:240]} if not ok else {}),
            })
            if ok:
                turn_payloads.append(out)
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
        invented = ungrounded_figures(answer, turn_payloads)
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
        if provider_name == "ollama":
            cfg.llm.ollama_model = args.model
        else:
            cfg.llm.gemini_model = args.model

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

            print("Type a question ( /tools /reset /provider ollama|gemini /quit )")
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
                    if len(parts) == 2 and parts[1] in ("ollama", "gemini"):
                        try:
                            provider = build_provider(parts[1], cfg, tools)
                            # Re-size tool results for the NEW provider: an 8B
                            # local model drowns past ~24k chars, and Gemini
                            # can take 120k — keeping the old limit gives the
                            # wrong behavior in both directions.
                            set_tool_result_limit(cfg, parts[1])
                            print(f"(switched to {provider.name}:{provider.model} — history reset)")
                        except (RuntimeError, SystemExit) as e:
                            print(f"(cannot switch: {e})")
                    else:
                        print("usage: /provider ollama|gemini")
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
    ap.add_argument("--provider", choices=["ollama", "gemini"], help="override config.llm.provider")
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
