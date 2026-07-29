-- XX_TB_JRNL_VW: posted journal lines with header context, for TB drill-down
-- ("what makes up this balance / who posted it"). Posted only (JRNL_HDR_STATUS
-- = 'P'); unposted work is surfaced separately by the integrity check.

CREATE OR REPLACE VIEW XX_TB_JRNL_VW AS
SELECT H.BUSINESS_UNIT,
       H.JOURNAL_ID,
       H.JOURNAL_DATE,
       H.UNPOST_SEQ,
       H.FISCAL_YEAR,
       H.ACCOUNTING_PERIOD,
       H.SOURCE,
       H.OPRID,
       H.DESCR254_MIXED,
       J.JOURNAL_LINE,
       J.LEDGER,
       J.ACCOUNT,
       J.DEPTID,
       J.CURRENCY_CD,
       J.MONETARY_AMOUNT,
       J.LINE_DESCR
  FROM PS_JRNL_HEADER H
  JOIN PS_JRNL_LN J
    ON J.BUSINESS_UNIT = H.BUSINESS_UNIT
   AND J.JOURNAL_ID = H.JOURNAL_ID
   AND J.JOURNAL_DATE = H.JOURNAL_DATE
   AND J.UNPOST_SEQ = H.UNPOST_SEQ
 WHERE H.JRNL_HDR_STATUS = 'P';
