#!/usr/bin/env python3
"""Stdlib-only smoke test: exercises the TB engine and wiki against the SQLite
sample directly (no MCP / LLM packages required).

Run after seeding:  python3 scripts/seed_sample_data.py && python3 scripts/smoke_test.py
"""
from __future__ import annotations

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
    check("base-currency rows only", "CURRENCY_CD = L.BASE_CURRENCY" in _sql)
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
