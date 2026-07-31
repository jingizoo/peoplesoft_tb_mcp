"""MCP server exposing PeopleSoft trial-balance tools over stdio.

Run:  python -m pstb.server          (config from $PSTB_CONFIG or ./config.yaml)

Any MCP-compatible client can use this server (this repo's chat client, an IDE
or desktop MCP host, the MCP Inspector). Never print to stdout here — stdio
carries the protocol; diagnostics go to stderr.
"""
from __future__ import annotations

import os
import sys

try:  # mcp SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

from .config import load_config
from .db import Database, DbError
from .engine import EngineError, TBEngine
from .ar import ARBilling, ARError
from .report import ReportError, ReportRunner
from . import wiki as wiki_mod
from .wiki import WikiError, make_wiki

cfg = load_config(os.environ.get("PSTB_CONFIG"))
db = Database(cfg)
engine = TBEngine(db, cfg)
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
from .sources import SourceRegistry
engine.registry = SourceRegistry(cfg, db)
try:
    wiki = make_wiki(cfg)
except WikiError as e:
    print(f"[pstb] wiki disabled: {e}", file=sys.stderr)
    wiki = None

mcp = FastMCP("peoplesoft-tb")


def _safe(fn, /, **kw) -> dict:
    try:
        return fn(**kw)
    except (EngineError, DbError, WikiError, ReportError, ARError) as e:
        return {"error": str(e)}
    except Exception as e:  # keep the agent loop alive on unexpected failures
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def get_trial_balance(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    ledger: str = "",
    group_by: str = "",
    dept: str = "",
    account: str = "",
    currency: str = "",
    include_adjustments: bool = False,
    max_rows: int = 0,
) -> dict:
    """Trial balance by account: beginning balance, period activity, ending balance, DR/CR.

    fiscal_year: LEAVE AS 0 unless the user named a year. 0 means the current
    fiscal year. Guessing a year (e.g. 2023) queries a year with no data and
    returns a false "nothing found" — never invent one.
    business_unit / ledger: leave empty unless the user named one.
    period: a regular fiscal period (commonly 1-12, sometimes 1-13); 0 means
    current. Pass a configured adjustment period such as 998 for the
    post-adjustment year-end TB.
    group_by: extra chartfields, comma-separated (e.g. "DEPTID" or "DEPTID,PROJECT_ID").
    account: exact "1000", range "6000-6999", list "1000,1100", or prefix "60%".
    currency: filter to one currency code, or "detail" to break out by currency.
    include_adjustments: add adjustment-period (998) amounts into ending balances.
    Amounts are signed: debits positive, credits negative.
    """
    return _safe(
        engine.trial_balance,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period,
        ledger=ledger, group_by=group_by, dept=dept, account=account,
        currency=currency, include_adjustments=include_adjustments, max_rows=max_rows,
    )


@mcp.tool()
def get_account_balance(
    account: str,
    business_unit: str = "",
    fiscal_year: int = 0,
    through_period: int = 0,
    ledger: str = "",
    dept: str = "",
) -> dict:
    """One account's beginning-of-year balance, month-by-month activity trend, and
    ending balance through a period (plus adjustment-period amounts). Use for
    'what is the balance of X' and 'show the monthly trend of X' questions."""
    return _safe(
        engine.account_balance,
        account=account, business_unit=business_unit, fiscal_year=fiscal_year,
        through_period=through_period, ledger=ledger, dept=dept,
    )


@mcp.tool()
def compare_trial_balance(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    vs_fiscal_year: int = 0,
    vs_period: int = 0,
    ledger: str = "",
    dept: str = "",
    account: str = "",
    min_abs_change: float = 0.0,
    top: int = 25,
) -> dict:
    """Compare ending balances between two fiscal year/period snapshots; returns the
    largest movers with change and % change. Defaults: current period vs the prior
    period (pass vs_fiscal_year for a prior-year comparison, e.g. same period last year).
    Flags accounts that are new or that dropped away."""
    return _safe(
        engine.compare_trial_balance,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period,
        vs_fiscal_year=vs_fiscal_year, vs_period=vs_period, ledger=ledger,
        dept=dept, account=account, min_abs_change=min_abs_change, top=top,
    )


