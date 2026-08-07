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

from typing import Optional

from .config import load_config
from .db import Database, DbError
from .engine import EngineError, TBEngine
from .ar import ARBilling, ARError
from .memory import MemoryError_, SiteMemory
from .modules import ModuleError, ModulePacks
from .playbooks import PlaybookError, PlaybookRunner
from .report import ReportError, ReportRunner
from . import wiki as wiki_mod
from .wiki import WikiError, make_wiki

cfg = load_config(os.environ.get("PSTB_CONFIG"))
db = Database(cfg)
engine = TBEngine(db, cfg)
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
from .connectors import coupa as _coupa_mod
coupa = _coupa_mod.from_env()
playbooks = PlaybookRunner(engine, ar)
modules = ModulePacks(engine)
memory = SiteMemory(cfg.resolve_path(
    getattr(cfg.tools, 'site_memory', 'site_memory.json')))
from .sources import SourceRegistry
engine.registry = SourceRegistry(cfg, db)
try:
    wiki = make_wiki(cfg)
except WikiError as e:
    print(f"[pstb] wiki disabled: {e}", file=sys.stderr)
    wiki = None
# Let the engine resolve policy figures into query binds. Approved site memory
# outranks the wiki, so both are handed over together.
engine.attach_policy(wiki, memory)
# Record discovery uses what people here have taught about their own tables.
engine.attach_memory(memory)

mcp = FastMCP("peoplesoft-tb")


def _safe(fn, /, **kw) -> dict:
    try:
        return fn(**kw)
    except (EngineError, DbError, WikiError, ReportError, ARError,
            PlaybookError, MemoryError_, ModuleError) as e:
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
    active_within_months: int = 0,
) -> dict:
    """Top customers by FINALIZED billing volume (PS_BI_HDR, status INV) over a
    trailing window, with invoice counts and share of total. Use for "top N
    billing customers / who do we bill the most". Mixed currencies are never
    summed — pass display_currency to rank on converted totals (rates applied
    server-side). This is billing volume; open balances are get_ar_aging.
    business_unit="ALL" ranks across EVERY business unit in ONE call — use it
    whenever the user says "across all BUs / company-wide"; NEVER loop this
    tool per business unit, that burns a model round per unit and never
    finishes. Cross-BU rankings should pass display_currency, since units
    bill in different currencies.
    active_within_months=N keeps only customers with an invoice in the last
    N months — the tool-level meaning of "still buying / active customers"
    (each row carries last_invoice_dt as evidence). A compound ask like
    "top 20 customers across all BUs still buying" is exactly ONE call:
    business_unit="ALL", n=20, active_within_months=3."""
    return _safe(
        ar.top_billing_customers, business_unit=business_unit, n=n,
        months=months, as_of_date=as_of_date, display_currency=display_currency,
        active_within_months=active_within_months,
    )


@mcp.tool()
def get_invoice_totals(business_unit: str = "", fiscal_year: int = 0) -> dict:
    """Total FINALIZED invoice amount (BILL_STATUS='INV') with a population
    block naming every applied default and everything excluded — pipeline
    bills (could still become revenue) separated from cancelled (never
    will). Use for "total invoice amount / how much have we invoiced /
    total billed". Totals are per currency, never summed across. When the
    finalized-only default empties the result it says so instead of
    answering 0.00. Pipeline detail by status is get_billing_workbench;
    open balances are get_ar_aging."""
    return _safe(ar.invoice_totals, business_unit=business_unit,
                 fiscal_year=fiscal_year)


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


@mcp.tool()
def recall_site_facts() -> dict:
    """Facts an operator approved about THIS installation — naming aliases,
    calendar conventions, exclusions. They are already in your context; call
    this only when the user asks what the agent has been told, or to check
    whether something is remembered."""
    return _safe(memory.list_facts, status="approved")


@mcp.tool()
def remember_site_fact(fact: str, kind: str = "context") -> dict:
    """PROPOSE a durable fact about this installation for operator approval.

    Use when the USER TEACHES you something that will still be true next
    week — "in our system TU_FILE_INTFC is the file interface", "our fiscal
    year runs July to June", "exclude intercompany from revenue totals".
    Do NOT use it for the answer to a question, for anything you inferred
    rather than were told, or for one-off context.
    kind: alias | convention | exclusion | context.
    The fact is stored as PENDING and does nothing until a human approves it,
    so tell the user it was noted for review — never that you have learned
    it."""
    return _safe(memory.propose, text=fact, kind=kind, source="conversation")


