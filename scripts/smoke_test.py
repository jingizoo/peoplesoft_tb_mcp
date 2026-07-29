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