@mcp.tool()
def drill_to_journals(
    account: str,
    period: int,
    business_unit: str = "",
    fiscal_year: int = 0,
    ledger: str = "",
    dept: str = "",
    limit: int = 100,
) -> dict:
    """Posted journal lines behind one account's activity in one period (journal id,
    date, source, operator, line amount, description), plus a tie-out of journal
    total vs ledger activity. Use to answer 'what makes up / who posted X'."""
    return _safe(
        engine.drill_to_journals,
        account=account, period=period, business_unit=business_unit,
        fiscal_year=fiscal_year, ledger=ledger, dept=dept, limit=limit,
    )


@mcp.tool()
def search_accounts(query: str = "", account_type: str = "", limit: int = 50,
                    business_unit: str = "") -> dict:
    """Find GL accounts by description text or account-number prefix.
    Uses the business unit's own SetID, so pass the active scope's BU.
    account_type filter: A=Asset, L=Liability, Q=Equity, R=Revenue, E=Expense.
    Empty query lists accounts (up to limit)."""
    return _safe(engine.search_accounts, query=query, account_type=account_type,
                 limit=limit, business_unit=business_unit)


@mcp.tool()
def resolve_period(date: str = "") -> dict:
    """Convert a calendar date (YYYY-MM-DD, or empty for today) into fiscal year and
    accounting period using the GL detail calendar. Call this first whenever the
    user gives a date or says 'current/last month'."""
    return _safe(engine.resolve_period, date=date)


@mcp.tool()
def list_periods(fiscal_year: int = 0) -> dict:
    """Accounting-period calendar (begin/end dates per period) for a fiscal year."""
    return _safe(engine.list_periods, fiscal_year=fiscal_year)


@mcp.tool()
def tb_integrity_check(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    ledger: str = "",
) -> dict:
    """Trial-balance health check through a period: does the TB net to zero, plus
    total debits and credits, suspense account balances, accounts missing chartfield
    definitions, inactive accounts with balances, unposted journals, posted journals
    that don't balance, and whether beginning balances roll correctly from the
    prior-year close (retained earnings).

    fiscal_year: LEAVE AS 0 unless the user named a year — never guess one.
    Read "balanced" for whether the books balance and "control_status" for whether
    exceptions were found; they are different verdicts. If scope_status is not "ok",
    no data was found — retry with a fiscal year from fiscal_years_with_data."""
    return _safe(
        engine.tb_integrity_check,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period, ledger=ledger,
    )


@mcp.tool()
def rollup_trial_balance(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    tree_name: str = "",
    level: int = 2,
    ledger: str = "",
) -> dict:
    """Trial balance summarized by PeopleSoft tree nodes (e.g. Total Assets,
    Liabilities, Revenue) using PSTREENODE/PSTREELEAF ranges. tree_name defaults to
    the configured account tree; level 2 = top summary captions."""
    return _safe(
        engine.rollup_trial_balance,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period,
        tree_name=tree_name, level=level, ledger=ledger,
    )


@mcp.tool()
def get_record_map() -> dict:
    """The semantic map of PeopleSoft records by domain (general ledger,
    billing, receivables, currency, setup), with live row counts from THIS
    database. Transaction records (they carry amounts) are flagged when
    suspiciously small; reference records are legitimately small. Each entry
    names the curated tool to prefer over raw SQL. CALL THIS BEFORE run_sql
    whenever unsure which record answers a question."""
    return _safe(engine.get_record_map)


@mcp.tool()
def get_exchange_rate(
    from_currency: str,
    to_currency: str,
    as_of_date: str = "",
    rate_type: str = "",
    amounts: str = "",
) -> dict:
    """Effective-dated exchange rate from PS_RT_RATE_TBL (direct, inverse, or
    triangulated via the base currency). Pass amounts as a comma list
    ("185196.06, 96000") to have them converted SERVER-SIDE — never multiply
    amounts yourself; copy the returned conversions verbatim."""
    return _safe(
        engine.exchange_rate, from_currency=from_currency,
        to_currency=to_currency, as_of_date=as_of_date, rate_type=rate_type,
        amounts=amounts,
    )


