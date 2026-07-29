"""SQL text builders for PeopleSoft GL queries.

Two source modes:
  - inline (default): joins base PS_* tables directly with effective-date logic,
    so nothing needs to be created in the database.
  - views: reads the XX_TB_* views (sql/oracle/*.sql) once a DBA deploys them.

Ledger amounts are signed: debits positive, credits negative.
"""
from __future__ import annotations

import re
from typing import Optional

from .db import Database

# Chartfields allowed as extra group-by / detail columns on PS_LEDGER.
GROUPABLE_CHARTFIELDS = {
    "DEPTID",
    "OPERATING_UNIT",
    "PRODUCT",
    "FUND_CODE",
    "CLASS_FLD",
    "PROGRAM_CODE",
    "BUDGET_REF",
    "AFFILIATE",
    "PROJECT_ID",
    "ALTACCT",
    "CURRENCY_CD",
}

IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#.]{0,63}$")


def basis_clause(db: Database, amount_basis: str, currency: str, params: dict,
                 alias: str = "L") -> str:
    """Currency / amount-basis contract, applied identically by every tool.

    PS_LEDGER holds one row per currency AND per statistics code. Summing
    POSTED_TOTAL_AMT without these predicates mixes transaction-currency rows
    and non-monetary statistical rows into the total, which is the usual reason
    a generated trial balance does not tie to PeopleSoft.

      base        -> POSTED_TOTAL_AMT where CURRENCY_CD = BASE_CURRENCY
      transaction -> POSTED_TRAN_AMT, caller groups by CURRENCY_CD
    Statistical rows (STATISTICS_CODE populated) are always excluded; they are
    headcount/area/units, not money.
    """
    if db.cfg.db.use_views:
        # The XX_TB_* views encode the same contract; don't double-apply it.
        clause = ""
    else:
        clause = (
            f" AND ({alias}.STATISTICS_CODE IS NULL OR "
            f"TRIM({alias}.STATISTICS_CODE) = '')"
        )
        if (amount_basis or "base").lower() == "base" and not currency:
            clause += f" AND {alias}.CURRENCY_CD = {alias}.BASE_CURRENCY"
    if currency and currency.lower() != "detail":
        params["curr"] = currency.upper()
        clause += f" AND {alias}.CURRENCY_CD = :curr"
    return clause


def amount_col(db: Database, amount_basis: str, alias: str = "L") -> str:
    if db.cfg.db.use_views:
        return f"{alias}.POSTED_TOTAL_AMT"
    if (amount_basis or "base").lower() == "transaction":
        return f"{alias}.POSTED_TRAN_AMT"
    return f"{alias}.POSTED_TOTAL_AMT"


def acct_join(db: Database) -> str:
    """Effective-dated LEFT JOIN from ledger alias L to PS_GL_ACCOUNT_TBL alias A."""
    p, today = db.prefix, db.today_expr()
    return f"""LEFT JOIN {p}PS_GL_ACCOUNT_TBL A
       ON A.SETID = :setid
      AND A.ACCOUNT = L.ACCOUNT
      AND A.EFFDT = (SELECT MAX(AX.EFFDT) FROM {p}PS_GL_ACCOUNT_TBL AX
                      WHERE AX.SETID = A.SETID
                        AND AX.ACCOUNT = A.ACCOUNT
                        AND AX.EFFDT <= {today})"""


def account_filter(account: str, params: dict, col: str = "L.ACCOUNT") -> str:
    """Build a filter clause for '6000-6999' (range), '1000,1100' (list),
    '60%' (prefix wildcard) or '1000' (exact)."""
    a = (account or "").strip()
    if not a:
        return ""
    if re.fullmatch(r"[\w]+\s*-\s*[\w]+", a):
        lo, hi = [x.strip() for x in a.split("-", 1)]
        params["acc_lo"], params["acc_hi"] = lo, hi
        return f" AND {col} BETWEEN :acc_lo AND :acc_hi"
    if "," in a:
        names = []
        for i, part in enumerate(x.strip() for x in a.split(",") if x.strip()):
            params[f"acc{i}"] = part
            names.append(f":acc{i}")
        return f" AND {col} IN ({', '.join(names)})"
    if a.endswith("%"):
        params["acc"] = a
        return f" AND {col} LIKE :acc"
    params["acc"] = a
    return f" AND {col} = :acc"


