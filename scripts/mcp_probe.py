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
    # This probe asserts against the bundled sample ledger (US001 / FY2026 /
    # known totals). Pointed at a real PeopleSoft database it would both fail
    # spuriously AND fire COUNT(*)-style probes at production, so refuse.
    from pstb.config import load_config

    backend = load_config(str(ROOT / "config.yaml")).db.backend.lower()
    if backend != "sqlite":
        print(f"config.yaml uses db.backend={backend!r}; this probe is a "
              "sample-data self-test and would query your real database.\n"
              "Skipping. Verify a real connection with: "
              "python scripts/diagnose_db.py")
        return

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
            NO_ARG_TOOLS = {"list_trees", "list_business_units",
                            "list_financial_scopes", "list_reports",
                            "get_record_map", "wiki_health",
                            "list_playbooks", "list_sources",
                            "recall_site_facts", "list_policy_terms"}
            missing = [s.name for s in specs if not s.schema.get("properties")
                       and s.name not in NO_ARG_TOOLS]
            assert not missing, f"tools resolved with empty schemas: {missing}"

            # Tools must still BE here. A module-level def written inside an
            # indented suite ends that suite, and every decorated def after it
            # becomes dead code in the new function's body — the file still
            # parses, the server still starts, and six discovery tools simply
            # stop existing. Naming them is the only way that shows up.
            REQUIRED = {
                "coupa_health", "get_coupa_invoices",
                "get_coupa_stuck_approvals", "get_coupa_rni",
                "get_coupa_supplier_spend", "coupa_to_ap_tie",
                "get_trial_balance", "get_account_balance", "drill_to_journals",
                "tb_integrity_check", "search_accounts", "resolve_period",
                "run_sql", "search_records", "describe_record", "list_tables",
                "describe_table", "profile_record", "compare_records",
                "run_playbook", "get_ar_aging", "resolve_policy_value",
                "list_policy_terms", "explain_query",
                "get_open_payables", "get_vendor_payments",
                "get_asset_register", "get_project_costs",
                "get_tree_node_accounts",
            }
            names = {t.name for t in listed.tools}
            gone = sorted(REQUIRED - names)
            assert not gone, (
                f"tools disappeared from the server: {gone} — check for a "
                "def that dedented out of an if-block")
            print(f"all {len(REQUIRED)} required tools present of {len(names)}")
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
            wh = await call("wiki_health")
            print(f"wiki_health: provider={wh.get('provider')}, "
                  f"demo={wh.get('is_bundled_demo_content')}")
            ws = await call("wiki_search", query="suspense")
            if wh.get("is_bundled_demo_content"):
                assert "demo_content_warning" in ws, \
                    "demo content must carry an inline warning in wiki_search"
                print("wiki_search carries demo_content_warning ✔")

            # Confluence health must fail closed on an unreachable host and
            # never raise or echo the token (needs httpx -> probe, not smoke).
            from pstb.wiki import ConfluenceWiki
            cw = ConfluenceWiki("https://nonexistent-host-xyz.invalid/wiki",
                                "a@b.com", "SECRETTOKEN")
            ch = cw.health()
            assert ch["connected"] is False and ch["stage_failed"] == "connect", ch
            assert "SECRETTOKEN" not in str(ch), "token leaked into health output"
            print("Confluence health fails closed, no token leak ✔")

            # CALL every zero-argument tool. Listing a tool proves it was
            # registered; it does NOT prove the module it delegates to was
            # imported. PR #34 shipped run_playbook whose module was never
            # initialized — every call returned "NameError: name 'playbooks'
            # is not defined" while the probe, the suites and CI stayed green
            # because none of them invoked it.
            for name in sorted(NO_ARG_TOOLS):
                if name not in {t.name for t in listed.tools}:
                    continue
                out = await call(name)
                assert "error" not in out, f"{name} failed: {out['error'][:120]}"
            print(f"called {len(NO_ARG_TOOLS)} no-arg tools, none errored")

            # Policy figures must arrive with provenance, and the bundled
            # fictional pages must never be usable as a filter on real data.
            # The chat client now issues a batch's tool calls CONCURRENTLY
            # over this same stdio session. FakeSession tests prove the loop;
            # only a real session proves the SDK multiplexes request ids
            # rather than interleaving corrupt frames — which is exactly the
            # kind of failure that would pass every unit test and break the
            # first real conversation.
            import asyncio as _aio
            g_tb, g_per, g_ws = await _aio.gather(
                call("get_trial_balance", fiscal_year=2026, period=6),
                call("list_periods", fiscal_year=2026),
                call("wiki_search", query="suspense"),
            )
            assert g_tb["totals"]["in_balance"], "concurrent TB wrong"
            assert g_per.get("periods"), "concurrent list_periods wrong"
            assert g_ws.get("results"), "concurrent wiki_search wrong"
            print("3 concurrent tool calls over one stdio session ✔")

            # Plan-before-join: the optimizer's answer must arrive with the
            # index catalog, alias-resolved to real tables.
            xp = await call("explain_query", sql=(
                "SELECT J.JOURNAL_ID FROM PS_JRNL_LN J JOIN PS_JRNL_HEADER H "
                "ON H.BUSINESS_UNIT = J.BUSINESS_UNIT "
                "AND H.JOURNAL_ID = J.JOURNAL_ID "
                "WHERE H.FISCAL_YEAR = 2026"))
            assert xp.get("advice"), xp
            joined = " ".join(xp["advice"])
            assert "PS_JRNL_HEADER" in joined or "run it" in joined.lower(), \
                f"explain advice unusable: {joined[:120]}"
            assert any(t.get("indexes") for t in xp.get("tables", [])), \
                "explain_query returned no index catalog"
            bad = await call("explain_query", sql="DELETE FROM PS_LEDGER")
            assert "error" in bad, "DML was explained instead of refused"
            print("explain_query: plan + index advice, DML refused ✔")

            # Technical research path: find a KB page by the system's NAME,
            # then read it whole through pagination. This is the flow a
            # "how does the <integration> work" question depends on; the
            # passages tool alone is a keyhole view of a spec.
            ws2 = await call("wiki_search", query="IDMart interface")
            assert ws2["results"], "the technical KB page was not findable by name"
            pid = str(ws2["results"][0]["id"])
            first = await call("wiki_get_page", page_id=pid, max_chars=800)
            assert first.get("next_offset"), \
                f"a {first.get('length')}-char spec did not paginate at 800"
            whole = first["text"]
            guard = 0
            nxt = first["next_offset"]
            while nxt is not None and guard < 50:
                part = await call("wiki_get_page", page_id=pid, offset=nxt,
                                  max_chars=800)
                whole += part["text"]
                nxt = part.get("next_offset")
                guard += 1
            assert len(whole) == first["length"], \
                f"reassembled {len(whole)} of {first['length']} chars"
            assert "XX_IDM_BI_STG" in whole and "BIIF0001" in whole, \
                "the spec's record and job names did not survive the read"
            print(f"wiki_get_page pagination: {first['length']} chars in "
                  f"{guard + 1} slices, reassembly exact ✔")

            pol = await call("resolve_policy_value",
                             policy="capitalization_threshold")
            assert pol.get("value") == 5000.0, pol
            assert pol.get("quote"), "a policy value without its sentence is a guess"
            if wh.get("is_bundled_demo_content"):
                assert pol["status"] == "demo_content", pol
                assert pol["usable_as_filter"] is False, \
                    "demo policy figures must not be usable as filters"
                blocked = await call(
                    "run_sql",
                    sql="SELECT 1 AS x FROM PS_JRNL_LN WHERE MONETARY_AMOUNT >= :t",
                    policy_binds={"t": "capitalization_threshold"})
                assert "error" in blocked and "NOT run" in blocked["error"], blocked
                print("demo policy figure refused as a query filter ✔")
            unknown = await call("resolve_policy_value", policy="bonus_accrual_rate")
            assert "error" in unknown and "capitalization_threshold" in unknown["error"]
            print("unknown policy names list the available ones ✔")

            # Module packs answer their whole question in one call.
            ap = await call("get_open_payables")
            assert ap["open_total"] > 0 and ap["overdue_total"] > 0, ap
            assert ap["pipeline_exceptions"], "the recycle voucher vanished"
            vp = await call("get_vendor_payments", vendor="Cobalt")
            assert vp["vendors"] and vp["vendors"][0]["payments"] >= 1, vp
            amr = await call("get_asset_register", months=8)
            assert amr["retirements_in_window"], amr
            pc2 = await call("get_project_costs")
            assert "PRJ-200" in pc2["over_budget"], pc2
            assert "PRJ-300" in pc2["stale"], pc2
            print("AP/AM/PC packs: owe+overdue+stuck, payments, register, "
                  "over-budget+stale ✔")

            # Chain by reference: node -> accounts -> IN-list, over the wire.
            tn = await call("get_tree_node_accounts", node="EXPENSES")
            assert tn["account_count"] >= 5, tn
            chained = await call(
                "run_sql",
                sql=("SELECT ACCOUNT, SUM(POSTED_TOTAL_AMT) AS amt "
                     "FROM PS_LEDGER WHERE BUSINESS_UNIT = 'US001' "
                     "AND ACCOUNT IN (:accts) GROUP BY ACCOUNT"),
                list_binds={"accts": tn["accounts"]})
            got = {r["account"] for r in chained["rows"]}
            assert got and got <= set(tn["accounts"]), \
                f"chained rows outside the node's accounts: {got}"
            print(f"tree chain: node EXPENSES -> {tn['account_count']} "
                  f"accounts -> {len(got)} ledger rows via IN-list ✔")

            # Partitioned execution over the wire: one slice per BU, merged.
            part = await call(
                "run_sql",
                sql=("SELECT H.BILL_TO_CUST_ID AS cust, "
                     "SUM(H.INVOICE_AMOUNT) AS billed "
                     "FROM PS_BI_HDR H WHERE H.BILL_STATUS = 'INV' "
                     "AND H.BUSINESS_UNIT = :partition "
                     "GROUP BY H.BILL_TO_CUST_ID "
                     "ORDER BY billed DESC FETCH FIRST 3 ROWS ONLY"),
                partition={"values": "business_units"})
            assert part.get("partitioned"), part
            assert part["rows"] and part["rows"][0]["billed"] > 0, part
            print(f"partitioned run_sql: "
                  f"{part['partitioned']['strategy']}, "
                  f"top row {part['rows'][0]['cust']} ✔")

            pb = await call("run_playbook", fiscal_year=2026, period=7)
            assert "error" not in pb, f"run_playbook failed: {pb.get('error')}"
            assert pb.get("verdict") in ("passed", "exceptions_found",
                                         "incomplete"), pb
            print(f"run_playbook close_readiness: {pb['verdict']} — "
                  f"{pb['summary']}")

            prof = await call("profile_record", table="PS_ITEM")
            assert "error" not in prof, f"profile_record failed: {prof.get('error')}"
            assert prof.get("value_counts", {}).get("ITEM_STATUS"), \
                f"profile_record returned no status codes: {list(prof)}"

            # The masking is a data-egress control, so assert it over the wire
            # rather than only in-process: this is the payload a model sees.
            cust = await call("profile_record", table="PS_CUSTOMER")
            assert "error" not in cust, f"profile_record failed: {cust.get('error')}"
            assert "NAME1" in (cust.get("masked_columns") or []), cust
            assert "ACME Industrial" not in json.dumps(cust), \
                "an unmasked customer name reached the tool payload"
            print("profile_record reports status codes, masks names ✔")

            cmp_ = await call("compare_records",
                              tables=["PS_ITEM", "PS_BI_HDR", "PS_NOPE_XX"])
            assert "error" not in cmp_, f"compare_records failed: {cmp_.get('error')}"
            assert "PS_ITEM" in cmp_["readable_and_populated"], cmp_
            assert "PS_NOPE_XX" in cmp_["empty_or_unreadable"], cmp_
            print("compare_records separates populated from unreadable ✔")

            print("\nMCP probe passed.")


if __name__ == "__main__":
    asyncio.run(main())
