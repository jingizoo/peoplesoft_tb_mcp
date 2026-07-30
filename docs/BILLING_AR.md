# Billing & Receivables Pack

Read-only AR aging and Billing pipeline tools over the delivered records:

| Record | Used for | Columns modeled (subset) |
|---|---|---|
| `PS_ITEM` | open receivables, aging | BUSINESS_UNIT, CUST_ID, ITEM, ITEM_LINE, ITEM_STATUS, BAL_AMT, ORIG_ITEM_AMT, BAL_CURRENCY, ACCTG_DT, DUE_DT, DISPUTE_STATUS |
| `PS_CUSTOMER` | names, status | SETID, CUST_ID, NAME1, CUST_STATUS |
| `PS_BI_HDR` | billing lifecycle | BUSINESS_UNIT, INVOICE, BILL_STATUS, BILL_TO_CUST_ID, INVOICE_DT, INVOICE_AMOUNT, BILL_TYPE_ID, BILL_SOURCE_ID |
| `INTFC_BI` | interface staging/errors | INTFC_ID, INTFC_LINE_NUM, TRANS_TYPE_BI, LOAD_STATUS_BI, BILL_TO_CUST_ID, TARGET_INVOICE |

These are column *subsets* of the delivered records — the queries port to a
real FSCM schema unchanged, but verify column availability against your
PeopleTools version before trusting production numbers.

## Tools

- **`get_ar_aging`** — buckets (default current / 1-30 / 31-60 / 61-90 /
  over-90, configurable via `defaults.aging_buckets`) by customer, aggregated
  in SQL so it scales to a real PS_ITEM. **Every aging carries a GL tie-out**
  against the AR control account(s) (`defaults.ar_control_accounts`, default
  1100). The tie has an explicit, honest basis: **all current open items vs
  the GL balance through the latest posted period** — comparing a date-cut
  subledger to a period-end GL fabricates breaks for any mid-period date, so
  the tie is deliberately decoupled from the aging as-of date and labeled.
  If a control lookup fails or the scope is empty, the tie reports
  `evaluated: false` with the reason — never a pass.
- **`get_customer_ar`** — one customer's open items with days past due,
  bucket, credit/dispute flags. Accepts an ID or a name fragment; ambiguous
  names return candidates instead of guessing.
- **`search_customers`** — id, name, active/inactive, open balance.
- **`get_billing_workbench`** — invoice counts/amounts by BILL_STATUS,
  invoices pending longer than a threshold, billing-interface lines in error,
  and **finalized invoices missing from PS_ITEM** (billed but never reached
  AR — the classic revenue-stuck handoff failure).

## Semantics

- `BAL_AMT` positive = the customer owes it; negative = credit memo or
  on-account/unapplied receipt. Credits age in their own due-date bucket and
  are reported in `credit_amt`.
- Buckets classify by days past `DUE_DT` (falling back to `ACCTG_DT` when the
  due date is null) at the as-of date; items count when `ACCTG_DT <= as-of`.
- **A backdated as-of is an approximation, and says so.** `PS_ITEM` is a
  current-state record: items closed since the as-of date are gone, and
  partial payments show today's residual. Results with an as-of before the
  latest posted period end carry `historical_approximation: true` and a
  warning. True historical aging requires PS_ITEM_ACTIVITY reconstruction —
  future work.
- The GL tie is computed over the whole book even when the query is filtered
  to one customer — a filtered subtotal can never tie.
- Billing lifecycle: `NEW`/`PND` → `HLD` → `RDY` → `INV` (finalized) or `CAN`.

## Relationship to the Billing/Top-20 review

This pack implements the read-only core of the earlier
`BILLING_TOP20_REVIEW.md` (in the old prototype repo): the revenue-stuck queue
(simplified to the workbench), AR exposure/aging, and the AR/GL handoff check.
Deliberately not yet built: invoice delivery status, the credit/rebill
adjustment graph, customer corporate hierarchies (a governed Top-20 needs a
Finance-approved hierarchy, not inference), payment/collection history
(PS_PAYMENT, item activity), and any write actions. Those are the next tier
once these read-only views prove out against real data.

## Sample data

Eight customers; open items that sum **exactly** to the GL AR control balance
(the seed computes a plug item, then asserts the tie); a disputed 42,000 item
220+ days old; a credit memo and an on-account receipt; two ready-not-finalized
invoices, one on hold, one canceled; two interface error lines (one for an
unknown customer); and one finalized invoice deliberately missing from AR.
