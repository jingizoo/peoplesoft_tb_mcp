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
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Config, load_config
from ..guards import promises_tool_call, unevidenced_verdict
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
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return text[:limit] + "\n...[truncated]"
    kept = rows
    while kept and len(json.dumps({**payload, "rows": kept}, default=str)) > limit:
        kept = kept[: max(1, len(kept) * 3 // 4)]
        if len(kept) == 1:
            break
    payload["rows"] = kept
    payload["rows_omitted_for_context"] = len(rows) - len(kept)
    payload["note_truncation"] = (
        f"{len(rows) - len(kept)} detail row(s) withheld to fit context; "
        "totals above cover the full result set."
    )
    out = json.dumps(payload, default=str)
    return out if len(out) <= limit else out[:limit] + "\n...[truncated]"


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


async def agent_turn(provider: LLMProvider, session: ClientSession,
                     user_text: str, qlog=None, surface: str = "terminal") -> str:
    resp: LLMResponse = provider.send_user(user_text)
    logged_calls: list[dict] = []
    rounds = 0
    hit_limit = False
    nudges = 0
    for _ in range(MAX_TOOL_ROUNDS):
        if not resp.tool_calls:
            # Promised a tool call but made none — continue rather than
            # handing the user an unfulfilled intention.
            if nudges < MAX_NUDGES and promises_tool_call(resp.text):
                nudges += 1
                resp = provider.send_user(
                    "You stated you would call a tool but did not. Issue that "
                    "tool call now, then answer. Do not describe what you will "
                    "do — do it."
                )
                continue
            break
        rounds += 1
        if resp.text.strip():
            print(f"{DIM}{resp.text.strip()}{RESET}")
        results = []
        for call in resp.tool_calls:
            arg_preview = json.dumps(call.args, default=str)
            if len(arg_preview) > 140:
                arg_preview = arg_preview[:140] + "…"
            print(f"{DIM}  ⚙ {call.name}({arg_preview}){RESET}")
            out = await call_mcp_tool(session, call.name, call.args)
            err = out.startswith("TOOL ERROR") or '"error":' in out[:200]
            logged_calls.append({"tool": call.name, "ok": not err,
                                 **({"error": out[:200]} if err else {})})
            results.append(ToolResult(call_id=call.id, name=call.name, content=out))
        resp = provider.send_tool_results(results)
    else:
        hit_limit = True
    answer = resp.text or ("(stopped: too many tool rounds)" if hit_limit
                           else "(no response)")

    # A compliance verdict needs a rule AND a figure. If one side is missing,
    # say so rather than letting a half-grounded judgement stand.
    used = {c["tool"] for c in logged_calls}
    missing = unevidenced_verdict(answer, used)
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
