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
from .relationships import Relationships
from .vendors import VendorNetwork
from .report import ReportError, ReportRunner
from . import wiki as wiki_mod
from .wiki import WikiError, make_wiki

cfg = load_config(os.environ.get("PSTB_CONFIG"))
db = Database(cfg)
engine = TBEngine(db, cfg)
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
relationships = Relationships(ar)
from .connectors import ConnectorError
from .connectors import coupa as _coupa_mod
coupa = _coupa_mod.from_env()
playbooks = PlaybookRunner(engine, ar, modules=None, coupa=coupa)
modules = ModulePacks(engine)
vendor_network = VendorNetwork(modules)
memory = SiteMemory(cfg.resolve_path(
    getattr(cfg.tools, 'site_memory', 'site_memory.json')))
from .psquery import QueryCatalog
from .sources import SourceRegistry
engine.registry = SourceRegistry(cfg, db)
query_catalog = QueryCatalog(engine)
from .connectors import psquery_api as _qas_mod


class _LazyQas:
    """Resolve the QAS gateway on FIRST USE, never at import.

    This used to call query_catalog.integration_endpoints() in module scope,
    which put an Oracle round trip on the import path: every consumer of
    pstb.server — the MCP server, the GUI, the probe, the eval harness —
    paid it before doing anything, and a failure there took the whole
    process down rather than one tool. On a real instance it did: the IB
    catalog's gateway column is not named the same across tools releases,
    the query raised ORA-00904, and the server never finished starting.

    Discovery is also worthless to the 69 tools that never touch QAS.
    """

    def __init__(self):
        self._real = None

    def _resolve(self):
        if self._real is None:
            target = ""
            try:
                target = ((query_catalog.integration_endpoints() or {})
                          .get("target_location") or "")
            except Exception as e:  # discovery is optional, never fatal
                print(f"[pstb] IB gateway discovery skipped: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
            self._real = _qas_mod.from_config(cfg, target)
        return self._real

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


qas = _LazyQas()
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
            PlaybookError, MemoryError_, ModuleError, ConnectorError) as e:
        # These carry a remedy written for the reader. Passing the message
        # through UNPREFIXED is the point: "ConnectorError: check
        # PSFT_QAS_NODE" reads as a crash, "check PSFT_QAS_NODE" reads as
        # an instruction.
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
    """    Trial balance by account: beginning balance, period activity, ending balance, DR/CR.
    Amounts are signed: debits positive, credits negative.

    fiscal_year / period: 0 = current. business_unit / ledger: empty = the
    default. Leave them alone unless the user named one — never invent a year.
    account: exact "1000", range "6000-6999", list "1000,1100", or prefix "60%".
    group_by: extra chartfields, comma-separated (e.g. "DEPTID" or "DEPTID,PROJECT_ID").
    currency: filter to one currency code, or "detail" to break out by currency.
    include_adjustments: fold adjustment-period (998) amounts into ending balances."""
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
def explain_balance_change(
    account: str,
    by: str = "",
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    vs_fiscal_year: int = 0,
    vs_period: int = 0,
    ledger: str = "",
    dept: str = "",
    min_abs_contribution: float = 0.0,
    top: int = 10,
) -> dict:
    """    WHY DID IT CHANGE / WHAT DROVE IT / BREAK DOWN THE MOVEMENT / WHICH
    DEPARTMENT CAUSED IT / EXPLAIN THE VARIANCE. Returns an exact additive
    BRIDGE of the movement across one dimension, with per-member
    contribution and share, plus the unexplained residual. Quote that
    residual — it is the point.

    Requires an account filter: the whole trial balance nets to zero, so
    "the total change" has no subject. One dimension per call — by=ACCOUNT
    for a range, or a chartfield such as DEPTID. Defaults are disclosed in
    by_source. No price/volume/mix: PS_LEDGER has no quantity column.
    Chartable."""
    return _safe(
        engine.explain_balance_change,
        account=account, by=by, business_unit=business_unit,
        fiscal_year=fiscal_year, period=period,
        vs_fiscal_year=vs_fiscal_year, vs_period=vs_period, ledger=ledger,
        dept=dept, min_abs_contribution=min_abs_contribution, top=top,
    )


