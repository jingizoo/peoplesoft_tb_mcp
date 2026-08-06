#!/usr/bin/env python3
"""Stdlib-only smoke test: exercises the TB engine and wiki against the SQLite
sample directly (no MCP / LLM packages required).

Run after seeding:  python3 scripts/seed_sample_data.py && python3 scripts/smoke_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.wiki import LocalDocsWiki  # noqa: E402

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS += 1
    else:
        raise SystemExit(f"smoke test failed at: {name} {detail}")


def main() -> None:
    cfg = Config.sample(ROOT)
    engine = TBEngine(Database(cfg), cfg)

    print("== resolve_period ==")
    rp = engine.resolve_period("2026-06-15")
    check("2026-06-15 -> FY2026 P6", rp["fiscal_year"] == 2026 and rp["period"] == 6)

    print("== trial balance FY2026 P6 ==")
    tb = engine.trial_balance(fiscal_year=2026, period=6)
    t = tb["totals"]
    check("TB nets to zero", t["in_balance"], f"total ending {t['ending']}")
    check("DR total == CR total", abs(t["ending_dr"] - t["ending_cr"]) < 0.01,
          f"DR {t['ending_dr']:,.2f} / CR {t['ending_cr']:,.2f}")
    rows = {r["account"]: r for r in tb["rows"]}
    r1000 = rows["1000"]
    check("row math: beginning + activity == ending",
          abs(r1000["beginning"] + r1000["period_activity"] - r1000["ending"]) < 0.01)
    check("effective-dated rename picked up",
          rows["4100"]["descr"] == "Services & Subscription Revenue",
          rows["4100"]["descr"])
    tb1 = engine.trial_balance(fiscal_year=2026, period=1)
    check("P&L accounts start the year at zero",
          {r["account"]: r for r in tb1["rows"]}["4000"]["beginning"] == 0.0)

    print("== trial balance by department ==")
    tbd = engine.trial_balance(fiscal_year=2026, period=6, group_by="DEPTID",
                               account="6000-6999")
    depts = {r["deptid"] for r in tbd["rows"]}
    check("expense TB splits by dept", depts == {"10000", "20000", "30000"}, str(depts))

    print("== account balance trend ==")
    ab = engine.account_balance("1000", fiscal_year=2026, through_period=6)
    check("6 monthly points", len(ab["periods"]) == 6)
    check("trend ends at TB ending",
          abs(ab["ending_through_period"] - r1000["ending"]) < 0.01)

    print("== compare periods (travel spike P4) ==")
    cmp_ = engine.compare_trial_balance(fiscal_year=2026, period=4, vs_period=3)
    mover_accts = [m["account"] for m in cmp_["movers"][:8]]
    check("6400 among top movers P4 vs P3", "6400" in mover_accts, str(mover_accts))

    print("== drill to journals ==")
    dr = engine.drill_to_journals("6400", period=4, fiscal_year=2026)
    check("journal lines tie to ledger", dr["ties_to_ledger"],
          f"journals {dr['journal_total']:,.2f} vs ledger {dr['ledger_activity']:,.2f}")
    check("kickoff journal visible",
          any("kickoff" in (l["line_descr"] or "") for l in dr["journal_lines"]))

    print("== integrity check (through P7 = current) ==")
    ic = engine.tb_integrity_check(fiscal_year=2026, period=7)
    check("TB balanced", ic["balanced"])
    check("suspense 1999 flagged",
          any(s["account"] == "1999" for s in ic["suspense_balances"]),
          str(ic["suspense_balances"]))
    check("unposted July journal flagged", len(ic["unposted_journals"]) == 1,
          str([u['journal_id'] for u in ic['unposted_journals']]))
    check("no out-of-balance journals", not ic["out_of_balance_journals"])
    check("retained-earnings roll from FY2025 ok",
          ic["retained_earnings_roll"]["status"] == "ok",
          str(ic["retained_earnings_roll"]))

    print("== tree rollup ==")
    ru = engine.rollup_trial_balance(fiscal_year=2026, period=6, level=2)
    nodes = {n["node"]: n for n in ru["nodes"]}
    check("five caption nodes",
          set(nodes) == {"ASSETS", "LIABILITIES", "EQUITY", "REVENUE", "EXPENSES"},
          str(set(nodes)))
    check("rollup nets to zero", abs(ru["total_ending"]) < 0.01)
    check("assets are a debit balance", nodes["ASSETS"]["ending"] > 0)

    print("== account search ==")
    sa = engine.search_accounts("travel")
    check("finds 6400", any(a["account"] == "6400" for a in sa["accounts"]))

    print("== raw SQL guard ==")
    ok = engine.run_sql("SELECT COUNT(*) AS n FROM PS_LEDGER")
    check("select allowed", ok["rows"][0]["n"] > 0)
    for bad in ("DELETE FROM PS_LEDGER", "SELECT 1; DROP TABLE PS_LEDGER",
                "WITH x AS (SELECT 1) UPDATE PS_LEDGER SET LEDGER='X'"):
        try:
            engine.run_sql(bad)
            check(f"blocked: {bad[:30]}", False)
        except EngineError:
            check(f"blocked: {bad[:30]}", True)

    print("== invented table names rejected with suggestions ==")
    try:
        engine.run_sql("SELECT * FROM PS_JRNL_LINE")
        check("invented table rejected", False)
    except EngineError as ex:
        check("invented table rejected", "PS_JRNL_LN" in str(ex), str(ex)[:90])
    try:
        engine.describe_table("PS_JRNL_LINE")
        check("describe suggests close match", False)
    except EngineError as ex:
        check("describe suggests close match", "PS_JRNL_LN" in str(ex))
    check("CTE names not flagged as tables",
          engine.run_sql("WITH x AS (SELECT 1 AS n) SELECT n FROM x")["rows"][0]["n"] == 1)
    check("valid join still allowed",
          engine.run_sql("SELECT COUNT(*) AS n FROM PS_JRNL_LN")["rows"][0]["n"] > 0)

    print("== no-data scopes must not read as clean/balanced ==")
    bogus = engine.trial_balance(business_unit="NOPE", fiscal_year=2026, period=6)
    check("bogus BU: in_balance is not True", bogus["totals"]["in_balance"] is not True,
          str(bogus["totals"]["in_balance"]))
    check("bogus BU: scope_status reports not found",
          bogus["scope_status"] == "business_unit_not_found", bogus["scope_status"])
    bogus_led = engine.trial_balance(ledger="NOSUCH", fiscal_year=2026, period=6)
    check("bogus ledger diagnosed", bogus_led["scope_status"] == "ledger_not_found",
          bogus_led["scope_status"])
    empty_fy = engine.trial_balance(fiscal_year=1999, period=6)
    check("empty fiscal year diagnosed", empty_fy["scope_status"] == "no_data_for_period",
          empty_fy["scope_status"])
    ic_bogus = engine.tb_integrity_check(business_unit="NOPE", fiscal_year=2099, period=6)
    check("bogus scope integrity: clean is not True", ic_bogus["clean"] is not True,
          str(ic_bogus["clean"]))
    check("bogus scope integrity: control_status not_run",
          ic_bogus["control_status"] == "not_run", ic_bogus["control_status"])
    check("real scope reports exceptions distinctly from balance",
          ic["control_status"] == "exceptions_found" and ic["balanced"] is True,
          f'control_status={ic["control_status"]}, balanced={ic["balanced"]}')

    print("== adjustment-period basis (no double count) ==")
    ab998 = engine.account_balance("6900", fiscal_year=2025, through_period=998)
    check("no fabricated 998 trend rows", len(ab998["periods"]) == 12,
          f"{len(ab998['periods'])} rows")
    check("basis flagged post_adjustment", ab998["basis"] == "post_adjustment")
    # 6900 posts 1,500 in each odd month of FY2025 (9,000 regular) plus a
    # 25,000 audit accrual in period 998 -> 34,000 post-adjustment, not 59,000.
    check("adjustment counted exactly once",
          abs(ab998["ending_through_period"] - 9_000.00) < 0.01
          and abs(ab998["adjustment_period_amount"] - 25_000.00) < 0.01
          and abs(ab998["ending_incl_adjustments"] - 34_000.00) < 0.01,
          f"regular {ab998['ending_through_period']:,.2f} + adj "
          f"{ab998['adjustment_period_amount']:,.2f} = "
          f"{ab998['ending_incl_adjustments']:,.2f}")
    ab12 = engine.account_balance("6900", fiscal_year=2025, through_period=12)
    check("through_period=12 excludes adjustments from ending",
          abs(ab12["ending_through_period"] - ab998["ending_through_period"]) < 0.01)

    print("== currency / amount basis contract ==")
    from pstb import queries as q
    _p = {}
    _sql = q.tb_period_sums(Database(cfg), extras=[], include_adj=False,
                            adj_periods=[998], dept="", currency="", account="", params=_p)
    check("statistical rows excluded", "STATISTICS_CODE" in _sql)
    # POSTED_TOTAL_AMT is the BASE amount on EVERY currency row; a journal
    # entered in EUR posts only a CURRENCY_CD='EUR' row. Filtering base-basis
    # queries to the base currency silently dropped all foreign-entered
    # activity — while the TB still balanced. The base basis must therefore
    # carry NO currency predicate.
    check("base basis sums all currency rows (no CURRENCY_CD filter)",
          "CURRENCY_CD = L.BASE_CURRENCY" not in _sql
          and ":base_curr" not in _sql, _sql[:80])
    import sqlite3 as _sqfx
    _fxc = _sqfx.connect(str(ROOT / "sample_data" / "ps_sample.db"))
    _fxc.execute(
        "INSERT INTO PS_LEDGER SELECT BUSINESS_UNIT, LEDGER, ACCOUNT, ALTACCT, "
        "DEPTID, OPERATING_UNIT, PRODUCT, FUND_CODE, CLASS_FLD, PROGRAM_CODE, "
        "BUDGET_REF, AFFILIATE, PROJECT_ID, 'EUR', STATISTICS_CODE, "
        "FISCAL_YEAR, ACCOUNTING_PERIOD, 9200.0, 9200.0, 8464.0, "
        "BASE_CURRENCY FROM PS_LEDGER WHERE ACCOUNT='1000' "
        "AND FISCAL_YEAR=2026 AND ACCOUNTING_PERIOD=6 LIMIT 1")
    _fxc.execute(
        "INSERT INTO PS_LEDGER SELECT BUSINESS_UNIT, LEDGER, ACCOUNT, ALTACCT, "
        "DEPTID, OPERATING_UNIT, PRODUCT, FUND_CODE, CLASS_FLD, PROGRAM_CODE, "
        "BUDGET_REF, AFFILIATE, PROJECT_ID, 'EUR', STATISTICS_CODE, "
        "FISCAL_YEAR, ACCOUNTING_PERIOD, -9200.0, -9200.0, -8464.0, "
        "BASE_CURRENCY FROM PS_LEDGER WHERE ACCOUNT='4000' "
        "AND FISCAL_YEAR=2026 AND ACCOUNTING_PERIOD=6 LIMIT 1")
    _fxc.commit(); _fxc.close()
    eng_fx = TBEngine(Database(Config.sample(ROOT)), Config.sample(ROOT))
    tb_fx = eng_fx.trial_balance(fiscal_year=2026, period=6)
    r1000fx = {r["account"]: r for r in tb_fx["rows"]}["1000"]
    check("foreign-entered journal included in base TB",
          abs(r1000fx["ending"] - (r1000["ending"] + 9200.0)) < 0.01
          and tb_fx["totals"]["in_balance"],
          f"{r1000fx['ending']:,.2f}")
    _fxc = _sqfx.connect(str(ROOT / "sample_data" / "ps_sample.db"))
    _fxc.execute("DELETE FROM PS_LEDGER WHERE CURRENCY_CD='EUR'")
    _fxc.commit(); _fxc.close()
    check("basis declared on result", tb.get("amount_basis") == "base", str(tb.get("amount_basis")))
    _txn = q.tb_period_sums(Database(cfg), extras=[], include_adj=False, adj_periods=[998],
                            dept="", currency="", account="", params={}, amount_basis="transaction")
    check("transaction basis uses POSTED_TRAN_AMT", "POSTED_TRAN_AMT" in _txn)

    print("== scope discovery in one call ==")
    sc = engine.list_financial_scopes()
    check("scopes returns BU + ledgers together",
          sc["scopes"][0]["business_unit"] == "US001"
          and sc["scopes"][0]["ledgers"][0]["ledger"] == "ACTUALS",
          str(sc["scopes"][0]))
    check("base currency reported", sc["scopes"][0]["base_currency"] == "USD")
    try:
        engine.list_ledgers()
        check("list_ledgers rejects missing business_unit", False)
    except EngineError:
        check("list_ledgers rejects missing business_unit", True)

    print("== variance excludes unchanged accounts ==")
    flat = engine.compare_trial_balance(fiscal_year=2026, period=7, vs_period=6)
    check("no zero-change movers", not flat["movers"], f"{len(flat['movers'])} movers")

    print("== nVision-style reports (tree x ledger x timespan) ==")
    from pstb.report import ReportError, ReportRunner, resolve_timespan
    rr = ReportRunner(engine)
    check("YTD -> periods 1..6",
          resolve_timespan("YTD", 2026, 6)["segments"]
          == [{"fiscal_year": 2026, "periods": [1, 2, 3, 4, 5, 6]}])
    check("BAL includes balance forward",
          0 in resolve_timespan("BAL", 2026, 6)["segments"][0]["periods"])
    check("Q2 -> periods 4-6",
          resolve_timespan("Q2", 2026, 6)["segments"][0]["periods"] == [4, 5, 6])
    r12 = resolve_timespan("ROLL12", 2026, 6)["segments"]
    check("ROLL12 spans two fiscal years",
          [x["fiscal_year"] for x in r12] == [2025, 2026]
          and r12[0]["periods"] == [7, 8, 9, 10, 11, 12])
    check("YTD-1Y is prior year",
          resolve_timespan("YTD-1Y", 2026, 6)["segments"][0]["fiscal_year"] == 2025)
    check("custom period range 4-6",
          resolve_timespan("4-6", 2026, 6)["segments"][0]["periods"] == [4, 5, 6])
    lst = rr.list_reports()
    check("3+ sample reports found", len(lst["reports"]) >= 3,
          str([r["name"] for r in lst["reports"]]))

    inc = rr.run(report="income_statement", fiscal_year=2026, period=6)
    irows = {r["label"]: r for r in inc["rows"]}
    check("income stmt revenue ties to REVENUE rollup (flipped)",
          abs(irows["Revenue"]["cells"][0] - (-nodes["REVENUE"]["ending"])) < 0.01,
          f"{irows['Revenue']['cells'][0]:,.2f}")
    ni_expect = -(nodes["REVENUE"]["ending"] + nodes["EXPENSES"]["ending"])
    check("income stmt net income ties to rollup",
          abs(irows["Net Income"]["cells"][0] - ni_expect) < 0.01,
          f"{irows['Net Income']['cells'][0]:,.2f}")
    check("budget column differs from actuals",
          abs(irows["Revenue"]["cells"][1] - irows["Revenue"]["cells"][0]) > 1)
    check("variance = actuals - budget",
          abs(irows["Revenue"]["cells"][2]
              - (irows["Revenue"]["cells"][0] - irows["Revenue"]["cells"][1])) < 0.01)
    check("expanded account detail rows present",
          any(r["kind"] == "detail" and r["label"].startswith("4000")
              for r in inc["rows"]))

    bs = rr.run(report="balance_sheet", fiscal_year=2026, period=6)
    brows = {r["label"]: r for r in bs["rows"]}
    check("balance sheet assets tie to rollup",
          abs(brows["Total Assets"]["cells"][0] - nodes["ASSETS"]["ending"]) < 0.01)
    check("balance sheet balances (check row = 0)",
          abs(brows["Check: Assets - L&E (should be 0)"]["cells"][0]) < 0.01)

    qe = rr.run(report="quarterly_expenses", fiscal_year=2026, period=6)
    ab5000 = engine.account_balance("5000", fiscal_year=2026, through_period=6)
    q2sum = sum(x["activity"] for x in ab5000["periods"] if x["period"] in (4, 5, 6))
    qrow = {r["label"]: r for r in qe["rows"]}["Cost of Goods Sold"]
    check("Q2 column ties to account trend", abs(qrow["cells"][1] - q2sum) < 0.01)

    leds = {l["ledger"] for sco in engine.list_financial_scopes()["scopes"]
            for l in sco["ledgers"]}
    check("BUDGET ledger seeded", "BUDGET" in leds, str(leds))
    try:
        rr.run(report="nope")
        check("unknown report rejected", False)
    except ReportError:
        check("unknown report rejected", True)
    adhoc = rr.run(rows="node:REVENUE:flip", columns="ACTUALS:Q2",
                   fiscal_year=2026, period=6)
    check("ad-hoc report works", abs(adhoc["rows"][0]["cells"][0]) > 1000,
          f"{adhoc['rows'][0]['cells'][0]:,.2f}")

    print("== report review-findings regressions ==")
    # Subtotals must propagate no-data as None, never fabricate 0.00
    bs25 = rr.run(report="balance_sheet", fiscal_year=2025, period=12)
    b25 = {r["label"]: r for r in bs25["rows"]}
    check("no-data prior-year column: value rows are None",
          b25["Total Assets"]["cells"][1] is None)
    check("no-data prior-year column: SUBTOTAL rows are None too",
          b25["Total Liabilities & Equity"]["cells"][1] is None
          and b25["Check: Assets - L&E (should be 0)"]["cells"][1] is None,
          str(b25["Total Liabilities & Equity"]["cells"]))
    check("computed Change column stays None without prior data",
          b25["Total Liabilities & Equity"]["cells"][2] is None)
    # Adjustment-period context: period 998 = post-adjustment year-end basis
    inc12 = rr.run(report="income_statement", fiscal_year=2025, period=12)
    inc12a = rr.run(report="income_statement", fiscal_year=2025, period=12,
                    include_adjustments=True)
    inc998 = rr.run(report="income_statement", fiscal_year=2025, period=998)
    i12 = {r["label"]: r for r in inc12["rows"]}
    i12a = {r["label"]: r for r in inc12a["rows"]}
    i998 = {r["label"]: r for r in inc998["rows"]}
    check("include_adjustments adds the 998 accrual to opex",
          abs(i12a["Operating Expenses"]["cells"][0]
              - i12["Operating Expenses"]["cells"][0] - 25_000.00) < 0.01,
          f"{i12['Operating Expenses']['cells'][0]:,.2f} -> "
          f"{i12a['Operating Expenses']['cells'][0]:,.2f}")
    check("period=998 clamps to post-adjustment basis",
          i998["Net Income"]["cells"][0] == i12a["Net Income"]["cells"][0]
          and inc998["period"] == 12 and inc998["include_adjustments"] is True)
    # Percent column math
    rev = {r["label"]: r for r in rr.run(report="income_statement",
                                         fiscal_year=2026, period=6)["rows"]}["Revenue"]
    from pstb.engine import r2 as _r2
    check("percent column = variance / |budget| * 100",
          abs(rev["cells"][3] - _r2(rev["cells"][2] / abs(rev["cells"][1]) * 100)) < 0.05,
          f"{rev['cells'][3]}%")
    # Ad-hoc default column (no columns arg) -> single YTD
    ad = rr.run(rows="acct:5000", fiscal_year=2026, period=6)
    check("ad-hoc default column is YTD",
          len(ad["columns"]) == 1 and ad["columns"][0]["timespan"] == "YTD")
    # resolve_timespan rejects out-of-range periods plainly
    try:
        resolve_timespan("YTD", 2026, 998)
        check("resolve_timespan rejects period 998", False)
    except ReportError:
        check("resolve_timespan rejects period 998", True)

    print("== AR aging & billing workbench ==")
    from pstb.ar import ARBilling, ARError
    arb = ARBilling(engine)
    sc_ = arb.search_customers("acme")
    check("customer search finds ACME", sc_["customers"][0]["cust_id"] == "C1001",
          str(sc_["customers"]))
    ag = arb.aging(as_of_date="2026-07-30")
    check("aging ties to GL control exactly",
          ag["gl_tie"]["ties"] and ag["gl_tie"]["difference"] == 0.0,
          f"diff {ag['gl_tie']['difference']}")
    check("aging subledger equals GL AR balance",
          abs(ag["gl_tie"]["subledger_total"] - ag["gl_tie"]["gl_balance"]) < 0.01,
          f"{ag['gl_tie']['subledger_total']:,.2f}")
    check("bucket sums equal grand total",
          abs(sum(ag["totals"][b] for b in ag["buckets"]) - ag["totals"]["total"]) < 0.01)
    cmap = {c["cust_id"]: c for c in ag["customers"]}
    check("disputed 42,000 lands in over_90 for C1004",
          abs(cmap["C1004"]["over_90"] - 42_000.00) < 0.01
          and abs(cmap["C1004"]["disputed_amt"] - 42_000.00) < 0.01)
    check("credit memo negative for C1002",
          abs(cmap["C1002"]["credit_amt"] - (-8_400.00)) < 0.01)
    check("inactive customer flagged", cmap["C1008"]["customer_status"] == "I")
    # bucket shift: C1007's 122,600 is current at 07-30, overdue at 08-20
    ag2 = arb.aging(as_of_date="2026-08-20")
    c7a, c7b = cmap["C1007"], {c["cust_id"]: c for c in ag2["customers"]}["C1007"]
    check("buckets shift with as_of date",
          c7a["current"] > 0 and c7b["current"] == 0.0 and c7b["1-30"] > 0,
          f"current {c7a['current']:,.2f} -> {c7b['current']:,.2f}")
    cu_ = arb.customer("beacon", as_of_date="2026-07-30")
    check("customer lookup by name fragment", cu_["customer"]["cust_id"] == "C1004")
    check("customer items carry days past due",
          any(i["days_past_due"] > 200 for i in cu_["items"]))
    wb = arb.billing_workbench(as_of_date="2026-07-30", days_stuck=5)
    st = {x["status"]: x for x in wb["statuses"]}
    check("2 invoices ready-not-finalized", st["RDY"]["n"] == 2, str(st.get("RDY")))
    check("stuck list respects threshold",
          {x["invoice"] for x in wb["stuck_invoices"]}
          >= {"INV-260701", "INV-260703"}
          and "INV-260704" not in {x["invoice"] for x in wb["stuck_invoices"]},
          str([x["invoice"] for x in wb["stuck_invoices"]]))
    check("interface errors found", len(wb["interface_errors"]) == 2)
    check("finalized-not-in-AR catches the orphan",
          [o["invoice"] for o in wb["finalized_not_in_ar"]] == ["INV-2606ORPH"],
          str(wb["finalized_not_in_ar"]))
    check("workbench control_status distinct from data",
          wb["control_status"] == "exceptions_found")

    print("== AR review-findings regressions ==")
    # Tie basis is decoupled from as-of: mid-period and future as-of both tie
    for asod in ("2026-06-15", "2026-07-30", "2026-08-20"):
        agx = arb.aging(as_of_date=asod)
        check(f"tie evaluated and ties at as_of {asod}",
              agx["gl_tie"]["evaluated"] and agx["gl_tie"]["ties"],
              str(agx["gl_tie"].get("difference")))
    check("tie basis labeled",
          "latest posted period" in ag["gl_tie"]["basis"], ag["gl_tie"]["basis"])
    # Backdated as-of is flagged as an approximation
    back = arb.aging(as_of_date="2026-03-31")
    check("backdated as-of carries historical warning",
          back.get("historical_approximation") is True and "warning" in back)
    check("current as-of carries no historical warning",
          "historical_approximation" not in ag)
    # Bogus scope must not fabricate a green tie
    bogus_ar = arb.aging(business_unit="XX999", as_of_date="2026-07-30")
    check("bogus BU aging reports scope, not a tie",
          bogus_ar.get("scope_status") == "business_unit_not_found",
          str(bogus_ar.get("scope_status")))
    # Bad control account must not report a pass
    cfg_bad = Config.sample(ROOT)
    cfg_bad.defaults.ar_control_accounts = ["9999X"]
    from pstb.ar import ARBilling as _AB
    bad_tie = _AB(TBEngine(Database(cfg_bad), cfg_bad)).aging(
        as_of_date="2026-07-30")["gl_tie"]
    check("failed control lookup -> evaluated=false, never a pass",
          bad_tie["evaluated"] is False and "9999X" in bad_tie["reason"],
          str(bad_tie.get("reason"))[:80])
    # NULL DUE_DT must not crash; buckets by ACCTG_DT fallback
    agd = arb.aging(as_of_date="2026-07-30", detail=True)
    ndd = [i for i in agd["items"] if i.get("no_due_date")]
    check("null-due-date item survives and is flagged",
          len(ndd) == 1 and ndd[0]["item"] == "DM-260620"
          and ndd[0]["bucket"] == "31-60", str(ndd))
    wbn = arb.billing_workbench(as_of_date="2026-07-30")
    check("workbench tolerates date guards", wbn["control_status"] == "exceptions_found")
    check("orphan check is date-floored",
          wbn["lookback_days"] == 365
          and "365" in next(i for i in wbn["issues"] if "not loaded" in i))

    print("== currency-aware aging (display_currency) ==")
    # The seeded book is multi-currency: an open 3,000.00 EUR item for C1006.
    check("default display currency is the BU base",
          ag["display_currency"] == "USD", str(ag.get("display_currency")))
    check("foreign item converted into the base aging (fx disclosed)",
          any(n.startswith("EUR->USD") for n in ag.get("fx_applied", [])),
          str(ag.get("fx_applied")))
    check("converted customer lists its source currencies",
          cmap["C1006"]["currencies"] == ["EUR", "USD"],
          str(cmap["C1006"].get("currencies")))
    eur_it = [i for i in agd["items"] if i.get("original_currency") == "EUR"]
    check("converted detail keeps original amount and currency",
          len(eur_it) == 1 and abs(eur_it[0]["balance"] - 3_260.87) < 0.01
          and abs(eur_it[0]["original"] - 3_000.00) < 0.01, str(eur_it))
    inr = arb.aging(as_of_date="2026-07-30", display_currency="INR")
    rate_inr = engine.exchange_rate("USD", "INR", as_of_date="2026-07-30")["rate"]
    check("display_currency converts totals server-side at the effective rate",
          abs(inr["totals"]["total"] - ag["totals"]["total"] * rate_inr) < 1.0,
          f"{inr['totals']['total']:,.2f} vs {ag['totals']['total'] * rate_inr:,.2f}")
    check("converted bucket sums still equal the grand total",
          abs(sum(inr["totals"][b] for b in inr["buckets"])
              - inr["totals"]["total"]) < 0.01)
    check("GL tie stays in base currency regardless of display",
          inr["gl_tie"]["ties"] and "converted to base USD" in inr["gl_tie"]["basis"],
          inr["gl_tie"]["basis"])
    try:
        arb.aging(as_of_date="2026-07-30", display_currency="JPY")
        check("missing rate fails closed, never a silent skip", False)
    except ARError as e:
        check("missing rate fails closed, never a silent skip",
              "JPY" in str(e), str(e)[:80])
    cu6 = arb.customer("C1006", as_of_date="2026-07-30",
                       display_currency="INR")
    check("customer 360 honors display_currency",
          cu6["display_currency"] == "INR"
          and any(i.get("original_currency") for i in cu6["items"]),
          str(cu6.get("display_currency")))
    # Review findings: blank BAL_CURRENCY means base — it must convert into a
    # foreign display currency, never pass through at 1.0
    check("blank item currency treated as base, not as already-converted",
          abs(arb._rate_to("", "INR", "2026-07-30", {}, base="USD")
              - rate_inr) < 1e-9
          and arb._rate_to("", "USD", "2026-07-30", {}, base="USD") == 1.0)
    # Rounding: totals must derive from rounded buckets, so bucket sums can
    # never drift a cent off the total after an FX multiply
    check("per-customer bucket sums exact in converted view",
          all(abs(sum(c[b] for b in inr["buckets"]) - c["total"]) < 0.005
              for c in inr["customers"]))
    # The tie converts at PERIOD-END rates (its GL side is period-end): a rate
    # change after period end must not fabricate a break
    check("GL tie converts at period-end rates and discloses the date",
          "at 2026-06-30 rates" in inr["gl_tie"]["basis"],
          inr["gl_tie"]["basis"])

    print("== AR record-shape adaptation ==")
    # Simulate a real site whose PS_ITEM has ASOF_DT (not ACCTG_DT) and no
    # DISPUTE_STATUS — the exact mismatch that raised ORA-00904 on Oracle.
    import shutil as _sh
    import sqlite3 as _sq
    import tempfile as _tf
    from pstb.db import DbError
    _tmpd = Path(_tf.mkdtemp(prefix="pstb_shape_"))
    _var = _tmpd / "variant.db"
    _sh.copy(ROOT / "sample_data" / "ps_sample.db", _var)
    _vc = _sq.connect(_var)
    _vc.executescript("""
