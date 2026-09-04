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

from .db import Database, DbError

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

# Leading underscore admitted everywhere (a positional widening --
# splice safety is the CHARACTER CLASS, which does not change);
# digit-leading stays refused: reaching `2024_x` needs backticks, and
# backtick refusal is a deliberate closed guard class.
_IDENT = (
    r'(?:[A-Za-z_][A-Za-z0-9_$#]{0,127}|'
    r'"[A-Za-z_][A-Za-z0-9_$#]{0,127}"|'
    r'\[[A-Za-z_][A-Za-z0-9_$#]{0,127}\])'
)
IDENT_RE = re.compile(rf"^\s*{_IDENT}(?:\s*\.\s*{_IDENT})?\s*$")


def opt(db: Database, table: str, column: str, alias: str,
        qualifier: str = "") -> str:
    """SELECT-list entry for an OPTIONAL column.

    Emits `Q.COLUMN AS alias` when the record really has that column here and
    `NULL AS alias` when it does not, so one absent descriptive column can
    never take down the whole query with ORA-00904. This is the general
    mechanism: builders declare which columns are optional, and the live
    catalog decides. Required columns are NOT routed through this - a trial
    balance without POSTED_TOTAL_AMT should fail loudly, not silently return
    nulls.
    """
    prefix = "{0}.".format(qualifier) if qualifier else ""
    if db.has_column(table, column):
        return "{0}{1} AS {2}".format(prefix, column, alias)
    return "NULL AS {0}".format(alias)


def opt_expr(db: Database, table: str, column: str, present: str,
             absent: str) -> str:
    """Pick between two SQL fragments depending on whether a column exists.

    For predicates and aggregates, where the fallback is not simply NULL
    (e.g. SUM(0) when there is no DISPUTE_STATUS to test).
    """
    return present if db.has_column(table, column) else absent


def header_descr(db: Database, alias: str, qualifier: str = "") -> str:
    """Journal-header description, whatever this release calls it.

    PeopleSoft carries the header's free text as DESCR254_MIXED on some
    releases and DESCR254 on others, and customised header records sometimes
    have neither. One helper so the builders that need it cannot drift apart —
    they already had, which is how drill_to_journals kept a hard reference
    after unposted_journals was fixed.
    """
    for col in ("DESCR254_MIXED", "DESCR254", "DESCR"):
        if db.has_column("PS_JRNL_HEADER", col):
            return opt(db, "PS_JRNL_HEADER", col, alias, qualifier)
    return "NULL AS {0}".format(alias)


def basis_clause(db: Database, amount_basis: str, currency: str, params: dict,
                 alias: str = "L", base_currency: str = "") -> str:
    """Currency / amount-basis contract, applied identically by every tool.

    PS_LEDGER holds one row per currency AND per statistics code. Summing
    POSTED_TOTAL_AMT without these predicates mixes transaction-currency rows
    and non-monetary statistical rows into the total, which is the usual reason
    a generated trial balance does not tie to PeopleSoft.

      base        -> SUM(POSTED_TOTAL_AMT) across ALL currency rows\n                     (POSTED_TOTAL_AMT is already the base amount)
      transaction -> POSTED_TRAN_AMT, caller groups by CURRENCY_CD
    Statistical rows (STATISTICS_CODE populated) are always excluded; they are
    headcount/area/units, not money.
    """
    if db.cfg.db.use_views:
        # The view encodes the STATISTICS_CODE exclusion (it does not expose
        # the column), but it keeps CURRENCY_CD as a dimension — so the
        # base-currency contract must still be applied here. Dropping the
        # whole clause silently mixed foreign-currency rows into every total.
        clause = ""
    else:
        # Oracle treats '' as NULL, so TRIM(' ') yields NULL and a plain
        # `TRIM(x) = ''` test is UNKNOWN — which would silently exclude every
        # monetary row on Oracle while passing on SQLite. Test both forms.
        # The predicate exists only to exclude statistical rows (headcount,
        # square footage, units). A ledger record with no STATISTICS_CODE
        # column cannot hold a statistical row, so dropping it is exactly
        # right rather than merely tolerable — and this clause is shared by
        # every ledger query in the product, so an ORA-00904 here would take
        # out every GL number at once. has_column defaults to True when the
        # catalog is unreadable, so the predicate survives wherever it should.
        clause = opt_expr(
            db, "PS_LEDGER", "STATISTICS_CODE",
            f" AND ({alias}.STATISTICS_CODE IS NULL"
            f" OR TRIM({alias}.STATISTICS_CODE) IS NULL"
            f" OR TRIM({alias}.STATISTICS_CODE) = '')",
            "")
        # NO currency predicate on the base basis. CURRENCY_CD is the
        # TRANSACTION currency and POSTED_TOTAL_AMT is already the base
        # amount on every row — a journal entered in EUR posts only a
        # CURRENCY_CD='EUR' row, so filtering to the base currency silently
        # dropped all foreign-entered activity while the TB still balanced
        # (both sides of the FX journal excluded). Base total = SUM of
        # POSTED_TOTAL_AMT across ALL currency rows. An explicit currency=
        # filter and the transaction basis keep their predicates below.
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


