-- XX_TB_TREE_VW: flattened account tree — every (node, leaf-range) pair,
-- including ancestor nodes via TREE_NODE_NUM span containment. Lets the agent
-- roll a TB up to any tree node/level:
--   ledger L joins on L.ACCOUNT BETWEEN RANGE_FROM AND RANGE_TO
--   for rows at the desired TREE_LEVEL_NUM.
--
-- Notes:
--  - Latest effective tree version only.
--  - Blank RANGE_TO (single-value leaves; PS stores ' ') is normalized to
--    RANGE_FROM so BETWEEN works.
--  - Dynamic-detail trees and winter (setcntrlvalue-specific) trees may need
--    extra handling — see docs/VIEWS.md.

CREATE OR REPLACE VIEW XX_TB_TREE_VW AS
SELECT N.SETID,
       N.TREE_NAME,
       N.EFFDT,
       N.TREE_NODE,
       N.TREE_LEVEL_NUM,
       N.TREE_NODE_NUM,
       N.TREE_NODE_NUM_END,
       LF.RANGE_FROM,
       COALESCE(NULLIF(LF.RANGE_TO, ' '), LF.RANGE_FROM) AS RANGE_TO
  FROM PSTREENODE N
  JOIN PSTREELEAF LF
    ON LF.SETID = N.SETID
   AND LF.SETCNTRLVALUE = N.SETCNTRLVALUE
   AND LF.TREE_NAME = N.TREE_NAME
   AND LF.EFFDT = N.EFFDT
   AND LF.TREE_NODE_NUM BETWEEN N.TREE_NODE_NUM AND N.TREE_NODE_NUM_END
 WHERE N.EFFDT = (SELECT MAX(D.EFFDT)
                    FROM PSTREEDEFN D
                   WHERE D.SETID = N.SETID
                     AND D.SETCNTRLVALUE = N.SETCNTRLVALUE
                     AND D.TREE_NAME = N.TREE_NAME
                     AND D.EFFDT <= TRUNC(SYSDATE));
