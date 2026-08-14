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
        self.budget: dict[tuple, float] = {}
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

    # Synthetic BUDGET ledger for the P&L accounts, so budget-vs-actual report
    # columns work out of the box. Real budgets are set before the year; here we
    # derive them deterministically from actuals: revenue budget slightly under
    # actuals (favorable), expense budget slightly over (favorable), except
    # travel (6400), budgeted under so the P4 kickoff spike blows through it.
    def bfactor(acct: str, atype: str) -> float:
        if acct == "6400":
            return 0.95
        return 0.97 if atype == "R" else 1.06

    for (fy, per, acct, dept), amt in list(s.ledger.items()):
        t = ATYPE.get(acct)
        if t in ("R", "E") and 1 <= per <= 12:
            s.budget[(fy, per, acct, dept)] = r2(amt * bfactor(acct, t))
    # FY2026 has actuals only through P6; extend its budget to a full year from
    # FY2025 actuals scaled by the same growth used for FY2026 activity.
    for (fy, per, acct, dept), amt in list(s.ledger.items()):
        t = ATYPE.get(acct)
        if fy == 2025 and 7 <= per <= 12 and t in ("R", "E"):
            s.budget[(2026, per, acct, dept)] = r2(amt * 1.12 * bfactor(acct, t))
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
-- Delivered PeopleSoft splits these deliberately, and the sample now matches
-- it: the NAME of a business unit is on the Financials record, while the GL
-- record carries only GL attributes and has NO DESCR. Shipping a DESCR on
-- PS_BUS_UNIT_TBL_GL modelled a shape no real instance has, so a query
-- reading the name from the wrong record passed here and returned NULL on
-- every real one.
-- COUNTRY is what lets "how do we invoice for India" narrow to units
-- without anything in the code knowing what India is: the process graph
-- reads the country of each unit from here. Real FSCM carries it; a site
-- whose copy does not is told so rather than guessed at.
CREATE TABLE PS_BUS_UNIT_TBL_FS (BUSINESS_UNIT TEXT, DESCR TEXT,
                                 COUNTRY TEXT);
-- The instance's own code-to-name table. It is what turns "for India" into
-- COUNTRY = 'IND' without any code shipping a list of countries.
CREATE TABLE PS_COUNTRY_TBL (COUNTRY TEXT, DESCR TEXT, DESCRSHORT TEXT);
CREATE TABLE PS_BUS_UNIT_TBL_GL (BUSINESS_UNIT TEXT, BASE_CURRENCY TEXT);
-- Ledger setup records. A real instance always has these, and the agent
-- discovers its BU/ledger catalog from them: they hold one row per business
-- unit / ledger group, so the lookup is a small indexed read instead of a
-- DISTINCT over every balance row in PS_LEDGER.
CREATE TABLE PS_BUS_UNIT_LED (BUSINESS_UNIT TEXT, LEDGER_GROUP TEXT);
-- PeopleTools metadata. A real instance always has these; record discovery
-- searches RECDESCR so a question phrased functionally ("file interface")
-- finds a record whose table name gives no clue. SQLTABLENAME is the site's
-- physical-name override, blank when the name is just PS_ + RECNAME.
-- A minimal Payables slice. AP has no curated tools by design: it is the
-- worked example that "ask anything" reaches every module through
-- search_records + run_sql, so it must be exercised like a real question.
CREATE TABLE PS_VENDOR (
  SETID TEXT, VENDOR_ID TEXT, NAME1 TEXT, VENDOR_STATUS TEXT,
  -- The supplier hierarchy the system records, mirroring PS_CUSTOMER.
  -- It is the ONLY thing allowed to say two suppliers are one company.
  CORPORATE_SETID TEXT, CORPORATE_VENDOR TEXT,
  -- Taxpayer identifier. Never returned; only ever compared as a keyed
  -- hash, because two suppliers sharing one is worth knowing and the
  -- number itself is not something a model needs to see.
  VNDR_TIN TEXT);
CREATE TABLE PS_VENDOR_ADDR (
  SETID TEXT, VENDOR_ID TEXT, ADDRESS_SEQ_NUM INTEGER,
  CITY TEXT, STATE TEXT, COUNTRY TEXT);
-- Remit-to bank accounts. Two suppliers pointing at ONE account is the
-- classic payables-fraud signal, and the account number is exactly the
-- value that must not travel: compared as a keyed hash, never selected
-- into a payload.
CREATE TABLE PS_VNDR_BANK_ACCT (
  SETID TEXT, VENDOR_ID TEXT, BANK_ACCT_SEQ_NBR INTEGER,
  BANK_ID_NBR TEXT, BANK_ACCOUNT_NUM TEXT);
CREATE TABLE PS_VOUCHER (
  BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, VENDOR_ID TEXT, INVOICE_ID TEXT,
  INVOICE_DT TEXT, DUE_DT TEXT, GROSS_AMT REAL, ENTRY_STATUS TEXT,
  CLOSE_STATUS TEXT, POST_STATUS TEXT, CURRENCY_CD TEXT,
  MATCH_STATUS_VCHR TEXT);
-- Purchasing: the order -> receipt -> voucher chain the three-way match
-- reads. Delivered shapes, abridged: amounts live on the PO SCHEDULE
-- (PS_PO_LINE_SHIP), not the line; receipt detail lives on the shipment
-- line (PS_RECV_LN_SHIP), which points back at the PO schedule it received
-- against; the voucher line points at both. MATCH_STATUS_VCHR above is the
-- system's own verdict — the match tool recomputes the arithmetic and
-- reports both, kept apart.
CREATE TABLE PS_PO_HDR (
  BUSINESS_UNIT TEXT, PO_ID TEXT, VENDOR_ID TEXT, PO_STATUS TEXT,
  PO_DT TEXT, CURRENCY_CD TEXT);
CREATE TABLE PS_PO_LINE (
  BUSINESS_UNIT TEXT, PO_ID TEXT, LINE_NBR INTEGER, DESCR254_MIXED TEXT);
CREATE TABLE PS_PO_LINE_SHIP (
  BUSINESS_UNIT TEXT, PO_ID TEXT, LINE_NBR INTEGER, SCHED_NBR INTEGER,
  QTY_PO REAL, PRICE_PO REAL, MERCHANDISE_AMT REAL);
CREATE TABLE PS_RECV_HDR (
  BUSINESS_UNIT TEXT, RECEIVER_ID TEXT, VENDOR_ID TEXT, RECEIPT_DT TEXT,
  RECV_STATUS TEXT);
CREATE TABLE PS_RECV_LN_SHIP (
  BUSINESS_UNIT TEXT, RECEIVER_ID TEXT, RECV_LN_NBR INTEGER,
  RECV_SHIP_SEQ_NBR INTEGER, BUSINESS_UNIT_PO TEXT, PO_ID TEXT,
  LINE_NBR INTEGER, SCHED_NBR INTEGER, QTY_SH_ACCPT_VUOM REAL,
  MERCHANDISE_AMT REAL);
CREATE TABLE PS_VOUCHER_LINE (
  BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, VOUCHER_LINE_NUM INTEGER,
  BUSINESS_UNIT_PO TEXT, PO_ID TEXT, LINE_NBR INTEGER, SCHED_NBR INTEGER,
  RECEIVER_ID TEXT, RECV_LN_NBR INTEGER, QTY_VCHR REAL, UNIT_PRICE REAL,
  MERCHANDISE_AMT REAL, DESCR TEXT);
-- Asset Management: the register the AM questions read. Delivered AM keys
-- cost rows by asset and book; category and NBV live on cost/book rows,
-- not the asset master.
CREATE TABLE PS_ASSET (
  BUSINESS_UNIT TEXT, ASSET_ID TEXT, DESCR TEXT, ASSET_STATUS TEXT,
  ACQUISITION_DT TEXT);
CREATE TABLE PS_COST (
  BUSINESS_UNIT TEXT, ASSET_ID TEXT, BOOK TEXT, TRANS_DT TEXT,
  TRANS_TYPE TEXT, CATEGORY TEXT, COST REAL, CURRENCY_CD TEXT);
-- Project Costing: PS_PROJ_RESOURCE is the transaction spine — actuals and
-- budgets are ROWS distinguished by ANALYSIS_TYPE (ACT/BUD), not columns.
CREATE TABLE PS_PROJECT (
  BUSINESS_UNIT TEXT, PROJECT_ID TEXT, DESCR TEXT, EFF_STATUS TEXT);
CREATE TABLE PS_PROJ_RESOURCE (
  BUSINESS_UNIT TEXT, PROJECT_ID TEXT, ACTIVITY_ID TEXT, ANALYSIS_TYPE TEXT,
  TRANS_DT TEXT, RESOURCE_AMOUNT REAL, CURRENCY_CD TEXT);