def nonblank(col: str) -> str:
    """Predicate for "this column has a non-blank value", true on BOTH dialects.

    Oracle stores the empty string AS NULL. So `x <> ''` is `x <> NULL`, which
    is UNKNOWN for every row including the populated ones — the predicate
    matches nothing and the query silently returns an empty set. `x = ''` is
    UNKNOWN the same way. Neither raises; both just quietly answer "no rows",
    and on SQLite, where '' is a real value distinct from NULL, both behave as
    written. That asymmetry is invisible in development and wrong in
    production.

    A companion `IS NOT NULL` does not rescue `<> ''` — the UNKNOWN from the
    second predicate still fails the AND.

    LENGTH(TRIM(x)) > 0 is UNKNOWN only for values that really are NULL or
    blank, which is the answer those rows should get anyway.

    Lives here, beside the other dialect rules, rather than in a feature
    module: four call sites re-derived this independently and three got it
    wrong. tests/test_oracle_blank_predicates.py bans the raw forms.
    """
    return f"LENGTH(TRIM({col})) > 0"


def asof_expr(db: Database, params: dict, as_of: str = "",
              key: str = "asof") -> str:
    """SQL for the date effective-dated master data should be read AT.

    PeopleSoft master data is effective-dated: PS_GL_ACCOUNT_TBL,
    PSTREEDEFN and friends hold one row per EFFDT, and the correct row is
    the latest one on or before the date the ANSWER is about. Reading them
    at ``today`` instead silently relabels history — an FY2025 comparative
    pulled in FY2026 shows FY2026 account descriptions and FY2026 tree
    structure, so two prints of the same statement disagree and neither
    says why.

    Passing "" keeps the old behaviour for the genuinely current questions
    (account search, "what does this tree look like now"), where today IS
    the right as-of date.
    """
    text = (as_of or "").strip()[:10]
    if not text:
        return db.today_expr()
    params[key] = text
    return f":{key}"


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
    base_currency: str = "",
    as_of: str = "",
) -> str:
    """Period-level sums by account (+extras), through :maxper for :bu/:ledger/:fy."""
    p = db.prefix
    extra_sel = "".join(f", L.{c} AS {c.lower()}" for c in extras)
    extra_grp = "".join(f", L.{c}" for c in extras)
    where_extra = ""
    if dept:
        params["dept"] = dept
        where_extra += " AND L.DEPTID = :dept"
    where_extra += basis_clause(db, amount_basis, currency, params,
                                base_currency=base_currency)
    where_extra += account_filter(account, params)
    amt = amount_col(db, amount_basis)

    period_filter = (
        f"(L.ACCOUNTING_PERIOD BETWEEN 0 AND :maxper"
        f"{_adj_clause(include_adj, adj_periods)})"
    )

    if db.cfg.db.use_views:
        # The view already carries account attributes, so a single pass is fine.
        return f"""SELECT L.ACCOUNT AS account, L.DESCR AS descr,
       L.ACCOUNT_TYPE AS acct_type, L.EFF_STATUS AS eff_status{extra_sel},
       L.ACCOUNTING_PERIOD AS period, SUM({amt}) AS amt
  FROM {p}XX_TB_BAL_VW L
 WHERE L.BUSINESS_UNIT = :bu
   AND L.LEDGER = :ledger
   AND L.FISCAL_YEAR = :fy
   AND {period_filter}
   {where_extra}
 GROUP BY L.ACCOUNT, L.DESCR, L.ACCOUNT_TYPE, L.EFF_STATUS{extra_grp},
          L.ACCOUNTING_PERIOD"""

    # Aggregate the ledger FIRST, then attach account attributes to the small
    # aggregated result. Joining the effective-dated account table directly to
    # PS_LEDGER makes the optimiser evaluate a correlated MAX(EFFDT) subquery
    # against every ledger row, which is unusably slow on a real ledger.
    extra_inner_sel = "".join(f", L.{c}" for c in extras)
    extra_outer_sel = "".join(f", T.{c} AS {c.lower()}" for c in extras)
    # The account attributes belong to the PERIOD being reported, not to the
    # day the report was run. Without this an FY2025 trial balance printed in
    # FY2026 carries FY2026 account names.
    today = asof_expr(db, params, as_of)
    # The account table supplies DECORATION on top of an already-correct
    # aggregate: the balances come from the subquery above and do not depend
    # on it. A site whose PS_GL_ACCOUNT_TBL lacks DESCR or EFF_STATUS must
    # still get its trial balance, so those are routed through opt(). Two
    # deliberate exclusions:
    #   ACCOUNT_TYPE stays required — a null account type silently misclassifies
    #   an income statement, which is a wrong number, not a missing label.
    #   SETID/EFFDT drop out together: without effective dating there is one
    #   row per account and no snapshot to pick.
    a_descr = opt(db, "PS_GL_ACCOUNT_TBL", "DESCR", "DESCR", "A2")
    a_effst = opt(db, "PS_GL_ACCOUNT_TBL", "EFF_STATUS", "EFF_STATUS", "A2")
    a_setid = opt_expr(db, "PS_GL_ACCOUNT_TBL", "SETID",
                       "A2.SETID = :setid", "1 = 1")
    if db.has_column("PS_GL_ACCOUNT_TBL", "EFFDT"):
        setid_corr = opt_expr(db, "PS_GL_ACCOUNT_TBL", "SETID",
                              "AX.SETID = A2.SETID AND ", "")
        a_effdt = (f"""AND A2.EFFDT = (SELECT MAX(AX.EFFDT)
                                 FROM {p}PS_GL_ACCOUNT_TBL AX
                                WHERE {setid_corr}AX.ACCOUNT = A2.ACCOUNT
                                  AND AX.EFFDT <= {today})""")
    else:
        a_effdt = ""
    return f"""SELECT T.account AS account, A.DESCR AS descr,
       A.ACCOUNT_TYPE AS acct_type, A.EFF_STATUS AS eff_status{extra_outer_sel},
       T.period AS period, T.amt AS amt
  FROM (SELECT L.ACCOUNT AS account{extra_inner_sel},
               L.ACCOUNTING_PERIOD AS period, SUM({amt}) AS amt
          FROM {p}PS_LEDGER L
         WHERE L.BUSINESS_UNIT = :bu
           AND L.LEDGER = :ledger
           AND L.FISCAL_YEAR = :fy
           AND {period_filter}
           {where_extra}
         GROUP BY L.ACCOUNT{extra_inner_sel}, L.ACCOUNTING_PERIOD) T
  LEFT JOIN (SELECT A2.ACCOUNT, {a_descr}, A2.ACCOUNT_TYPE, {a_effst}
               FROM {p}PS_GL_ACCOUNT_TBL A2
              WHERE {a_setid}
                {a_effdt}) A
    ON A.ACCOUNT = T.account"""