@mcp.tool()
def get_budget_variance(
    business_unit: str = "", fiscal_year: int = 0, period: int = 0,
    ledger: str = "", budget_ledger: str = "", account: str = "",
    dept: str = "", top: int = 25, min_abs_variance: float = 0.0,
    include_balance_sheet: bool = False,
) -> dict:
    """Actual vs BUDGET year-to-date by account, with variance, % and
    FAVOURABILITY. Use for "budget vs actual / are we over budget / where
    are we overspending / variance to plan". Profit-and-loss accounts only
    by default (budgets are set on the P&L; the payload discloses this and
    include_balance_sheet=true overrides). Favourability comes from the
    account TYPE, never the sign — under-spending is favourable,
    under-earning is not. Totals are split revenue vs expense because one
    merged total of signed amounts means nothing. For period-over-period
    movement use compare_trial_balance instead."""
    return _safe(
        engine.budget_variance, business_unit=business_unit,
        fiscal_year=fiscal_year, period=period, ledger=ledger,
        budget_ledger=budget_ledger, account=account, dept=dept, top=top,
        min_abs_variance=min_abs_variance,
        include_balance_sheet=include_balance_sheet)


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
    """    Trial-balance health check through a period: does the TB net to zero,
    plus total debits and credits, suspense account balances, accounts
    missing chartfield definitions, inactive accounts with balances,
    unposted journals, posted journals that don't balance, and whether
    beginning balances roll correctly from the prior-year close (retained
    earnings). Use for "does it balance / is it clean / out of balance / TB
    does not match / ready to close".
    Read "balanced" for whether the books balance and "control_status" for
    whether exceptions were found; they are different verdicts."""
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
    """    Top customers by FINALIZED billing volume (PS_BI_HDR, status INV) over a
    trailing window, with invoice counts and share of total. Use for "top N
    billing customers / who do we bill the most". This is billing volume;
    open balances are get_ar_aging.

    business_unit="ALL" ranks across EVERY business unit in ONE call.
    active_within_months=N keeps only customers with an invoice in the last
    N months — the tool-level meaning of "still buying / active".
    Mixed currencies are never summed — pass display_currency to rank on
    converted totals.
    The window is CALENDAR months, closed at both ends: invoices dated after
    as_of_date are outside it. Check ranking_complete — when it is false the
    grouped read hit its row cap, customers past the cut-off were never
    counted, and the list must NOT be presented as "the top N"."""
    return _safe(
        ar.top_billing_customers, business_unit=business_unit, n=n,
        months=months, as_of_date=as_of_date, display_currency=display_currency,
        active_within_months=active_within_months,
    )


@mcp.tool()
def get_invoice_lifecycle(business_unit: str = "",
                          as_of_date: str = "") -> dict:
    """Where is the billing delay: the pipeline from the billing interface
    to AR as STAGES — interface waiting/error, bills by status with amounts
    and ages, finalized-but-not-in-AR orphans, open in AR — with the
    bottleneck named. Use for "where is the delay / how does an invoice
    flow / what is stuck between order and cash". Cycle times appear only
    where the site stores creation timestamps; otherwise current stage
    ages are reported and the limitation is disclosed."""
    return _safe(ar.invoice_lifecycle, business_unit=business_unit,
                 as_of_date=as_of_date)


