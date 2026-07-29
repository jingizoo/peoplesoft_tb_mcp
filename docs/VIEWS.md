# PeopleSoft Views for the TB Agent

The agent runs out of the box **without** any database objects — its inline SQL
joins the delivered PS_* tables directly (effective dating, setid resolution and
tree flattening included). The `XX_TB_*` views below are the recommended
production setup: they centralize that logic, give the DBA one grant surface,
and are what `db.use_views: true` switches the agent to.

DDL: [sql/oracle/](../sql/oracle/) (numbered, run in order). The SQLite sample
creates identical views so both modes are tested.

| View | Source tables | Purpose |
|------|---------------|---------|
| `XX_TB_SETID_VW` | PS_SET_CNTRL_REC | BU → SETID indirection for chartfield joins |
| `XX_TB_ACCT_VW` | PS_GL_ACCOUNT_TBL | Current effective account attributes (descr, type A/L/Q/R/E, status) |
| `XX_TB_BAL_VW` | PS_LEDGER + the two above | Period-level balances by account + common chartfields — the TB workhorse |
| `XX_TB_JRNL_VW` | PS_JRNL_HEADER + PS_JRNL_LN | Posted journal lines with header context for drill-down |
| `XX_TB_PERIOD_VW` | PS_CAL_DETP_TBL | Calendar periods with begin/end dates (date → FY/period) |
| `XX_TB_TREE_VW` | PSTREEDEFN/PSTREENODE/PSTREELEAF | Flattened account tree (node ↔ leaf ranges) for rollups |

## Semantics the views encode

- **Signed amounts:** POSTED_TOTAL_AMT is debit-positive / credit-negative.
  Ending balance through period P = SUM over periods 0..P.
- **Period 0** = beginning balances written by year-end close; **998** =
  adjustment period, included only for "final/post-adjustment" reporting.
- **Effective dating:** `XX_TB_ACCT_VW` takes the max EFFDT ≤ today per
  SETID+ACCOUNT (status is exposed, not filtered, so inactive accounts with
  balances can be flagged rather than hidden).
- **Tree flattening:** leaf ranges attach to their node; ancestor rollup uses
  `TREE_NODE_NUM BETWEEN node.TREE_NODE_NUM AND node.TREE_NODE_NUM_END`.
  Blank RANGE_TO (single-value leaves) is normalized to RANGE_FROM.

## Deployment notes

- **App Designer vs direct DDL:** creating these as App Designer records of
  type *SQL View* (names ≤ 15 chars fit: XX_TB_BAL_VW etc.) keeps them in
  migration projects and PS security. Direct `CREATE VIEW` in the SYSADM schema
  plus a `GRANT SELECT ... TO <reporting_user>` is equally fine for a
  reporting-only consumer.
- **Access:** give the agent a dedicated **read-only** Oracle account with
  SELECT on these views (or the PS_ tables for inline mode) and nothing else.
  Set `db.schema: SYSADM` in config.yaml when connecting as that user.
- **Performance:** PS_LEDGER's delivered index (BU, LEDGER, FY, PERIOD,
  chartfields) serves these queries well. If your ledger is very large or
  finance asks for sub-second TBs, convert `XX_TB_BAL_VW` to a **materialized
  view** refreshed after posting cycles/close, and add a bitmap index on
  ACCOUNT if advised by AWR.
- **Known gaps to revisit per environment:**
  - Multibook/translation ledgers: filter LEDGER per reporting need; the agent
    exposes `list_ledgers` to discover them.
  - Adjustment-period conventions vary (901–912, 998); set
    `defaults.adjustment_periods` accordingly.
  - Dynamic-detail or setcntrlvalue-specific ("winter") trees need extra
    predicates in `XX_TB_TREE_VW`.
  - `PS_OPEN_PERIOD` (open-period status by module) is a good future view for
    "can we still post to P6?" questions.