def journal_lines(db: Database, dept: str, params: dict) -> str:
    p = db.prefix
    dept_clause = ""
    if dept:
        # A department filter that cannot be applied must not return the
        # unfiltered set — that is a wrong answer wearing a filter's label.
        # This one refuses out loud, unlike the descriptive columns below.
        if not db.has_column("PS_JRNL_LN", "DEPTID"):
            raise ValueError(
                "This site's PS_JRNL_LN has no DEPTID column, so journal "
                "lines cannot be filtered by department. Re-run without the "
                "department filter, or use describe_record PS_JRNL_LN to see "
                "which chartfields this record does carry."
            )
        params["dept"] = dept
        dept_clause = " AND J.DEPTID = :dept"
    # Everything except the identity and the amount is a label here: the drill
    # answers "which journals moved this account", and JOURNAL_ID plus
    # MONETARY_AMOUNT answer it. The sibling builder unposted_journals already
    # adapts the identical header columns; this one was left behind, so an
    # absent OPRID took out the only tool that proves a balance.
    j, h = "PS_JRNL_LN", "PS_JRNL_HEADER"
    return f"""SELECT H.JOURNAL_ID AS journal_id, H.JOURNAL_DATE AS journal_date,
       {opt(db, h, 'SOURCE', 'source', 'H')}, {opt(db, h, 'OPRID', 'oprid', 'H')},
       {header_descr(db, 'jrnl_descr', 'H')},
       J.JOURNAL_LINE AS line_nbr, {opt(db, j, 'DEPTID', 'deptid', 'J')},
       {opt(db, j, 'CURRENCY_CD', 'currency_cd', 'J')},
       J.MONETARY_AMOUNT AS amount, {opt(db, j, 'LINE_DESCR', 'line_descr', 'J')}
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
    """Journals whose exact current status requires close review.

    This legacy function name is kept for payload compatibility, but the
    population is not a blanket ``status != P``: deleted siblings, standard-
    journal models and upgrade journals are documented non-actionable states.
    Who entered a finding, its subsystem and free-text description are labels,
    so they degrade to NULL rather than taking the check down.
    DESCR254_MIXED in particular is renamed or absent across releases.
    """
    p = db.prefix
    j = "PS_JRNL_HEADER"
    descr = header_descr(db, "descr")
    # No LEDGER_GROUP here means the record cannot be narrowed to one ledger
    # group; listing every unposted journal for the unit is the safe side of
    # that trade — a close check must not under-report.
    led_clause = opt_expr(
        db, j, "LEDGER_GROUP",
        "AND (LEDGER_GROUP = :ledger OR :ledger IS NULL OR LEDGER_GROUP IS NULL)",
        "")
    return f"""SELECT JOURNAL_ID AS journal_id, JOURNAL_DATE AS journal_date,
       ACCOUNTING_PERIOD AS period, JRNL_HDR_STATUS AS status,
       {opt(db, j, 'SOURCE', 'source')}, {opt(db, j, 'OPRID', 'oprid')},
       {descr}
  FROM {p}{j}
 WHERE BUSINESS_UNIT = :bu
   AND FISCAL_YEAR = :fy
   AND ACCOUNTING_PERIOD BETWEEN :minper AND :maxper
   -- P is posted; D is a deleted sibling; M is a non-posting model; Z is
   -- the special upgrade/cannot-unpost state. None belongs in the actionable
   -- close population. A NULL status is unknown and must remain visible.
   AND (JRNL_HDR_STATUS IS NULL
        OR JRNL_HDR_STATUS NOT IN ('P', 'D', 'M', 'Z'))
   {led_clause}
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
   AND H.ACCOUNTING_PERIOD BETWEEN :minper AND :maxper
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


