"""Web UI for the trial-balance agent.

Financial figures are served straight from the engine and rendered by the
browser — the model never produces a number that reaches the screen. The chat
panel is an assistant over already-verified data, not the source of it.

Run:  python -m pstb.gui            (then open http://127.0.0.1:8000)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from ..config import load_config
from ..db import Database, DbError
from ..engine import EngineError, TBEngine
from ..ar import ARBilling, ARError
from ..report import ReportError, ReportRunner
from ..wiki import WikiError, make_wiki

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "The web UI needs FastAPI. Install it with:\n"
        "  python scripts/bootstrap.py --gui\n"
        "or: pip install -e '.[gui]'"
    ) from e

STATIC = Path(__file__).parent / "static"

cfg = load_config(os.environ.get("PSTB_CONFIG"))
db = Database(cfg)
engine = TBEngine(db, cfg)
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
try:
    wiki = make_wiki(cfg)
except WikiError:
    wiki = None

app = FastAPI(title="PeopleSoft Trial Balance", docs_url=None, redoc_url=None)

# Conversation state for the chat panel, kept per process.
_chat_state: dict = {"provider": None, "name": None}


def _guard(fn, **kw):
    try:
        return fn(**kw)
    except (EngineError, DbError, ReportError, ARError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # surface the reason instead of a bare 500
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    d = cfg.defaults
    out = {
        "defaults": {
            "business_unit": d.business_unit,
            "ledger": d.ledger,
            "base_currency": d.base_currency,
            "adjustment_periods": d.adjustment_periods,
            "account_tree": d.account_tree,
        },
        "backend": cfg.db.backend,
        "use_views": cfg.db.use_views,
        "wiki": getattr(wiki, "provider_name", None),
        "llm": {"provider": cfg.llm.provider,
                "model": cfg.llm.ollama_model if cfg.llm.provider == "ollama"
                else cfg.llm.gemini_model},
        "raw_sql": cfg.tools.allow_raw_sql,
    }
    try:
        out["business_units"] = engine.list_business_units()["business_units"]
    except Exception:
        out["business_units"] = []
    try:
        out["ledgers"] = engine.list_ledgers(d.business_unit)["ledgers"]
    except Exception:
        out["ledgers"] = [d.ledger]
    try:
        cur = engine.resolve_period("")
        out["current"] = {"fiscal_year": cur["fiscal_year"], "period": cur["period"]}
    except Exception:
        fy, per = engine._current_fy_period()
        out["current"] = {"fiscal_year": fy, "period": per}

    # The calendar's current period may have no postings yet (early in a month,
    # or before close). Opening on an empty screen reads as "broken", so tell
    # the UI the newest period that actually has activity.
    try:
        rows, _ = db.query(
            f"SELECT MAX(ACCOUNTING_PERIOD) AS p FROM {db.prefix}PS_LEDGER "
            "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led AND FISCAL_YEAR = :fy "
            "AND ACCOUNTING_PERIOD BETWEEN 1 AND 12",
            {"bu": cfg.defaults.business_unit, "led": cfg.defaults.ledger,
             "fy": out["current"]["fiscal_year"]},
            max_rows=1,
        )
        p = rows[0]["p"] if rows else None
        out["last_period_with_data"] = int(p) if p is not None else out["current"]["period"]
    except Exception:
        out["last_period_with_data"] = out["current"]["period"]
    return out


@app.get("/api/trial-balance")
def trial_balance(
    business_unit: str = "", ledger: str = "", fiscal_year: int = 0, period: int = 0,
    group_by: str = "", account: str = "", dept: str = "",
    include_adjustments: bool = False, max_rows: int = 500,
):
    return _guard(
        engine.trial_balance, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period, group_by=group_by, account=account,
        dept=dept, include_adjustments=include_adjustments, max_rows=max_rows,
    )


@app.get("/api/account/{account}")
def account_detail(
    account: str, business_unit: str = "", ledger: str = "",
    fiscal_year: int = 0, through_period: int = 0, dept: str = "",
):
    return _guard(
        engine.account_balance, account=account, business_unit=business_unit,
        ledger=ledger, fiscal_year=fiscal_year, through_period=through_period, dept=dept,
    )


@app.get("/api/journals")
def journals(
    account: str, period: int, business_unit: str = "", ledger: str = "",
    fiscal_year: int = 0, dept: str = "", limit: int = 200,
):
    return _guard(
        engine.drill_to_journals, account=account, period=period,
        business_unit=business_unit, ledger=ledger, fiscal_year=fiscal_year,
        dept=dept, limit=limit,
    )


@app.get("/api/integrity")
def integrity(business_unit: str = "", ledger: str = "", fiscal_year: int = 0, period: int = 0):
    return _guard(
        engine.tb_integrity_check, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period,
    )


@app.get("/api/rollup")
def rollup(
    business_unit: str = "", ledger: str = "", fiscal_year: int = 0,
    period: int = 0, tree_name: str = "", level: int = 2,
):
    return _guard(
        engine.rollup_trial_balance, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period, tree_name=tree_name, level=level,
    )


@app.get("/api/compare")
def compare(
    business_unit: str = "", ledger: str = "", fiscal_year: int = 0, period: int = 0,
    vs_fiscal_year: int = 0, vs_period: int = 0, top: int = 40, min_abs_change: float = 0.0,
):
    return _guard(
        engine.compare_trial_balance, business_unit=business_unit, ledger=ledger,
        fiscal_year=fiscal_year, period=period, vs_fiscal_year=vs_fiscal_year,
        vs_period=vs_period, top=top, min_abs_change=min_abs_change,
    )


@app.get("/api/reports")
def reports_list():
    return _guard(report_runner.list_reports)


@app.get("/api/report")
def report_run(
    name: str, business_unit: str = "", ledger: str = "",
    fiscal_year: int = 0, period: int = 0, include_adjustments: bool = False,
):
    return _guard(
        report_runner.run, report=name, business_unit=business_unit,
        ledger=ledger, fiscal_year=fiscal_year, period=period,
        include_adjustments=include_adjustments,
    )


@app.get("/api/ar/aging")
def ar_aging(business_unit: str = "", as_of_date: str = "",
             customer_id: str = "", detail: bool = False):
    return _guard(ar.aging, business_unit=business_unit, as_of_date=as_of_date,
                  customer_id=customer_id, detail=detail)


@app.get("/api/ar/customer")
def ar_customer(customer: str, business_unit: str = "", as_of_date: str = ""):
    return _guard(ar.customer, customer=customer, business_unit=business_unit,
                  as_of_date=as_of_date)


@app.get("/api/ar/customers")
def ar_customers(query: str = "", limit: int = 25):
    return _guard(ar.search_customers, query=query, limit=limit)


@app.get("/api/billing")
def billing(business_unit: str = "", days_stuck: int = 5, as_of_date: str = ""):
    return _guard(ar.billing_workbench, business_unit=business_unit,
                  days_stuck=days_stuck, as_of_date=as_of_date)


@app.get("/api/accounts")
def accounts(query: str = "", account_type: str = "", limit: int = 300):
    return _guard(engine.search_accounts, query=query, account_type=account_type, limit=limit)


@app.get("/api/wiki/search")
def wiki_search(query: str, limit: int = 8):
    if wiki is None:
        raise HTTPException(status_code=503, detail="No wiki provider configured")
    try:
        return {"provider": wiki.provider_name, "results": wiki.search(query, limit)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/wiki/page")
def wiki_page(page_id: str):
    if wiki is None:
        raise HTTPException(status_code=503, detail="No wiki provider configured")
    try:
        return wiki.get_page(page_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/chat")
async def chat(payload: dict):
    """Run one agent turn over a fresh MCP session, keeping conversation history
    in the provider between requests."""
    message = (payload or {}).get("message", "").strip()
    provider_name = (payload or {}).get("provider") or cfg.llm.provider
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from ..client.chat import agent_turn, set_tool_result_limit, tool_specs
    from ..client.prompt import system_prompt

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(command=sys.executable, args=["-m", "pstb.server"], env=env)

    calls: list = []
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = tool_specs(await session.list_tools())
                set_tool_result_limit(cfg, provider_name)
                if _chat_state["provider"] is None or _chat_state["name"] != provider_name:
                    prompt = system_prompt(cfg, surface="gui")
                    if provider_name == "gemini":
                        from ..client.llm_gemini import GeminiVertexProvider as P
                    else:
                        from ..client.llm_ollama import OllamaProvider as P
                    _chat_state["provider"] = P(cfg, prompt, tools)
                    _chat_state["name"] = provider_name
                provider = _chat_state["provider"]

                # Record which tools ran so the UI can show the evidence trail.
                import pstb.client.chat as chat_mod

                original = chat_mod.call_mcp_tool

                async def traced(sess, name, args):
                    import json as _json
                    import time as _time

                    t0 = _time.perf_counter()
                    out = await original(sess, name, args)
                    ms = int((_time.perf_counter() - t0) * 1000)
                    # Hand the browser the actual payload so it can render the
                    # result inline — the model's prose never carries a figure
                    # that the UI then re-displays.
                    try:
                        data = _json.loads(out)
                    except (ValueError, TypeError):
                        data = None
                    calls.append({
                        "tool": name, "args": args, "ms": ms,
                        "ok": not str(out).startswith("TOOL ERROR"),
                        "result": data,
                    })
                    return out

                chat_mod.call_mcp_tool = traced
                try:
                    answer = await agent_turn(provider, session, message)
                finally:
                    chat_mod.call_mcp_tool = original
    except RuntimeError as e:
        return JSONResponse(status_code=400, content={"error": str(e), "tool_calls": calls})
    except BaseException as e:  # noqa: BLE001 - includes ExceptionGroup
        import traceback

        # anyio wraps failures in an ExceptionGroup whose str() hides the cause.
        # Unwrap to the innermost real exception so the UI can show something
        # actionable instead of "unhandled errors in a TaskGroup".
        def innermost(exc):
            subs = getattr(exc, "exceptions", None)
            return innermost(subs[0]) if subs else exc

        root = innermost(e)
        traceback.print_exception(type(root), root, root.__traceback__, file=sys.stderr)
        return JSONResponse(
            status_code=500,
            content={"error": f"{type(root).__name__}: {root}", "tool_calls": calls},
        )
    return {"answer": answer, "tool_calls": calls, "provider": provider_name}


@app.post("/api/chat/reset")
def chat_reset():
    if _chat_state["provider"] is not None:
        _chat_state["provider"].reset()
    return {"ok": True}


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="PeopleSoft trial-balance web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open a browser window")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  PeopleSoft Trial Balance — {url}")
    print(f"  data: {cfg.db.backend}{' (views)' if cfg.db.use_views else ''} | "
          f"llm: {cfg.llm.provider} | wiki: {getattr(wiki, 'provider_name', 'off')}")
    print("  Ctrl+C to stop\n")
    if args.open:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
