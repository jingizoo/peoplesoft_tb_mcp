-- XX_TB_BAL_VW: the trial-balance workhorse. Period-level ledger amounts by
-- account + common chartfields, with account attributes joined in. The agent
-- derives beginning / activity / ending from these period buckets:
--   ending through P = SUM(periods 0..P)   (period 0 = beginning balances,
--   998 = adjustments — included only when requested).
-- Amounts are signed: debits positive, credits negative.
--
-- Collapses book code / statistics / currency detail dimensions not needed for
-- TB reporting. On very large ledgers consider a materialized view refreshed
-- after each posting cycle / close (see docs/VIEWS.md).

CREATE OR REPLACE VIEW XX_TB_BAL_VW AS
SELECT L.BUSINESS_UNIT,
       L.LEDGER,
       L.FISCAL_YEAR,
       L.ACCOUNTING_PERIOD,
       L.ACCOUNT,
       ACC.DESCR,
       ACC.ACCOUNT_TYPE,
       ACC.EFF_STATUS,
       L.DEPTID,
       L.OPERATING_UNIT,
       L.PRODUCT,
       L.PROJECT_ID,
       L.CURRENCY_CD,
       SUM(L.POSTED_TOTAL_AMT) AS POSTED_TOTAL_AMT
  FROM PS_LEDGER L
  LEFT JOIN XX_TB_SETID_VW S
    ON S.BUSINESS_UNIT = L.BUSINESS_UNIT
   AND S.RECNAME = 'GL_ACCOUNT_TBL'
  LEFT JOIN XX_TB_ACCT_VW ACC
    ON ACC.SETID = S.SETID
   AND ACC.ACCOUNT = L.ACCOUNT
 GROUP BY L.BUSINESS_UNIT, L.LEDGER, L.FISCAL_YEAR, L.ACCOUNTING_PERIOD,
          L.ACCOUNT, ACC.DESCR, ACC.ACCOUNT_TYPE, ACC.EFF_STATUS,
          L.DEPTID, L.OPERATING_UNIT, L.PRODUCT, L.PROJECT_ID, L.CURRENCY_CD;
