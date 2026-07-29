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
48. Combined: "Is the suspense balance within policy?" *(TB number + wiki rule)*

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