def _adj_clause(include_adj: bool, adj_periods: list) -> str:
    if not include_adj or not adj_periods:
        return ""
    nums = ", ".join(str(int(x)) for x in adj_periods)
    return f" OR L.ACCOUNTING_PERIOD IN ({nums})"


def tb_period_sums(
    db: Database,
    *,
    extras: list[str],
    include_adj: bool,
    adj_periods: list,
    dept: str,
    currency: str,
    account: str,
    params: dict,
    amount_basis: str = "base",
) -> str:
    """Period-level sums by account (+extras), through :maxper for :bu/:ledger/:fy."""
    p = db.prefix
    extra_sel = "".join(f", L.{c} AS {c.lower()}" for c in extras)
    extra_grp = "".join(f", L.{c}" for c in extras)
    where_extra = ""
    if dept:
        params["dept"] = dept
        where_extra += " AND L.DEPTID = :dept"
    where_extra += basis_clause(db, amount_basis, currency, params)
    where_extra += account_filter(account, params)
    amt = amount_col(db, amount_basis)

    if db.cfg.db.use_views:
        src = f"{p}XX_TB_BAL_VW L"
        attr_sel = "L.DESCR AS descr, L.ACCOUNT_TYPE AS acct_type, L.EFF_STATUS AS eff_status"
        attr_grp = ", L.DESCR, L.ACCOUNT_TYPE, L.EFF_STATUS"
        join = ""
    else:
        src = f"{p}PS_LEDGER L"
        attr_sel = "A.DESCR AS descr, A.ACCOUNT_TYPE AS acct_type, A.EFF_STATUS AS eff_status"
        attr_grp = ", A.DESCR, A.ACCOUNT_TYPE, A.EFF_STATUS"
        join = acct_join(db)

    return f"""SELECT L.ACCOUNT AS account, {attr_sel}{extra_sel},
       L.ACCOUNTING_PERIOD AS period, SUM({amt}) AS amt
  FROM {src}
  {join}
 WHERE L.BUSINESS_UNIT = :bu
   AND L.LEDGER = :ledger
   AND L.FISCAL_YEAR = :fy
   AND (L.ACCOUNTING_PERIOD BETWEEN 0 AND :maxper{_adj_clause(include_adj, adj_periods)})
   {where_extra}
 GROUP BY L.ACCOUNT{attr_grp}{extra_grp}, L.ACCOUNTING_PERIOD"""


def journal_lines(db: Database, dept: str, params: dict) -> str:
    p = db.prefix
    dept_clause = ""
    if dept:
        params["dept"] = dept
        dept_clause = " AND J.DEPTID = :dept"
    return f"""SELECT H.JOURNAL_ID AS journal_id, H.JOURNAL_DATE AS journal_date,
       H.SOURCE AS source, H.OPRID AS oprid, H.DESCR254_MIXED AS jrnl_descr,
       J.JOURNAL_LINE AS line_nbr, J.DEPTID AS deptid, J.CURRENCY_CD AS currency_cd,
       J.MONETARY_AMOUNT AS amount, J.LINE_DESCR AS line_descr
  FROM {p}PS_JRNL_LN J
  JOIN {p}PS_JRNL_HEADER H
    ON H.BUSINESS_UNIT = J.BUSINESS_UNIT
   AND H.JOURNAL_ID = J.JOURNAL_ID
   AND H.JOURNAL_DATE = J.JOURNAL_DATE
   AND H.UNPOST_SEQ = J.UNPOST_SEQ
 WHERE J.BUSINESS_UNIT = :bu
   AND J.LEDGER = :ledger
   AND J.ACCOUNT = :acct
   AND H.FISCAL_YEAR = :fy
   AND H.ACCOUNTING_PERIOD = :per
   AND H.JRNL_HDR_STATUS = 'P'{dept_clause}
 ORDER BY H.JOURNAL_DATE, H.JOURNAL_ID, J.JOURNAL_LINE"""


def unposted_journals(db: Database) -> str:
    p = db.prefix
    return f"""SELECT JOURNAL_ID AS journal_id, JOURNAL_DATE AS journal_date,
       ACCOUNTING_PERIOD AS period, JRNL_HDR_STATUS AS status,
       SOURCE AS source, OPRID AS oprid, DESCR254_MIXED AS descr
  FROM {p}PS_JRNL_HEADER
 WHERE BUSINESS_UNIT = :bu
   AND FISCAL_YEAR = :fy
   AND ACCOUNTING_PERIOD BETWEEN 1 AND :maxper
   AND JRNL_HDR_STATUS NOT IN ('P', 'D')
   AND (LEDGER_GROUP = :ledger OR :ledger IS NULL OR LEDGER_GROUP IS NULL)
 ORDER BY ACCOUNTING_PERIOD, JOURNAL_DATE, JOURNAL_ID"""