CREATE TABLE PS_PAYMENT_TBL (
  BANK_SETID TEXT, PYMNT_ID TEXT, VENDOR_ID TEXT, PYMNT_DT TEXT,
  PYMNT_AMT REAL, CURRENCY_CD TEXT, PYMNT_STATUS TEXT);
CREATE TABLE PS_PYMNT_VCHR_XREF (
  BUSINESS_UNIT TEXT, VOUCHER_ID TEXT, PYMNT_ID TEXT, PAID_AMT REAL);
CREATE TABLE PSRECDEFN (
  RECNAME TEXT, RECDESCR TEXT, RECTYPE INTEGER, SQLTABLENAME TEXT);
CREATE TABLE PSRECFIELD (RECNAME TEXT, FIELDNAME TEXT, FIELDNUM INTEGER);
-- The PeopleTools layer that says how work is DONE, not where it is stored:
-- a page, the records that page reads, the component the page belongs to,
-- and where the portal registry hangs that component in the menu. This is
-- the chain the process graph walks to answer "how do we do invoicing" —
-- see pstb/procgraph.py. Real column names, abridged shape.
CREATE TABLE PSPNLDEFN (PNLNAME TEXT, DESCR TEXT);
CREATE TABLE PSPNLFIELD (PNLNAME TEXT, FIELDNUM INTEGER, RECNAME TEXT,
                         FIELDNAME TEXT);
CREATE TABLE PSPNLGROUP (PNLGRPNAME TEXT, PNLNAME TEXT, ITEMNUM INTEGER,
                         MARKET TEXT);
CREATE TABLE PSPRSMDEFN (PORTAL_NAME TEXT, PORTAL_OBJNAME TEXT,
                         PORTAL_PRNTOBJNAME TEXT, PORTAL_LABEL TEXT,
                         PORTAL_URI_SEG2 TEXT);
-- PeopleTools operator definitions and FSCM business-unit row security.
-- Real shape (abridged): PSOPRDEFN is the user list, and ROWSECCLASS on it
-- is the permission list PeopleSoft uses for ROW security specifically —
-- which is what PS_SEC_BU_CLS keys on. PS_SEC_BU_OPR is the user-level
-- form of the same rule. A site uses one or the other; both are seeded so
-- the discovery in pstb/security.py has both shapes to find.
CREATE TABLE PSOPRDEFN (
  OPRID TEXT, OPRDEFNDESC TEXT, ROWSECCLASS TEXT, ACCTLOCK INTEGER);
CREATE TABLE PS_SEC_BU_OPR (OPRID TEXT, BUSINESS_UNIT TEXT);
CREATE TABLE PS_SEC_BU_CLS (OPRCLASS TEXT, BUSINESS_UNIT TEXT);
-- PeopleTools QUERY catalog. Real shape (abridged): PSQRYDEFN holds the
-- definition and its owner (OPRID blank = public), PSQRYBIND the runtime
-- prompts, PSQRYRECORD which records it reads. Discovery of existing
-- queries is therefore plain SQL — no gateway, no credentials.
CREATE TABLE PSQRYDEFN (
  OPRID TEXT, QRYNAME TEXT, QRYTYPE INTEGER, DESCR TEXT,
  LASTUPDDTTM TEXT, LASTUPDOPRID TEXT);
-- Execution statistics live in their OWN table, keyed like the query, and
-- only for queries with statistics logging enabled. There is no QRYRUNCNT
-- on PSQRYDEFN in any release — the sample once had one, and code written
-- against it would have silently lost popularity ranking in production.
CREATE TABLE PSQRYSTATS (
  OPRID TEXT, QRYNAME TEXT, EXECCOUNT INTEGER, LASTEXECDTTM TEXT);
CREATE TABLE PSQRYBIND (
  OPRID TEXT, QRYNAME TEXT, BNDNUM INTEGER, BNDNAME TEXT,
  HEADING TEXT, FIELDTYPE INTEGER);
CREATE TABLE PSQRYRECORD (
  OPRID TEXT, QRYNAME TEXT, SELNUM INTEGER, RECNAME TEXT, CORRNAME TEXT);
-- Integration Broker catalog, REAL column names (PT 8.58 data dictionary;
-- go-faster.co.uk/peopletools/psibsvcsetup.htm): the service target
-- locations live on PSIBSVCSETUP as IB_TGTLOCATION (SOAP) and
-- IB_RESTTGTLOC (REST); services and operations link through the
-- PSSERVICEOPR bridge. The REST columns are present but blank here, so
-- discovery exercises the fall-through to the SOAP target and the
-- SOAP->REST derivation in connectors/psquery_api.rest_base.
CREATE TABLE PSIBSVCSETUP (
  SEQNO INTEGER, IB_NAMESPACE TEXT, IB_SCHEMANAMESPACE TEXT,
  IB_TGTLOCATION TEXT, IB_SECTGTLOCATION TEXT, IB_RESTTGTLOC TEXT,
  IB_RESTSECTGTLOC TEXT);
CREATE TABLE PSSERVICE (
  IB_SERVICENAME TEXT, DESCR TEXT, IB_ALIASNAME TEXT);
CREATE TABLE PSOPERATION (IB_OPERATIONNAME TEXT, DESCR TEXT);
CREATE TABLE PSSERVICEOPR (
  IB_SERVICENAME TEXT, IB_OPERATIONNAME TEXT);
CREATE TABLE PS_LED_GRP_TBL (LEDGER_GROUP TEXT, LEDGER TEXT, DESCR TEXT);
CREATE TABLE PS_SET_CNTRL_REC (SETCNTRLVALUE TEXT, RECNAME TEXT, SETID TEXT);
CREATE TABLE PS_CAL_DETP_TBL (
  SETID TEXT, CALENDAR_ID TEXT, FISCAL_YEAR INTEGER, ACCOUNTING_PERIOD INTEGER,
  BEGIN_DT TEXT, END_DT TEXT);
CREATE TABLE PS_RT_RATE_TBL (
  FROM_CUR TEXT, TO_CUR TEXT, RT_TYPE TEXT, EFFDT TEXT,
  RATE_MULT REAL, RATE_DIV REAL);
CREATE TABLE PS_CUSTOMER (
  SETID TEXT, CUST_ID TEXT, NAME1 TEXT, CUST_STATUS TEXT,
  -- The corporate hierarchy PeopleSoft itself keeps: a subsidiary points at
  -- its parent, and a parent points at itself. This is the edge that makes
  -- "which subsidiaries drive this parent's overdue balance" answerable
  -- without anyone GUESSING that two customers are related.
  CORPORATE_SETID TEXT, CORPORATE_CUST_ID TEXT);

-- AR payments and how they were applied. A payment exists on its own (money
-- arrived) and is APPLIED to items separately — money received but not
-- applied is one of the states worth surfacing, and flattening the two into
-- one row would hide it.
CREATE TABLE PS_PAYMENT (
  DEPOSIT_BU TEXT, DEPOSIT_ID TEXT, PAYMENT_SEQ_NUM INTEGER, CUST_ID TEXT,
  PAYMENT_AMT REAL, PAYMENT_DT TEXT, PAYMENT_CURRENCY TEXT,
  PAYMENT_STATUS TEXT);
CREATE TABLE PS_PAYMENT_ITEM (
  DEPOSIT_BU TEXT, DEPOSIT_ID TEXT, PAYMENT_SEQ_NUM INTEGER,
  BUSINESS_UNIT TEXT, CUST_ID TEXT, ITEM TEXT, APPLIED_AMT REAL,
  ACCTG_DT TEXT);
CREATE TABLE PS_ITEM (
  BUSINESS_UNIT TEXT, CUST_ID TEXT, ITEM TEXT, ITEM_LINE INTEGER,
  ITEM_STATUS TEXT, BAL_AMT REAL, ORIG_ITEM_AMT REAL, BAL_CURRENCY TEXT,
  ACCTG_DT TEXT, DUE_DT TEXT, ASOF_DT TEXT, DISPUTE_STATUS TEXT, PO_REF TEXT);
CREATE TABLE PS_BI_HDR (
  BUSINESS_UNIT TEXT, INVOICE TEXT, BILL_STATUS TEXT, BILL_TO_CUST_ID TEXT,
  INVOICE_DT TEXT, ACCOUNTING_DT TEXT, INVOICE_AMOUNT REAL,
  BI_CURRENCY_CD TEXT, BILL_TYPE_ID TEXT, BILL_SOURCE_ID TEXT,
  -- Credit and rebill point BACK at the invoice they correct. The CHAIN,
  -- not the single row, is what tells you the net effect.
  CR_REBILL_INV TEXT, ADJUSTMENT_TYPE TEXT);
