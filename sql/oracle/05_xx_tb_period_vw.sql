-- XX_TB_PERIOD_VW: detail-calendar periods with begin/end dates, so natural
-- language dates ("as of June 30", "last month") resolve to FISCAL_YEAR +
-- ACCOUNTING_PERIOD.

CREATE OR REPLACE VIEW XX_TB_PERIOD_VW AS
SELECT SETID,
       CALENDAR_ID,
       FISCAL_YEAR,
       ACCOUNTING_PERIOD,
       BEGIN_DT,
       END_DT
  FROM PS_CAL_DETP_TBL;