def out_of_balance_journals(db: Database) -> str:
    p = db.prefix
    return f"""SELECT J.JOURNAL_ID AS journal_id, J.JOURNAL_DATE AS journal_date,
       SUM(J.MONETARY_AMOUNT) AS net_amount
  FROM {p}PS_JRNL_LN J
  JOIN {p}PS_JRNL_HEADER H
    ON H.BUSINESS_UNIT = J.BUSINESS_UNIT
   AND H.JOURNAL_ID = J.JOURNAL_ID
   AND H.JOURNAL_DATE = J.JOURNAL_DATE
   AND H.UNPOST_SEQ = J.UNPOST_SEQ
 WHERE J.BUSINESS_UNIT = :bu
   AND J.LEDGER = :ledger
   AND H.FISCAL_YEAR = :fy
   AND H.ACCOUNTING_PERIOD BETWEEN 0 AND :maxper
   AND H.JRNL_HDR_STATUS = 'P'
 GROUP BY J.JOURNAL_ID, J.JOURNAL_DATE
HAVING ABS(SUM(J.MONETARY_AMOUNT)) > 0.005"""


def cal_resolve(db: Database) -> str:
    p, d = db.prefix, db.date_bind("d")
    return f"""SELECT FISCAL_YEAR AS fiscal_year, ACCOUNTING_PERIOD AS period,
       BEGIN_DT AS begin_dt, END_DT AS end_dt
  FROM {p}PS_CAL_DETP_TBL
 WHERE SETID = :setid AND CALENDAR_ID = :cal
   AND BEGIN_DT <= {d} AND END_DT >= {d}"""


def cal_periods(db: Database) -> str:
    p = db.prefix
    return f"""SELECT FISCAL_YEAR AS fiscal_year, ACCOUNTING_PERIOD AS period,
       BEGIN_DT AS begin_dt, END_DT AS end_dt
  FROM {p}PS_CAL_DETP_TBL
 WHERE SETID = :setid AND CALENDAR_ID = :cal AND FISCAL_YEAR = :fy
 ORDER BY ACCOUNTING_PERIOD"""


def accounts_search(db: Database, acct_type: str, params: dict) -> str:
    p, today = db.prefix, db.today_expr()
    type_clause = ""
    if acct_type:
        params["atype"] = acct_type.upper()
        type_clause = " AND A.ACCOUNT_TYPE = :atype"
    return f"""SELECT A.ACCOUNT AS account, A.DESCR AS descr,
       A.ACCOUNT_TYPE AS acct_type, A.EFF_STATUS AS eff_status
  FROM {p}PS_GL_ACCOUNT_TBL A
 WHERE A.SETID = :setid
   AND A.EFFDT = (SELECT MAX(AX.EFFDT) FROM {p}PS_GL_ACCOUNT_TBL AX
                   WHERE AX.SETID = A.SETID AND AX.ACCOUNT = A.ACCOUNT
                     AND AX.EFFDT <= {today})
   AND (UPPER(A.DESCR) LIKE :qd OR A.ACCOUNT LIKE :qa){type_clause}
 ORDER BY A.ACCOUNT"""


def setid_for(db: Database) -> str:
    return f"""SELECT SETID AS setid FROM {db.prefix}PS_SET_CNTRL_REC
 WHERE SETCNTRLVALUE = :bu AND RECNAME = :recname"""


def trees_list(db: Database) -> str:
    return f"""SELECT SETID AS setid, TREE_NAME AS tree_name, MAX(EFFDT) AS effdt
  FROM {db.prefix}PSTREEDEFN GROUP BY SETID, TREE_NAME ORDER BY TREE_NAME"""


def tree_effdt(db: Database) -> str:
    p, today = db.prefix, db.today_expr()
    return f"""SELECT MAX(EFFDT) AS effdt FROM {p}PSTREEDEFN
 WHERE SETID = :setid AND TREE_NAME = :tree AND EFFDT <= {today}"""


