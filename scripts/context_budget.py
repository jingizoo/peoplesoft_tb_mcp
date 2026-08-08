#!/usr/bin/env python3
"""What the model is actually sent, before anyone asks it anything.

    .venv/bin/python scripts/context_budget.py
    .venv/bin/python scripts/context_budget.py --provider gemini

Why this exists: the fixed prompt — system prompt + every tool description +
every tool schema — is paid on every single turn, and nothing was measuring
it. It crossed the local model's context window unnoticed, Ollama truncated
silently at an end nothing controls, and routing degraded across the whole
product. tb_integrity_check started answering through run_report. There was
no error, no warning, and no test.

This is the same failure the num_ctx=2048 default caused once already: the
prompt never arrived, and every routing decision was measured against a
prompt that was never read.

Exit code 1 when the fixed prompt plus one full-size tool result does not fit
the configured window, so it can gate a merge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Ollama's llama3.1 tokenizer averages close to 3.6 chars/token on this
# prompt's register (identifiers, JSON punctuation, English). 4.0 flatters
# the number; measured against the real tokenizer the ratio sat at 3.6, so
# that is what is used and the estimate is labelled as one.
CHARS_PER_TOKEN = 3.6


def _tok(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN)


async def measure(provider: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from pstb.client.chat import tool_result_limit, tool_specs
    from pstb.client.prompt import system_prompt
    from pstb.config import load_config

    cfg = load_config(os.environ.get("PSTB_CONFIG")
                      or str(ROOT / "config.yaml"))
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "pstb.server"],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = tool_specs(await session.list_tools())

    prompt = system_prompt(cfg, surface="gui")
    descs = sum(len(t.description) for t in tools)
    schemas = sum(len(json.dumps(t.schema)) for t in tools)
    result_cap = tool_result_limit(cfg, provider)
    window = (int(getattr(cfg.llm, "ollama_num_ctx", 0) or 0)
              if provider == "ollama" else 1_000_000)
    return {
        "provider": provider, "tools": len(tools), "window": window,
        "prompt": len(prompt), "descriptions": descs, "schemas": schemas,
        "fixed": len(prompt) + descs + schemas, "result_cap": result_cap,
        "largest": sorted(((len(t.description), t.name) for t in tools),
                          reverse=True)[:8],
    }


def report(m: dict) -> int:
    fixed_t, cap_t = _tok(m["fixed"]), _tok(m["result_cap"])
    # One tool result, plus the question, the model's own reasoning and the
    # answer. A turn that only just fits the PROMPT still fails the moment a
    # tool returns anything.
    need = fixed_t + cap_t + 1500
    print(f"provider {m['provider']} · {m['tools']} tools · window "
          f"{m['window']:,} tokens\n")
    for label, chars in (("system prompt", m["prompt"]),
                         ("tool descriptions", m["descriptions"]),
                         ("tool schemas", m["schemas"])):
        print(f"  {label:<20} {chars:>7,} chars  ~{_tok(chars):>6,} tok")
    print(f"  {'-' * 20} {'-' * 7}        {'-' * 6}")
    print(f"  {'fixed, every turn':<20} {m['fixed']:>7,} chars  "
          f"~{fixed_t:>6,} tok")
    print(f"  {'+ one tool result':<20} {m['result_cap']:>7,} chars  "
          f"~{cap_t:>6,} tok")
    print(f"  {'+ question & answer':<20} {'':>7}        ~{1500:>6,} tok")
    print(f"  {'=' * 20} {'=' * 7}        {'=' * 6}")
    print(f"  {'NEEDED':<20} {'':>7}        ~{need:>6,} tok")

    print("\n  largest tool descriptions:")
    for chars, name in m["largest"]:
        print(f"    {name:<32} {chars:>5,} chars  ~{_tok(chars):>4,} tok")

    if not m["window"]:
        print("\n  no window configured — nothing to check")
        return 0
    slack = m["window"] - need
    if slack < 0:
        print(f"\n  OVER BUDGET by {-slack:,} tokens.")
        print("  Ollama truncates silently, so the tail of the tool list is")
        print("  simply not read and routing degrades with no error.")
        print("  Fix by cutting descriptions, or raise llm.ollama_num_ctx.")
        return 1
    print(f"\n  fits, with {slack:,} tokens of headroom")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama",
                    choices=["ollama", "gemini"])
    args = ap.parse_args()
    sys.exit(report(asyncio.run(measure(args.provider))))


if __name__ == "__main__":
    main()