@mcp.tool()
def get_customer_financial_360(cust_id: str = "", business_unit: str = "",
                               include_family: bool = True,
                               months: int = 12,
                               as_of_date: str = "") -> dict:
    """ONE customer, everything connected: the corporate family, current
    billing (finalized and still in flight), open receivables with aging
    and disputes, cash received and how much of it was actually applied,
    credit/rebill chains and what they netted to, plus a needs_attention
    list and a node/edge relationship graph. Use for "give me the complete
    picture for customer X", "what is stuck", "which subsidiaries drive the
    parent's overdue balance", "is any of their cash unapplied". Family
    membership comes from the corporate hierarchy recorded in the system —
    customers are NEVER grouped by name. Totals are per currency and are
    not added across currencies. For a ranked list of many customers use
    get_customer_intelligence instead."""
    return _safe(relationships.customer_financial_360, cust_id=cust_id,
                 business_unit=business_unit, include_family=include_family,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def search_vendors(query: str = "", limit: int = 25,
                   business_unit: str = "", as_of_date: str = "") -> dict:
    """Find a supplier by ID or name substring, with its open payables and
    whether it belongs to a corporate supplier group. The AP counterpart of
    search_customers. Use it FIRST when the question names a supplier by
    name rather than by id. If the payload reports
    belongs_to_a_corporate_family or heads_a_corporate_family, the balance
    shown is that legal entity ALONE — say so, and use
    get_vendor_payables_network for the group."""
    # as_of_date is accepted so the scope guard can inject it uniformly;
    # a supplier's identity does not change with the date, so it is not
    # used. Refusing it would raise a TypeError on every scoped call.
    return _safe(modules.search_vendors, query=query, limit=limit,
                 business_unit=business_unit)


@mcp.tool()
def get_vendor_payables_network(vendor_id: str = "", business_unit: str = "",
                                include_family: bool = True,
                                months: int = 12,
                                as_of_date: str = "") -> dict:
    """ONE supplier, everything connected: the corporate supplier family,
    open payables with overdue and stuck vouchers, cash paid, duplicate
    vouchers, and IDENTITY LINKS — other suppliers sharing the same remit
    bank account or the same taxpayer id. Use for "the full picture for
    supplier X", "who else banks where X banks", "are we paying two
    suppliers into one account", "which subsidiaries owe the group's
    balance". Takes a supplier ID: use search_vendors to turn a name into
    one. Bank accounts and taxpayer ids are NEVER returned — a shared key
    appears only as a keyed hash token, and a shared key is a reason to
    investigate, NOT evidence that two suppliers are the same company.
    Only the recorded corporate hierarchy says that; never group suppliers
    by name yourself."""
    return _safe(vendor_network.vendor_payables_network, vendor_id=vendor_id,
                 business_unit=business_unit, include_family=include_family,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def get_dso_trend(business_unit: str = "", fiscal_year: int = 0) -> dict:
    """Monthly DSO (days sales outstanding) computed from the ledger:
    ending AR control balance / period revenue x days in period, revenue
    from ACCOUNT_TYPE='R' postings. Formula disclosed in the payload. Use
    for "DSO trend / are we collecting faster or slower". Chartable."""
    return _safe(ar.dso_trend, business_unit=business_unit,
                 fiscal_year=fiscal_year)


@mcp.tool()
def get_cash_outlook(business_unit: str = "", weeks: int = 8,
                     as_of_date: str = "") -> dict:
    """Expected cash by week from DUE DATES: inflows from open AR items,
    outflows from open vouchers, net per currency, overdue in its own
    bucket. This is due-date arithmetic — the starting point a treasurer
    refines — NOT a payment-behavior forecast, and the payload says so.
    Use for "cash outlook / what is due in and out over the next weeks"."""
    return _safe(ar.cash_outlook, business_unit=business_unit, weeks=weeks,
                 as_of_date=as_of_date)


@mcp.tool()
def get_vendor_intelligence(business_unit: str = "", months: int = 12,
                            n: int = 20, as_of_date: str = "") -> dict:
    """Top vendors with HOW WE PAY them: payment totals and share, plus
    amount-weighted days early/late versus voucher due dates, and computed
    observations (paying early = cash handed over sooner than required;
    late = relationship risk; concentration). The AP mirror of customer
    intelligence. Use for "top vendors / do we pay early or late / vendor
    concentration". Copy observations verbatim — they are arithmetic."""
    return _safe(modules.vendor_intelligence, business_unit=business_unit,
                 months=months, n=n, as_of_date=as_of_date)


@mcp.tool()
def get_customer_intelligence(business_unit: str = "", n: int = 20,
                              months: int = 12,
                              display_currency: str = "",
                              as_of_date: str = "") -> dict:
    """Top customers WITH context: where they are based (city/state/
    country), what they buy (top products from bill lines), and how they
    pay (open AR, overdue, average days late, disputes) — plus computed
    observations: concentration risk, late payers with the working capital
    at stake, disputes, and lapsed top billers. Use for "who are my top
    customers and where are they based / what do they buy / what can I
    optimize". Copy observations verbatim — they are arithmetic over
    records, and every figure is in the payload. Plain ranking with no
    context is get_top_billing_customers."""
    return _safe(ar.customer_intelligence, business_unit=business_unit,
                 n=n, months=months, display_currency=display_currency,
                 as_of_date=as_of_date)


@mcp.tool()
def get_invoice_totals(business_unit: str = "", fiscal_year: int = 0) -> dict:
    """    Total FINALIZED invoice amount (BILL_STATUS='INV'), with a population
    block naming every applied default and everything excluded. Use for
    "total invoice amount / how much have we invoiced / total billed".
    Totals are per currency, never summed across. Pipeline detail by status
    is get_billing_workbench; open balances are get_ar_aging."""
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
    """    AR aging by customer — "who owes us", "overdue", "collections", "aging",
    "does AR tie to the GL": current / 1-30 / 31-60 / 61-90 / over-90 buckets
    from open items (PS_ITEM), credits and disputes broken out, PLUS a tie-out
    of the subledger total to the GL AR control account.
    Positive = owed by customer; negative = credit memo / on-account receipt.

    as_of_date: YYYY-MM-DD, empty = today. detail=true adds item-level rows.
    display_currency: ISO code ("USD", "INR"...) — items are converted
    server-side at the effective rate before bucketing and ranking; empty =
    the BU's base currency. Never convert amounts yourself."""
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
    query: str = "", limit: int = 25, business_unit: str = "",
    display_currency: str = "", as_of_date: str = ""
) -> dict:
    """Find AR customers by name or ID; returns id, name, active/inactive
    status, and open balance CONVERTED to one currency (the unit's base
    unless display_currency says otherwise), with balances_by_currency
    beside it when the customer bills in more than one. Empty query lists
    customers. If the payload reports belongs_to_a_corporate_family or
    heads_a_corporate_family, the balance shown is that legal entity
    ALONE — say so, and use get_customer_financial_360 for the group."""
    return _safe(
        ar.search_customers,
        query=query,
        limit=limit,
        business_unit=business_unit,
        display_currency=display_currency,
        as_of_date=as_of_date,
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
    """    PROPOSE a durable fact about this INSTALLATION for operator approval —
    a naming alias, a calendar convention, a standing exclusion ("exclude
    intercompany from revenue totals"). One table's purpose goes to
    remember_record_fact instead.
    kind: alias | convention | exclusion | context.
    Stored PENDING until a human approves it, so say it was noted for
    review — never that you have learned it."""
    return _safe(memory.propose, text=fact, kind=kind, source="conversation")


@mcp.tool()
def list_playbooks() -> dict:
    """    The playbooks run_playbook can execute — close_readiness,
    receivables_health, daily_brief, post_close_watch, ap_completeness —
    with their steps. Lists only; the verdict comes from run_playbook."""
    return _safe(playbooks.list_playbooks)


@mcp.tool()
def run_playbook(playbook: str = "", business_unit: str = "", ledger: str = "",
                 fiscal_year: int = 0, period: int = 0) -> dict:
    """    Run a review workflow end to end and return a composed verdict.

    playbook: close_readiness (default), receivables_health,
    daily_brief ("what needs my attention today" — exceptions only:
    billing errors, orphans, duplicates, stuck vouchers, late posts,
    two-week cash pressure), post_close_watch, or ap_completeness ("is AP
    complete for month-end / did everything approved reach AP / what should
    we accrue" as ONE composed check) — see list_playbooks.
    Read "verdict": passed = every step ran and found nothing;
    exceptions_found = every step ran and some found something;
    incomplete = a step COULD NOT RUN and must never be reported as a pass.
    Report the verdict and the steps needing attention."""
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
                CUSTOM and site-specific records. Guarded: one SELECT/WITH statement, no
                DML/DDL, rows capped at 500, every table validated against the catalog
                and unqualified names schema-qualified automatically (write
                OTHER_OWNER.TBL to reach another schema). NEVER invent a table name.
                PS_ tables use signed amounts (credits negative).
                business_unit: reported back as scope_filtered / scope_note — the SQL is
                never rewritten. Relay scope_note when it says NOT restricted.
                policy_binds: when the question refers to a POLICY figure ("above the
                capitalization threshold", "over the approval limit"), do NOT type the
                number. Write the bind in the SQL and name the policy here — e.g.
                sql="... WHERE COST >= :threshold", policy_binds={"threshold":
                "capitalization_threshold"}. The value is looked up in the wiki and
                bound server-side, and the result carries policy_basis with the exact
                sentence and page it came from — quote that when you answer. Call
                list_policy_terms for the available names.
                pivot: USE THIS for any "trend", "by month", "over the last N periods",
                "compare X across Y" question. Return three columns — row label, column
                label, value — grouped by the first two, and name them:
                pivot={"row_field":"customer","column_field":"period","value_field":"amt"}.
                Totals, change and percentage change come back computed — quote them,
                never recompute.
                partition: BREAK a slow grouped aggregate into indexed slices and
                merge — the fix for a query that TIMES OUT. Write the query for ONE
                slice with a :partition bind (e.g. AND BUSINESS_UNIT = :partition) and
                pass partition={"values": "business_units"}. ORDER BY / FETCH FIRST
                apply ONCE after the merge, so a top-20 is the top-20 of the WHOLE, not
                of each slice. AVG must be rewritten as SUM + COUNT. Partition on an
                INDEXED column; slicing an unindexed column is N full scans and worse
                than the direct query.
                list_binds: CHAIN a set produced by an earlier tool into this query
                WITHOUT retyping the values. Write `IN (:accts)` in the SQL and pass
                list_binds={"accts": {"from_result": "r2", "field": "accounts"}} — r2
                is the result_id of the earlier result, field is a dot path into it
                ("accounts", "rows[].account", "customers[].cust_id")."""
        return _safe(engine.for_source(source).run_sql, sql=sql, max_rows=max_rows,
                     business_unit=business_unit, policy_binds=policy_binds,
                     pivot=pivot, list_binds=list_binds, partition=partition)


    @mcp.tool()
    def explain_query(sql: str, source: str = "") -> dict:
        """Ask the optimizer how it WOULD run a SELECT — WITHOUT running it.
                USE THIS BEFORE any join or aggregate over large tables (PS_LEDGER,
                PS_JRNL_LN, PS_ITEM, custom transaction tables), and IMMEDIATELY after
                a query times out. Returns row counts, each table's INDEXES with their
                column order, which full scans are planned, and the leading columns
                your WHERE/JOIN must include."""
        return _safe(engine.for_source(source).explain_query, sql=sql)

    @mcp.tool()
    def join_path(from_record: str, to_record: str, source: str = "") -> dict:
        """How to JOIN two PeopleSoft records here — the ON columns, and
                whether this database's indexes can actually serve them.
                USE THIS BEFORE writing any multi-table run_sql. It returns a
                FROM/JOIN skeleton, which columns to pin as constants to turn a
                scan into a range scan (usually SETID or BUSINESS_UNIT, which the
                selected scope already fixes), and a confidence. Guessing a join
                against PS_LEDGER or PS_ITEM does not fail fast — it consumes the
                whole query timeout."""
        return _safe(engine.for_source(source).join_path,
                     from_record=from_record, to_record=to_record)

    @mcp.tool()
    def search_records(query: str = "", limit: int = 25, source: str = "") -> dict:
        """Find the right PeopleSoft record for a question by searching
                PeopleTools metadata — record DESCRIPTIONS and field names, not just
                table names. Use whenever the question names a module, a document, or
                a custom record you have not seen ("file interface", "voucher",
                "asset profile", "TU_FILE"). Returns the PeopleTools record name, the
                physical table to query, the description, and approximate row counts,
                most-populated first."""
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
        """See what a record actually CONTAINS before querying it: every column,
                how many sampled rows populate each, and a few sample rows. Use it when
                more than one record could answer a question, when a record is custom or
                unfamiliar, or when a query returned nothing and you need to know
                whether the record is empty or your predicate was wrong. value_counts is
                the strongest signal — seeing ITEM_STATUS holds 'O' and 'C' turns a
                guessed predicate into a correct one; always_null names columns this
                site never populates whatever the catalog claims.
                Personal columns (names, addresses, tax and bank identifiers) are
                masked to a shape-preserving placeholder. fill_percent and value_counts
                are measured over the rows sampled, so never report them as totals."""
        return _safe(engine.for_source(source).profile_record,
                     table=table, sample_rows=sample_rows)

    @mcp.tool()
    def compare_records(tables: list, sample_rows: int = -1,
                        source: str = "") -> dict:
        """Profile up to 6 candidate records side by side. The intended sequence
                for any unfamiliar question is search_records -> compare_records ->
                run_sql: search_records proposes candidates by description, this shows
                which are readable, populated, and carry the columns and status codes
                the question implies. Sites keep live vs history vs staging vs custom
                copies of one record; names cannot separate those and contents can."""
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
    """    Record what the user tells you a table HOLDS, so the next question finds
    it — "the interface file info is in TU_FILE_INTFC", "PS_XX_REBATE is our
    custom rebate accrual table". Table-scoped; installation-wide facts go to
    remember_site_fact.
    table: the record or table name exactly as it exists (TU_FILE_INTFC).
    fact: what it holds, in one sentence, in the user's own words.
    Both are required. This proposes the fact for approval — say it was NOTED
    FOR REVIEW; never say you have learned or will remember it. Do NOT use
    this for figures, policies or rules — those belong in the wiki, via
    resolve_policy_value."""
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
    """    Flatten a PeopleSoft TREE NODE into its concrete account list — the SET
    PRODUCER for cross-module chains. Trees attach account RANGES to nodes,
    so "the accounts in node X" cannot be answered by a join; this resolves
    the ranges against the account master. Use THIS when the account set
    feeds something else: journals for those accounts, budget rows, any
    run_sql — pass its result_id into run_sql's list_binds instead of
    retyping account numbers. For a trial balance BY node use
    rollup_trial_balance."""
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
    last pay X". A payment is made by a pay cycle, not by a business unit,
    so the unit is applied through the voucher cross-reference: check
    scoped_to_business_unit, and if it is false the totals cover the whole
    installation — say so rather than naming the unit."""
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
def get_coupa_budget_lines(period: str = "") -> dict:
    """Budget lines as the procurement system holds them: account
    segments, period, amount. Use when asked what the budget IS. period
    filters like "FY2026". Segment meanings are per-tenant configuration
    and are reported, never assumed."""
    return _safe(coupa.budget_lines, period=period)


@mcp.tool()
def coupa_budget_variance(business_unit: str = "", fiscal_year: int = 0,
                          period: int = 0, top: int = 25) -> dict:
    """RECONCILIATION: procurement-system BUDGET vs PeopleSoft ACTUALS,
    matched on natural account, year-to-date. Use for "budget vs actual /
    are we over budget / where are we overspending" AT SITES THAT BUDGET
    IN PROCUREMENT rather than in a ledger. Reports compared lines with
    variance and favourability, plus two separate lists that mean
    different things: unbudgeted spend (expense with no budget line) and
    unspent budget. Revenue is excluded — procurement budgets cover
    spend. If budgets live in a PeopleSoft ledger instead, use
    get_budget_variance."""
    return _safe(coupa.budget_variance, engine=engine,
                 business_unit=business_unit, fiscal_year=fiscal_year,
                 period=period, top=top)


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
def get_duplicate_payments(business_unit: str = "", months: int = 12,
                           tolerance_days: int = 7,
                           as_of_date: str = "") -> dict:
    """AP audit: possible duplicate vouchers. Two disclosed lists — the
    same vendor invoice number vouchered twice (near-certain duplicate),
    and same vendor + same amount within a few days under different
    invoice numbers (a REVIEW list; recurring charges look like this).
    Use for "any duplicate payments / did we pay anything twice". An empty
    result is a real answer."""
    return _safe(modules.duplicate_payments, business_unit=business_unit,
                 months=months, tolerance_days=tolerance_days,
                 as_of_date=as_of_date)


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
def run_ps_query(query: str, prompts: Optional[dict] = None,
                 private_owner: str = "", max_rows: int = 0) -> dict:
    """EXECUTE an existing PeopleSoft query (PSQuery) through the Query
    Access Service and return its rows. Use after search_ps_queries and
    describe_ps_query: pass the query name and a prompts map keyed by the
    BIND NAMES that describe_ps_query listed. Cite the query name in your
    answer — this is logic the site built and validated, which is stronger
    evidence than SQL we write. Results reflect the executing PeopleSoft
    user's permission lists and may legitimately differ from the curated
    tools, which read through a database account; say so if the figures
    disagree rather than treating it as an error."""
    return _safe(qas.execute, query=query, prompts=prompts or {},
                 private_owner=private_owner, max_rows=max_rows)


@mcp.tool()
def search_ps_queries(text: str = "", record: str = "",
                      include_private: bool = False, limit: int = 25) -> dict:
    """    Find EXISTING PeopleSoft queries (PSQuery) this site already built:
    "is there already a query for AP aging?", "what queries read
    PS_VOUCHER?" (pass record=). Returns names, descriptions,
    public/private, and run counts. Catalog read only — run_ps_query
    executes."""
    return _safe(query_catalog.search_queries, text=text, record=record,
                 include_private=include_private, limit=limit)


@mcp.tool()
def describe_ps_query(query: str, owner: str = "") -> dict:
    """One existing PSQuery in detail: its runtime PROMPTS in order with
    their types, and the records it reads. Use after search_ps_queries to
    judge whether a query answers the question and what values it would
    need. This reads the catalog only — it cannot execute the query."""
    return _safe(query_catalog.query_detail, query=query, owner=owner)


@mcp.tool()
def list_integration_endpoints() -> dict:
    """Discover this site's Integration Broker target location and its
    service operations from the IB catalog — so the gateway URL comes
    from the database rather than from configuration. Every operation is
    listed with whether it is CALLABLE: only read-only query operations
    are on the invocation allowlist, because IB also carries voucher
    builds and journal loads. Use for "what APIs does this instance
    expose / can we call PeopleSoft directly"."""
    return _safe(query_catalog.integration_endpoints)


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
                PASSAGES that answer the question. USE THIS for any
                policy/process/threshold/"why" question — wiki_search alone returns
                links, and an answer built from titles is a guess."""
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
        """READ a whole wiki page — the tool for TECHNICAL specs and KB articles.
                For "how does X work" / "how do I" questions, find the page
                (wiki_search or wiki_lookup sources) and read it ALL before acting.
                page_id: the id from wiki_search results or wiki_lookup sources.
                Long pages arrive in slices: when the result carries next_offset, call
                again with offset=next_offset until it is absent."""
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