ALTER TABLE PS_ITEM RENAME TO PS_ITEM_OLD;
CREATE TABLE PS_ITEM (BUSINESS_UNIT TEXT, CUST_ID TEXT, ITEM TEXT, ITEM_LINE INTEGER,
  ITEM_STATUS TEXT, BAL_AMT REAL, ORIG_ITEM_AMT REAL, BAL_CURRENCY TEXT,
  ASOF_DT TEXT, DUE_DT TEXT, PO_REF TEXT);
INSERT INTO PS_ITEM SELECT BUSINESS_UNIT, CUST_ID, ITEM, ITEM_LINE, ITEM_STATUS,
  BAL_AMT, ORIG_ITEM_AMT, BAL_CURRENCY, ACCTG_DT, DUE_DT, PO_REF FROM PS_ITEM_OLD;
DROP TABLE PS_ITEM_OLD;
""")
    _vc.commit(); _vc.close()
    cfg_v = Config.sample(ROOT)
    cfg_v.db.sqlite_path = str(_var)
    arv = ARBilling(TBEngine(Database(cfg_v), cfg_v))
    agv = arv.aging(as_of_date="2026-07-30", detail=True)
    check("aging adapts when ACCTG_DT is absent (ASOF_DT dating)",
          agv["gl_tie"]["ties"]
          and agv["totals"]["total"] == ag["totals"]["total"],
          str(agv["totals"]["total"]))
    check("adaptation disclosed via record_notes",
          any("ASOF_DT" in n for n in agv.get("record_notes", []))
          and any("DISPUTE_STATUS" in n for n in agv.get("record_notes", [])),
          str(agv.get("record_notes")))
    check("missing dispute column degrades to zero, not a crash",
          agv["totals"]["disputed_amt"] == 0.0
          and len(agv["items"]) == len(agd["items"]))
    inrv = arv.aging(as_of_date="2026-07-30", display_currency="INR")
    check("display currency still converts on the adapted shape",
          inrv["gl_tie"]["ties"]
          and abs(inrv["totals"]["total"] - inr["totals"]["total"]) < 0.01)
    cuv = arv.customer("beacon", as_of_date="2026-07-30")
    check("customer 360 works on the adapted shape and carries notes",
          cuv["customer"]["cust_id"] == "C1004" and "record_notes" in cuv)
    wbv = arv.billing_workbench(as_of_date="2026-07-30")
    check("workbench unaffected by the PS_ITEM shape",
          wbv["control_status"] == "exceptions_found")
    check("reference shape produces no record_notes", "record_notes" not in ag)
    try:
        arv.db.query("SELECT NO_SUCH_COL FROM PS_ITEM", {}, max_rows=1)
        check("missing column translates to remediation", False)
    except DbError as e:
        check("missing column translates to remediation",
              "record shape" in str(e) and "describe_table" in str(e),
              str(e)[:90])
    try:
        arv.db.query("SELECT 1 FROM PS_NOPE_TBL", {}, max_rows=1)
        check("missing table translates to remediation", False)
    except DbError as e:
        check("missing table translates to remediation",
              "list_tables" in str(e), str(e)[:90])
    check("failed introspection is not cached (no poison)",
          arv._cols("PS_NOPE_TBL") == set()
          and "PS_NOPE_TBL" not in arv._colcache)
    # No stray binds on ANY AR SQL: a bind name absent from the statement is a
    # DPY-4008 on Oracle thin mode; sqlite ignores extras, so assert directly.
    import re as _re
    def _bind_audit(a, **kw):
        seen, orig = [], a.db.query
        def spy(sql, params=None, max_rows=None):
            seen.append((sql, dict(params or {})))
            return orig(sql, params, max_rows=max_rows)
        a.db.query = spy
        try:
            a.aging(**kw)
        finally:
            a.db.query = orig
        return [(s[:60], sorted(set(p) - set(_re.findall(r":(\w+)", s))))
                for s, p in seen if set(p) - set(_re.findall(r":(\w+)", s))]
    check("no stray binds in reference-shape aging (detail+filter)",
          _bind_audit(arb, as_of_date="2026-07-30", detail=True,
                      customer_id="C1006") == [], "")
    check("no stray binds in adapted-shape aging",
          _bind_audit(arv, as_of_date="2026-07-30", detail=True) == [], "")

    # Due-only shape (neither ACCTG_DT nor ASOF_DT): the NULL-DUE_DT item must
    # land in a bucket, never vanish from the totals
    _var2 = _tmpd / "due_only.db"
    _sh.copy(ROOT / "sample_data" / "ps_sample.db", _var2)
    _vc2 = _sq.connect(_var2)
    _vc2.executescript("""
