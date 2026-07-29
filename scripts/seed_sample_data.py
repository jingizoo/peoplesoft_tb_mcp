#!/usr/bin/env python3
"""Build the SQLite sample PeopleSoft GL (sample_data/ps_sample.db).

Design goals:
  - Shapes match real PS records (PS_LEDGER, PS_JRNL_HEADER/LN, PS_GL_ACCOUNT_TBL,
    PS_CAL_DETP_TBL, PS_SET_CNTRL_REC, PSTREE*), so queries port to Oracle unchanged.
  - PS_LEDGER is built BY AGGREGATING the posted journals, so drill-to-journal
    ties to ledger activity to the penny.
  - FY2025 is a full year (incl. adjustment period 998) closed into FY2026
    period 0 — so retained-earnings roll checks pass.
  - Deliberate demo artifacts: suspense balance in FY2026 P5, an unposted July
    journal, a travel spike in P4, an effective-dated account rename.

Stdlib only. Deterministic — no randomness.
"""
from __future__ import annotations

import calendar
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "sample_data" / "ps_sample.db"

BU, LEDGER, SETID, CAL_ID, CURR = "US001", "ACTUALS", "SHARE", "01", "USD"

# (account, type, description)  A=Asset L=Liability Q=Equity R=Revenue E=Expense
ACCOUNTS = [
    ("1000", "A", "Cash - Operating"),
    ("1010", "A", "Cash - Payroll"),
    ("1100", "A", "Accounts Receivable"),
    ("1200", "A", "Inventory"),
    ("1500", "A", "Fixed Assets - Gross"),
    ("1590", "A", "Accumulated Depreciation"),
    ("1999", "A", "Suspense - Unidentified Receipts"),
    ("2000", "L", "Accounts Payable"),
    ("2100", "L", "Accrued Liabilities"),
    ("2200", "L", "Payroll Withholding Payable"),
    ("2500", "L", "Long-Term Debt"),
    ("3000", "Q", "Common Stock"),
    ("3500", "Q", "Retained Earnings"),
    ("4000", "R", "Product Revenue"),
    ("4100", "R", "Service Revenue"),
    ("5000", "E", "Cost of Goods Sold"),
    ("6000", "E", "Salaries & Wages"),
    ("6100", "E", "Employee Benefits"),
    ("6200", "E", "Rent Expense"),
    ("6300", "E", "Utilities Expense"),
    ("6400", "E", "Travel & Entertainment"),
    ("6500", "E", "Depreciation Expense"),
    ("6900", "E", "Miscellaneous Expense"),
]
ATYPE = {a: t for a, t, _ in ACCOUNTS}
RE_ACCT = "3500"

DEPTS = [("10000", "Corporate"), ("20000", "Sales"), ("30000", "Operations")]

# FY2025 opening balances (period 0), all in dept 10000. Sums to zero.
OPENING_2025 = {
    "1000": 500_000.00, "1010": 20_000.00, "1100": 180_000.00, "1200": 240_000.00,
    "1500": 900_000.00, "1590": -300_000.00,
    "2000": -160_000.00, "2100": -45_000.00, "2200": -26_000.00, "2500": -600_000.00,
    "3000": -400_000.00, "3500": -309_000.00,
}


def r2(x: float) -> float:
    return round(x, 2)


