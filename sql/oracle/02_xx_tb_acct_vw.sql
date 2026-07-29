-- XX_TB_ACCT_VW: current effective-dated row per account (description, type,
-- status). Collapses PS_GL_ACCOUNT_TBL's EFFDT history to "as of today".
-- ACCOUNT_TYPE: A=Asset, L=Liability, Q=Equity, R=Revenue, E=Expense.

CREATE OR REPLACE VIEW XX_TB_ACCT_VW AS
SELECT A.SETID,
       A.ACCOUNT,
       A.EFFDT,
       A.EFF_STATUS,
       A.DESCR,
       A.ACCOUNT_TYPE
  FROM PS_GL_ACCOUNT_TBL A
 WHERE A.EFFDT = (SELECT MAX(AX.EFFDT)
                    FROM PS_GL_ACCOUNT_TBL AX
                   WHERE AX.SETID = A.SETID
                     AND AX.ACCOUNT = A.ACCOUNT
                     AND AX.EFFDT <= TRUNC(SYSDATE));