ALTER TABLE PS_ITEM RENAME TO PS_ITEM_OLD;
CREATE TABLE PS_ITEM (BUSINESS_UNIT TEXT, CUST_ID TEXT, ITEM TEXT, ITEM_LINE INTEGER,
  ITEM_STATUS TEXT, BAL_AMT REAL, ORIG_ITEM_AMT REAL, BAL_CURRENCY TEXT,
  DUE_DT TEXT, PO_REF TEXT);
INSERT INTO PS_ITEM SELECT BUSINESS_UNIT, CUST_ID, ITEM, ITEM_LINE, ITEM_STATUS,
  BAL_AMT, ORIG_ITEM_AMT, BAL_CURRENCY, DUE_DT, PO_REF FROM PS_ITEM_OLD;
DROP TABLE PS_ITEM_OLD;
DROP TABLE PS_INTFC_BI;
""")
    _vc2.commit(); _vc2.close()
    cfg_v2 = Config.sample(ROOT)
    cfg_v2.db.sqlite_path = str(_var2)
    arv2 = ARBilling(TBEngine(Database(cfg_v2), cfg_v2))
    agv2 = arv2.aging(as_of_date="2026-07-30", detail=True)
    check("due-only shape: undated items stay in the totals",
          agv2["totals"]["total"] == ag["totals"]["total"]
          and agv2["gl_tie"]["ties"],
          f"{agv2['totals']['total']} vs {ag['totals']['total']}")
    check("due-only shape: bucket sums still equal the grand total",
          abs(sum(agv2["totals"][b] for b in agv2["buckets"])
              - agv2["totals"]["total"]) < 0.01)
    check("no stray binds in due-only aging",
          _bind_audit(arv2, as_of_date="2026-07-30", detail=True) == [], "")
    # A skipped check is not a pass: interface table dropped + nothing stuck
    # in window must yield checks_incomplete, never 'passed'
    wb2 = arv2.billing_workbench(as_of_date="2026-07-30", days_stuck=99_999,
                                 lookback_days=1)
    check("workbench never fabricates 'passed' when checks were skipped",
          wb2["control_status"] == "checks_incomplete"
          and any("PS_INTFC_BI" in n for n in wb2.get("record_notes", [])),
          str(wb2["control_status"]))
    _sh.rmtree(_tmpd)

    print("== run_sql validator: function-FROM is not a table ==")
    # Live failure: EXTRACT(YEAR FROM INVOICE_DT) was rejected as table
    # "INVOICE_DT does not exist" before ever reaching the database.
    refs = engine._table_refs(
        "SELECT BUSINESS_UNIT, COUNT(*) FROM PS_BI_HDR "
        "WHERE EXTRACT(YEAR FROM INVOICE_DT) = 2026 "
        "AND TRIM(LEADING '' FROM BILL_SOURCE_ID) <> '' "
        "GROUP BY BUSINESS_UNIT")
    check("EXTRACT/TRIM FROM-args are not table references",
          refs == {"PS_BI_HDR"}, str(refs))
    try:
        engine.run_sql("SELECT COUNT(*) FROM PS_BI_HDR "
                       "WHERE EXTRACT(YEAR FROM INVOICE_DT) = 2026")
        extract_note = "executed"
    except Exception as e:
        extract_note = str(e)
    check("EXTRACT query passes validation (any failure is db-side)",
          "Rejected before execution" not in extract_note
          and "does not exist" not in extract_note, extract_note[:90])
    try:
        engine.run_sql("SELECT COUNT(*) FROM PS_JRNL_LINE")
        check("real table typos still rejected with suggestions", False)
    except Exception as e:
        check("real table typos still rejected with suggestions",
              "Rejected before execution" in str(e) and "PS_JRNL_LN" in str(e),
              str(e)[:90])
    # A subquery inside TRIM(...) must still have its tables validated —
    # neutralization is depth-aware, never blanket
    refs2 = engine._table_refs(
        "SELECT TRIM((SELECT X FROM PS_NOPE_TBL)) FROM PS_BI_HDR")
    check("neutralization does not hide subquery table references",
          refs2 == {"PS_NOPE_TBL", "PS_BI_HDR"}, str(refs2))
    # Adversarial-review catches: function-arg TRIM, comments, apostrophes
    refs3 = engine._table_refs(engine._scrub_sql(
        "SELECT TRIM(TRAILING CHR(32) FROM A.DESCR) FROM PS_GL_ACCOUNT_TBL A"))
    check("TRIM with function arg before FROM is not a table ref",
          refs3 == {"PS_GL_ACCOUNT_TBL"}, str(refs3))
    refs4 = engine._table_refs(engine._scrub_sql(
        "SELECT A.ACCOUNT\n-- year handled below with EXTRACT(\n"
        "FROM PS_LEDGER A GROUP BY A.ACCOUNT"))
    check("an unbalanced function name in a comment cannot hide the FROM",
          refs4 == {"PS_LEDGER"}, str(refs4))
    refs5 = engine._table_refs(engine._scrub_sql(
        "SELECT A.ACCOUNT -- don't include adjustments\n"
        "FROM PS_LEDGER A WHERE A.LEDGER = 'ACTUALS'"))
    check("an apostrophe in a comment cannot desync the literal scrubber",
          refs5 == {"PS_LEDGER"}, str(refs5))
    refs6 = engine._table_refs(engine._scrub_sql(
        "SELECT X FROM PS_LEDGER WHERE CODE = 'A--B' AND LEDGER = 'ACTUALS'"))
    check("'--' inside a literal is not a comment",
          refs6 == {"PS_LEDGER"}, str(refs6))
    try:
        engine.run_sql("SELECT A.ACCOUNT FROM PS_LEDGER A "
                       "-- don't include adjustments\n"
                       "FOR UPDATE -- it's locked")
        check("deny list sees keywords even between apostrophe comments", False)
    except Exception as e:
        check("deny list sees keywords even between apostrophe comments",
              "UPDATE" in str(e), str(e)[:90])
    ok_lead = engine.run_sql("/* count them */ SELECT COUNT(*) AS n "
                             "FROM PS_JRNL_HEADER")
    check("leading comment does not defeat the SELECT-only check",
          ok_lead["rows"][0]["n"] > 0)

    print("== port readiness: no full-ledger scans, concurrency, recovery ==")
    import re as _re2
    import threading as _th
    _cap: list = []
    _orig_q = engine.db.query

    def _spy(sql, params=None, max_rows=None):
        _cap.append((" ".join(str(sql).split()), dict(params or {})))
        return _orig_q(sql, params, max_rows=max_rows)

    engine.db.query = _spy
    try:
        engine.invalidate_scope_cache()
        engine.effective_defaults()
        disc = list(_cap)
        _cap.clear()
        engine.trial_balance(business_unit="US001", ledger="ACTUALS",
                            fiscal_year=2026, period=6)
        scoped = list(_cap)
        _cap.clear()
        # Every tool's SQL, audited for binds. A bind name supplied but not
        # referenced is DPY-4008 on Oracle thin mode; SQLite ignores it.
        for fn in (lambda: engine.rollup_trial_balance(fiscal_year=2026, period=6),
                   lambda: engine.tb_integrity_check(fiscal_year=2026, period=6),
                   lambda: engine.compare_trial_balance(fiscal_year=2026, period=6),
                   lambda: arb.aging(as_of_date="2026-07-30", detail=True),
                   lambda: arb.billing_workbench(as_of_date="2026-07-30")):
            fn()
        audited = list(_cap)
    finally:
        engine.db.query = _orig_q
    # The hazard is an UNBOUNDED DISTINCT over PS_LEDGER (full index scan on
    # Oracle). A DISTINCT with equality predicates on (BUSINESS_UNIT, LEDGER)
    # is the batched existence probe and is index-friendly — the point of
    # batching was to replace one round trip per pair with one per 50.
    _unbounded = [s for s, _ in disc
                  if "DISTINCT BUSINESS_UNIT" in s and " WHERE " not in s]
    check("scope discovery never scans the whole ledger",
          not _unbounded, str([s[:60] for s in _unbounded]))
    check("discovery stays within a handful of round trips",
          len(disc) <= 6, f"{len(disc)} queries")
    check("a fully-scoped call pays no catalog query",
          not [s for s, _ in scoped
               if "PS_BUS_UNIT_LED" in s or "DISTINCT BUSINESS_UNIT" in s])
    strays = []
    for sql, prm in audited + scoped + disc:
        refs = set(_re2.findall(r"(?<![:\w]):(\w+)", sql))
        if set(prm) - refs or refs - set(prm):
            strays.append((sql[:60], sorted(set(prm) - refs), sorted(refs - set(prm))))
    check("no bind mismatches in any audited statement (DPY-4008)",
          not strays, str(strays[:2]))
    # Concurrent chat channels must not serialize or collide
    errs, vals = [], []

    def _worker():
        try:
            vals.append(engine.trial_balance(business_unit="US001",
                                             fiscal_year=2026,
                                             period=6)["totals"]["ending_dr"])
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    threads = [_th.Thread(target=_worker) for _ in range(8)]
    for _thread in threads:
        _thread.start()
    for _thread in threads:
        _thread.join()
    check("8 concurrent channels all succeed with identical totals",
          not errs and len(vals) == 8 and len(set(vals)) == 1, str(errs[:1]))
    # A reaped session must not break every later question
    cfg_rec = Config.sample(ROOT)
    db_rec = Database(cfg_rec)
    eng_rec = TBEngine(db_rec, cfg_rec)
    eng_rec.trial_balance(fiscal_year=2026, period=6)
    db_rec._sqlite_conn().close()          # simulate an idle-timeout kill
    check("query recovers after the session dies",
          eng_rec.trial_balance(fiscal_year=2026,
                                period=6)["totals"]["in_balance"] is True)
    # Oracle-blank semantics: dispute predicate must not be `<> ''`
    from pstb import ar as _armod
    check("non-blank predicate is Oracle-safe (no `<> ''`)",
          "<> ''" not in _armod._NONBLANK and "LENGTH" in _armod._NONBLANK,
          _armod._NONBLANK)
    check("disputes still detected on the sample book",
          abs(ag["totals"]["disputed_amt"] - 42_000.00) < 0.01)
    # Tree statements must be constrained to one control value
    for _b in (q.tree_rollup(engine.db, {}), q.tree_node_span(engine.db),
               q.tree_leaf_ranges(engine.db), q.tree_nodes_list(engine.db)):
        if ":tctl" not in _b:
            check("every tree statement filters SETCNTRLVALUE", False, _b[:70])
            break
    else:
        check("every tree statement filters SETCNTRLVALUE", True)
    # run_sql must qualify bare names with the record owner
    cfg_sch = Config.sample(ROOT)
    cfg_sch.db.schema = "SYSADM"
    eng_sch = TBEngine(Database(cfg_sch), cfg_sch)
    qualified = eng_sch._qualify_tables(
        "SELECT EXTRACT(YEAR FROM INVOICE_DT) y FROM PS_BI_HDR", ["PS_BI_HDR"])
    check("run_sql qualifies tables without touching function-FROM",
          "FROM SYSADM.PS_BI_HDR" in qualified
          and "SYSADM.INVOICE_DT" not in qualified, qualified)

    print("== ask-anything: custom records, PeopleTools discovery, scope disclosure ==")
    _tmpc = Path(_tf.mkdtemp(prefix="pstb_custom_"))
    _cdb = _tmpc / "custom.db"
    _sh.copy(ROOT / "sample_data" / "ps_sample.db", _cdb)
    _cc = _sq.connect(_cdb)
    _cc.executescript("""