@mcp.tool()
def list_playbooks() -> dict:
    """Named multi-step review workflows that run SERVER-SIDE and return one
    verdict — close readiness, receivables health. Use for a broad readiness
    question ("are we ready to close?", "how healthy is AR?") rather than one
    specific figure. Never run the steps yourself: the sequence is fixed so it
    cannot be reordered or forgotten."""
    return _safe(playbooks.list_playbooks)


@mcp.tool()
def run_playbook(playbook: str = "", business_unit: str = "", ledger: str = "",
                 fiscal_year: int = 0, period: int = 0) -> dict:
    """Run a review workflow end to end and return a composed verdict.

    playbook: close_readiness (default) or receivables_health — see
    list_playbooks. Each step calls the same curated tool the individual
    question would, so the numbers cannot disagree.
    Read "verdict": passed = every step ran and found nothing;
    exceptions_found = every step ran and some found something;
    incomplete = a step COULD NOT RUN and must never be reported as a pass.
    Report the verdict and the steps needing attention; the card beside your
    reply already lists every step."""
    return _safe(playbooks.run, playbook=playbook, business_unit=business_unit,
                 ledger=ledger, fiscal_year=fiscal_year, period=period)


@mcp.tool()
def list_sources() -> dict:
    """Databases this agent can query. 'default' is the PeopleSoft primary —
    every curated tool answers from it. Extra sources are reachable from
    run_sql / list_tables / describe_table / search_records via source=<name>;
    use them when the user names a non-PeopleSoft system."""
    return _safe(lambda: {"sources": engine.registry.describe()})