@mcp.tool()
def get_top_billing_customers(
    business_unit: str = "",
    n: int = 10,
    months: int = 12,
    as_of_date: str = "",
    display_currency: str = "",
) -> dict:
    """Top customers by FINALIZED billing volume (PS_BI_HDR, status INV) over a
    trailing window, with invoice counts and share of total. Use for "top N
    billing customers / who do we bill the most". Mixed currencies are never
    summed — pass display_currency to rank on converted totals (rates applied
    server-side). This is billing volume; open balances are get_ar_aging."""
    return _safe(
        ar.top_billing_customers, business_unit=business_unit, n=n,
        months=months, as_of_date=as_of_date, display_currency=display_currency,
    )


@mcp.tool()
def get_ar_aging(
    business_unit: str = "",
    as_of_date: str = "",
    customer_id: str = "",
    detail: bool = False,
    display_currency: str = "",
) -> dict:
    """AR aging by customer: current / 1-30 / 31-60 / 61-90 / over-90 buckets from
    open items (PS_ITEM), with credits and disputes broken out, PLUS a tie-out of
    the subledger total to the GL AR control account. Use for "aging", "overdue",
    "who owes us", "collections", "does AR tie to the GL".
    as_of_date: YYYY-MM-DD, empty = today. detail=true adds item-level rows.
    display_currency: ISO code ("USD", "INR"...) — every item is converted
    server-side at the effective PS_RT_RATE_TBL rate before bucketing and
    ranking, so use this (never convert amounts yourself) when the user wants
    figures "in USD terms" etc. Empty = the BU's base currency. fx_applied in
    the result lists the conversions performed.
    The PS_ITEM record shape is introspected at runtime (some sites have
    ACCTG_DT, some ASOF_DT; DISPUTE_STATUS/BAL_CURRENCY may be absent) —
    record_notes in the result lists any adaptations; relay them.
    Positive = owed by customer; negative = credit memo / on-account receipt."""
    return _safe(
        ar.aging, business_unit=business_unit, as_of_date=as_of_date,
        customer_id=customer_id, detail=detail, display_currency=display_currency,
    )


@mcp.tool()
def get_customer_ar(
    customer: str, business_unit: str = "", as_of_date: str = "",
    display_currency: str = "",
) -> dict:
    """One customer's open AR: every open item with days past due and bucket,
    credit memos, disputes, and the customer's aging summary. customer can be an
    ID (C1001) or a name fragment; ambiguous names return the candidates to ask
    the user about. display_currency converts every item server-side (empty =
    BU base currency); converted items keep original/original_currency."""
    return _safe(
        ar.customer, customer=customer, business_unit=business_unit,
        as_of_date=as_of_date, display_currency=display_currency,
    )


@mcp.tool()
def search_customers(
    query: str = "", limit: int = 25, business_unit: str = ""
) -> dict:
    """Find AR customers by name or ID; returns id, name, active/inactive status,
    and open balance. Empty query lists customers."""
    return _safe(
        ar.search_customers,
        query=query,
        limit=limit,
        business_unit=business_unit,
    )


@mcp.tool()
def get_billing_workbench(
    business_unit: str = "", days_stuck: int = 5, as_of_date: str = ""
) -> dict:
    """Billing pipeline health: invoice counts/amounts by status (NEW, HLD, RDY,
    INV=finalized, CAN), invoices pending longer than days_stuck, billing
    interface lines in error, and finalized invoices that never reached AR.
    Use for "stuck invoices", "billing errors", "revenue not billed",
    "did every invoice make it to AR"."""
    return _safe(
        ar.billing_workbench, business_unit=business_unit,
        days_stuck=days_stuck, as_of_date=as_of_date,
    )


@mcp.tool()
def list_reports() -> dict:
    """Named financial reports (PS/nVision equivalents): income_statement,
    balance_sheet, quarterly_expenses, plus any added to reports/. Shows each
    report's columns and the available timespans. Use before run_report when
    the user asks for a statement, budget comparison, or quarterly view."""
    return _safe(report_runner.list_reports)