def tree_ctl_values(db: Database, params: dict, as_of: str = "") -> str:
    """Control values a tree is defined under.

    PSTREE* records are keyed by SETCNTRLVALUE as well as SETID/TREE_NAME.
    A BU-controlled tree has one row per business unit; joining without it
    matches every control value at once and multiplies node totals — while
    still summing to a balanced-looking number.
    """
    p, today = db.prefix, asof_expr(db, params, as_of)
    return f"""SELECT DISTINCT SETCNTRLVALUE AS setcntrlvalue FROM {p}PSTREEDEFN
 WHERE SETID = :setid AND TREE_NAME = :tree AND EFFDT <= {today}"""


def tree_effdt(db: Database, params: dict, as_of: str = "") -> str:
    """The tree VERSION in force at the as-of date.

    A tree reorganised this year restates last year's rollup if it is read
    at today: the same accounts land under different nodes, so a prior-year
    departmental report changes shape without any ledger row changing.
    """
    p, today = db.prefix, asof_expr(db, params, as_of)
    return f"""SELECT MAX(EFFDT) AS effdt FROM {p}PSTREEDEFN
 WHERE SETID = :setid AND TREE_NAME = :tree AND EFFDT <= {today}"""


def tree_rollup(db: Database, params: dict, amount_basis: str = "base",
                base_currency: str = "") -> str:
    """Ending-balance components by tree node at a given level.

    Leaf ranges attach to nodes; ancestor containment uses the node-number span.
    Blank RANGE_TO (stored as '' or ' ') means a single-value leaf.
    """
    p = db.prefix
    d = db.date_bind("teffdt")
    amt = amount_col(db, amount_basis)
    basis = basis_clause(db, amount_basis, "", params, base_currency=base_currency)
    return f"""SELECT N.TREE_NODE AS tree_node, MIN(N.TREE_NODE_NUM) AS node_ord,
       L.ACCOUNTING_PERIOD AS period, SUM({amt}) AS amt
  FROM {p}PS_LEDGER L
  JOIN {p}PSTREELEAF LF
    ON LF.SETID = :tsetid AND LF.TREE_NAME = :tree AND LF.EFFDT = {d}
   AND LF.SETCNTRLVALUE = :tctl
   AND L.ACCOUNT BETWEEN LF.RANGE_FROM
                     AND COALESCE(NULLIF(NULLIF(LF.RANGE_TO, ''), ' '), LF.RANGE_FROM)
  JOIN {p}PSTREENODE N
    ON N.SETID = LF.SETID AND N.TREE_NAME = LF.TREE_NAME AND N.EFFDT = LF.EFFDT
   AND N.SETCNTRLVALUE = LF.SETCNTRLVALUE
   AND LF.TREE_NODE_NUM BETWEEN N.TREE_NODE_NUM AND N.TREE_NODE_NUM_END
   AND N.TREE_LEVEL_NUM = :lvl
 WHERE L.BUSINESS_UNIT = :bu AND L.LEDGER = :ledger AND L.FISCAL_YEAR = :fy
   AND L.ACCOUNTING_PERIOD BETWEEN 0 AND :maxper{basis}
 GROUP BY N.TREE_NODE, L.ACCOUNTING_PERIOD"""


