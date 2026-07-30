#!/usr/bin/env python3
"""End-to-end MCP check without an LLM: spawns the server over stdio, lists
tools, and calls a few. Requires the package installed (make venv).

Run:  .venv/bin/python scripts/mcp_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from pstb.client.chat import tool_specs  # noqa: E402
from pstb.client.llm_base import clean_schema  # noqa: E402


async def main() -> None:
    env = dict(os.environ)
    env["PSTB_CONFIG"] = str(ROOT / "config.yaml")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    server = StdioServerParameters(command=sys.executable, args=["-m", "pstb.server"], env=env)

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            print(f"{len(listed.tools)} tools exposed:")
            for t in listed.tools:
                print(f"  - {t.name}")

            # Build the provider tool specs exactly as the chat client does.
            # This is the step that MCP 1.x/2.x field renames break; probing
            # only tool names hid that failure until runtime.
            specs = tool_specs(listed)
            assert len(specs) == len(listed.tools), "tool spec count mismatch"
            NO_ARG_TOOLS = {"list_trees", "list_business_units", "list_financial_scopes", "list_reports", "get_record_map"}
            missing = [s.name for s in specs if not s.schema.get("properties")
                       and s.name not in NO_ARG_TOOLS]
            assert not missing, f"tools resolved with empty schemas: {missing}"
            for s in specs:
                clean_schema(s.schema)  # must not raise for either provider
            tb_spec = next(s for s in specs if s.name == "get_trial_balance")
            print(f"\nschema discovery OK — get_trial_balance exposes "
                  f"{len(tb_spec.schema.get('properties', {}))} params ✔")

            async def call(name: str, **args):
                res = await session.call_tool(name, arguments=args)
                text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
                return json.loads(text)

            tb = await call("get_trial_balance", fiscal_year=2026, period=6)
            t = tb["totals"]
            assert t["in_balance"], t
            print(f"\nget_trial_balance FY2026 P6: {tb['row_count']} accounts, "
                  f"DR {t['ending_dr']:,.2f} = CR {t['ending_cr']:,.2f} ✔")

            ic = await call("tb_integrity_check", fiscal_year=2026, period=7)
            print(f"tb_integrity_check: balanced={ic['balanced']}, "
                  f"issues={len(ic['issues'])} -> {ic['issues']}")

            w = await call("wiki_search", query="suspense")
            titles = [r["title"] for r in w["results"]]
            assert titles, w
            print(f"wiki_search('suspense') via {w['provider']}: {titles}")

            rep = await call("run_report", report="income_statement",
                             fiscal_year=2026, period=6)
            assert rep.get("rows"), rep
            print(f"run_report income_statement: {len(rep['rows'])} rows x "
                  f"{len(rep['columns'])} cols ✔")

            sql = await call("run_sql", sql="SELECT COUNT(*) AS n FROM PS_JRNL_HEADER")
            print(f"run_sql journal count: {sql['rows'][0]['n']}")
            print("\nMCP probe passed.")


if __name__ == "__main__":
    asyncio.run(main())