@mcp.tool()
def run_report(
    report: str = "",
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    ledger: str = "",
    rows: str = "",
    columns: str = "",
    include_adjustments: bool = False,
) -> dict:
    """Run a financial-statement style report grid (the PS/nVision pattern):
    rows from account-tree nodes or account ranges, columns as
    ledger + timespan, amounts summed from the ledger in base currency.

    report: a name from list_reports (e.g. income_statement). fiscal_year and
    period set the context — LEAVE AS 0 for current unless the user named them.
    Timespans: PER, YTD, BAL (includes balance forward), YR, QTD, Q1-Q4,
    ROLL12, PER-1Y, YTD-1Y, BAL-1Y, YR-1Y, or a period range like "4-6".
    Ad-hoc without a saved report: rows="node:REVENUE:flip,acct:5000-5999",
    columns="ACTUALS:YTD,BUDGET:YTD" (:flip shows credit balances positive)."""
    return _safe(
        report_runner.run,
        report=report, business_unit=business_unit, fiscal_year=fiscal_year,
        period=period, ledger=ledger, rows=rows, columns=columns,
        include_adjustments=include_adjustments,
    )


@mcp.tool()
def resolve_timespan(timespan: str, fiscal_year: int = 0, period: int = 0) -> dict:
    """Show exactly which fiscal periods a timespan covers in context (YTD, BAL,
    QTD, Q1-Q4, ROLL12, the -1Y prior-year variants, or "4-6"). Use when the
    user asks what a report basis means or wants period math confirmed."""
    def _res(timespan: str, fiscal_year: int, period: int) -> dict:
        from .report import resolve_timespan as rts

        _, fy, per, _ = engine._defaults("", fiscal_year, period, "")
        maxreg = engine._max_regular_period(fy)
        clamped = per > maxreg
        out = rts(timespan, fy, min(per, maxreg), maxreg)
        if clamped:
            out["context_note"] = (
                f"Period {per} is an adjustment period; resolved at the "
                f"post-adjustment year-end context (period {maxreg})."
            )
        return out
    return _safe(_res, timespan=timespan, fiscal_year=fiscal_year, period=period)


@mcp.tool()
def list_trees() -> dict:
    """List available PeopleSoft trees (name, setid, latest effective date)."""
    return _safe(engine.list_trees)


@mcp.tool()
def list_financial_scopes(include_activity: bool = False) -> dict:
    """Business units, their ledgers, base currency, and which fiscal years hold
    data — all in ONE call. Use this first when you don't know the scope.
    The default is a fast BU/ledger inventory. Set include_activity=true only
    when the user also asks for fiscal-year ranges or latest posted periods.
    Do not call list_business_units and list_ledgers together to work this out:
    both run in the same turn, so the second cannot use the first's result."""
    return _safe(
        engine.list_financial_scopes, include_activity=include_activity
    )


@mcp.tool()
def list_business_units() -> dict:
    """List GL business units with descriptions and base currency."""
    return _safe(engine.list_business_units)


@mcp.tool()
def list_ledgers(business_unit: str) -> dict:
    """Ledgers holding data for ONE business unit (e.g. ACTUALS, BUDGET).
    business_unit is required — prefer list_financial_scopes, which returns
    business units and ledgers together."""
    return _safe(engine.list_ledgers, business_unit=business_unit)


if cfg.tools.allow_raw_sql:

    @mcp.tool()
    def run_sql(sql: str, max_rows: int = 100, business_unit: str = "",
                source: str = "") -> dict:
        """Run a read-only SQL SELECT against the PeopleSoft database — including
        CUSTOM and site-specific records. Guarded: single SELECT/WITH statement
        only, DML/DDL rejected, rows capped at 500, every table validated against
        the catalog before execution, and unqualified names are schema-qualified
        automatically (write OTHER_OWNER.TBL to reach another schema).
        NEVER invent a table name. For a custom or unfamiliar record call
        search_records FIRST — it searches PeopleTools record descriptions and
        field names — then describe_record/describe_table for its columns.
        get_record_map covers the core GL/AR records (billing = PS_BI_HDR,
        journal lines = PS_JRNL_LN not PS_JRNL_LINE).
        business_unit: the active scope, used only to report whether the query
        was actually restricted to it (scope_filtered / scope_note) — the SQL
        is never rewritten. Relay scope_note when it says NOT restricted.
        PS_ tables use signed amounts (credits negative)."""
        return _safe(engine.for_source(source).run_sql, sql=sql, max_rows=max_rows,
                     business_unit=business_unit)

    @mcp.tool()
    def search_records(query: str = "", limit: int = 25, source: str = "") -> dict:
        """Find the right PeopleSoft record for a question by searching
        PeopleTools metadata — record DESCRIPTIONS and field names, not just
        table names. Use this whenever the question names a module, a document,
        or a custom record you have not seen ("file interface", "voucher",
        "asset profile", "TU_FILE"): searching descriptions finds records whose
        table name gives no clue. Returns the PeopleTools record name, the
        physical table to query (honoring a site's SQLTABLENAME override),
        the description, and approximate row counts, most-populated first.
        Follow with describe_record then run_sql."""
        return _safe(engine.for_source(source).search_records, query=query, limit=limit)

    @mcp.tool()
    def describe_record(record: str) -> dict:
        """Fields of one PeopleSoft record from PeopleTools (PSRECFIELD) plus
        the physical column list, so you can see both the record definition and
        what the database actually has. Accepts a record name (TU_FILE_INTFC)
        or a table name (PS_TU_FILE_INTFC)."""
        return _safe(engine.describe_record, record=record)

    @mcp.tool()
    def list_tables(pattern: str = "", source: str = "") -> dict:
        """List tables/views matching a pattern (e.g. 'JRNL' or 'PS_LEDGER%')."""
        return _safe(engine.for_source(source).list_tables, pattern=pattern)

    @mcp.tool()
    def describe_table(table_name: str, source: str = "") -> dict:
        """Column names and types for one table/view (e.g. PS_JRNL_HEADER)."""
        return _safe(engine.for_source(source).describe_table, table_name=table_name)