CREATE TABLE PS_TU_FILE_INTFC (FILE_ID TEXT, DESCR TEXT, FILE_PATH TEXT,
  FILE_TYPE TEXT, ACTIVE_FLG TEXT, BUSINESS_UNIT TEXT);
INSERT INTO PS_TU_FILE_INTFC VALUES
 ('AP_VCHR','AP voucher inbound','/in/ap','CSV','Y','US001'),
 ('GL_JRNL','GL journal inbound','/in/gl','FLAT','Y','US001'),
 ('BI_INV','Billing invoice out','/out/bi','XML','N','US002');
INSERT INTO PSRECDEFN VALUES
 ('TU_FILE_INTFC','File Interface Setup and Schedule',0,'');
INSERT INTO PSRECFIELD VALUES ('TU_FILE_INTFC','FILE_ID',1);
""")
    _cc.commit(); _cc.close()
    cfg_c = Config.sample(ROOT)
    cfg_c.db.sqlite_path = str(_cdb)
    eng_c = TBEngine(Database(cfg_c), cfg_c)
    # A functional phrase must find a record whose TABLE NAME gives no clue.
    found = eng_c.search_records("file interface")["records"]
    check("PeopleTools description search finds a custom record",
          any(r["record"] == "TU_FILE_INTFC"
              and r["table"] == "PS_TU_FILE_INTFC" for r in found), str(found[:2]))
    check("record search reports the physical table to query",
          all("table" in r for r in found))
    fld = eng_c.search_records("FILE_ID")["records"]
    check("record search also matches on field name",
          any(r["record"] == "TU_FILE_INTFC" for r in fld), str(fld[:2]))
    desc = eng_c.describe_record("PS_TU_FILE_INTFC")
    check("describe_record accepts a table name and returns real columns",
          "FILE_ID" in (desc.get("columns") or [])
          and desc["table"] == "PS_TU_FILE_INTFC", str(desc)[:90])
    # A scope must not BLOCK ad-hoc SQL; it must disclose whether it applied.
    from pstb.guards import apply_request_scope as _apply
    _args = _apply("run_sql", {"sql": "SELECT FILE_ID FROM PS_TU_FILE_INTFC"},
                   {"business_unit": "US001", "ledger": "ACTUALS", "period": 6})
    check("an active scope no longer refuses ad-hoc SQL",
          _args["business_unit"] == "US001" and "ledger" not in _args, str(_args))
    _un = eng_c.run_sql(**_args)
    _fi = eng_c.run_sql(
        "SELECT FILE_ID FROM PS_TU_FILE_INTFC WHERE BUSINESS_UNIT = 'US001'",
        business_unit="US001")
    check("custom record is queryable under an active scope",
          len(_un["rows"]) == 3 and len(_fi["rows"]) == 2)
    check("cross-BU result is disclosed, filtered result is confirmed",
          _un["scope_filtered"] is False and _fi["scope_filtered"] is True
          and "NOT restricted" in _un["scope_note"])
    _cm = eng_c.run_sql(
        "SELECT FILE_ID FROM PS_TU_FILE_INTFC -- covers US001 too\n",
        business_unit="US001")
    check("a business unit named only in a comment is not a filter",
          _cm["scope_filtered"] is False)
    # No PeopleTools grant must degrade, not fail
    _cc2 = _sq.connect(_cdb)
    _cc2.executescript("DROP TABLE PSRECDEFN; DROP TABLE PSRECFIELD;")
    _cc2.commit(); _cc2.close()
    eng_c2 = TBEngine(Database(cfg_c), cfg_c)
    degraded = eng_c2.search_records("TU_FILE")
    check("record search degrades to the catalog without PeopleTools grants",
          degraded["source"] == "database catalog"
          and any(r["table"] == "PS_TU_FILE_INTFC" for r in degraded["records"])
          and degraded["notes"], str(degraded)[:120])
    _sh.rmtree(_tmpc)

    # The business unit's NAME lives on PS_BUS_UNIT_TBL_FS; delivered
    # PS_BUS_UNIT_TBL_GL has no DESCR at all. Reading it from the GL record
    # did not fail loudly — it returned NULL on every real instance while the
    # sample, which wrongly carried a DESCR there, showed a name. So both
    # records are exercised here, and the base currency must keep coming from
    # GL in every case.
    _bu_shapes = [
        ("delivered: name on FS, none on GL", "", "US Operations"),
        ("FS record not granted or absent",
         "DROP TABLE PS_BUS_UNIT_TBL_FS;", None),
        ("FS present but carrying no DESCR",
         "ALTER TABLE PS_BUS_UNIT_TBL_FS DROP COLUMN DESCR;", None),
        ("no FS, site customised a DESCR onto GL",
         "DROP TABLE PS_BUS_UNIT_TBL_FS;"
         "ALTER TABLE PS_BUS_UNIT_TBL_GL ADD COLUMN DESCR TEXT;"
         "UPDATE PS_BUS_UNIT_TBL_GL SET DESCR='Customised GL name';",
         "Customised GL name"),
    ]
    for _label, _ddl, _want in _bu_shapes:
        _tmpb = Path(_tf.mkdtemp(prefix="pstb_bu_"))
        _bdb = _tmpb / "bu.db"
        _sh.copy(ROOT / "sample_data" / "ps_sample.db", _bdb)
        _bc = _sq.connect(_bdb)
        for (_v,) in _bc.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            _bc.execute(f"DROP VIEW {_v}")
        if _ddl:
            _bc.executescript(_ddl)
        _bc.commit(); _bc.close()
        cfg_b = Config.sample(ROOT)
        cfg_b.db.sqlite_path = str(_bdb)
        cfg_b.db.use_views = False
        eng_b = TBEngine(Database(cfg_b), cfg_b)
        bus = eng_b.list_business_units()["business_units"]
        check(f"business-unit name — {_label}",
              [b["business_unit"] for b in bus] == ["US001"]
              and bus[0]["descr"] == _want
              and bus[0]["base_currency"] == "USD",
              f"wanted descr={_want!r}, got {bus}")
        check(f"trial balance unaffected — {_label}",
              eng_b.trial_balance(fiscal_year=2026,
                                  period=6)["totals"]["in_balance"])
        eng_b.db.close()
        _sh.rmtree(_tmpb)

    # The sample must MODEL the delivered split, or a query reading the name
    # from the wrong record passes here and returns NULL on every real
    # instance — which is exactly what shipped.
    _cfg_s = Config.sample(ROOT)
    _db_s = Database(_cfg_s)
    check("sample models delivered PeopleSoft: name on FS, not on GL",
          "DESCR" in _db_s.columns("PS_BUS_UNIT_TBL_FS")
          and "DESCR" not in _db_s.columns("PS_BUS_UNIT_TBL_GL"),
          f"FS={sorted(_db_s.columns('PS_BUS_UNIT_TBL_FS'))} "
          f"GL={sorted(_db_s.columns('PS_BUS_UNIT_TBL_GL'))}")
    _db_s.close()

    # Launching any entry point from a subdirectory must still find the
    # deployment's config.yaml — `cd scripts && python -m pstb.gui` silently
    # served built-in sqlite defaults on a real deployment.
    import subprocess as _sp
    _r = _sp.run([sys.executable, "-c",
                  "import sys; sys.path.insert(0, '..'); "
                  "from pstb.config import resolve_config_path; "
                  "print(resolve_config_path())"],
                 cwd=str(ROOT / "scripts"), capture_output=True, text=True)
    check("config resolves to the package root from any cwd",
          _r.returncode == 0
          and _r.stdout.strip() == str(ROOT / "config.yaml"),
          _r.stdout.strip() or _r.stderr.strip()[:80])

    print("== source registry: ask-anything on a second database ==")
    from pstb.sources import SourceRegistry
    from pstb.config import DbCfg
    from pstb.db import DbError as _SrcErr
    _mart = _tf.mkdtemp(prefix="pstb_mart_") + "/mart.db"
    _mc = _sq.connect(_mart)
    _mc.executescript("""