def month_end(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


class Seeder:
    def __init__(self) -> None:
        self.headers: list[tuple] = []
        self.lines: list[tuple] = []
        self.ledger: dict[tuple, float] = defaultdict(float)
        self.seq = 0

    def jrnl(self, fy, per, pfx, source, oprid, descr, jlines, date=None, status="P"):
        """jlines: [(account, dept, amount, line_descr)] — must net to ~zero."""
        self.seq += 1
        jid = f"{pfx}{fy % 100:02d}{min(per, 99):02d}{self.seq:03d}"[:10]
        jdate = date or month_end(fy, min(per, 12))
        total = sum(a for _, _, a, _ in jlines)
        assert abs(total) < 0.005, f"unbalanced journal {jid}: {total}"
        posted = jdate if status == "P" else None
        self.headers.append(
            (BU, jid, jdate, 0, status, fy, per, source, oprid, posted, descr, LEDGER, CURR)
        )
        for i, (acct, dept, amt, ldescr) in enumerate(jlines, start=1):
            self.lines.append(
                (BU, jid, jdate, 0, i, LEDGER, acct, dept, "", "", "", CURR,
                 r2(amt), r2(amt), CURR, ldescr, "")
            )
            if status == "P":
                self.ledger[(fy, per, acct, dept)] += r2(amt)

    # ------------------------------------------------------------- generators
    def month(self, fy: int, m: int, s: float) -> None:
        g = 1.03 ** (m - 1)
        tag = f"{fy}-{m:02d}"

        prod, svc = r2(250_000 * s * g), r2(90_000 * s * g)
        self.jrnl(fy, m, "REV", "BIL", "GLBATCH", f"Billing revenue {tag}", [
            ("1100", "10000", r2(prod + svc), "AR from billing"),
            ("4000", "20000", -prod, "Product revenue"),
            ("4100", "20000", -svc, "Service revenue"),
        ])
        coll = r2(0.9 * (prod + svc))
        self.jrnl(fy, m, "CSH", "AR", "GLBATCH", f"Cash receipts {tag}", [
            ("1000", "10000", coll, "Customer receipts"),
            ("1100", "10000", -coll, "AR relief"),
        ])
        pur = r2(120_000 * s * g)
        self.jrnl(fy, m, "PUR", "AP", "GLBATCH", f"Inventory purchases {tag}", [
            ("1200", "10000", pur, "Inventory receipts"),
            ("2000", "10000", -pur, "AP vouchers"),
        ])
        pay = r2(0.85 * pur)
        self.jrnl(fy, m, "PAY", "AP", "GLBATCH", f"AP payment run {tag}", [
            ("2000", "10000", pay, "Payments to vendors"),
            ("1000", "10000", -pay, "Cash disbursed"),
        ])
        cogs = r2(100_000 * s * g)
        self.jrnl(fy, m, "COG", "CST", "GLBATCH", f"Cost of sales {tag}", [
            ("5000", "30000", cogs, "Standard cost relief"),
            ("1200", "10000", -cogs, "Inventory relief"),
        ])
        sal_c, sal_s, sal_o, ben = r2(40_000 * s), r2(30_000 * s), r2(25_000 * s), r2(19_000 * s)
        net = r2(88_000 * s)
        wh = r2(sal_c + sal_s + sal_o + ben - net)
        self.jrnl(fy, m, "SAL", "PAY", "GLBATCH", f"Payroll {tag}", [
            ("6000", "10000", sal_c, "Salaries - corporate"),
            ("6000", "20000", sal_s, "Salaries - sales"),
            ("6000", "30000", sal_o, "Salaries - operations"),
            ("6100", "10000", ben, "Employer benefits"),
            ("1010", "10000", -net, "Net payroll funding"),
            ("2200", "10000", -wh, "Withholdings payable"),
        ])
        xfr = r2(net + wh)
        self.jrnl(fy, m, "XFR", "TRF", "GLBATCH", f"Fund payroll account {tag}", [
            ("1010", "10000", xfr, "Transfer in"),
            ("1000", "10000", -xfr, "Transfer out"),
        ])
        self.jrnl(fy, m, "REM", "PAY", "GLBATCH", f"Remit withholdings {tag}", [
            ("2200", "10000", wh, "Withholding remittance"),
            ("1010", "10000", -wh, "Cash out - payroll acct"),
        ])
        rent = r2(18_000 * s)
        self.jrnl(fy, m, "RNT", "AP", "GLBATCH", f"Office rent {tag}", [
            ("6200", "10000", rent, "Monthly rent"),
            ("1000", "10000", -rent, "Rent payment"),
        ])
        seasonal = {1: 1.30, 2: 1.25, 7: 1.15, 8: 1.15, 12: 1.20}.get(m, 1.0)
        util = r2(3_200 * s * seasonal)
        self.jrnl(fy, m, "UTL", "AP", "GLBATCH", f"Utilities {tag}", [
            ("6300", "30000", util, "Utilities"),
            ("1000", "10000", -util, "Utilities payment"),
        ])
        trv_s = r2(8_000 * s * (3.5 if m == 4 else 1.0))
        trv_c = r2(2_000 * s)
        note = " - annual sales kickoff" if m == 4 else ""
        self.jrnl(fy, m, "TRV", "EXP", "GLBATCH", f"T&E settlements {tag}{note}", [
            ("6400", "20000", trv_s, f"Sales travel{note}"),
            ("6400", "10000", trv_c, "Corporate travel"),
            ("1000", "10000", -r2(trv_s + trv_c), "Expense reimbursements"),
        ])
        dep = r2(12_500 * s)
        self.jrnl(fy, m, "DEP", "AM", "GLBATCH", f"Depreciation {tag}", [
            ("6500", "10000", dep, "Monthly depreciation"),
            ("1590", "10000", -dep, "Accumulated depreciation"),
        ])
        if m % 2 == 1:
            misc = r2(1_500 * s)
            self.jrnl(fy, m, "MSC", "ONL", "SJONES", f"Misc expense {tag}", [
                ("6900", "10000", misc, "Office supplies & misc"),
                ("1000", "10000", -misc, "Misc payment"),
            ])

    def adjustment(self, fy: int, s: float) -> None:
        amt = r2(25_000 * s)
        self.jrnl(fy, 998, "ACR", "ONL", "MPATEL", f"FY{fy} year-end audit accrual", [
            ("6900", "10000", amt, "External audit fee accrual"),
            ("2100", "10000", -amt, "Accrued liabilities"),
        ], date=f"{fy}-12-31")

    def close_year(self, fy: int) -> None:
        """Write next-year period 0: carry A/L/Q endings, fold R/E into RE."""
        ends: dict[tuple, float] = defaultdict(float)
        for (y, per, acct, dept), amt in self.ledger.items():
            if y == fy:
                ends[(acct, dept)] += amt
        ni = 0.0
        for (acct, dept), amt in sorted(ends.items()):
            if abs(amt) < 0.005:
                continue
            if ATYPE[acct] in ("R", "E"):
                ni += amt
            else:
                self.ledger[(fy + 1, 0, acct, dept)] += r2(amt)
        self.ledger[(fy + 1, 0, RE_ACCT, "10000")] += r2(ni)


def build() -> Seeder:
    s = Seeder()
    for acct, amt in OPENING_2025.items():
        s.ledger[(2025, 0, acct, "10000")] += amt
    for m in range(1, 13):
        s.month(2025, m, 1.00)
    s.adjustment(2025, 1.00)
    s.close_year(2025)
    for m in range(1, 7):
        s.month(2026, m, 1.12)
    # demo artifact: unidentified receipt parked in suspense (May 2026)
    s.jrnl(2026, 5, "SUS", "ONL", "JCHEN", "Unidentified wire receipt - pending research", [
        ("1000", "10000", 15_000.00, "Wire received"),
        ("1999", "10000", -15_000.00, "Parked in suspense"),
    ], date="2026-05-19")
    # demo artifact: July rent journal entered but not yet posted
    rent = r2(18_000 * 1.12)
    s.jrnl(2026, 7, "RNT", "AP", "GLBATCH", "Office rent 2026-07 (pending approval)", [
        ("6200", "10000", rent, "Monthly rent"),
        ("1000", "10000", -rent, "Rent payment"),
    ], status="V")
    return s


DDL = """
CREATE TABLE PS_LEDGER (
  BUSINESS_UNIT TEXT, LEDGER TEXT, ACCOUNT TEXT, ALTACCT TEXT, DEPTID TEXT,
  OPERATING_UNIT TEXT, PRODUCT TEXT, FUND_CODE TEXT, CLASS_FLD TEXT,
  PROGRAM_CODE TEXT, BUDGET_REF TEXT, AFFILIATE TEXT, PROJECT_ID TEXT,
  CURRENCY_CD TEXT, STATISTICS_CODE TEXT, FISCAL_YEAR INTEGER,
  ACCOUNTING_PERIOD INTEGER, POSTED_TOTAL_AMT REAL, POSTED_BASE_AMT REAL,
  POSTED_TRAN_AMT REAL, BASE_CURRENCY TEXT);
CREATE TABLE PS_JRNL_HEADER (
  BUSINESS_UNIT TEXT, JOURNAL_ID TEXT, JOURNAL_DATE TEXT, UNPOST_SEQ INTEGER,
  JRNL_HDR_STATUS TEXT, FISCAL_YEAR INTEGER, ACCOUNTING_PERIOD INTEGER,
  SOURCE TEXT, OPRID TEXT, POSTED_DATE TEXT, DESCR254_MIXED TEXT,
  LEDGER_GROUP TEXT, CURRENCY_CD TEXT);
CREATE TABLE PS_JRNL_LN (
  BUSINESS_UNIT TEXT, JOURNAL_ID TEXT, JOURNAL_DATE TEXT, UNPOST_SEQ INTEGER,
  JOURNAL_LINE INTEGER, LEDGER TEXT, ACCOUNT TEXT, DEPTID TEXT,
  OPERATING_UNIT TEXT, PRODUCT TEXT, PROJECT_ID TEXT, CURRENCY_CD TEXT,
  MONETARY_AMOUNT REAL, FOREIGN_AMOUNT REAL, FOREIGN_CURRENCY TEXT,
  LINE_DESCR TEXT, JRNL_LINE_REF TEXT);
CREATE TABLE PS_GL_ACCOUNT_TBL (
  SETID TEXT, ACCOUNT TEXT, EFFDT TEXT, EFF_STATUS TEXT, DESCR TEXT,
  ACCOUNT_TYPE TEXT);
CREATE TABLE PS_DEPT_TBL (
  SETID TEXT, DEPTID TEXT, EFFDT TEXT, EFF_STATUS TEXT, DESCR TEXT);
CREATE TABLE PS_BUS_UNIT_TBL_GL (BUSINESS_UNIT TEXT, DESCR TEXT, BASE_CURRENCY TEXT);
CREATE TABLE PS_SET_CNTRL_REC (SETCNTRLVALUE TEXT, RECNAME TEXT, SETID TEXT);
CREATE TABLE PS_CAL_DETP_TBL (
  SETID TEXT, CALENDAR_ID TEXT, FISCAL_YEAR INTEGER, ACCOUNTING_PERIOD INTEGER,
  BEGIN_DT TEXT, END_DT TEXT);
CREATE TABLE PSTREEDEFN (SETID TEXT, SETCNTRLVALUE TEXT, TREE_NAME TEXT, EFFDT TEXT, DESCR TEXT);
CREATE TABLE PSTREENODE (
  SETID TEXT, SETCNTRLVALUE TEXT, TREE_NAME TEXT, EFFDT TEXT, TREE_NODE TEXT,
  TREE_NODE_NUM INTEGER, TREE_NODE_NUM_END INTEGER, TREE_LEVEL_NUM INTEGER,
  PARENT_NODE_NUM INTEGER);
CREATE TABLE PSTREELEAF (
  SETID TEXT, SETCNTRLVALUE TEXT, TREE_NAME TEXT, EFFDT TEXT,
  TREE_NODE_NUM INTEGER, RANGE_FROM TEXT, RANGE_TO TEXT);
"""

VIEWS = """
CREATE VIEW XX_TB_SETID_VW AS
SELECT SETCNTRLVALUE AS BUSINESS_UNIT, RECNAME, SETID FROM PS_SET_CNTRL_REC;

CREATE VIEW XX_TB_ACCT_VW AS
SELECT A.SETID, A.ACCOUNT, A.EFFDT, A.EFF_STATUS, A.DESCR, A.ACCOUNT_TYPE
  FROM PS_GL_ACCOUNT_TBL A
 WHERE A.EFFDT = (SELECT MAX(AX.EFFDT) FROM PS_GL_ACCOUNT_TBL AX
                   WHERE AX.SETID = A.SETID AND AX.ACCOUNT = A.ACCOUNT
                     AND AX.EFFDT <= DATE('now'));

CREATE VIEW XX_TB_BAL_VW AS
SELECT L.BUSINESS_UNIT, L.LEDGER, L.FISCAL_YEAR, L.ACCOUNTING_PERIOD, L.ACCOUNT,
       ACC.DESCR, ACC.ACCOUNT_TYPE, ACC.EFF_STATUS,
       L.DEPTID, L.OPERATING_UNIT, L.PRODUCT, L.PROJECT_ID, L.CURRENCY_CD,
       SUM(L.POSTED_TOTAL_AMT) AS POSTED_TOTAL_AMT
  FROM PS_LEDGER L
  LEFT JOIN XX_TB_SETID_VW S
    ON S.BUSINESS_UNIT = L.BUSINESS_UNIT AND S.RECNAME = 'GL_ACCOUNT_TBL'
  LEFT JOIN XX_TB_ACCT_VW ACC ON ACC.SETID = S.SETID AND ACC.ACCOUNT = L.ACCOUNT
 GROUP BY L.BUSINESS_UNIT, L.LEDGER, L.FISCAL_YEAR, L.ACCOUNTING_PERIOD, L.ACCOUNT,
          ACC.DESCR, ACC.ACCOUNT_TYPE, ACC.EFF_STATUS, L.DEPTID, L.OPERATING_UNIT,
          L.PRODUCT, L.PROJECT_ID, L.CURRENCY_CD;

CREATE VIEW XX_TB_JRNL_VW AS
SELECT H.BUSINESS_UNIT, H.JOURNAL_ID, H.JOURNAL_DATE, H.UNPOST_SEQ, H.FISCAL_YEAR,
       H.ACCOUNTING_PERIOD, H.SOURCE, H.OPRID, H.DESCR254_MIXED,
       J.JOURNAL_LINE, J.LEDGER, J.ACCOUNT, J.DEPTID, J.CURRENCY_CD,
       J.MONETARY_AMOUNT, J.LINE_DESCR
  FROM PS_JRNL_HEADER H
  JOIN PS_JRNL_LN J
    ON J.BUSINESS_UNIT = H.BUSINESS_UNIT AND J.JOURNAL_ID = H.JOURNAL_ID
   AND J.JOURNAL_DATE = H.JOURNAL_DATE AND J.UNPOST_SEQ = H.UNPOST_SEQ
 WHERE H.JRNL_HDR_STATUS = 'P';

CREATE VIEW XX_TB_PERIOD_VW AS
SELECT SETID, CALENDAR_ID, FISCAL_YEAR, ACCOUNTING_PERIOD, BEGIN_DT, END_DT
  FROM PS_CAL_DETP_TBL;

CREATE VIEW XX_TB_TREE_VW AS
SELECT N.SETID, N.TREE_NAME, N.EFFDT, N.TREE_NODE, N.TREE_LEVEL_NUM,
       N.TREE_NODE_NUM, N.TREE_NODE_NUM_END, LF.RANGE_FROM,
       COALESCE(NULLIF(NULLIF(LF.RANGE_TO, ''), ' '), LF.RANGE_FROM) AS RANGE_TO
  FROM PSTREENODE N
  JOIN PSTREELEAF LF
    ON LF.SETID = N.SETID AND LF.TREE_NAME = N.TREE_NAME AND LF.EFFDT = N.EFFDT
   AND LF.TREE_NODE_NUM BETWEEN N.TREE_NODE_NUM AND N.TREE_NODE_NUM_END;
"""

INDEXES = """
CREATE INDEX IX_LEDGER ON PS_LEDGER (BUSINESS_UNIT, LEDGER, FISCAL_YEAR, ACCOUNTING_PERIOD, ACCOUNT);
CREATE INDEX IX_JRNL_LN ON PS_JRNL_LN (BUSINESS_UNIT, LEDGER, ACCOUNT);
CREATE INDEX IX_JRNL_HDR ON PS_JRNL_HEADER (BUSINESS_UNIT, FISCAL_YEAR, ACCOUNTING_PERIOD);
"""


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    seeder = build()

    con = sqlite3.connect(str(DB_PATH))
    con.executescript(DDL)

    con.executemany(
        "INSERT INTO PS_JRNL_HEADER VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", seeder.headers
    )
    con.executemany(
        "INSERT INTO PS_JRNL_LN VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", seeder.lines
    )
    ledger_rows = [
        (BU, LEDGER, acct, "", dept, "", "", "", "", "", "", "", "", CURR, "",
         fy, per, r2(amt), r2(amt), r2(amt), CURR)
        for (fy, per, acct, dept), amt in sorted(seeder.ledger.items())
        if abs(amt) >= 0.005
    ]
    con.executemany(
        "INSERT INTO PS_LEDGER VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ledger_rows,
    )

    acct_rows = [(SETID, a, "1900-01-01", "A", d, t) for a, t, d in ACCOUNTS]
    # effective-dated rename to prove EFFDT logic works end-to-end
    acct_rows.append((SETID, "4100", "2026-01-01", "A", "Services & Subscription Revenue", "R"))
    con.executemany("INSERT INTO PS_GL_ACCOUNT_TBL VALUES (?,?,?,?,?,?)", acct_rows)

    con.executemany(
        "INSERT INTO PS_DEPT_TBL VALUES (?,?,?,?,?)",
        [(SETID, d, "1900-01-01", "A", n) for d, n in DEPTS],
    )
    con.execute(
        "INSERT INTO PS_BUS_UNIT_TBL_GL VALUES (?,?,?)", (BU, "US Operations", CURR)
    )
    con.executemany(
        "INSERT INTO PS_SET_CNTRL_REC VALUES (?,?,?)",
        [(BU, r, SETID) for r in ("GL_ACCOUNT_TBL", "DEPT_TBL", "CAL_DETP_TBL")],
    )
    cal_rows = [
        (SETID, CAL_ID, y, m, f"{y:04d}-{m:02d}-01", month_end(y, m))
        for y in (2024, 2025, 2026)
        for m in range(1, 13)
    ]
    con.executemany("INSERT INTO PS_CAL_DETP_TBL VALUES (?,?,?,?,?,?)", cal_rows)

    effdt = "1900-01-01"
    con.execute(
        "INSERT INTO PSTREEDEFN VALUES (?,?,?,?,?)",
        (SETID, "", "ACCOUNT", effdt, "Account rollup for trial balance"),
    )
    nodes = [
        ("ALLACCTS", 1, 1000, 1, 0),
        ("ASSETS", 10, 99, 2, 1),
        ("LIABILITIES", 100, 199, 2, 1),
        ("EQUITY", 200, 299, 2, 1),
        ("REVENUE", 300, 399, 2, 1),
        ("EXPENSES", 400, 499, 2, 1),
    ]
    con.executemany(
        "INSERT INTO PSTREENODE VALUES (?,?,?,?,?,?,?,?,?)",
        [(SETID, "", "ACCOUNT", effdt, n, a, b, lvl, p) for n, a, b, lvl, p in nodes],
    )
    leaves = [
        (10, "1000", "1999"), (100, "2000", "2999"), (200, "3000", "3999"),
        (300, "4000", "4999"), (400, "5000", "6999"),
    ]
    con.executemany(
        "INSERT INTO PSTREELEAF VALUES (?,?,?,?,?,?,?)",
        [(SETID, "", "ACCOUNT", effdt, num, lo, hi) for num, lo, hi in leaves],
    )

    con.executescript(VIEWS)
    con.executescript(INDEXES)
    con.commit()

    # ---- verification ----------------------------------------------------
    def one(sql, *p):
        return con.execute(sql, p).fetchone()[0]

    for fy in (2025, 2026):
        total = one(
            "SELECT SUM(POSTED_TOTAL_AMT) FROM PS_LEDGER WHERE FISCAL_YEAR=?", fy
        )
        assert abs(total) < 0.01, f"FY{fy} ledger does not net to zero: {total}"
    n_led = one("SELECT COUNT(*) FROM PS_LEDGER")
    n_hdr = one("SELECT COUNT(*) FROM PS_JRNL_HEADER")
    n_ln = one("SELECT COUNT(*) FROM PS_JRNL_LN")
    cash_p6 = one(
        "SELECT SUM(POSTED_TOTAL_AMT) FROM PS_LEDGER WHERE FISCAL_YEAR=2026 "
        "AND ACCOUNTING_PERIOD BETWEEN 0 AND 6 AND ACCOUNT='1000'"
    )
    con.close()
    print(f"Seeded {DB_PATH}")
    print(f"  PS_LEDGER rows:      {n_led}")
    print(f"  PS_JRNL_HEADER rows: {n_hdr}")
    print(f"  PS_JRNL_LN rows:     {n_ln}")
    print(f"  FY2025 and FY2026 ledgers net to zero ✔")
    print(f"  Cash (1000) ending FY2026 P6: {cash_p6:,.2f}")


if __name__ == "__main__":
    sys.exit(main())