def tree_node_span(db: Database) -> str:
    """Node-number span of one named tree node (for leaf-range lookup)."""
    p, d = db.prefix, db.date_bind("teffdt")
    return f"""SELECT TREE_NODE_NUM AS a, TREE_NODE_NUM_END AS b
  FROM {p}PSTREENODE
 WHERE SETID = :tsetid AND TREE_NAME = :tree AND EFFDT = {d}
   AND SETCNTRLVALUE = :tctl AND TREE_NODE = :node"""


def tree_leaf_ranges(db: Database) -> str:
    """Account ranges attached at or under a node span. Blank RANGE_TO ('' or
    ' ') means a single-value leaf."""
    p, d = db.prefix, db.date_bind("teffdt")
    return f"""SELECT LF.RANGE_FROM AS range_from,
       COALESCE(NULLIF(NULLIF(LF.RANGE_TO, ''), ' '), LF.RANGE_FROM) AS range_to
  FROM {p}PSTREELEAF LF
 WHERE LF.SETID = :tsetid AND LF.TREE_NAME = :tree AND LF.EFFDT = {d}
   AND LF.SETCNTRLVALUE = :tctl
   AND LF.TREE_NODE_NUM BETWEEN :a AND :b"""


def tree_nodes_list(db: Database) -> str:
    p, d = db.prefix, db.date_bind("teffdt")
    return f"""SELECT TREE_NODE AS tree_node, TREE_LEVEL_NUM AS level_num
  FROM {p}PSTREENODE
 WHERE SETID = :tsetid AND TREE_NAME = :tree AND EFFDT = {d}
   AND SETCNTRLVALUE = :tctl
 ORDER BY TREE_NODE_NUM"""


def psrecdefn_search(db: Database) -> str:
    """Find PeopleSoft RECORDS by name or description from PeopleTools
    metadata. PSRECDEFN is the definitive catalog: it carries the record
    description a functional user would recognise ("File Interface Setup"),
    and SQLTABLENAME when a site overrode the physical name, so the caller
    never has to guess that PS_ + RECNAME is right.

    RECTYPE: 0=SQL table, 1=SQL view, 2=derived/work, 3=subrecord,
    5=dynamic view, 6=query view, 7=temporary table. Only 0/1/7 exist in the
    database; the rest have no physical object to query.
    """
    p = db.prefix
    return f"""SELECT RECNAME AS recname, RECDESCR AS recdescr,
       RECTYPE AS rectype, SQLTABLENAME AS sqltablename
  FROM {p}PSRECDEFN
 WHERE RECTYPE IN (0, 1, 7)
   AND (UPPER(RECNAME) LIKE :q OR UPPER(RECDESCR) LIKE :q
        OR UPPER(SQLTABLENAME) LIKE :q)
 ORDER BY RECNAME"""


def psrecfield_search(db: Database) -> str:
    """Records that contain a field whose name matches — how you find the
    record behind "which table holds FILE_ID" without knowing its name."""
    p = db.prefix
    return f"""SELECT R.RECNAME AS recname, R.RECDESCR AS recdescr,
       R.RECTYPE AS rectype, R.SQLTABLENAME AS sqltablename,
       F.FIELDNAME AS fieldname
  FROM {p}PSRECFIELD F
  JOIN {p}PSRECDEFN R ON R.RECNAME = F.RECNAME
 WHERE R.RECTYPE IN (0, 1, 7) AND UPPER(F.FIELDNAME) LIKE :q
 ORDER BY R.RECNAME, F.FIELDNAME"""


def psrecfield_for_record(db: Database) -> str:
    """Field list of one record, in PeopleTools order."""
    p = db.prefix
    return f"""SELECT FIELDNAME AS fieldname, FIELDNUM AS fieldnum
  FROM {p}PSRECFIELD WHERE RECNAME = :rec ORDER BY FIELDNUM"""


