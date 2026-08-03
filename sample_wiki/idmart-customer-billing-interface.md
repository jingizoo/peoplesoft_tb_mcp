# IDMart Customer & Billing Interface (KB-1204)

Owner: Finance Systems. Applies to: all business units on the shared SetID.
Status: FICTIONAL SAMPLE — this page ships with the demo wiki so technical
research can be exercised before Confluence is connected.

## Overview

IDMart is the upstream data mart that feeds customer master changes and
billable activity into PeopleSoft. Two nightly flows run under the
process scheduler:

1. **Customer sync** — IDMART_CUST loads customer adds and changes into the
   staging record XX_IDM_CUST_STG, validates them, and applies survivors to
   PS_CUSTOMER under the shared SetID.
2. **Billing activity** — IDMART_BI extracts billable events, stages them in
   XX_IDM_BI_STG, maps them to charge codes, and inserts rows into
   PS_INTFC_BI for the delivered Billing Interface (BIIF0001) to pick up.

## Job stream

| Step | Process | Type | Notes |
|---|---|---|---|
| 1 | IDM_EXTRACT | SQR | pulls the nightly file from the mart landing zone |
| 2 | IDMART_CUST | App Engine | stages + validates customer rows |
| 3 | IDMART_BI | App Engine | stages billable events, maps charge codes |
| 4 | BIIF0001 | App Engine | delivered billing interface, creates BI_HDR |

Run controls live under Main Menu > IDMart Integration. The run control id
convention is IDM_<BU>_<freq>, e.g. IDM_US001_NIGHTLY.

## Staging records

- **XX_IDM_CUST_STG** — one row per inbound customer change. Key columns:
  IDM_BATCH_ID, CUST_ID, NAME1, LOAD_STATUS (N=new, E=error, P=posted),
  ERROR_MSG. Rows in E are NOT retried automatically.
- **XX_IDM_BI_STG** — one row per billable event. Key columns: IDM_BATCH_ID,
  BUSINESS_UNIT, CUST_ID, CHARGE_CODE, EVENT_AMT, LOAD_STATUS, ERROR_MSG.
  Mapped rows are written to PS_INTFC_BI with INTFC_ID = IDM_BATCH_ID.

## Rerunning after a failure

1. Find the failed batch: rows in the staging record with LOAD_STATUS = 'E'.
2. Fix the cause (most commonly a missing charge-code mapping or an inactive
   customer), then reset LOAD_STATUS to 'N' for that IDM_BATCH_ID only.
3. Re-run the App Engine (IDMART_CUST or IDMART_BI) with the SAME run
   control — the process is restartable and skips posted rows.
4. For billing, confirm PS_INTFC_BI rows reached LOAD_STATUS_BI = 'DON' and
   that BIIF0001 finalized the invoices; anything stuck in ERR needs the
   Billing workbench.

Never delete staging rows to clear an error: the batch id is the audit trail
back to the mart, and a deleted row simply reappears in the next extract.

## Common errors

| Symptom | Cause | Fix |
|---|---|---|
| E rows with "CHARGE_CODE not mapped" | new product in the mart | add the mapping row, reset to N, rerun IDMART_BI |
| E rows with "customer inactive" | CUST_STATUS = I in PS_CUSTOMER | reactivate or route to the exceptions queue |
| batch loads twice | run control date not advanced | the process is idempotent on IDM_BATCH_ID; duplicates are skipped, check the message log |