CREATE TABLE PS_CUST_ADDRESS (
  SETID TEXT, CUST_ID TEXT, ADDRESS_SEQ_NUM INTEGER,
  CITY TEXT, STATE TEXT, COUNTRY TEXT);
CREATE TABLE PS_BI_LINE (
  BUSINESS_UNIT TEXT, INVOICE TEXT, LINE_SEQ_NUM INTEGER,
  IDENTIFIER TEXT, DESCR TEXT, NET_EXTENDED_AMT REAL);
CREATE TABLE PS_INTFC_BI (
  INTFC_ID INTEGER, INTFC_LINE_NUM INTEGER, TRANS_TYPE_BI TEXT,
  BUSINESS_UNIT TEXT, BILL_TO_CUST_ID TEXT, BILL_SOURCE_ID TEXT,
  LOAD_STATUS_BI TEXT, TARGET_INVOICE TEXT);
CREATE TABLE PSTREEDEFN (SETID TEXT, SETCNTRLVALUE TEXT, TREE_NAME TEXT, EFFDT TEXT, DESCR TEXT);
CREATE TABLE PSTREENODE (
  SETID TEXT, SETCNTRLVALUE TEXT, TREE_NAME TEXT, EFFDT TEXT, TREE_NODE TEXT,
  TREE_NODE_NUM INTEGER, TREE_NODE_NUM_END INTEGER, TREE_LEVEL_NUM INTEGER,
  PARENT_NODE_NUM INTEGER);
CREATE TABLE PSTREELEAF (
  SETID TEXT, SETCNTRLVALUE TEXT, TREE_NAME TEXT, EFFDT TEXT,
  TREE_NODE_NUM INTEGER, RANGE_FROM TEXT, RANGE_TO TEXT);

-- Indexes mirroring the real PeopleSoft key order. They are here because
-- the join graph RANKS hops by whether the shared columns lead an index —
-- an index on (BUSINESS_UNIT, LEDGER, FISCAL_YEAR, ...) is usable by a join
-- that supplies BUSINESS_UNIT and useless to one that supplies only ACCOUNT.
-- Without them the sample would teach that every join costs the same, which
-- is the single most expensive thing it could teach.
CREATE INDEX PS_LEDGER_IDX ON PS_LEDGER
  (BUSINESS_UNIT, LEDGER, FISCAL_YEAR, ACCOUNTING_PERIOD, ACCOUNT);
CREATE INDEX PS_ITEM_IDX ON PS_ITEM (BUSINESS_UNIT, CUST_ID, ITEM);
CREATE INDEX PS_CUSTOMER_IDX ON PS_CUSTOMER (SETID, CUST_ID);
CREATE INDEX PS_CUST_ADDRESS_IDX ON PS_CUST_ADDRESS (SETID, CUST_ID);
CREATE INDEX PS_PAYMENT_IDX ON PS_PAYMENT (DEPOSIT_BU, CUST_ID, DEPOSIT_ID);
CREATE INDEX PS_PAYMENT_ITEM_IDX ON PS_PAYMENT_ITEM
  (BUSINESS_UNIT, CUST_ID, ITEM);
CREATE INDEX PS_BI_HDR_IDX ON PS_BI_HDR
  (BUSINESS_UNIT, INVOICE, BILL_TO_CUST_ID);
CREATE INDEX PS_BI_LINE_IDX ON PS_BI_LINE (BUSINESS_UNIT, INVOICE);
CREATE INDEX PS_VOUCHER_IDX ON PS_VOUCHER (BUSINESS_UNIT, VOUCHER_ID);
CREATE INDEX PS_VENDOR_IDX ON PS_VENDOR (SETID, VENDOR_ID);
CREATE INDEX PS_JRNL_HEADER_IDX ON PS_JRNL_HEADER
  (BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE);
CREATE INDEX PS_JRNL_LN_IDX ON PS_JRNL_LN
  (BUSINESS_UNIT, JOURNAL_ID, JOURNAL_DATE);