if cfg.tools.allow_raw_sql:

    @mcp.tool()
    def run_sql(sql: str, max_rows: int = 100, business_unit: str = "",
                source: str = "", policy_binds: Optional[dict] = None,
                pivot: Optional[dict] = None,
                list_binds: Optional[dict] = None,
                partition: Optional[dict] = None) -> dict:
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
        PS_ tables use signed amounts (credits negative).
        policy_binds: when the question refers to a POLICY figure ("above the
        capitalization threshold", "over the approval limit"), do NOT type the
        number. Write the bind in the SQL and name the policy here — e.g.
        sql="... WHERE COST >= :threshold", policy_binds={"threshold":
        "capitalization_threshold"}. The value is looked up in the wiki and
        bound server-side, and the result carries policy_basis with the exact
        sentence and page it came from — quote that when you answer. Call
        list_policy_terms for the available names.
        pivot: USE THIS for any "trend", "by month", "over the last N periods",
        "compare X across Y" question. Write ONE query returning three columns
        — the row label, the column label and the numeric value, grouped by the
        first two — then name them here:
          sql="SELECT C.NAME1 AS customer, H.INVOICE_PERIOD AS period,
               SUM(H.INVOICE_AMOUNT) AS amt FROM ... GROUP BY 1, 2"
          pivot={"row_field":"customer","column_field":"period","value_field":"amt"}
        You get back a cross-tab with the totals, the change and the percentage
        change already computed. Quote those figures directly and NEVER
        recompute or add them up yourself — arithmetic done in prose cannot be
        checked, and the answer guard will reject figures no tool produced.
        Do not run one query per month and add them up; one grouped query is
        both faster and correct.
        partition: BREAK a slow grouped aggregate into indexed slices and
        merge — the fix for a query that TIMES OUT. Write the query for ONE
        slice with a :partition bind (e.g. AND BUSINESS_UNIT = :partition)
        and pass partition={"values": "business_units"} (or an explicit
        list, or a from_result reference). Slices run concurrently; SUM/
        COUNT/MIN/MAX partials merge with the right combiner; ORDER BY /
        FETCH FIRST apply ONCE after the merge, so a top-20 is the top-20 of
        the WHOLE, not of each slice. AVG must be rewritten as SUM + COUNT.
        Partition on an INDEXED column (business unit is the usual one) —
        explain_query on one slice confirms; slicing an unindexed column is
        N full scans and worse than the direct query.
        list_binds: CHAIN a set produced by an earlier tool into this query
        WITHOUT retyping the values. Write `IN (:accts)` in the SQL and pass
        list_binds={"accts": {"from_result": "r2", "field": "accounts"}} —
        r2 is the result_id of the earlier result, field is a dot path into
        it ("accounts", "rows[].account", "customers[].cust_id"). The values
        are substituted mechanically from that stored result; produce the
        set in one round, reference it in the next. Never copy a list of
        ids/accounts by hand into SQL text."""
        return _safe(engine.for_source(source).run_sql, sql=sql, max_rows=max_rows,
                     business_unit=business_unit, policy_binds=policy_binds,
                     pivot=pivot, list_binds=list_binds, partition=partition)


    @mcp.tool()
    def explain_query(sql: str, source: str = "") -> dict:
        """Ask the optimizer how it WOULD run a SELECT — WITHOUT running it.
        USE THIS BEFORE any join or aggregate over large tables (PS_LEDGER,
        PS_JRNL_LN, PS_ITEM, custom transaction tables), and IMMEDIATELY
        after any query times out. Returns the plan, each referenced table's
        approximate row count and its INDEXES with their column order, plus
        concrete advice: which full scans are planned and which index's
        leading columns your WHERE/JOIN must include to avoid them. Rewrite,
        re-explain if unsure, then run_sql. An index helps only when its
        LEADING columns appear in your predicates."""
        return _safe(engine.for_source(source).explain_query, sql=sql)

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
    def profile_record(table: str, sample_rows: int = -1, source: str = "") -> dict:
        """See what a record actually CONTAINS before querying it: every
        column, how many sampled rows populate each one, the real codes held
        by its status/type columns, and a few sample rows.
        Use this when more than one record could answer a question, when a
        record is custom or unfamiliar, or when a query returned nothing and
        you need to know whether the record is empty or your predicate was
        wrong. value_counts is the strongest signal — seeing ITEM_STATUS holds
        'O' and 'C' turns a guessed predicate into a correct one, and
        always_null names columns this site never populates whatever the
        catalog claims.
        Personal columns (names, addresses, tax and bank identifiers) are
        masked to a shape-preserving placeholder before they leave the
        database; fill_percent and value_counts are measured over the rows
        sampled, so never report them as totals."""
        return _safe(engine.for_source(source).profile_record,
                     table=table, sample_rows=sample_rows)

    @mcp.tool()
    def compare_records(tables: list, sample_rows: int = -1,
                        source: str = "") -> dict:
        """Profile up to 6 candidate records side by side and pick between
        them on evidence instead of on their names. The intended sequence for
        any unfamiliar question is search_records -> compare_records -> run_sql:
        search_records proposes candidates by description, this shows which of
        them are readable, populated, and carry the columns and status codes
        the question implies. PeopleSoft ships many near-identically named
        records and sites add more (live vs history vs staging vs custom);
        names cannot separate those and contents can."""
        return _safe(engine.for_source(source).compare_records,
                     tables=tables, sample_rows=sample_rows)

    @mcp.tool()
    def list_tables(pattern: str = "", source: str = "") -> dict:
        """List tables/views matching a pattern (e.g. 'JRNL' or 'PS_LEDGER%')."""
        return _safe(engine.for_source(source).list_tables, pattern=pattern)

    @mcp.tool()
    def describe_table(table_name: str, source: str = "") -> dict:
        """Column names and types for one table/view (e.g. PS_JRNL_HEADER)."""
        return _safe(engine.for_source(source).describe_table, table_name=table_name)


@mcp.tool()
def remember_record_fact(table: str, fact: str) -> dict:
    """REMEMBER what the user tells you about a table, so it is found next
    time. Use this the moment someone explains what a record holds — "the
    interface file info is in TU_FILE_INTFC", "PS_XX_REBATE is our custom
    rebate accrual table". A client-specific record has no PeopleTools
    description and a name that gives nothing away, so no metadata search will
    ever surface it; being told once is the only way it becomes findable.
    Afterwards search_records('interface file') returns it, ranked first.
    table: the record or table name exactly as it exists (TU_FILE_INTFC).
    fact: what it holds, in one sentence, in the user's own words.
    Confirm to the user that you have noted it. Do NOT use this for figures,
    policies or rules — those belong in the wiki, via resolve_policy_value."""
    name = (table or "").strip().upper()
    body = (fact or "").strip()
    if not name or not body:
        return {"error": "remember_record_fact needs both a table and a fact"}
    return _safe(memory.propose, text=f"{name}: {body}", kind="record",
                 source="taught in conversation")


@mcp.tool()
def what_do_we_know_about(table: str) -> dict:
    """What this site has TAUGHT the agent about a record, if anything. Use
    alongside describe_record and profile_record when a table is unfamiliar:
    the catalog says what columns exist, this says what the record is FOR."""
    return _safe(lambda: {
        "table": (table or "").strip().upper(),
        "taught": memory.record_facts(table),
        "note": ("Taught facts are pointers, not authority. Verify the shape "
                 "with describe_record and the contents with profile_record; "
                 "the live catalog wins any disagreement."),
    })


@mcp.tool()
def list_policy_terms() -> dict:
    """Which POLICY figures this agent can resolve from the wiki and bind into
    a query (capitalization threshold, approval limit, write-off limit,
    materiality, suspense clearing days, payment terms). Use with
    resolve_policy_value, or pass policy_binds to run_sql."""
    from .policy import list_terms

    return _safe(list_terms)


@mcp.tool()
def resolve_policy_value(policy: str) -> dict:
    """Look up a POLICY figure in the company wiki and return it with the exact
    sentence and page it came from. USE THIS instead of recalling a threshold
    from memory — a paraphrased figure silently changes which rows an answer
    contains. Returns status: 'resolved' (one figure, cite the source),
    'ambiguous' (the wiki disagrees with itself — quote every value and ask
    which applies), 'not_found' (say so; do not supply one yourself), or
    'demo_content' (bundled fictional pages — never filter real data with it).
    To filter a query by the value, prefer run_sql's policy_binds so the number
    is bound server-side rather than retyped."""
    # Reuse the engine's resolver rather than building one per call — a
    # fresh instance starts with an empty cache, so every ask would pay the
    # full wiki cost again.
    return _safe(engine._policy.resolve, policy=policy)


@mcp.tool()
def get_tree_node_accounts(node: str, tree_name: str = "",
                           business_unit: str = "") -> dict:
    """Flatten a PeopleSoft TREE NODE into its concrete account list — the
    SET PRODUCER for cross-module chains. Trees attach account RANGES to
    nodes, so "the accounts in node X" cannot be answered by a join; this
    resolves the ranges against the account master. For a trial balance BY
    node use rollup_trial_balance directly (one call, no chain needed). Use
    THIS when the account set feeds something else: journals for those
    accounts, budget rows, any run_sql — then reference the result via
    list_binds={"accts": {"from_result": "<result_id>", "field":
    "accounts"}} instead of retyping account numbers."""
    return _safe(report_runner.node_accounts, business_unit=business_unit,
                 tree_name=tree_name, node=node)


@mcp.tool()
def get_open_payables(business_unit: str = "", as_of_date: str = "") -> dict:
    """AP: what do we OWE. Open vouchers by vendor with totals, overdue and
    due-within-7-days amounts, oldest due date per vendor, and pipeline
    exceptions (vouchers in recycle or unposted — owed but invisible to a
    payment run). Use for "what do we owe / open payables / overdue AP /
    anything stuck in AP". This is the payables mirror of get_ar_aging."""
    return _safe(modules.open_payables, business_unit=business_unit,
                 as_of_date=as_of_date)


@mcp.tool()
def get_vendor_payments(business_unit: str = "", vendor: str = "",
                        months: int = 12, n: int = 20,
                        as_of_date: str = "") -> dict:
    """AP: whom did we PAY. Payments over a trailing window ranked by vendor
    — count, total, last payment date; voids excluded where the record
    carries a status. vendor filters by id or name substring; empty ranks
    all. Use for "how much did we pay X / top vendors by spend / when did we
    last pay X"."""
    return _safe(modules.vendor_payments, business_unit=business_unit,
                 vendor=vendor, months=months, n=n, as_of_date=as_of_date)


@mcp.tool()
def coupa_health() -> dict:
    """Coupa connectivity: mode (live vs bundled sample fixtures),
    reachability, latency. Call when Coupa data looks wrong or missing —
    fixture mode means COUPA_BASE_URL is not configured on this host."""
    return _safe(coupa.health)


@mcp.tool()
def get_coupa_invoices(status: str = "", supplier: str = "",
                       days: int = 30, max_rows: int = 50) -> dict:
    """Coupa invoices over a trailing window with per-currency totals.
    status filters exactly (approved, paid, pending_approval, draft);
    supplier matches by normalized name. Use for "what invoices are in
    Coupa / what did supplier X invoice us / Coupa invoice status". This
    is the PROCUREMENT side; the ledger's AP view is get_open_payables."""
    return _safe(coupa.invoices, status=status, supplier=supplier,
                 days=days, max_rows=max_rows)


@mcp.tool()
def get_coupa_stuck_approvals(days_pending: int = 3) -> dict:
    """Coupa invoices sitting in pending approval past a threshold, with
    the current approver and days pending, slowest first. Use for "what is
    stuck in approvals / why hasn't X been booked" — the close cannot book
    what approval is still holding."""
    return _safe(coupa.stuck_approvals, days_pending=days_pending)


@mcp.tool()
def get_coupa_rni(min_amount: float = 0.0) -> dict:
    """Coupa received-not-invoiced by PO line — the month-end ACCRUAL
    CANDIDATES: value received from suppliers with no invoice yet. Totals
    are per currency, never summed across. Use for "what should we accrue /
    received not invoiced / open receipts"."""
    return _safe(coupa.received_not_invoiced, min_amount=min_amount)


@mcp.tool()
def get_coupa_supplier_spend(months: int = 12, top_n: int = 10) -> dict:
    """Coupa invoice spend ranked by supplier and currency over a trailing
    window. Use for "top suppliers by spend / how much have we bought from
    X". Procurement view; cash actually PAID is get_vendor_payments."""
    return _safe(coupa.supplier_spend, months=months, top_n=top_n)


@mcp.tool()
def coupa_to_ap_tie(days: int = 90) -> dict:
    """RECONCILIATION: approved/paid Coupa invoices vs PS vouchers, matched
    server-side on invoice number with the supplier verified against the
    vendor master. Reports matched pairs, amount breaks (same invoice,
    different amount — the dangerous kind), and invoices that never reached
    AP. Use for "did everything approved in Coupa land in AP / AP
    completeness". Every figure comes from this payload — never recompute
    differences in prose."""
    return _safe(coupa.ap_tie, db=db, days=days)


@mcp.tool()
def get_asset_register(business_unit: str = "", months: int = 12,
                       as_of_date: str = "") -> dict:
    """AM: what do we OWN. Asset cost by category with counts, plus this
    window's additions and retirements. COST basis — net book value needs
    the site's depreciation record and is never approximated. Use for "what
    assets do we have / what was added or retired / asset register"."""
    return _safe(modules.asset_register, business_unit=business_unit,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def get_project_costs(business_unit: str = "", project: str = "",
                      stale_after_months: int = 3,
                      as_of_date: str = "") -> dict:
    """PC: every project's ACTUALS against its BUDGET with the two flags a
    reviewer scans for first — over_budget and stale (budget remaining but
    no recent activity). Handles the PS_PROJ_RESOURCE ANALYSIS_TYPE split
    (BUD* rows are budget, the rest actuals) server-side. pct_used is null
    when a project has no budget row — unbudgeted, not 0%. Use for "project
    spend / budget vs actual / which projects are over or dormant"."""
    return _safe(modules.project_costs, business_unit=business_unit,
                 project=project, stale_after_months=stale_after_months,
                 as_of_date=as_of_date)

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
    def wiki_get_page(page_id: str, offset: int = 0, max_chars: int = 0) -> dict:
        """READ a whole wiki page — the tool for TECHNICAL specs and KB
        articles. wiki_lookup returns a handful of ranked passages, which
        answers a policy question but is a keyhole view of an integration
        spec: the record layouts, job names and run controls that make a spec
        actionable rarely share vocabulary with the question, so passage
        ranking drops them. For "how does X work" / "how do I" questions,
        find the page (wiki_search or wiki_lookup sources), then read it ALL
        with this tool before acting.
        Long pages arrive in slices: when the result carries next_offset,
        call again with offset=next_offset until it is absent. Do not act on
        a spec you have only partly read.
        page_id: the id from wiki_search results or wiki_lookup sources."""
        try:
            page = wiki.get_page(page_id)
        except Exception as e:
            return {"error": f"wiki_get_page failed: {e}"}
        text = str(page.get("text") or "")
        # Default slice stays inside the chat client's tool-result budget
        # (24k chars on the local provider) with room for the JSON wrapper —
        # otherwise a long spec is truncated mid-sentence with no way to ask
        # for the rest.
        cap = max(500, min(int(max_chars or 18_000), 100_000))
        start = max(0, int(offset or 0))
        if start >= len(text) and start:
            return {"error": (
                f"offset {start} is past the end of this page "
                f"({len(text)} chars). Re-read from offset 0.")}
        page["text"] = text[start:start + cap]
        page["length"] = len(text)
        page["offset"] = start
        if start + cap < len(text):
            page["next_offset"] = start + cap
            page["note"] = (
                f"PARTIAL: characters {start}-{start + cap} of {len(text)}. "
                f"Call wiki_get_page again with offset={start + cap} to "
                "continue — do not act on a spec you have only partly read.")
        return page


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