def business_units(db: Database) -> str:
    """Business units with their names and base currency.

    The NAME lives on PS_BUS_UNIT_TBL_FS, the Financials business-unit
    definition. PS_BUS_UNIT_TBL_GL holds the GL-specific attributes — ledger
    group, base currency — and in delivered PeopleSoft carries no DESCR at
    all. Reading the description from the GL record therefore did not fail
    loudly; opt() simply emitted NULL on every real instance, and the scope
    bar showed a bare code with nothing indicating why.

    The join is conditional rather than unconditional: reporting roles are
    often granted one of these records and not the other, and an outer join
    to a table the account cannot read fails the whole query. When FS is not
    readable this falls back to a DESCR on the GL record, which is where a
    site that customised one would have put it.
    """
    p = db.prefix
    gl, fs = "PS_BUS_UNIT_TBL_GL", "PS_BUS_UNIT_TBL_FS"
    base = opt(db, gl, "BASE_CURRENCY", "base_currency", "G")
    # columns() is empty for BOTH "no such table" and "catalog unreadable";
    # either way the join is unsafe, so both take the fallback.
    if db.columns(fs) and db.has_column(fs, "DESCR"):
        return f"""SELECT G.BUSINESS_UNIT AS business_unit,
       F.DESCR AS descr, {base}
  FROM {p}{gl} G
  LEFT JOIN {p}{fs} F ON F.BUSINESS_UNIT = G.BUSINESS_UNIT
 ORDER BY G.BUSINESS_UNIT"""
    return f"""SELECT G.BUSINESS_UNIT AS business_unit,
       {opt(db, gl, 'DESCR', 'descr', 'G')}, {base}
  FROM {p}{gl} G ORDER BY G.BUSINESS_UNIT"""


def ledger_business_units(db: Database) -> str:
    """Business units that actually have GL data.

    PS_BUS_UNIT_TBL_GL is useful setup metadata, but it is not a safe discovery
    source: reporting users are often granted PS_LEDGER without the setup
    table, and setup can contain inactive units with no accessible ledger data.
    """
    return f"""SELECT DISTINCT BUSINESS_UNIT AS business_unit
  FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT IS NOT NULL
 ORDER BY BUSINESS_UNIT"""


def scope_setup_pairs(db: Database) -> str:
    """BU/ledger catalog from SETUP tables, not from balance rows.

    PS_BUS_UNIT_LED / PS_BUS_UNIT_TBL_GL hold one row per business unit (and
    per ledger group), i.e. hundreds of rows. Deriving the catalog here costs
    a small indexed read. Deriving it from PS_LEDGER instead means a DISTINCT
    over tens of millions of balance rows: Oracle has no skip-scan for
    DISTINCT, so the plan is a full scan of every leaf block of PSALEDGER.
    That is what hung the first question on a real instance.
    """
    return f"""SELECT B.BUSINESS_UNIT AS business_unit, G.LEDGER AS ledger
  FROM {db.prefix}PS_BUS_UNIT_LED B
  JOIN {db.prefix}PS_LED_GRP_TBL G ON G.LEDGER_GROUP = B.LEDGER_GROUP
 WHERE B.LEDGER_GROUP IS NOT NULL
 ORDER BY B.BUSINESS_UNIT, G.LEDGER"""


def scope_bu_list(db: Database) -> str:
    """Fallback catalog: business units only, from the GL BU setup table."""
    return f"""SELECT BUSINESS_UNIT AS business_unit
  FROM {db.prefix}PS_BUS_UNIT_TBL_GL
 ORDER BY BUSINESS_UNIT"""


def financial_scope_pairs(db: Database) -> str:
    """LAST-RESORT catalog straight from PS_LEDGER.

    Only used when the setup tables are not granted. Callers MUST bound this
    (row cap + a note), because on Oracle it scans the whole ledger index.
    """
    return f"""SELECT DISTINCT BUSINESS_UNIT AS business_unit, LEDGER AS ledger
  FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT IS NOT NULL AND LEDGER IS NOT NULL
 ORDER BY BUSINESS_UNIT, LEDGER"""


def _regular_period_predicate(
    column: str, adjustment_periods: tuple[int, ...] | list[int] = ()
) -> str:
    """Site-aware regular-period predicate for scope discovery.

    Period zero is balance forward and 999 is year-end close. Other
    adjustment periods are installation-specific and come from configuration,
    so a legitimate period 13 is not silently treated as an adjustment.
    """
    excluded = sorted(
        {int(value) for value in adjustment_periods if 0 < int(value) < 999}
    )
    predicate = f"{column} > 0 AND {column} <> 999"
    if excluded:
        predicate += (
            f" AND {column} NOT IN ({', '.join(str(value) for value in excluded)})"
        )
    return predicate


