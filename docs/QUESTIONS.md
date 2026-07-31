# Trial-Balance Question Catalog

Questions the agent is built to answer, grouped by intent, with the tool(s) each
routes through. Use these as acceptance tests when trying a new model.

## Balances & the TB itself → `get_trial_balance`, `get_account_balance`
1. Show me the trial balance for period 6, FY2026.
2. Trial balance for US001 as of June 30. *(date → `resolve_period` first)*
3. TB for accounts 6000–6999 only. *(account="6000-6999")*
4. Trial balance by department. *(group_by="DEPTID")*
5. TB for department 20000 only.
6. What's the ending balance of Accounts Receivable? *(search_accounts → 1100)*
7. What was the beginning balance of cash this period?
8. Show the monthly trend of account 1000 this year.
9. YTD activity for account 6400.
10. Trial balance in currency detail. *(currency="detail")*
11. Final year-end TB including audit adjustments. *(include_adjustments=true / period 998)*
12. Which accounts have a credit balance right now?
13. List every expense account with its balance. *(account_type filter + TB)*

## Comparisons & variance → `compare_trial_balance`
14. Compare period 6 to period 5 — what moved the most?
15. How does P6 FY2026 compare to P6 FY2025?
16. Which accounts changed more than $50,000 vs last period?
17. Top 10 movers this month with % change.
18. Any accounts that are new this year, or that dropped to zero?
19. Why did travel expense spike in April? *(compare → drill_to_journals)*

## Drill-down → `drill_to_journals`
20. What journals make up the travel expense in period 4?
21. Who posted to accrued liabilities in December?
22. Show the journal lines behind account 5000 for period 3 with descriptions.
23. Does the journal detail tie to the ledger for account 6400 P4? *(tie-out flag)*
24. What sources (AP, payroll, billing...) posted to cash this period?

## Health / integrity → `tb_integrity_check`
25. Does the trial balance balance?
26. Are there unposted journals this period?
27. Is there anything in suspense? How old is it? *(+ wiki for the 30-day rule)*
28. Any accounts with balances but no chartfield definition?
29. Any posted journals that don't net to zero?
30. Did beginning balances roll correctly from last year's close? *(RE roll)*
31. Is the ledger clean enough to close?

## Rollups & captions → `rollup_trial_balance`, `list_trees`
32. What are total assets as of period 6?
33. Summarize the TB by financial-statement caption.
34. What's net income YTD? *(REVENUE + EXPENSES nodes, sign-flipped)*
35. Do assets equal liabilities plus equity?

## Chartfields & metadata → `search_accounts`, `list_*`
36. What account is 1590? Is it active?
37. Find all accounts with "cash" in the name.
38. List the revenue accounts.
39. What business units exist? What ledgers does US001 have?
40. What departments are charged for travel? *(TB group_by DEPTID, account 6400)*

## Calendar → `resolve_period`, `list_periods`
41. What fiscal period is today in?
42. What are the period start/end dates for FY2026?
43. Which period does 2026-03-15 fall in?

## Policy & context (wiki) → `wiki_search`, `wiki_get_page`
44. What's our policy on suspense balances?
45. What's the capitalization threshold?
46. Walk me through the month-end close checklist — where does TB review happen?
47. When is adjustment period 998 used, and who can post to it?
48. Combined: "Is the suspense balance within policy?" *(wiki_lookup rule + ledger figure; the answer guard flags a verdict missing either half)*
48p. "What is our capitalization threshold, and does this $6,000 purchase qualify?"
48q. "Quote the rule on adjustment period 998 — who can post to it?"

## Financial statements & nVision-style → `list_reports`, `run_report`, `resolve_timespan`
48a. Run the income statement for period 6.
48b. Balance sheet as of Q2 — compared to last year.
48c. Budget vs actuals YTD; which lines are over budget?
48d. Quarterly expense trend for this year.
48e. Rolling 12-month revenue.
48f. What periods does the BAL timespan cover?
48g. Income statement including audit adjustments (post-998 basis).
See docs/NVISION.md for migrating existing nVision layouts.