def tree_rollup(db: Database, params: dict, amount_basis: str = "base") -> str:
    """Ending-balance components by tree node at a given level.

    Leaf ranges attach to nodes; ancestor containment uses the node-number span.
    Blank RANGE_TO (stored as '' or ' ') means a single-value leaf.
    """
    p = db.prefix
    d = db.date_bind("teffdt")
    amt = amount_col(db, amount_basis)
    basis = basis_clause(db, amount_basis, "", params)
    return f"""SELECT N.TREE_NODE AS tree_node, MIN(N.TREE_NODE_NUM) AS node_ord,
       L.ACCOUNTING_PERIOD AS period, SUM({amt}) AS amt
  FROM {p}PS_LEDGER L
  JOIN {p}PSTREELEAF LF
    ON LF.SETID = :tsetid AND LF.TREE_NAME = :tree AND LF.EFFDT = {d}
   AND L.ACCOUNT BETWEEN LF.RANGE_FROM
                     AND COALESCE(NULLIF(NULLIF(LF.RANGE_TO, ''), ' '), LF.RANGE_FROM)
  JOIN {p}PSTREENODE N
    ON N.SETID = LF.SETID AND N.TREE_NAME = LF.TREE_NAME AND N.EFFDT = LF.EFFDT
   AND LF.TREE_NODE_NUM BETWEEN N.TREE_NODE_NUM AND N.TREE_NODE_NUM_END
   AND N.TREE_LEVEL_NUM = :lvl
 WHERE L.BUSINESS_UNIT = :bu AND L.LEDGER = :ledger AND L.FISCAL_YEAR = :fy
   AND L.ACCOUNTING_PERIOD BETWEEN 0 AND :maxper{basis}
 GROUP BY N.TREE_NODE, L.ACCOUNTING_PERIOD"""


def business_units(db: Database) -> str:
    return f"""SELECT BUSINESS_UNIT AS business_unit, DESCR AS descr,
       BASE_CURRENCY AS base_currency
  FROM {db.prefix}PS_BUS_UNIT_TBL_GL ORDER BY BUSINESS_UNIT"""


def ledgers_for_bu(db: Database) -> str:
    return f"""SELECT DISTINCT LEDGER AS ledger FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT = :bu ORDER BY LEDGER"""


def table_list(db: Database, params: dict) -> str:
    if db.dialect == "sqlite":
        return """SELECT name AS table_name, type AS object_type FROM sqlite_master
 WHERE type IN ('table', 'view') AND name LIKE :pat ORDER BY name"""
    if db.dialect == "oracle":
        owner = db.cfg.db.schema.strip().rstrip(".").upper()
        if owner:
            params["owner"] = owner
            return """SELECT OBJECT_NAME AS table_name, OBJECT_TYPE AS object_type
  FROM ALL_OBJECTS WHERE OWNER = :owner AND OBJECT_TYPE IN ('TABLE', 'VIEW')
   AND OBJECT_NAME LIKE :pat ORDER BY OBJECT_NAME"""
        return """SELECT OBJECT_NAME AS table_name, OBJECT_TYPE AS object_type
  FROM USER_OBJECTS WHERE OBJECT_TYPE IN ('TABLE', 'VIEW')
   AND OBJECT_NAME LIKE :pat ORDER BY OBJECT_NAME"""
    return """SELECT TABLE_NAME AS table_name, TABLE_TYPE AS object_type
  FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME LIKE :pat ORDER BY TABLE_NAME"""


def table_describe(db: Database, table: str, params: dict) -> str:
    if not IDENT_RE.match(table):
        raise ValueError(f"Invalid table name: {table!r}")
    if db.dialect == "sqlite":
        # PRAGMA cannot take binds; the identifier is regex-validated above.
        return f"PRAGMA table_info({table})"
    if db.dialect == "oracle":
        params["tname"] = table.upper()
        owner = db.cfg.db.schema.strip().rstrip(".").upper()
        if owner:
            params["owner"] = owner
            return """SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
       DATA_LENGTH AS data_length, NULLABLE AS nullable
  FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :tname
 ORDER BY COLUMN_ID"""
        return """SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
       DATA_LENGTH AS data_length, NULLABLE AS nullable
  FROM USER_TAB_COLUMNS WHERE TABLE_NAME = :tname ORDER BY COLUMN_ID"""
    params["tname"] = table
    return """SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
       CHARACTER_MAXIMUM_LENGTH AS data_length, IS_NULLABLE AS nullable
  FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :tname
 ORDER BY ORDINAL_POSITION"""
