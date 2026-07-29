-- XX_TB_SETID_VW: business unit -> SETID resolution for the chartfield tables
-- the TB agent joins to. Source: PS_SET_CNTRL_REC (setid indirection).
-- Run as the PS schema owner (SYSADM) or prefix tables with SYSADM.
-- Recommended: create these as App Designer SQL View records (so they migrate
-- with projects); direct DB views work fine for a read-only reporting user.

CREATE OR REPLACE VIEW XX_TB_SETID_VW AS
SELECT SETCNTRLVALUE AS BUSINESS_UNIT,
       RECNAME,
       SETID
  FROM PS_SET_CNTRL_REC
 WHERE RECNAME IN ('GL_ACCOUNT_TBL', 'DEPT_TBL', 'CAL_DETP_TBL');