CREATE INDEX PS_GL_ACCOUNT_TBL_IDX ON PS_GL_ACCOUNT_TBL (SETID, ACCOUNT);
CREATE INDEX PS_DEPT_TBL_IDX ON PS_DEPT_TBL (SETID, DEPTID);
CREATE INDEX PS_PYMNT_VCHR_XREF_IDX ON PS_PYMNT_VCHR_XREF
  (BUSINESS_UNIT, VOUCHER_ID, PYMNT_ID);
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

    def one(sql, *p):
        return con.execute(sql, p).fetchone()[0]

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
    ledger_rows += [
        (BU, "BUDGET", acct, "", dept, "", "", "", "", "", "", "", "", CURR, "",
         fy, per, r2(amt), r2(amt), r2(amt), CURR)
        for (fy, per, acct, dept), amt in sorted(seeder.budget.items())
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
        "INSERT INTO PS_BUS_UNIT_TBL_GL VALUES (?,?)", (BU, CURR)
    )
    con.execute(
        "INSERT INTO PS_BUS_UNIT_TBL_FS VALUES (?,?,?)",
        (BU, "US Operations", "USA")
    )
    # A handful of ISO-3 country rows. India is here and NO business unit is
    # in it on purpose: "how do we invoice for India" must be able to answer
    # "this system knows India, and no unit here operates there" rather than
    # silently returning the US process wearing an Indian label.
    con.executemany(
        "INSERT INTO PS_COUNTRY_TBL VALUES (?,?,?)",
        [("USA", "United States", "USA"), ("IND", "India", "India"),
         ("CAN", "Canada", "Canada"), ("GBR", "United Kingdom", "UK"),
         ("DEU", "Germany", "Germany"), ("SGP", "Singapore", "Singapore")],
    )
    # PeopleTools catalog rows for the records this sample ships, plus a
    # custom-looking one so description-based discovery is exercised.
    _recs = [
        ("LEDGER", "Ledger Balances", 0, ""),
        ("JRNL_HEADER", "Journal Header", 0, ""),
        ("JRNL_LN", "Journal Line", 0, ""),
        ("GL_ACCOUNT_TBL", "GL Account Definition", 0, ""),
        ("BI_HDR", "Billing Invoice Header", 0, ""),
        ("INTFC_BI", "Billing Interface Staging", 0, ""),
        ("ITEM", "AR Open Item", 0, ""),
        ("CUSTOMER", "Customer Master", 0, ""),
        ("TU_FILE_INTFC", "File Interface Setup and Schedule", 0, ""),
    ]
    _recs += [
        ("VENDOR", "Supplier Master", 0, ""),
        ("VOUCHER", "AP Voucher Header", 0, ""),
        ("PAYMENT_TBL", "AP Payment Header", 0, ""),
        ("PYMNT_VCHR_XREF", "Payment to Voucher Cross Reference", 0, ""),
        ("ASSET", "Asset Master", 0, ""),
        ("COST", "Asset Cost and Activity", 0, ""),
        ("PROJECT", "Project Master", 0, ""),
        ("PO_HDR", "Purchase Order Header", 0, ""),
        ("PO_LINE", "Purchase Order Line", 0, ""),
        ("PO_LINE_SHIP", "PO Line Schedule", 0, ""),
        ("RECV_HDR", "Receipt Header", 0, ""),
        ("RECV_LN_SHIP", "Receipt Shipment Line", 0, ""),
        ("VOUCHER_LINE", "AP Voucher Line", 0, ""),
        ("PROJ_RESOURCE", "Project Cost Transactions", 0, ""),
    ]
    con.executemany("INSERT INTO PSRECDEFN VALUES (?,?,?,?)", _recs)

    # Users and their business-unit reach. US001 is the only unit with
    # ledger data, and that is deliberate: CA001 exists so a restricted
    # user can be DENIED something, which is the only way the restriction
    # is observable. A grant to a unit that holds no rows is a real
    # PeopleSoft state too, and the app has to say so rather than look
    # broken.
    con.executemany(
        "INSERT INTO PSOPRDEFN VALUES (?,?,?,?)",
        [
            ("FIN_US001", "US ledger analyst", "", 0),
            ("FIN_CA001", "Canada ledger analyst", "", 0),
            ("AP_CLERK", "AP clerk (row security by class)", "ROWSEC_US", 0),
            ("AUDITOR", "Auditor, all units", "ROWSEC_ALL", 0),
            ("NOACCESS", "Starter account, no units granted", "", 0),
        ],
    )
    con.executemany(
        "INSERT INTO PS_SEC_BU_OPR VALUES (?,?)",
        [("FIN_US001", "US001"), ("FIN_CA001", "CA001"),
         ("AUDITOR", "US001"), ("AUDITOR", "CA001")],
    )
    con.executemany(
        "INSERT INTO PS_SEC_BU_CLS VALUES (?,?)",
        [("ROWSEC_US", "US001"), ("ROWSEC_ALL", "US001"),
         ("ROWSEC_ALL", "CA001")],
    )

    # Existing PSQueries — the institutional knowledge worth reusing. Two
    # public finance queries with prompts, one private (OPRID set) that
    # discovery must label, and one high-run-count query so "what does the
    # business actually run" is answerable.
    con.executemany(
        "INSERT INTO PSQRYDEFN VALUES (?,?,?,?,?,?)",
        [
            ("", "GL_TB_BY_DEPT", 1,
             "Trial balance by department and account",
             "2026-05-14", "JSMITH"),
            ("", "AP_AGING_BY_VENDOR", 1,
             "Open payables aged by vendor with due dates",
             "2026-06-02", "MRAO"),
            ("", "BI_INVOICE_REGISTER", 1,
             "Finalized invoice register for a period",
             "2026-04-28", "JSMITH"),
            ("MRAO", "MY_ADHOC_SPEND", 1,
             "Personal ad-hoc spend extract",
             "2026-07-30", "MRAO"),
        ])
    con.executemany(
        "INSERT INTO PSQRYSTATS VALUES (?,?,?,?)",
        [
            ("", "GL_TB_BY_DEPT", 412, "2026-08-05"),
            ("", "AP_AGING_BY_VENDOR", 1180, "2026-08-07"),
            ("", "BI_INVOICE_REGISTER", 96, "2026-07-31"),
            ("MRAO", "MY_ADHOC_SPEND", 3, "2026-07-30"),
        ])
    con.executemany(
        "INSERT INTO PSQRYBIND VALUES (?,?,?,?,?,?)",
        [
            ("", "GL_TB_BY_DEPT", 1, "BIND1", "Business Unit", 0),
            ("", "GL_TB_BY_DEPT", 2, "BIND2", "Fiscal Year", 1),
            ("", "GL_TB_BY_DEPT", 3, "BIND3", "Accounting Period", 1),
            ("", "AP_AGING_BY_VENDOR", 1, "BIND1", "Business Unit", 0),
            ("", "AP_AGING_BY_VENDOR", 2, "BIND2", "As Of Date", 4),
            ("", "BI_INVOICE_REGISTER", 1, "BIND1", "Business Unit", 0),
            ("", "BI_INVOICE_REGISTER", 2, "BIND2", "Invoice Date From", 4),
            ("", "BI_INVOICE_REGISTER", 3, "BIND3", "Invoice Date To", 4),
        ])
    con.executemany(
        "INSERT INTO PSQRYRECORD VALUES (?,?,?,?,?)",
        [
            ("", "GL_TB_BY_DEPT", 1, "LEDGER", "A"),
            ("", "GL_TB_BY_DEPT", 2, "GL_ACCOUNT_TBL", "B"),
            ("", "AP_AGING_BY_VENDOR", 1, "VOUCHER", "A"),
            ("", "AP_AGING_BY_VENDOR", 2, "VENDOR", "B"),
            ("", "BI_INVOICE_REGISTER", 1, "BI_HDR", "A"),
            ("MRAO", "MY_ADHOC_SPEND", 1, "VOUCHER", "A"),
        ])

    # Integration Broker: the site's published target location and the
    # Query Access Service operations. Note the write-capable operation —
    # discovery must SEE it and invocation must still refuse it.
    con.execute("INSERT INTO PSIBSVCSETUP VALUES (?,?,?,?,?,?,?)",
                (1, "http://xmlns.oracle.com/Enterprise/Tools/services", "",
                 "http://dc15-pserp-dv-rp01.example:8016/PSIGW/"
                 "PeopleSoftServiceListeningConnector", "", "", ""))
    con.executemany(
        "INSERT INTO PSSERVICE VALUES (?,?,?)",
        [("QAS", "Query Access Service", "QAS"),
         ("PT_QRY", "Query Manager Service", "PT_QRY"),
         ("VOUCHER_BUILD", "Voucher Build Service", "VCHR")])
    con.executemany(
        "INSERT INTO PSSERVICEOPR VALUES (?,?)",
        [("QAS", "QAS_LISTQUERIES"),
         ("QAS", "QAS_GETQUERYPROPERTIES"),
         ("QAS", "QAS_EXECUTEQUERY"),
         ("QAS", "QAS_EXECUTENONBLOCKING"),
         ("VOUCHER_BUILD", "VOUCHER_LOAD")])
    con.executemany(
        "INSERT INTO PSOPERATION VALUES (?,?)",
        [
            ("QAS_LISTQUERIES", "List available queries"),
            ("QAS_GETQUERYPROPERTIES", "Query prompts and result fields"),
            ("QAS_EXECUTEQUERY", "Execute a query and return rows"),
            ("QAS_EXECUTENONBLOCKING",
             "Execute a long-running query asynchronously"),
            ("VOUCHER_LOAD", "Create vouchers from staged data"),
        ])
    # Suppliers, with the two traps every identity check has to survive.
    #
    # V1004/V1005 are subsidiaries of V1001 in the RECORDED hierarchy, so
    # the family rollup has something real to roll up. V1006 is called
    # "Ridgeline Supply Group" and is its own corporate parent — a
    # different company that any name match folds into the Ridgeline
    # family, producing a combined exposure that reads exactly as
    # authoritative as a correct one.
    #
    # V1002 and V1007 share a remit-to bank account. V1003 and V1008 share
    # a taxpayer id written two different ways. Neither pair shares a name.
    con.executemany(
        "INSERT INTO PS_VENDOR VALUES (?,?,?,?,?,?,?)",
        [(SETID, "V1001", "Ridgeline Supply Co", "A", SETID, "V1001",
          "94-3177001"),
         (SETID, "V1002", "Cobalt IT Services", "A", SETID, "V1002",
          "81-2299450"),
         (SETID, "V1003", "Harbor Freight Lines", "I", SETID, "V1003",
          "45-6001122"),
         (SETID, "V1004", "Ridgeline Fasteners", "A", SETID, "V1001",
          "94-3177002"),
         (SETID, "V1005", "Ridgeline Logistics", "A", SETID, "V1001",
          "94-3177003"),
         (SETID, "V1006", "Ridgeline Supply Group", "A", SETID, "V1006",
          "27-8830014"),
         (SETID, "V1007", "Meridian Office Supply", "A", SETID, "V1007",
          "33-5512009"),
         # Same taxpayer id as V1003, written with punctuation and a
         # leading zero. Literal equality misses it; normalising first
         # does not.
         (SETID, "V1008", "Harborline Transport LLC", "A", SETID, "V1008",
          "045.600.1122"),
         # The purchasing supplier. Its identifiers are unique on purpose:
         # the chain feature must not move the identity-link counts.
         (SETID, "V1009", "Summit Machining Co", "A", SETID, "V1009",
          "52-4410077")])
    con.executemany(
        "INSERT INTO PS_VENDOR_ADDR VALUES (?,?,?,?,?,?)",
        [(SETID, "V1001", 1, "Akron", "OH", "USA"),
         (SETID, "V1002", 1, "Raleigh", "NC", "USA"),
         (SETID, "V1003", 1, "Long Beach", "CA", "USA"),
         (SETID, "V1004", 1, "Akron", "OH", "USA"),
         (SETID, "V1005", 1, "Toledo", "OH", "USA"),
         (SETID, "V1006", 1, "Reno", "NV", "USA"),
         (SETID, "V1007", 1, "Raleigh", "NC", "USA"),
         (SETID, "V1008", 1, "Long Beach", "CA", "USA"),
         (SETID, "V1009", 1, "Reno", "NV", "USA")])
    # V1002 and V1007 remit to the SAME account, spelled differently. Two
    # unrelated suppliers, one bank account, and nothing about their names
    # or ids connects them.
    con.executemany(
        "INSERT INTO PS_VNDR_BANK_ACCT VALUES (?,?,?,?,?)",
        [(SETID, "V1001", 1, "041000124", "8837-2210-04"),
         (SETID, "V1002", 1, "053100300", "000123456789"),
         (SETID, "V1003", 1, "121000358", "5590-8811-72"),
         (SETID, "V1007", 1, "053100300", "0000123456789"),
         (SETID, "V1004", 1, "041000124", "8837-2210-99"),
         (SETID, "V1009", 1, "107002192", "7741-0023-10")])
    _vouchers, _payments, _xref = [], [], []
    for i, (vend, amt, mon) in enumerate(
            [("V1001", 12_500.00, 3), ("V1001", 8_200.00, 4),
             ("V1001", 15_300.00, 5), ("V1002", 42_000.00, 4),
             ("V1002", 9_900.00, 6), ("V1003", 3_400.00, 5)], start=1):
        vid = f"VCHR{i:05d}"
        pid = f"PAY{i:05d}"
        _vouchers.append((BU, vid, vend, f"INV-{i:04d}",
                          month_end(2026, mon), month_end(2026, mon), amt,
                          "P", "C", "P", CURR, "N"))
        _payments.append((SETID, pid, vend, month_end(2026, mon), amt, CURR, "P"))
        _xref.append((BU, vid, pid, amt))
    # OPEN payables — what the AP questions are actually about. One current,
    # one past due, one large and due soon, one stuck in recycle (the
    # exception queue), across two vendors.
    _vouchers += [
        (BU, "VCHR90001", "V1001", "INV-9001", "2026-07-10", "2026-08-09",
         18_400.00, "P", "O", "P", CURR, "N"),
        (BU, "VCHR90002", "V1002", "INV-9002", "2026-06-05", "2026-07-05",
         27_650.00, "P", "O", "P", CURR, "N"),        # past due
        (BU, "VCHR90003", "V1002", "INV-9003", "2026-07-28", "2026-08-27",
         64_000.00, "P", "O", "P", CURR, "N"),        # large, due this month
        (BU, "VCHR90004", "V1003", "INV-9004", "2026-07-20", "2026-08-19",
         5_150.00, "R", "O", "U", CURR, "N"),         # recycle + unposted
    ]
    # Payables for the supplier family, so the rollup has something to add
    # and the subsidiaries are not empty rows on a screen.
    _vouchers += [
        (BU, "VCHR90005", "V1004", "INV-9005", "2026-06-18", "2026-07-18",
         22_300.00, "P", "O", "P", CURR, "N"),        # past due, a subsidiary
        (BU, "VCHR90006", "V1005", "INV-9006", "2026-07-25", "2026-08-24",
         9_450.00, "P", "O", "P", CURR, "N"),
        # The lookalike. Its exposure must never be added to Ridgeline's.
        (BU, "VCHR90007", "V1006", "INV-9007", "2026-07-30", "2026-08-29",
         31_000.00, "P", "O", "P", CURR, "N"),
        # The shared-bank pair's other half, so the alert lands on a
        # supplier that actually has money moving through it.
        (BU, "VCHR90008", "V1007", "INV-9008", "2026-07-12", "2026-08-11",
         14_800.00, "P", "O", "P", CURR, "N"),
    ]
    # A staged duplicate pair (same vendor + invoice number vouchered
    # twice) and a same-amount near-pair — closed, dated MARCH so the AP
    # tie's 90-day window and open payables are untouched. The duplicate-
    # payments audit needs something real to find.
    _vouchers += [
        (BU, "VCHR80001", "V1001", "INV-DUP01", "2026-03-12", "2026-04-11",
         7_800.00, "P", "C", "P", CURR, "N"),
        (BU, "VCHR80002", "V1001", "INV-DUP01", "2026-03-14", "2026-04-13",
         7_800.00, "P", "C", "P", CURR, "N"),
        (BU, "VCHR80003", "V1002", "INV-8003", "2026-03-20", "2026-04-19",
         12_400.00, "P", "C", "P", CURR, "N"),
        (BU, "VCHR80004", "V1002", "INV-8004", "2026-03-23", "2026-04-22",
         12_400.00, "P", "C", "P", CURR, "N"),
    ]
    # Xref rows for the staged duplicates — the close-status FALLBACK
    # decides paid-ness purely from xref EXISTENCE, so these keep a site
    # missing CLOSE_STATUS from seeing four staged duplicates as open
    # payables. Deliberately NO PS_PAYMENT_TBL rows: payment rankings and
    # totals must not move for rows staged only to be found by an audit.
    for i, (vid, amt) in enumerate([("VCHR80001", 7_800.00),
                                    ("VCHR80002", 7_800.00),
                                    ("VCHR80003", 12_400.00),
                                    ("VCHR80004", 12_400.00)], start=1):
        _xref.append((BU, vid, f"PMT8000{i}", amt))
    # ---- the purchase-to-pay chain, under V1009 only -------------------
    # Six orders, one deliberate state each, so every branch of the
    # three-way match has something real to find — and each break's figures
    # are chosen to be visibly wrong, not rounding noise.
    #
    #   PO2001  the CLEAN chain: ordered 8,500 = received = vouchered = paid
    #   PO2002  PRICE break: received at 25.00, vouchered at 28.75 (+750)
    #   PO2003  QTY break: 50 vouchered, 30 received (2,400 not received)
    #   PO2004  NO RECEIPT: vouchered 12,000 against nothing received
    #   PO2005  NEVER INVOICED: received 2,400 in June, no voucher since
    #   PO2006  the trap — CANCELED. Nothing received, and it must never
    #           be reported as "awaiting receipt".
    #   PO2007  genuinely AWAITING: dispatched, nothing arrived. The pair
    #           with PO2006 — identical in every column the check reads
    #           except PO_STATUS, which is the whole point.
    #
    # The three open vouchers are due mid-September, so the overdue pin at
    # 2026-08-04 (49,950.00) does not move; only count and open totals do,
    # and the tests state the new arithmetic.
    con.executemany(
        "INSERT INTO PS_PO_HDR VALUES (?,?,?,?,?,?)",
        [(BU, "PO2001", "V1009", "D", "2026-06-10", CURR),
         (BU, "PO2002", "V1009", "D", "2026-07-01", CURR),
         (BU, "PO2003", "V1009", "D", "2026-07-05", CURR),
         (BU, "PO2004", "V1009", "D", "2026-07-20", CURR),
         (BU, "PO2005", "V1009", "D", "2026-06-15", CURR),
         (BU, "PO2006", "V1009", "X", "2026-05-10", CURR),
         (BU, "PO2007", "V1009", "D", "2026-07-25", CURR)])
    con.executemany(
        "INSERT INTO PS_PO_LINE VALUES (?,?,?,?)",
        [(BU, "PO2001", 1, "Machined brackets, stainless"),
         (BU, "PO2002", 1, "Anodized housings"),
         (BU, "PO2003", 1, "Precision spindles"),
         (BU, "PO2004", 1, "Line retooling service"),
         (BU, "PO2005", 1, "Tooling inserts"),
         (BU, "PO2006", 1, "Prototype fixtures (canceled)"),
         (BU, "PO2007", 1, "Carbide end mills")])
    con.executemany(
        "INSERT INTO PS_PO_LINE_SHIP VALUES (?,?,?,?,?,?,?)",
        [(BU, "PO2001", 1, 1, 100.0, 85.00, 8_500.00),
         (BU, "PO2002", 1, 1, 200.0, 25.00, 5_000.00),
         (BU, "PO2003", 1, 1, 50.0, 120.00, 6_000.00),
         (BU, "PO2004", 1, 1, 1.0, 12_000.00, 12_000.00),
         (BU, "PO2005", 1, 1, 40.0, 60.00, 2_400.00),
         (BU, "PO2006", 1, 1, 10.0, 100.00, 1_000.00),
         (BU, "PO2007", 1, 1, 25.0, 90.00, 2_250.00)])
    con.executemany(
        "INSERT INTO PS_RECV_HDR VALUES (?,?,?,?,?)",
        [(BU, "RECV3001", "V1009", "2026-06-20", "C"),
         (BU, "RECV3002", "V1009", "2026-07-08", "C"),
         (BU, "RECV3003", "V1009", "2026-07-15", "C"),
         (BU, "RECV3004", "V1009", "2026-06-28", "O")])
    con.executemany(
        "INSERT INTO PS_RECV_LN_SHIP VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(BU, "RECV3001", 1, 1, BU, "PO2001", 1, 1, 100.0, 8_500.00),
         (BU, "RECV3002", 1, 1, BU, "PO2002", 1, 1, 200.0, 5_000.00),
         (BU, "RECV3003", 1, 1, BU, "PO2003", 1, 1, 30.0, 3_600.00),
         (BU, "RECV3004", 1, 1, BU, "PO2005", 1, 1, 40.0, 2_400.00)])
    _vouchers += [
        (BU, "VCHR2001", "V1009", "INV-PO-2001", "2026-06-25", "2026-07-25",
         8_500.00, "P", "C", "P", CURR, "T"),
        (BU, "VCHR2002", "V1009", "INV-PO-2002", "2026-07-12", "2026-09-10",
         5_750.00, "P", "O", "P", CURR, "E"),
        (BU, "VCHR2003", "V1009", "INV-PO-2003", "2026-07-18", "2026-09-15",
         6_000.00, "P", "O", "P", CURR, "E"),
        (BU, "VCHR2004", "V1009", "INV-PO-2004", "2026-07-28", "2026-09-20",
         12_000.00, "P", "O", "P", CURR, "E"),
    ]
    con.executemany(
        "INSERT INTO PS_VOUCHER_LINE VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(BU, "VCHR2001", 1, BU, "PO2001", 1, 1, "RECV3001", 1,
          100.0, 85.00, 8_500.00, "Machined brackets, stainless"),
         (BU, "VCHR2002", 1, BU, "PO2002", 1, 1, "RECV3002", 1,
          200.0, 28.75, 5_750.00, "Anodized housings"),
         (BU, "VCHR2003", 1, BU, "PO2003", 1, 1, "RECV3003", 1,
          50.0, 120.00, 6_000.00, "Precision spindles"),
         (BU, "VCHR2004", 1, BU, "PO2004", 1, 1, "", 0,
          1.0, 12_000.00, 12_000.00, "Line retooling service")])
    # The clean chain ends PAID — the payment row keeps V1009 out of every
    # per-vendor payment pin (those are keyed to other vendors).
    _payments.append((SETID, "PAY91001", "V1009", "2026-07-20",
                      8_500.00, CURR, "P"))
    _xref.append((BU, "VCHR2001", "PAY91001", 8_500.00))
    con.executemany("INSERT INTO PS_VOUCHER VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    _vouchers)
    # Asset register: two categories, one retirement, one fully-in-service
    # add this year, plus a disposed asset that must not count as in service.
    con.executemany(
        "INSERT INTO PS_ASSET VALUES (?,?,?,?,?)",
        [(BU, "A-0001", "Warehouse forklift", "I", "2024-03-15"),
         (BU, "A-0002", "Server cluster",     "I", "2025-01-10"),
         (BU, "A-0003", "Office build-out",   "I", "2026-02-01"),
         (BU, "A-0004", "Retired delivery van", "D", "2021-06-01")])
    con.executemany(
        "INSERT INTO PS_COST VALUES (?,?,?,?,?,?,?,?)",
        [(BU, "A-0001", "CORP", "2024-03-15", "ADD", "MACH", 62_000.00, CURR),
         (BU, "A-0002", "CORP", "2025-01-10", "ADD", "IT",   184_000.00, CURR),
         (BU, "A-0002", "CORP", "2026-03-20", "ADJ", "IT",    12_500.00, CURR),
         (BU, "A-0003", "CORP", "2026-02-01", "ADD", "FURN",  48_300.00, CURR),
         (BU, "A-0004", "CORP", "2021-06-01", "ADD", "AUTO",  35_000.00, CURR),
         (BU, "A-0004", "CORP", "2026-04-30", "RET", "AUTO", -35_000.00, CURR)])
    # Projects: one healthy, one OVER BUDGET, one stale with budget left.
    con.executemany(
        "INSERT INTO PS_PROJECT VALUES (?,?,?,?)",
        [(BU, "PRJ-100", "ERP Upgrade Phase 2", "A"),
         (BU, "PRJ-200", "Warehouse Automation", "A"),
         (BU, "PRJ-300", "Legacy Decommission", "A")])
    _pr = []
    for proj, atype, mon, amt in [
        ("PRJ-100", "BUD", 1, 250_000.00), ("PRJ-100", "ACT", 3, 80_000.00),
        ("PRJ-100", "ACT", 5, 95_000.00),  ("PRJ-100", "ACT", 7, 40_000.00),
        ("PRJ-200", "BUD", 1, 120_000.00), ("PRJ-200", "ACT", 4, 70_000.00),
        ("PRJ-200", "ACT", 6, 85_000.00),  # 155k vs 120k -> overrun
        ("PRJ-300", "BUD", 1, 90_000.00),  ("PRJ-300", "ACT", 2, 15_000.00),
    ]:
        _pr.append((BU, proj, "GEN", atype, month_end(2026, mon), amt, CURR))
    con.executemany("INSERT INTO PS_PROJ_RESOURCE VALUES (?,?,?,?,?,?,?)", _pr)
    con.executemany("INSERT INTO PS_PAYMENT_TBL VALUES (?,?,?,?,?,?,?)", _payments)
    con.executemany("INSERT INTO PS_PYMNT_VCHR_XREF VALUES (?,?,?,?)", _xref)
    con.executemany(
        "INSERT INTO PSRECFIELD VALUES (?,?,?)",
        [("LEDGER", "BUSINESS_UNIT", 1), ("LEDGER", "LEDGER", 2),
         ("LEDGER", "ACCOUNT", 3), ("BI_HDR", "INVOICE", 1),
         ("BI_HDR", "BILL_STATUS", 2), ("ITEM", "CUST_ID", 1),
         ("ITEM", "BAL_AMT", 2), ("TU_FILE_INTFC", "FILE_ID", 1),
         ("TU_FILE_INTFC", "FILE_PATH", 2)],
    )

    # ---- the "how work is DONE" layer: pages, components, navigation ----
    # Delivered FSCM names for the flows this sample already carries, so the
    # process graph has a real chain to walk: a menu path reaches a
    # component, the component holds pages, and each page names the records
    # it reads. Abridged (a delivered component has a dozen pages) but not
    # invented — these are the names a functional consultant would recognise.
    _pages = [
        ("BI_HDR", "Bill Header - Info 1", ["BI_HDR", "CUSTOMER"]),
        ("BI_LINE", "Bill Line", ["BI_LINE", "BI_HDR"]),
        ("BI_HDR_ADDR", "Bill Header - Address", ["BI_HDR", "CUSTOMER"]),
        ("BI_IVC_SUMMARY", "Invoice Summary", ["BI_HDR", "BI_LINE"]),
        ("BI_INTFC_SEARCH", "Billing Interface Search", ["INTFC_BI"]),
        ("ITEM_MAINT", "Item Maintenance", ["ITEM", "CUSTOMER"]),
        ("ITEM_LIST", "Item List", ["ITEM"]),
        ("PAYMENT_WS", "Payment Worksheet", ["PAYMENT", "ITEM"]),
        ("CUST_GENERAL1", "General Info", ["CUSTOMER"]),
        ("CUST_BILLTO", "Bill To Options", ["CUSTOMER"]),
        ("VCHR_EXPRESS1", "Voucher - Invoice Information",
         ["VOUCHER", "VOUCHER_LINE", "VENDOR"]),
        ("VCHR_LINE", "Voucher Line", ["VOUCHER_LINE", "DISTRIB_LINE"]),
        ("PYMNT_EXPRESS", "Express Payment", ["PAYMENT_TBL",
                                              "PYMNT_VCHR_XREF"]),
        ("VNDR_ID1", "Supplier Identifying Information", ["VENDOR"]),
        ("JOURNAL_ENTRY1", "Journal Header", ["JRNL_HEADER"]),
        ("JOURNAL_ENTRY2", "Journal Lines", ["JRNL_LN", "JRNL_HEADER"]),
        ("LEDGER_INQ", "Ledger Inquiry", ["LEDGER", "GL_ACCOUNT_TBL"]),
        ("BUS_UNIT_TBL_FS1", "Business Unit Definition",
         ["BUS_UNIT_TBL_FS"]),
    ]
    con.executemany("INSERT INTO PSPNLDEFN VALUES (?,?)",
                    [(p, d) for p, d, _ in _pages])
    con.executemany(
        "INSERT INTO PSPNLFIELD VALUES (?,?,?,?)",
        [(page, i + 1, rec, "")
         for page, _, recs in _pages for i, rec in enumerate(recs)])

    # component -> its pages, and the portal path that reaches the component
    _components = [
        ("BI_ENTRY", ["BI_HDR", "BI_LINE", "BI_HDR_ADDR"],
         "Billing > Maintain Bills > Standard Billing"),
        ("BI_INVOICE_SUM", ["BI_IVC_SUMMARY"],
         "Billing > Review Billing Information > Summary"),
        ("BI_INTFC_CORRECT", ["BI_INTFC_SEARCH"],
         "Billing > Interface Transactions > Correct Interface Errors"),
        ("ITEM_MAINTAIN", ["ITEM_MAINT", "ITEM_LIST"],
         "Accounts Receivable > Customer Accounts > Item Information"),
        ("PAYMENT_WORKSHEET", ["PAYMENT_WS"],
         "Accounts Receivable > Payments > Apply Payments"),
        ("CUSTOMER_GENERAL", ["CUST_GENERAL1", "CUST_BILLTO"],
         "Customers > Customer Information > General Information"),
        ("VCHR_EXPRESS", ["VCHR_EXPRESS1", "VCHR_LINE"],
         "Accounts Payable > Vouchers > Add/Update > Regular Entry"),
        ("PYMNT_EXPRESS", ["PYMNT_EXPRESS"],
         "Accounts Payable > Payments > Express Payment"),
        ("VNDR_ID", ["VNDR_ID1"],
         "Suppliers > Supplier Information > Add/Update > Supplier"),
        ("JOURNAL_ENTRY_IE", ["JOURNAL_ENTRY1", "JOURNAL_ENTRY2"],
         "General Ledger > Journals > Journal Entry > Create/Update"),
        ("LEDGER_INQUIRY", ["LEDGER_INQ"],
         "General Ledger > Review Financial Information > Ledger"),
        ("BUS_UNIT_TBL_FS", ["BUS_UNIT_TBL_FS1"],
         "Set Up Financials/Supply Chain > Business Unit Related > "
         "General Ledger > General Ledger Definition"),
    ]
    con.executemany(
        "INSERT INTO PSPNLGROUP VALUES (?,?,?,?)",
        [(comp, page, i + 1, "GBL")
         for comp, pages, _ in _components for i, page in enumerate(pages)])
    con.executemany(
        "INSERT INTO PSPRSMDEFN VALUES (?,?,?,?,?)",
        [("EMPLOYEE", f"{comp}_GBL", "PORTAL_ROOT_OBJECT", label, comp)
         for comp, _, label in _components])
    con.executemany(
        "INSERT INTO PS_BUS_UNIT_LED VALUES (?,?)",
        [(BU, "ACTUALS"), (BU, "BUDGETS")],
    )
    con.executemany(
        "INSERT INTO PS_LED_GRP_TBL VALUES (?,?,?)",
        [("ACTUALS", LEDGER, "Actuals ledger group"),
         ("BUDGETS", "BUDGET", "Budget ledger group")],
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

    # ---- AR / Billing sample (column subsets of the delivered records) ----
    customers = [
        ("C1001", "ACME Industrial"), ("C1002", "Northwind Retail"),
        ("C1003", "Cascade Foods"), ("C1004", "Beacon Health Systems"),
        ("C1005", "Orion Logistics"), ("C1006", "Summit Media"),
        ("C1007", "Redwood Utilities"),
    ]
    # A corporate family, because a hierarchy of one proves nothing: ACME
    # Industrial is the parent of two subsidiaries that bill separately, so
    # "which subsidiaries drive this parent's overdue balance" has a real
    # answer. Everyone else is their own parent, which is how PeopleSoft
    # represents an unaffiliated customer.
    FAMILY = {"C1009": "C1001", "C1010": "C1001"}
    customers = customers + [("C1009", "ACME Industrial - West"),
                             ("C1010", "ACME Industrial - Components"),
                             # Same name, different company. It is its own
                             # corporate parent, so nothing may fold it into
                             # the ACME family — the trap that any
                             # name-similarity match walks straight into.
                             ("C1011", "ACME Logistics Group")]
    cust_rows = [(SETID, cid, name, "A", SETID, FAMILY.get(cid, cid))
                 for cid, name in customers]
    cust_rows.append((SETID, "C1008", "Harborview Hotels", "I",
                      SETID, "C1008"))
    con.executemany("INSERT INTO PS_CUSTOMER VALUES (?,?,?,?,?,?)", cust_rows)
    con.execute("INSERT INTO PS_SET_CNTRL_REC VALUES (?,?,?)", (BU, "CUSTOMER", SETID))

    # Open items sum EXACTLY to the GL AR control (1100) balance at 2026-06-30,
    # via a computed plug — so aging ties to the ledger to the penny.
    # (cust, item, orig, bal, acctg_dt, due_dt, dispute)
    ar_target = one(
        "SELECT SUM(POSTED_TOTAL_AMT) FROM PS_LEDGER WHERE LEDGER='ACTUALS' "
        "AND ACCOUNT='1100' AND FISCAL_YEAR=2026 AND ACCOUNTING_PERIOD BETWEEN 0 AND 6"
    )
    items = [
        ("C1001", "INV-260501", 38_500.00, "2026-05-14", "2026-06-14", ""),
        ("C1001", "INV-260602", 61_250.00, "2026-06-12", "2026-07-12", ""),
        ("C1002", "INV-260420", 27_300.00, "2026-04-20", "2026-05-20", ""),
        ("C1002", "INV-260605", 44_800.00, "2026-06-20", "2026-07-20", ""),
        ("C1002", "CM-260012", -8_400.00, "2026-06-25", "2026-06-25", ""),
        ("C1003", "INV-260610", 96_000.00, "2026-06-25", "2026-07-25", ""),
        ("C1003", "INV-260618", 58_700.00, "2026-06-30", "2026-08-15", ""),
        ("C1004", "INV-251120", 42_000.00, "2025-11-20", "2025-12-20", "DSP"),
        ("C1004", "INV-260601", 88_400.00, "2026-06-15", "2026-07-15", ""),
        ("C1005", "INV-260528", 73_900.00, "2026-05-28", "2026-06-27", ""),
        ("C1005", "OA-260701", -15_000.00, "2026-06-30", "2026-07-01", ""),
        ("C1006", "INV-260612", 49_300.00, "2026-06-27", "2026-07-27", ""),
        ("C1007", "INV-260609", 122_600.00, "2026-06-17", "2026-08-01", ""),
        ("C1007", "INV-260415", 31_800.00, "2026-04-15", "2026-05-15", ""),
        ("C1008", "INV-260210", 12_500.00, "2026-02-10", "2026-03-12", ""),
        # The two subsidiaries bill in their own right. One of them is the
        # only overdue member of the family, which is the whole point of
        # being able to ask the parent about its children.
        ("C1009", "INV-260620", 34_750.00, "2026-06-20", "2026-07-20", ""),
        ("C1010", "INV-260505", 19_900.00, "2026-05-05", "2026-06-04", ""),
    ]
    # real PS_ITEM rows can carry NULL DUE_DT; one is seeded deliberately
    items.append(("C1006", "DM-260620", 4_200.00, "2026-06-20", None, ""))
    # One OPEN EUR item so multi-currency aging is exercised. Its USD
    # equivalent (3,000 / 0.92 = 3,260.87 at the seeded EUR->USD rate) is
    # carved out of the plug so the converted subledger still ties to GL 1100
    # to the penny.
    # Cash already received and applied. An open item's BALANCE is what is
    # still owed; its ORIGINAL amount is what was billed. Seeding them equal
    # everywhere would mean no customer had ever paid anything, and every
    # payment question would have the same empty answer.
    PAID = {"INV-260602": 25_000.00,   # paid down, still open
            "INV-260609": 60_000.00,
            "INV-260601": 20_000.00}   # partly paid; the rest sits unapplied
    eur_open, eur_usd_equiv = 3_000.00, 3_260.87
    plug = r2(ar_target - sum(a - PAID.get(i, 0.0)
                              for _, i, a, _, _, _ in items) - eur_usd_equiv)
    assert plug > 0, f"AR plug went negative: {plug}"
    items.append(("C1001", "INV-260614", plug, "2026-06-29", "2026-08-08", ""))
    item_rows = [
        (BU, cid, item, 1, "O", r2(amt - PAID.get(item, 0.0)), amt, CURR,
         acctg, due, acctg, disp, "")
        for cid, item, amt, acctg, due, disp in items
    ]
    # a few closed items for realism (excluded by ITEM_STATUS filters)
    item_rows += [
        (BU, "C1001", "INV-260301", 1, "C", 0.0, 52_000.00, CURR,
         "2026-03-05", "2026-04-04", "2026-03-05", "", ""),
        (BU, "C1006", "INV-260315", 1, "C", 0.0, 18_750.00, CURR,
         "2026-03-18", "2026-04-17", "2026-03-18", "", ""),
        # The credit that closed INV-260301. See REBILL below.
        (BU, "C1001", "CM-260301", 1, "C", 0.0, -52_000.00, CURR,
         "2026-04-02", "2026-04-02", "2026-04-02", "", ""),
    ]
    con.executemany("INSERT INTO PS_ITEM VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", item_rows)

    # Billing headers: one finalized (INV) header per AR invoice item, plus a
    # pipeline of not-yet-finalized work and one finalized-but-not-in-AR orphan.
    # Credit and rebill: March's invoice to ACME was credited in full and
    # rebilled in May for 13,500 less. Each row on its own looks ordinary;
    # only the chain shows the money that never came back. item -> the
    # invoice it corrects, and what kind of correction it is.
    REBILL = {"CM-260301": ("INV-260301", "CR"),
              "INV-260501": ("INV-260301", "RB")}
    hdrs = [
        (BU, item, "INV", cid, acctg, acctg, amt, CURR, "STD", "CRM",
         *REBILL.get(item, ("", "")))
        for cid, item, amt, acctg, _due, _d in items
        if item.startswith("INV-")
    ]
    hdrs += [
        (BU, "INV-260301", "INV", "C1001", "2026-03-05", "2026-03-05",
         52_000.00, CURR, "STD", "CRM", "", ""),
        (BU, "CM-260301", "INV", "C1001", "2026-04-02", "2026-04-02",
         -52_000.00, CURR, "STD", "CRM", *REBILL["CM-260301"]),
    ]
    hdrs += [
        (BU, "INV-260701", "RDY", "C1003", "2026-07-18", "2026-07-18", 21_400.00, CURR, "STD", "CRM", "", ""),
        (BU, "INV-260702", "RDY", "C1006", "2026-07-24", "2026-07-24", 9_850.00, CURR, "SVC", "PROJ", "", ""),
        (BU, "INV-260703", "HLD", "C1004", "2026-07-10", "2026-07-10", 33_000.00, CURR, "STD", "CRM", "", ""),
        (BU, "INV-260704", "NEW", "C1002", "2026-07-28", "2026-07-28", 5_600.00, CURR, "SVC", "MAN", "", ""),
        (BU, "INV-260630", "CAN", "C1005", "2026-06-30", "2026-06-30", 14_000.00, CURR, "STD", "CRM", "", ""),
        (BU, "INV-2606ORPH", "INV", "C1007", "2026-06-28", "2026-06-28", 27_500.00, CURR, "STD", "CRM", "", ""),
    ]
    con.executemany("INSERT INTO PS_BI_HDR VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", hdrs)

    # Cash received, and what it was applied to. The two are separate on
    # purpose: a payment that arrived and was never applied still shows as
    # money you are holding while the customer still shows as owing it.
    # Applied amounts here must equal PAID above, or the subledger stops
    # agreeing with itself.
    con.executemany(
        "INSERT INTO PS_PAYMENT VALUES (?,?,?,?,?,?,?,?)",
        [
            (BU, "DEP-26031", 1, "C1006", 18_750.00, "2026-04-15", CURR, "C"),
            (BU, "DEP-26061", 1, "C1001", 25_000.00, "2026-06-20", CURR, "C"),
            (BU, "DEP-26062", 1, "C1007", 60_000.00, "2026-06-22", CURR, "C"),
            # More cash than they applied it to: 10,000 of this customer's
            # money is sitting on the deposit while their invoice still
            # shows a balance.
            (BU, "DEP-26063", 1, "C1004", 30_000.00, "2026-06-28", CURR, "W"),
            # Received and never applied to anything at all.
            (BU, "DEP-26064", 1, "C1005", 15_000.00, "2026-06-30", CURR, "U"),
        ])
    con.executemany(
        "INSERT INTO PS_PAYMENT_ITEM VALUES (?,?,?,?,?,?,?,?)",
        [
            (BU, "DEP-26031", 1, BU, "C1006", "INV-260315", 18_750.00,
             "2026-04-15"),
            (BU, "DEP-26061", 1, BU, "C1001", "INV-260602", 25_000.00,
             "2026-06-20"),
            (BU, "DEP-26062", 1, BU, "C1007", "INV-260609", 60_000.00,
             "2026-06-22"),
            (BU, "DEP-26063", 1, BU, "C1004", "INV-260601", 20_000.00,
             "2026-06-28"),
        ])

    # Customer geography — primary address per customer (sequence 1).
    con.executemany(
        "INSERT INTO PS_CUST_ADDRESS VALUES (?,?,?,?,?,?)",
        [
            (SETID, "C1001", 1, "Columbus", "OH", "USA"),
            (SETID, "C1002", 1, "Austin", "TX", "USA"),
            (SETID, "C1003", 1, "Portland", "OR", "USA"),
            (SETID, "C1004", 1, "Newark", "NJ", "USA"),
            (SETID, "C1005", 1, "Chicago", "IL", "USA"),
            (SETID, "C1006", 1, "Toronto", "ON", "CAN"),
            (SETID, "C1007", 1, "Denver", "CO", "USA"),
            (SETID, "C1008", 1, "Miami", "FL", "USA"),
            (SETID, "C1009", 1, "Phoenix", "AZ", "USA"),
            (SETID, "C1010", 1, "Columbus", "OH", "USA"),
        ])

    # Bill lines: what each invoice actually charged. Lines sum EXACTLY to
    # the header amount (a 70/30 split into a customer-specific product
    # pair), so line-level product mix reconciles to header billing to the
    # penny — the same coherence rule the AR/GL tie follows.
    catalog = [
        ("LIC-SAAS", "Platform subscription"),
        ("SVC-CONSULT", "Consulting services"),
        ("HW-EQUIP", "Equipment"),
        ("MNT-SUPPORT", "Support & maintenance"),
        ("FRT", "Freight & handling"),
    ]
    line_rows = []
    for bu_, inv, status, cid, *_rest, amt, cur, _bt, _bs in [
        (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], h[8], h[9])
        for h in hdrs
    ]:
        k = int(cid[1:]) % len(catalog)
        primary, secondary = catalog[k], catalog[(k + 2) % len(catalog)]
        first = r2(amt * 0.7)
        rest = r2(amt - first)
        line_rows.append((bu_, inv, 1, primary[0], primary[1], first))
        if rest:
            line_rows.append((bu_, inv, 2, secondary[0], secondary[1], rest))
    con.executemany("INSERT INTO PS_BI_LINE VALUES (?,?,?,?,?,?)", line_rows)

    con.executemany(
        "INSERT INTO PS_INTFC_BI VALUES (?,?,?,?,?,?,?,?)",
        [
            (4001, 1, "LINE", BU, "C1002", "CRM", "DON", "INV-260605"),
            (4001, 2, "LINE", BU, "C1003", "CRM", "ERR", ""),
            (4001, 3, "LINE", BU, "C1006", "PROJ", "NEW", ""),
            (4002, 1, "LINE", BU, "C9999", "MAN", "ERR", ""),
        ],
    )

    # Exchange rates (PS_RT_RATE_TBL subset): effective-dated FROM->TO
    con.executemany(
        "INSERT INTO PS_RT_RATE_TBL VALUES (?,?,?,?,?,?)",
        [
            ("USD", "INR", "CRRNT", "2026-01-01", 83.25, 1.0),
            ("USD", "INR", "CRRNT", "2026-07-01", 84.10, 1.0),
            ("USD", "EUR", "CRRNT", "2026-01-01", 0.92, 1.0),
            ("EUR", "USD", "CRRNT", "2026-01-01", 1.0, 0.92),
            ("USD", "GBP", "CRRNT", "2026-01-01", 0.79, 1.0),
        ],
    )
    # One finalized EUR invoice (closed in AR so aging/tie stay single-currency)
    con.execute("INSERT INTO PS_BI_HDR VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (BU, "INV-2606EUR", "INV", "C1006", "2026-06-22", "2026-06-22",
                 2_000.00, "EUR", "STD", "CRM", "", ""))
    con.execute("INSERT INTO PS_ITEM VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (BU, "C1006", "INV-2606EUR", 1, "C", 0.0, 2_000.00, "EUR",
                 "2026-06-22", "2026-07-22", "2026-06-22", "", ""))
    con.execute("INSERT INTO PS_ITEM VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (BU, "C1006", "INV-2606EU2", 1, "O", eur_open, eur_open, "EUR",
                 "2026-06-24", "2026-07-24", "2026-06-24", "", ""))
    con.execute("INSERT INTO PS_BI_HDR VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (BU, "INV-2606EU2", "INV", "C1006", "2026-06-24", "2026-06-24",
                 eur_open, "EUR", "STD", "CRM", "", ""))

    con.executescript(VIEWS)
    con.executescript(INDEXES)
    con.commit()

    # ---- verification ----------------------------------------------------
    for fy in (2025, 2026):
        total = one(
            "SELECT SUM(POSTED_TOTAL_AMT) FROM PS_LEDGER "
            "WHERE FISCAL_YEAR=? AND LEDGER='ACTUALS'", fy
        )
        assert abs(total) < 0.01, f"FY{fy} ACTUALS ledger does not net to zero: {total}"
    n_bud = one("SELECT COUNT(*) FROM PS_LEDGER WHERE LEDGER='BUDGET'")
    assert n_bud > 0, "BUDGET ledger missing"
    ar_usd = one("SELECT SUM(BAL_AMT) FROM PS_ITEM WHERE ITEM_STATUS='O' "
                 "AND BAL_CURRENCY='USD'")
    ar_items = r2(ar_usd + eur_usd_equiv)  # converted subledger
    ar_gl = one(
        "SELECT SUM(POSTED_TOTAL_AMT) FROM PS_LEDGER WHERE LEDGER='ACTUALS' "
        "AND ACCOUNT='1100' AND FISCAL_YEAR=2026 AND ACCOUNTING_PERIOD BETWEEN 0 AND 6"
    )
    assert abs(ar_items - ar_gl) < 0.01, f"AR subledger {ar_items} != GL {ar_gl}"
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
    print(f"  BUDGET ledger rows:  {n_bud}")
    print(f"  AR open items tie to GL 1100: {ar_items:,.2f} ✔")
    print(f"  FY2025 and FY2026 ACTUALS net to zero ✔")
    print(f"  Cash (1000) ending FY2026 P6: {cash_p6:,.2f}")


if __name__ == "__main__":
    sys.exit(main())