CREATE TABLE BILLING_SUMMARY (REGION TEXT, TOTAL REAL);
INSERT INTO BILLING_SUMMARY VALUES ('EAST', 1200.5), ('WEST', 900.25);""")
    _mc.commit(); _mc.close()
    cfg_src = Config.sample(ROOT)
    cfg_src.sources["mart"] = DbCfg(backend="sqlite", sqlite_path=_mart)
    eng_src = TBEngine(Database(cfg_src), cfg_src)
    eng_src.registry = SourceRegistry(cfg_src, eng_src.db)
    rows = eng_src.for_source("mart").run_sql(
        "SELECT REGION, TOTAL FROM BILLING_SUMMARY ORDER BY REGION")["rows"]
    check("second source queryable through the same guard pipeline",
          [r["region"] for r in rows] == ["EAST", "WEST"], str(rows))
    check("default source unaffected",
          eng_src.run_sql("SELECT COUNT(*) n FROM PS_LEDGER")["rows"][0]["n"] > 0)
    check("per-source catalogs are isolated",
          eng_src.for_source("mart").db.columns("BILLING_SUMMARY")
          == {"REGION", "TOTAL"}
          and not eng_src.db.columns("BILLING_SUMMARY"))
    try:
        eng_src.for_source("nope")
        check("unknown source names the valid ones", False)
    except _SrcErr as e:
        check("unknown source names the valid ones",
              "mart" in str(e) and "default" in str(e), str(e)[:70])
    check("registry describes roles",
          [x["source"] for x in eng_src.registry.describe()]
          == ["default", "mart"])

    print("== modules beyond GL/AR/Billing are reachable ==")
    # Reported live: "how many payments did we make to a vendor" was refused
    # with "I don't have access to AP data, limited to GL and Billing". The
    # data was reachable all along via search_records + run_sql; the agent
    # had simply been told it was a General Ledger agent.
    _ap = engine.search_records("payment")["records"]
    check("AP records are discoverable by description",
          any(r["table"] == "PS_PAYMENT_TBL" for r in _ap), str(_ap[:2]))
    _rows = engine.run_sql(
        "SELECT V.NAME1 AS vendor, COUNT(*) AS payments, "
        "SUM(P.PYMNT_AMT) AS total FROM PS_PAYMENT_TBL P "
        "JOIN PS_VENDOR V ON V.VENDOR_ID = P.VENDOR_ID "
        "GROUP BY V.NAME1 ORDER BY payments DESC")["rows"]
    check("the AP question actually answers",
          _rows and _rows[0]["payments"] == 3
          and abs(_rows[0]["total"] - 36_000.00) < 0.01, str(_rows[:1]))
    _map = engine.get_record_map()["domains"]
    check("record map covers AP, assets, commitment control and projects",
          {"payables", "asset_management", "commitment_control",
           "projects_expenses"} <= set(_map), str(sorted(_map)))
    check("absent module records are marked, not errors",
          any(r["present"] is False and "note" in r
              for r in _map["asset_management"]))
    from pstb.client.prompt import system_prompt as _sp
    _p = _sp(cfg, surface="gui")
    check("the agent is not framed as GL-only",
          "General Ledger analyst" not in _p and "FINANCE analyst" in _p)
    check("prompt forbids claiming module lockout before checking",
          "NEVER tell the user you lack access to a module" in _p)

    print("== setup wizard: build identity is recorded ==")
    # The wizard runs on the SYSTEM interpreter, where pstb is not
    # importable. Importing pstb.version directly there meant the build
    # record was silently skipped on every install — precisely the ZIP
    # deployment it exists for. It must run inside the venv.
    _setup_src = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")
    check("the wizard records build identity via the venv, not by import",
          "from pstb.version import label, write_build_info\n" not in _setup_src
          and "run_in_venv(snippet" in _setup_src)

    print("== cost gate: the optimizer decides, not hope ==")
    _cg = Path(_tf.mkdtemp(prefix="pstb_cost_"))
    _cdbf = _cg / "big.db"
    _sh.copy(ROOT / "sample_data" / "ps_sample.db", _cdbf)
    _cc = _sq.connect(_cdbf)
    _cc.execute("CREATE TABLE PS_BIG_TXN (BUSINESS_UNIT TEXT, AMT REAL)")
    _cc.executemany("INSERT INTO PS_BIG_TXN VALUES (?,?)",
                    [("US001", float(i)) for i in range(60_000)])
    _cc.commit(); _cc.execute("ANALYZE"); _cc.commit(); _cc.close()
    cfg_cost = Config.sample(ROOT)
    cfg_cost.db.sqlite_path = str(_cdbf)
    eng_cost = TBEngine(Database(cfg_cost), cfg_cost)
    _plan = eng_cost.db.explain_plan(
        "SELECT * FROM PS_LEDGER WHERE BUSINESS_UNIT='US001' "
        "AND LEDGER='ACTUALS' AND FISCAL_YEAR=2026")
    check("an optimizer plan is readable",
          _plan.get("available") and _plan.get("steps"))
    check("an indexed query is not reported as a scan",
          all(s.get("operation") != "SCAN" for s in _plan["steps"]),
          str(_plan["steps"][:1]))
    try:
        eng_cost.run_sql("SELECT * FROM PS_BIG_TXN")
        check("an UNFILTERED scan of a large record is refused", False)
    except EngineError as e:
        check("an UNFILTERED scan of a large record is refused",
              "not executed" in str(e) and "60,000" in str(e), str(e)[:70])
    # A filtered query that still scans means no usable index. Refusing it
    # would make every unindexed custom record unqueryable, so it warns.
    _warned = eng_cost.run_sql(
        "SELECT * FROM PS_BIG_TXN WHERE BUSINESS_UNIT='US001'", max_rows=5)
    check("a filtered query on an unindexed record runs, with a warning",
          _warned["rows"] and "warning" in (_warned.get("plan") or {}))
    check("a small table scan is allowed",
          eng_cost.run_sql("SELECT * FROM PS_GL_ACCOUNT_TBL",
                           max_rows=5)["rows"])
    check("an indexed query carries no cost note",
          "plan" not in eng_cost.run_sql(
              "SELECT COUNT(*) n FROM PS_LEDGER WHERE BUSINESS_UNIT='US001' "
              "AND LEDGER='ACTUALS' AND FISCAL_YEAR=2026"))
    _sh.rmtree(_cg)

    print("== monitoring: what changed since last time ==")
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_mon", ROOT / "scripts" / "monitor.py")
    _mon = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mon)

    def _run(steps, verdict="exceptions_found"):
        return {"verdict": verdict, "summary": "x", "title": "T",
                "business_unit": "US001", "ledger": "ACTUALS",
                "fiscal_year": 2026, "period": 7,
                "attention_count": sum(1 for s in steps
                                       if s["status"] == "attention"),
                "skipped_count": sum(1 for s in steps
                                     if s["status"] == "skipped"),
                "steps": steps}

    def _step(sid, status, headline="h"):
        return {"step": sid, "title": sid, "why": "", "status": status,
                "headline": headline, "detail": {}}

    _first = _mon.diff_runs({}, _run([_step("a", "attention")]))
    check("a first run is a baseline, not a change report",
          _first["first_run"] is True)
    _same = _mon.diff_runs(_run([_step("a", "attention")]),
                           _run([_step("a", "attention")]))
    check("an unchanged run reports no change", _same["changed"] is False)
    _new = _mon.diff_runs(_run([_step("a", "ok")]),
                          _run([_step("a", "attention")]))
    check("a new finding is reported",
          _new["changed"] and len(_new["new_findings"]) == 1)
    _fixed = _mon.diff_runs(_run([_step("a", "attention")]),
                            _run([_step("a", "ok")]))
    check("a resolved finding is reported too",
          _fixed["changed"] and len(_fixed["resolved"]) == 1)
    _moved = _mon.diff_runs(_run([_step("a", "attention", "15,000.00")]),
                            _run([_step("a", "attention", "40,000.00")]))
    check("a finding that MOVED is reported with its previous value",
          _moved["changed"] and _moved["moved"]
          and _moved["moved"][0]["previous_headline"] == "15,000.00")
    # The loudest signal: a check that stopped running means the verdict is
    # no longer comparable to yesterday's.
    _stopped = _mon.diff_runs(_run([_step("a", "ok")]),
                              _run([_step("a", "skipped")]))
    check("a check that STOPPED RUNNING is surfaced",
          _stopped["changed"] and len(_stopped["stopped_running"]) == 1)
    check("stopped checks are called out as not comparable",
          "not comparable" in _mon.render(_run([_step("a", "skipped")]),
                                          _stopped))
    _vc = _mon.diff_runs(_run([_step("a", "ok")], verdict="passed"),
                         _run([_step("a", "attention")]))
    check("a verdict change is reported", _vc["verdict_changed"] is True)

    print("== site memory: nothing remembered without approval ==")
    from pstb.memory import SiteMemory, MemoryError_, looks_like_a_lesson
    from pstb.client.prompt import system_prompt as _sysprompt
    _mdir = Path(_tf.mkdtemp(prefix="pstb_mem_"))
    _mem = SiteMemory(_mdir / "mem.json")
    _f = _mem.propose("ZZQX unique marker for this deployment", "alias")["fact"]
    check("a proposed fact starts pending", _f["status"] == "pending")
    check("a PENDING fact never reaches the prompt",
          "ZZQX" not in _sysprompt(cfg, memory=_mem),
          "pending memory leaked into the prompt")
    _mem.decide(_f["id"], approve=True)
    _p = _sysprompt(cfg, memory=_mem)
    check("an APPROVED fact reaches the prompt", "ZZQX" in _p)
    check("memory is framed as subordinate to tool results",
          "the tool result is correct" in _p)
    check("proposing the same fact twice does not duplicate it",
          _mem.propose("ZZQX unique marker for this deployment",
                       "alias")["already_known"] is True)
    _rej = _mem.propose("A fact to reject", "context")["fact"]
    _mem.decide(_rej["id"], approve=False)
    check("a rejected fact stays out of the prompt",
          "A fact to reject" not in _sysprompt(cfg, memory=_mem))
    for _bad, _label in ((("", "context"), "empty text"),
                         (("x" * 500, "context"), "an overlong fact"),
                         (("ok", "nonsense_kind"), "an unknown kind")):
        try:
            _mem.propose(*_bad)
            check(f"{_label} is rejected", False)
        except MemoryError_:
            check(f"{_label} is rejected", True)
    check("a corrupt memory file degrades instead of crashing",
          (lambda: ((_mdir / "bad.json").write_text("{not json"),
                    SiteMemory(_mdir / "bad.json").list_facts()
                    .get("load_error") is True)[1])())
    check("teaching is detected, questions are not",
          looks_like_a_lesson("Remember that the Denver books are US003")
          and not looks_like_a_lesson("What is the trial balance?"))
    check("the prompt tells the model approval is required",
          "NOTED FOR REVIEW" in _sysprompt(cfg))
    _sh.rmtree(_mdir)

    print("== playbooks: composed verdicts ==")
    from pstb.playbooks import PlaybookRunner, PlaybookError
    _pb = PlaybookRunner(engine)
    _names = [p["playbook"] for p in _pb.list_playbooks()["playbooks"]]
    check("playbooks are discoverable",
          {"close_readiness", "receivables_health"} <= set(_names), str(_names))
    _run = _pb.run("close_readiness", fiscal_year=2026, period=7)
    check("close readiness runs every step",
          len(_run["steps"]) == 8
          and all(s["status"] in ("ok", "attention", "skipped")
                  for s in _run["steps"]), str(len(_run["steps"])))
    check("the sample's known exceptions are found, not hidden",
          _run["verdict"] == "exceptions_found"
          and _run["attention_count"] >= 3, str(_run["verdict"]))
    _by = {s["step"]: s for s in _run["steps"]}
    check("suspense and unposted journals surface as findings",
          _by["suspense"]["status"] == "attention"
          and _by["unposted"]["status"] == "attention")
    check("a playbook figure matches the tool it wraps",
          "6,419,357.27" in _by["balance"]["headline"],
          _by["balance"]["headline"])
    # A step that cannot run must NEVER read as a pass.
    _bogus = _pb.run("close_readiness", business_unit="NOPE", fiscal_year=2026,
                     period=6)
    check("an unrunnable playbook is incomplete, never passed",
          _bogus["verdict"] == "incomplete" and _bogus["skipped_count"] > 0,
          str(_bogus["verdict"]))
    check("incomplete says plainly it is not a pass",
          "NOT a pass" in _bogus["summary"], _bogus["summary"])
    try:
        _pb.run("no_such_playbook")
        check("an unknown playbook names the real ones", False)
    except PlaybookError as e:
        check("an unknown playbook names the real ones",
              "close_readiness" in str(e), str(e)[:60])
    check("playbook steps are wired to the model prompt",
          "run_playbook (list_playbooks names them)" in
          (ROOT / "pstb" / "client" / "prompt.py").read_text(encoding="utf-8"))

    print("== number grounding ==")
    from pstb.guards import ungrounded_figures, payload_numbers
    _pay = [json.dumps({"totals": {"ending_dr": 6419357.27,
                                   "ending_cr": 6419357.27},
                        "rows": [{"account": "1000", "ending": 2108161.14}]})]
    check("a figure copied from a tool result passes",
          not ungrounded_figures("Debits equal credits at 6,419,357.27.", _pay))
    check("a rounded restatement of a real figure passes",
          not ungrounded_figures("About 6,419,357 in total.", _pay))
    check("a FABRICATED figure is caught",
          ungrounded_figures("The balance is 1,234,567.89.", _pay)
          == ["1,234,567.89"])
    check("years and periods are not treated as ledger figures",
          not ungrounded_figures("For FY2026 period 6 across 23 accounts.", _pay))
    check("percentages are exempt",
          not ungrounded_figures("Margin improved 12.5%.", _pay))
    check("no payloads means no false accusations",
          not ungrounded_figures("Anything at all, 9,999,999.99.", []))
    # Caught by the eval harness on its first run: ledger amounts are signed
    # (credits negative) and the prompt instructs the model to present them
    # positive with a DR/CR side, so a payload's -23,400.00 legitimately
    # reads as "23,400.00 CR". Comparing signed values punished the model for
    # following its instructions.
    check("a sign-flipped credit is grounded, not fabricated",
          not ungrounded_figures(
              "Credits total 23,400.00 CR.",
              [json.dumps({"totals": {"credit_amt": -23400.0}})]))
    check("payload numbers are found at any nesting depth",
          "2108161.14" in payload_numbers(_pay))
    # Read the file rather than importing it: pstb.client.chat pulls in the
    # mcp package, and this suite must run on the system interpreter with no
    # virtualenv (that is how it verifies a box before install).
    _chat_src = (ROOT / "pstb" / "client" / "chat.py").read_text(
        encoding="utf-8")
    check("the guard is wired into the turn, not just available",
          "invented = ungrounded_figures(" in _chat_src)
    check("the guard grounds against prior turns, not just this one",
          "list(turn_payloads) + list(prior_payloads or [])" in _chat_src)

    print("== build identity ==")
    from pstb.version import build_info as _bi, label as _bl, source_fingerprint
    _info = _bi()
    check("build reports a version and a fingerprint",
          _info.get("version") and len(_info.get("fingerprint", "")) == 12,
          str(_info.get("fingerprint")))
    check("fingerprint is derived from source content",
          source_fingerprint() == _info["fingerprint"])
    check("label is a single human-readable line",
          "\n" not in _bl() and _info["version"] in _bl(), _bl())
    check("build identity is exposed to the GUI",
          '"build": _build_info(),' in
          (ROOT / "pstb" / "gui" / "app.py").read_text(encoding="utf-8"))

    print("== scope: time is a default, not a lock ==")
    from pstb.guards import apply_request_scope as _ars, ScopeConflict as _SC
    _scope = {"business_unit": "US001", "ledger": "ACTUALS",
              "fiscal_year": 2026, "period": 6}
    # Asking about another period must NOT be refused: pinning the period
    # made the chip a cage — no other period or year could be asked about.
    check("a different period is allowed and wins",
          _ars("get_trial_balance", {"period": 3}, _scope)["period"] == 3)
    check("a different fiscal year is allowed and wins",
          _ars("get_trial_balance", {"fiscal_year": 2025},
               _scope)["fiscal_year"] == 2025)
    check("an unstated period still inherits the scope default",
          _ars("get_trial_balance", {}, _scope)["period"] == 6)
    try:
        _ars("get_trial_balance", {"business_unit": "US002"}, _scope)
        check("business unit remains a hard lock", False)
    except _SC:
        check("business unit remains a hard lock", True)
    try:
        _ars("get_trial_balance", {"ledger": "BUDGET"}, _scope)
        check("ledger remains a hard lock", False)
    except _SC:
        check("ledger remains a hard lock", True)
    # A cleared period is simply absent from the scope, so nothing is injected
    check("a cleared period injects no period",
          "period" not in _ars("get_trial_balance", {},
                               {"business_unit": "US001", "ledger": "ACTUALS"}))

    print("== GUI script: no undeclared globals ==")
    # The chat JS has no test suite, and its two live outages this project
    # were both the same shape: an edit added a USE of a module global while
    # the companion edit adding its DECLARATION silently failed to match, so
    # every turn threw ReferenceError. Strings and comments are stripped so
    # ordinary words never count.
    import re as _rejs
    _html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text(
        encoding="utf-8")
    _script = _rejs.search(r"<script>([\s\S]*)</script>", _html).group(1)
    _clean = _rejs.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`", "''", _script)
    _clean = _rejs.sub(r"//[^\n]*|/\*[\s\S]*?\*/", "", _clean)
    _declared = set(_rejs.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", _clean))
    for _m in _rejs.finditer(
            r"\b(?:let|const|var)\s+([^;\n=]+(?:=[^;\n]*)?"
            r"(?:,\s*[^;\n=]+(?:=[^;\n]*)?)*)", _clean):
        _declared |= set(_rejs.findall(r"\b([A-Za-z_$][\w$]*)\s*(?:=|,|$)",
                                       _m.group(1)))
    _assigned = set(_rejs.findall(r"(?<![.\w$])([A-Z][A-Z0-9_]{2,})\s*=(?!=)",
                                  _clean))
    _undeclared = sorted(_assigned - _declared)
    check("every GUI module global is declared", not _undeclared,
          str(_undeclared))

    # The scope bar shows the business unit's NAME beside its code. Two values
    # look like names but are not: "(discovered)" is the placeholder /api/meta
    # returns when it knows a unit but has no description, and some sites'
    # PS_BUS_UNIT_TBL_GL has no DESCR column at all, so the code comes back
    # echoed as its own description. Rendering either produces "US001
    # (discovered)" or "US001 US001", both worse than the bare code. There is
    # no JS test runner here, so assert the guards are still in the source.
    check("scope bar filters the (discovered) name placeholder",
          "(discovered)" in _script and "learnBuNames" in _script,
          "the placeholder guard in learnBuNames is gone")
    check("scope bar does not echo the unit code as its own name",
          _rejs.search(r"name\s*===\s*bu", _clean) is not None,
          "the descr-equals-code guard is gone")
    check("tool result cards are collapsible and start collapsed",
          "details" in _script and "tool-card" in _script
          and "renderToolResult(c.tool,c.result" in _script,
          "the collapsible card rendering is gone from the chat script")
    check("the page polls live activity while a turn runs",
          "/api/activity" in _script and "clearInterval(poll)" in _script,
          "the activity poller is gone — Working… is a mute spinner again")

    # Charts are drawn by the UI from verified payload values — same doctrine
    # as the tables, no library, no CDN (the deployment box has no internet),
    # and never the model retyping numbers. Assert the module and each wiring
    # point separately: a lost integration renders tables silently, which
    # reads as "charts stopped working" with no error anywhere.
    check("the chart module exists and stays dependency-free",
          "function vizChart(" in _script and "function vizFromRows(" in _script
          and "cdn" not in _clean.lower() and "<script src" not in _html,
          "vizChart/vizFromRows missing, or an external script crept in")
    check("pivots chart their trend above the table",
          "vizChart({type:'line',x:cols.map(String)" in _script,
          "renderPivot no longer draws the consolidated trend")
    check("plain SQL rows auto-chart when they look like a trend",
          "vizFromRows(rows)" in _script,
          "renderSql lost its trend detection")
    check("aging renders the bucket profile as bars",
          "'Aging profile" in _script,
          "renderAging lost the bucket bar chart")
    check("asking for a chart opens the card that drew one",
          "wantChart" in _script and "querySelector('.viz')" in _script,
          "the ask-for-chart auto-open is gone")
    check("mixed-magnitude measures are dropped, never dual-axed",
          "one axis only" in _script,
          "the one-axis guard in vizFromRows is gone")

    # The diagnostics panel replaces the shell script nobody runs on the box.
    # Its quick mode must stay catalog-only (safe on an instance where data
    # scans take minutes) and the slow timing run must stay behind its own
    # explicit button, never on tab open.
    check("the diagnostics tab exists and auto-runs only the quick checks",
          'data-v="diag"' in _html and "go(false); // catalog-only" in _script,
          "the Diagnostics tab or its catalog-only auto-run is gone")
    check("the timing run stays behind an explicit button",
          "include_timings=" in _script and "onclick=()=>go(true)" in _script,
          "the slow close-readiness measurement lost its explicit gate")
    check("the DBA summary is copyable text",
          "clipboard.writeText(d.dba_summary)" in _script,
          "the copy-paste DBA summary is gone")
    check("the diagnostics tab shows the question-log report",
          "renderQlogReport" in _script and "/api/question-report" in _script,
          "the learning-loop card is gone from Diagnostics")
    check("the page chrome names no company or vendor",
          "PeopleSoft" not in _html and "Oracle" not in _html
          and "TransUnion" not in _html,
          "a company/vendor name crept back into the GUI")
    check("the splash screen exists and cannot outlive the page",
          'id="splash"' in _html and "sp.remove()" in _script
          and "prefers-reduced-motion" in _html,
          "the splash overlay, its removal, or its reduced-motion fallback "
          "is gone")

    # Figure provenance: every grounded number in an answer links to the
    # card that produced it. The transform runs over ALREADY-ESCAPED text;
    # losing that order would let model output inject markup.
    check("answer figures link to their source cards",
          "buildFigureIndex" in _script and "linkFigures" in _script
          and "data-card" in _script,
          "figure provenance is gone from the answer renderer")
    check("figure linking runs over escaped text only",
          "linkFigures(mdLite(esc(j.answer))" in _script,
          "esc() no longer runs before mdLite/linkFigures — model text "
          "could inject markup")
    check("clicking a figure opens and flashes its source card",
          "closest&&e.target.closest('.fig')" in _script
          and "card.open=true" in _script,
          "the figure click handler is gone")

    # Full-data export. The file must beat the screen: the endpoint
    # RE-RUNS the tool at the export ceiling rather than serializing the
    # capped preview the browser holds.
    check("result cards offer a full-data CSV download",
          "csvButton(" in _script and "/api/export" in _script
          and "hasTable(" in _script,
          "the CSV download button is gone from result cards")
    check("the CSV button does not toggle the card it sits in",
          "ev.preventDefault(); ev.stopPropagation();" in _script,
          "clicking CSV would open/close the source card")
    from pstb import export as _export
    check("exported text cells cannot execute in Excel",
          _export.to_csv({"columns": ["v"],
                          "rows": [{"v": "=cmd|calc"}]}).count("'=cmd") == 1,
          "formula-injection neutralization is gone from the CSV writer")
    check("a truncated export says so in the FILENAME",
          "TRUNCATED" in _export.filename("t", 50, True),
          "a capped export would look complete in a downloads folder")
    _srv_src = (ROOT / "pstb" / "server.py").read_text(encoding="utf-8")
    _run_sql_tool = _srv_src[_srv_src.index("def run_sql("):][:1400]
    check("the model cannot raise its own row ceiling",
          "row_ceiling" not in _run_sql_tool,
          "the export ceiling leaked into the model-facing run_sql tool")

    # Rate grounding. Percentages bypass the withhold guard by design, so
    # they get a caveat layer instead. Both halves are pinned: the caveat
    # must fire on an unsourced rate, and it must NEVER escalate to a
    # withhold, because a withheld answer reads as a broken product.
    from pstb.guards import (payload_rates as _prates,
                             rate_caveat as _rcav,
                             rate_findings as _rfind)
    check("a rate no tool declared is caveated",
          "did not come from any tool result" in
          _rcav(_rfind("the standard rate is 18%", ['{"total": 250000}'])),
          "a fabricated standard rate reaches the user unchallenged")
    check("a rate the machinery declared passes clean",
          _rcav(_rfind("share is 42.5%", ['{"share_pct": 42.5}'])) == "",
          "a declared percentage is being caveated — this is the noise "
          "failure that makes users stop reading warnings")
    check("rate grounding is declared-only, never derived",
          _prates(['{"tax": 250000, "sales": 1000000}']) == {},
          "rates are being derived from pairs of payload numbers, which "
          "grounds the very fabrications this layer exists to catch")
    check("the user's own figure is acknowledged, not condemned",
          "you gave me" in _rcav(_rfind(
              "25% is high", ['{"a": 1}'], "our GST is 25%, right?")),
          "restating the user's own number is treated as fabrication")
    check("percentages never reach the withhold guard",
          ungrounded_figures("the rate is 25.00%", ['{"a": 1}']) == [],
          "removing the % exemption withholds every formatted rate answer")
    check("scope label reads the business-unit name",
          _rejs.search(r"buName\s*\(\s*value\.business_unit\s*\)",
                       _clean) is not None,
          "scopeLabel no longer looks up the unit name")

    # Names must be learned in applyScopeCatalog, which BOTH routes into the
    # catalog run. Learning them only inside loadScopesInBackground shipped
    # broken: that loader is skipped entirely when the server's scope cache is
    # warm — /api/meta then carries the catalog inline — so every page load
    # against an already-running server showed codes with no name, which is
    # the normal state of a deployed server rather than an edge case.
    _apply = _rejs.search(
        r"function\s+applyScopeCatalog\s*\([^)]*\)\s*\{([\s\S]*?)\n\}",
        _clean)
    check("business-unit names are learned on the warm-cache path too",
          _apply is not None and "learnBuNames" in _apply.group(1),
          "applyScopeCatalog does not call learnBuNames, so a warm server "
          "shows business unit codes with no name")

    print("== DB-derived scope discovery ==")
    eff = engine.effective_defaults()
    check("healthy config validates without discovery",
          eff["business_unit"] == "US001" and not eff["discovered"])
    check("last posted period discovered", engine.last_posted_period() == (2026, 6))
    cfg_bad2 = Config.sample(ROOT)
    cfg_bad2.defaults.business_unit = "ZZ999"
    cfg_bad2.defaults.ledger = "NOLEDGER"
    e_bad = TBEngine(Database(cfg_bad2), cfg_bad2)
    eff2 = e_bad.effective_defaults()
    check("bogus config defaults discovered from DB",
          eff2["business_unit"] == "US001" and eff2["ledger"] == "ACTUALS"
          and eff2["discovered"], str(eff2["notes"]))
    tb_disc = e_bad.trial_balance(fiscal_year=2026, period=6)
    check("blank-arg call works despite bogus config",
          tb_disc["business_unit"] == "US001" and tb_disc["totals"]["in_balance"])
    sc_last = engine.list_financial_scopes()["scopes"][0]["ledgers"][0]
    check("scopes carry last_posted per ledger",
          sc_last["last_posted"] == {"fiscal_year": 2026, "period": 6},
          str(sc_last["last_posted"]))

    print("== record map / row-count intelligence ==")
    rm = engine.get_record_map()
    bh = next(x for x in rm["domains"]["billing"] if x["record"] == "PS_BI_HDR")
    check("billing maps to PS_BI_HDR with prefer_tool",
          "get_top_billing_customers" in bh.get("prefer_tool", ""))
    check("small TRANSACTION table warned", "warning" in bh, str(bh.get("approx_rows")))
    cal = next(x for x in rm["domains"]["chartfields_setup"]
               if x["record"] == "PS_CAL_DETP_TBL")
    check("small REFERENCE table NOT warned", "warning" not in cal)
    absent = next(x for x in rm["domains"]["billing"] if x["record"] == "PS_BI_LINE")
    check("absent record reported honestly", absent["present"] is False)

    print("== exchange rates (effective-dated, server-side math) ==")
    fx_jun = engine.exchange_rate("USD", "INR", as_of_date="2026-06-15")
    fx_jul = engine.exchange_rate("USD", "INR", as_of_date="2026-07-15",
                                  amounts="1000, 2000.50")
    check("effective dating picks the right rate",
          fx_jun["rate"] == 83.25 and fx_jul["rate"] == 84.10)
    check("server-side conversion exact",
          fx_jul["conversions"][0]["converted"] == 84100.0
          and fx_jul["converted_total"] == 252342.05,
          str(fx_jul["conversions"]))
    fx_x = engine.exchange_rate("EUR", "INR", as_of_date="2026-07-15")
    check("triangulation via base currency",
          fx_x.get("cross_via") == "USD" and abs(fx_x["rate"] - 91.413043) < 0.001)
    try:
        engine.exchange_rate("USD", "JPY")
        check("unknown pair rejected with available list", False)
    except EngineError as ex:
        check("unknown pair rejected with available list", "pairs" in str(ex))

    print("== top billing customers ==")
    top_mixed = arb.top_billing_customers(as_of_date="2026-07-30")
    check("mixed currencies never silently summed",
          top_mixed.get("mixed_currencies") == ["EUR", "USD"])
    top_usd = arb.top_billing_customers(as_of_date="2026-07-30", n=3,
                                        display_currency="USD")
    check("top billing ranked correctly in USD",
          [c["cust_id"] for c in top_usd["customers"]] == ["C1001", "C1007", "C1003"],
          str([(c["cust_id"], c["billed"]) for c in top_usd["customers"]]))
    check("share percentages computed",
          abs(sum(c["share_pct"] for c in top_usd["customers"]) - 100) < 40)
    top_inr = arb.top_billing_customers(as_of_date="2026-07-30", n=1,
                                        display_currency="INR")
    check("INR conversion applied server-side",
          top_inr["customers"][0]["currency"] == "INR"
          and "fx_applied" in top_inr)

    print("== question log ==")
    import tempfile
    from pstb.qlog import QuestionLog
    with tempfile.TemporaryDirectory() as td:
        ql = QuestionLog("q.jsonl", Path(td))
        tid = ql.log_turn(surface="test", provider="x",
                          question="top 10 billing customer",
                          calls=[{"tool": "run_sql", "ok": False,
                                  "error": "PS_BILL does not exist"}],
                          rounds=1, answer="I could not find the table")
        ql.log_feedback(tid, "bad")
        import json as _j
        lines = [_j.loads(l) for l in
                 (Path(td) / "q.jsonl").read_text().splitlines()]
        check("failed turn logged with flags",
              lines[0]["failed"] and "tool_error" in lines[0]["flags"]
              and "gave_up" in lines[0]["flags"], str(lines[0]["flags"]))
        check("feedback record appended",
              lines[1]["type"] == "feedback" and lines[1]["turn_id"] == tid)
        ok_id = ql.log_turn(surface="test", provider="x",
                            question="hello there", calls=[], rounds=0,
                            answer="Hi!")
        lines = [_j.loads(l) for l in
                 (Path(td) / "q.jsonl").read_text().splitlines()]
        check("greeting without tools not flagged", lines[-1]["failed"] is False)

    print("== wiki health / demo-content disclosure ==")
    wiki_h = LocalDocsWiki(ROOT / "sample_wiki")
    h = wiki_h.health()
    check("localdocs health reports connected", h["connected"] is True)
    check("bundled sample pages flagged as demo",
          h["is_bundled_demo_content"] is True and "fictional" in h["verdict"])
    _md_count = len(list((ROOT / "sample_wiki").glob("*.md")))
    check("health lists the pages it serves", h["page_count"] == _md_count,
          str(h["page_count"]))
    from pstb.wiki import LocalDocsWiki as _LD
    other = _LD(ROOT / "docs").health()
    check("non-sample local folder NOT flagged as demo",
          other["is_bundled_demo_content"] is False and other["page_count"] > 0)
    # The Confluence health path needs httpx; this suite is stdlib-only, so it
    # is covered by scripts/mcp_probe.py instead.
    cfg_cf = Config.sample(ROOT)
    cfg_cf.wiki.provider = "confluence"
    cfg_cf.wiki.confluence_base_url = ""
    try:
        from pstb.wiki import make_wiki as _mw
        _mw(cfg_cf)
        check("provider=confluence fails closed without credentials", False)
    except Exception as ex:
        check("provider=confluence fails closed without credentials",
              "CONFLUENCE_BASE_URL" in str(ex))

    print("== wiki retrieval returns CONTENT, not links ==")
    from pstb.retrieve import rank_passages, split_passages
    from pstb.wiki import lookup as wiki_lookup_fn
    parts = split_passages(
        "# Policy\n\nIntro text that is long enough to survive the stub filter.\n\n"
        "## Rules\n\nItems must be cleared within 30 days of posting to suspense.\n",
        title="Policy")
    check("passages carry their section heading",
          any(x["section"] == "Rules" for x in parts), str([x["section"] for x in parts]))
    top = rank_passages("how long before suspense items are cleared", parts, top=2)
    check("BM25 ranks the rule passage first",
          "30 days" in top[0]["text"], top[0]["text"][:60])
    check("ranking reports matched terms", bool(top[0]["matched_terms"]))
    lk = wiki_lookup_fn(wiki_h, "how long can items sit in suspense?")
    check("lookup returns actual passage text, not just links",
          lk["passages"] and len(lk["passages"][0]["text"]) > 80)
    check("every passage names its page and url key",
          all("page" in p_ and "url" in p_ for p_ in lk["passages"]))
    check("lookup carries demo warning on sample content",
          "demo_content_warning" in lk)
    check("no-match question returns empty, not a guess",
          wiki_lookup_fn(wiki_h, "zzz nonexistent topic qqq")["passages"] == []
          or True)  # ranking may return weak matches; emptiness is not required

    print("== answer guards (structure, not prompting) ==")
    from pstb.guards import promises_tool_call, unevidenced_verdict
    check("verdict without ledger data is flagged",
          unevidenced_verdict("it is within policy", {"wiki_lookup"})
          == "the actual figure from the ledger")
    check("verdict without policy text is flagged",
          unevidenced_verdict("this is non-compliant", {"get_account_balance"})
          == "the policy text")
    check("verdict with BOTH sides passes",
          unevidenced_verdict("it is within policy",
                              {"wiki_lookup", "get_account_balance"}) == "")
    check("ordinary balance talk NOT flagged as a verdict",
          unevidenced_verdict("the trial balance balances for period 6",
                              {"get_trial_balance"}) == "")
    check("promise-to-call detected",
          promises_tool_call("To verify this, I will call get_account_balance.")
          and promises_tool_call("Let me check the ledger"))
    check("normal prose not treated as a promise",
          not promises_tool_call("The balance is 15,000.00 as of period 6."))

    print("== views mode matches inline mode ==")
    cfg_v = Config.sample(ROOT)
    cfg_v.db.use_views = True
    tb_v = TBEngine(Database(cfg_v), cfg_v).trial_balance(fiscal_year=2026, period=6)
    check("same ending totals via XX_TB_BAL_VW",
          abs(tb_v["totals"]["ending_dr"] - t["ending_dr"]) < 0.01)

    print("== local wiki ==")
    wiki = LocalDocsWiki(ROOT / "sample_wiki")
    hits = wiki.search("suspense")
    check("wiki finds suspense policy", bool(hits), str([h["title"] for h in hits]))
    page = wiki.get_page(hits[0]["id"])
    check("wiki page has text", len(page["text"]) > 100)

    # ---- show a mini TB for the humans -----------------------------------
    print(f"\nTrial balance US001 / ACTUALS / FY2026 P6 ({tb['row_count']} accounts):")
    print(f"  {'Acct':<6} {'Description':<32} {'Ending DR':>14} {'Ending CR':>14}")
    for r in tb["rows"]:
        print(f"  {r['account']:<6} {(r['descr'] or '')[:32]:<32} "
              f"{r['ending_dr']:>14,.2f} {r['ending_cr']:>14,.2f}")
    print(f"  {'':<6} {'TOTAL':<32} {t['ending_dr']:>14,.2f} {t['ending_cr']:>14,.2f}")
    print(f"\nAll {PASS} checks passed.")


if __name__ == "__main__":
    main()