if wiki is not None:

    @mcp.tool()
    def wiki_health() -> dict:
        """Is the company wiki actually connected, and is it serving REAL pages?
        Reports the active provider, auth mode, space/label scoping, and — most
        importantly — whether the bundled fictional demo pages are being served
        instead of Confluence. Call this whenever the user doubts a policy
        answer, and NEVER cite a policy figure as authoritative when
        is_bundled_demo_content is true."""
        try:
            return wiki.health()
        except Exception as e:
            return {"error": f"wiki_health failed: {e}"}

    @mcp.tool()
    def wiki_lookup(question: str, max_pages: int = 3,
                    max_passages: int = 6) -> dict:
        """READ the wiki: searches, fetches the top pages, and returns the actual
        PASSAGES that answer the question, each with page title, section, URL and
        version. USE THIS for any policy/process/threshold/"why" question —
        wiki_search alone returns links, and an answer built from titles is a
        guess. Quote the sentence you rely on and name its page. If the passages
        do not contain the answer, say so."""
        try:
            return wiki_mod.lookup(wiki, question, max_pages=max_pages,
                                   max_passages=max_passages)
        except Exception as e:
            return {"error": f"wiki_lookup failed: {e}"}

    @mcp.tool()
    def wiki_search(query: str, limit: int = 5) -> dict:
        """Find candidate wiki pages (title, snippet, URL). This returns
        POINTERS, not full content — for an actual answer call wiki_lookup,
        which returns the passages. Never answer a policy question from these
        titles/links alone."""
        try:
            hits = wiki.search(query, limit)
            for h in hits:
                h.pop("text", None)  # keep search light; wiki_lookup reads pages
            out = {"provider": wiki.provider_name, "results": hits,
                   "next_step": "Call wiki_lookup(question=...) to read the "
                                "actual passages before answering."}
            if getattr(wiki, "provider_name", "") == "localdocs":
                h = wiki.health()
                if h.get("is_bundled_demo_content"):
                    out["demo_content_warning"] = (
                        "These are the repo's FICTIONAL sample policy pages, not "
                        "your company wiki. Do not present any figure from them "
                        "as company policy — say the wiki is not connected."
                    )
            return out
        except Exception as e:
            return {"error": f"wiki_search failed: {e}"}

    @mcp.tool()
    def wiki_get_page(page_id: str) -> dict:
        """Fetch the full text of a wiki page found via wiki_search (pass its id)."""
        try:
            return wiki.get_page(page_id)
        except Exception as e:
            return {"error": f"wiki_get_page failed: {e}"}


def main() -> None:
    print(
        f"[pstb] MCP server starting — db={cfg.db.backend}"
        f"{' (views)' if cfg.db.use_views else ''}, "
        f"wiki={getattr(wiki, 'provider_name', 'off')}, "
        f"raw_sql={'on' if cfg.tools.allow_raw_sql else 'off'}",
        file=sys.stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