def scope_year_bounds(
    db: Database, adjustment_periods: tuple[int, ...] | list[int] = ()
) -> str:
    """Small, indexed-scope aggregate; deliberately never counts ledger rows."""
    regular = _regular_period_predicate(
        "ACCOUNTING_PERIOD", adjustment_periods
    )
    return f"""SELECT MIN(FISCAL_YEAR) AS first_fy,
       MAX(FISCAL_YEAR) AS last_fy,
       MAX(CASE WHEN {regular}
                THEN FISCAL_YEAR END) AS last_regular_fy
  FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT = :bu AND LEDGER = :led"""


def scope_last_regular_period(
    db: Database, adjustment_periods: tuple[int, ...] | list[int] = ()
) -> str:
    regular = _regular_period_predicate(
        "ACCOUNTING_PERIOD", adjustment_periods
    )
    return f"""SELECT MAX(ACCOUNTING_PERIOD) AS last_period
  FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT = :bu AND LEDGER = :led AND FISCAL_YEAR = :fy
   AND {regular}"""


def scope_base_currency(db: Database) -> str:
    """One representative ledger currency for setup-table-free discovery."""
    sql = f"""SELECT BASE_CURRENCY AS base_currency
  FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT = :bu AND LEDGER = :led
   AND BASE_CURRENCY IS NOT NULL"""
    return db.exists_sql(sql)


def business_unit_base_currency(db: Database) -> str:
    """One PS_LEDGER base currency for a BU when setup metadata is unavailable."""
    sql = f"""SELECT BASE_CURRENCY AS base_currency
  FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT = :bu AND BASE_CURRENCY IS NOT NULL"""
    return db.exists_sql(sql)


def ledgers_for_bu_setup(db: Database) -> str:
    """A unit's ledgers from SETUP, which is where the answer actually lives.

    PS_BUS_UNIT_LED holds one row per unit and ledger group — tens of rows,
    indexed on the unit. The balance table holds one row per unit, ledger,
    account, chartfield combination and period, which for a real unit is
    millions. Asking the balance table which ledgers exist reads all of
    them to return three strings.
    """
    return f"""SELECT DISTINCT G.LEDGER AS ledger
  FROM {db.prefix}PS_BUS_UNIT_LED B
  JOIN {db.prefix}PS_LED_GRP_TBL G ON G.LEDGER_GROUP = B.LEDGER_GROUP
 WHERE B.BUSINESS_UNIT = :bu AND B.LEDGER_GROUP IS NOT NULL
 ORDER BY G.LEDGER"""


def ledger_names_setup(db: Database) -> str:
    """Every ledger this installation defines — tens of rows, from setup.

    The name list is all a grid probe needs: which units actually HOLD each
    ledger is then one short-circuiting EXISTS per pair, instead of one
    unbounded DISTINCT over the balance table per unit.
    """
    return f"""SELECT DISTINCT LEDGER AS ledger
  FROM {db.prefix}PS_LED_GRP_TBL
 WHERE LEDGER IS NOT NULL
 ORDER BY LEDGER"""


def ledgers_for_bu(db: Database) -> str:
    """LAST RESORT: the same question asked of the balance table.

    Only when the setup records are not granted. DISTINCT does not
    short-circuit — Oracle reads every index entry for the unit before it
    can promise the list is complete — so on a live instance this is a
    range scan of millions of entries to return a handful of values. It is
    kept because a site that cannot read PS_BUS_UNIT_LED still needs an
    answer, not because it is a reasonable way to get one.
    """
    return f"""SELECT DISTINCT LEDGER AS ledger FROM {db.prefix}PS_LEDGER
 WHERE BUSINESS_UNIT = :bu ORDER BY LEDGER"""