## Receivables & Billing → `get_ar_aging`, `get_customer_ar`, `search_customers`, `get_billing_workbench`
48h. Show the AR aging by customer. Does it tie to the GL?
48i. Who owes us the most? What's overdue past 90 days?
48j. What does Beacon Health owe, and is anything disputed?
48k. Are there credit memos or unapplied receipts outstanding?
48l. Any invoices stuck in billing — ready but not finalized, or on hold?
48m. Are there billing interface errors?
48n. Did every finalized invoice make it to AR?
48o. Which customers are inactive but still carry a balance?
48r. Top 10 customers by open AR in USD terms. *(display_currency="USD" — the
     server converts each item at the effective rate and ranks; `fx_applied`
     lists the conversions)*
48s. Show the aging converted to INR. *(same — never per-row math in the model)*

## Payables, Assets, and every other module → `search_records` + `run_sql`
48w. How many payments did we make to each vendor? *(no curated AP tool —
     search_records("payment") finds PS_PAYMENT_TBL / PS_PYMNT_VCHR_XREF,
     then run_sql joins to PS_VENDOR)*
48x. Which vouchers are posted but unpaid? *(PS_VOUCHER + PS_PYMNT_VCHR_XREF)*
48y. What is our depreciation this period? *(PS_DEPRECIATION)*
Curated tools cover GL, Receivables and Billing because those need exact
semantics. Everything else — Payables, Asset Management, Commitment Control,
Projects, Expenses, custom records — is reached this way. The agent must
never claim a module is unavailable before checking; if records really are
absent or ungranted it names them so you can request exactly those grants.

## Custom & site-specific records → `search_records`, `describe_record`
48t. What files are configured in our file interface? *(no table name known —
     search_records("file interface") searches PeopleTools RECDESCR, finds
     TU_FILE_INTFC, then run_sql against PS_TU_FILE_INTFC)*
48u. Which record holds FILE_ID? *(field-name search)*
48v. What columns does PS_TU_FILE_INTFC have? *(describe_record)*
48z. Which of these records actually holds our open invoices? *(compare_records
     profiles each candidate: which are populated, which columns this site
     fills in, and what codes their status columns really hold — names alone
     cannot separate a live record from its history shell or staging table)*
Ad-hoc results carry `scope_filtered`: when a business unit is selected and the
query did not filter on it, the answer says the rows span business units.

## Readiness reviews → `run_playbook`, `list_playbooks`
51. Are we ready to close the period? *(close_readiness: balance, suspense,
    unposted and unbalanced journals, RE roll, AR tie, billing pipeline,
    period position — one verdict)*
52. How healthy are receivables? *(receivables_health)*
Verdicts: `passed` (every step ran, nothing found), `exceptions_found` (every
step ran, some found something), `incomplete` (a step COULD NOT run — never a
pass). Each step calls the same curated tool the individual question would, so
a playbook can never disagree with the tool it wraps.

## Anything else → guarded `run_sql` (+ `list_tables`, `describe_table`)
49. How many journals were posted in FY2026 by source?
50. Which operator posted the most journal lines this year?

## Future direction (PeopleSoft Finance–wide roadmap)
- Budget vs actuals (LEDGER_BUDG / KK_ ledgers) — budget comparison tool pack.
- AP: open vouchers, payments due, vendor aging (PS_VOUCHER, PS_PYMNT_VCHR_XREF).
- AR: customer aging, unapplied cash (PS_ITEM, PS_PAYMENT).
- Asset Management: FA roll-forward tie-out to 1500/1590 (PS_COST, PS_DEPRECIATION).
- Commitment control: encumbrance vs budget (PS_KK_ACTIVITY_LOG).
- Consolidations: intercompany/affiliate elimination checks (AFFILIATE chartfield).
- Allocation results tracing (PS_ALLOC_*).