def table_list(db: Database, params: dict) -> str:
    if db.dialect == "bigquery":
        # {ds} is the operator-configured, identifier-validated dataset
        # (db.bigquery_dataset) -- guards-doctrine interpolation, the
        # same trust level as db.prefix, never model-influenced.
        ds = db.bigquery_dataset
        return f"""SELECT table_schema AS schema_name, table_name,
       CASE table_type WHEN 'BASE TABLE' THEN 'TABLE' ELSE table_type END
         AS object_type
  FROM {ds}.INFORMATION_SCHEMA.TABLES
 WHERE LOWER(table_name) LIKE LOWER(:pat) ORDER BY table_name"""
    if db.dialect == "sqlite":
        return """SELECT name AS table_name, type AS object_type FROM sqlite_master
 WHERE type IN ('table', 'view') AND name LIKE :pat ORDER BY name"""
    if db.dialect == "oracle":
        schemas = db.allowed_schemas
        if len(schemas) == 1:
            params["owner"] = schemas[0]
            return """SELECT OWNER AS schema_name, OBJECT_NAME AS table_name,
       OBJECT_TYPE AS object_type
  FROM ALL_OBJECTS WHERE OWNER = :owner AND OBJECT_TYPE IN ('TABLE', 'VIEW')
   AND OBJECT_NAME LIKE :pat ORDER BY OWNER, OBJECT_NAME"""
        if schemas:
            binds = []
            for i, owner in enumerate(schemas):
                key = f"owner{i}"
                params[key] = owner
                binds.append(f":{key}")
            return f"""SELECT OWNER AS schema_name, OBJECT_NAME AS table_name,
       OBJECT_TYPE AS object_type
  FROM ALL_OBJECTS WHERE OWNER IN ({', '.join(binds)})
   AND OBJECT_TYPE IN ('TABLE', 'VIEW')
   AND OBJECT_NAME LIKE :pat ORDER BY OWNER, OBJECT_NAME"""
        return """SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AS schema_name,
       OBJECT_NAME AS table_name, OBJECT_TYPE AS object_type
  FROM USER_OBJECTS WHERE OBJECT_TYPE IN ('TABLE', 'VIEW')
   AND OBJECT_NAME LIKE :pat ORDER BY OBJECT_NAME"""
    schemas = db.allowed_schemas
    where = "TABLE_NAME LIKE :pat"
    if len(schemas) == 1:
        params["owner"] = schemas[0]
        where += " AND UPPER(TABLE_SCHEMA) = :owner"
    elif schemas:
        binds = []
        for i, owner in enumerate(schemas):
            key = f"owner{i}"
            params[key] = owner
            binds.append(f":{key}")
        where += f" AND UPPER(TABLE_SCHEMA) IN ({', '.join(binds)})"
    return f"""SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS table_name,
       TABLE_TYPE AS object_type
  FROM INFORMATION_SCHEMA.TABLES WHERE {where}
 ORDER BY TABLE_SCHEMA, TABLE_NAME"""


def table_describe(db: Database, table: str, params: dict) -> str:
    if not IDENT_RE.match(table):
        if (db.dialect == "bigquery"
                and str(table or "").strip()[:1].isdigit()):
            raise ValueError(
                f"Invalid table name: {table!r}. Digit-leading names are "
                "not addressable through the guarded tools: unquoted "
                "digit-leading identifiers are invalid GoogleSQL and "
                "backtick quoting is refused as a guard boundary. Rename "
                "or view-wrap the object to reach it here.")
        raise ValueError(f"Invalid table name: {table!r}")
    try:
        owner, name = db.table_scope(table)
    except DbError as exc:
        raise ValueError(str(exc)) from exc
    if db.dialect == "bigquery":
        # Case-insensitive VALIDATION against a case-sensitive backend:
        # UPPER on both sides finds the row whatever case the model
        # typed; execution paths use the true-case name from list_tables.
        ds = db.bigquery_dataset
        params["tname"] = name
        return f"""SELECT column_name, data_type, NULL AS data_length,
       CASE is_nullable WHEN 'YES' THEN 'Y' ELSE 'N' END AS nullable
  FROM {ds}.INFORMATION_SCHEMA.COLUMNS
 WHERE UPPER(table_name) = :tname ORDER BY ordinal_position"""
    if db.dialect == "sqlite":
        # PRAGMA cannot take binds; the identifier is regex-validated above.
        return f"PRAGMA table_info({name})"
    if db.dialect == "oracle":
        params["tname"] = name
        if owner:
            params["owner"] = owner
            return """SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
       DATA_LENGTH AS data_length, NULLABLE AS nullable
  FROM ALL_TAB_COLUMNS WHERE OWNER = :owner AND TABLE_NAME = :tname
 ORDER BY COLUMN_ID"""
        return """SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
       DATA_LENGTH AS data_length, NULLABLE AS nullable
  FROM USER_TAB_COLUMNS WHERE TABLE_NAME = :tname ORDER BY COLUMN_ID"""
    params["tname"] = name
    where = "UPPER(TABLE_NAME) = :tname"
    if owner:
        params["owner"] = owner
        where += " AND UPPER(TABLE_SCHEMA) = :owner"
    return f"""SELECT COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
       CHARACTER_MAXIMUM_LENGTH AS data_length, IS_NULLABLE AS nullable
  FROM INFORMATION_SCHEMA.COLUMNS WHERE {where}
 ORDER BY ORDINAL_POSITION"""
