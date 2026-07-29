# Chart of Accounts Policy

**Owner:** Controller's office · **SetID:** SHARE

## Numbering ranges

| Range | Type | Examples |
|-------|------|----------|
| 1000–1999 | Assets (A) | 1000 Cash-Operating, 1100 AR, 1200 Inventory, 1590 Accum Depr (contra), 1999 Suspense |
| 2000–2999 | Liabilities (L) | 2000 AP, 2100 Accrued Liabilities, 2200 Payroll Withholding |
| 3000–3999 | Equity (Q) | 3000 Common Stock, 3500 Retained Earnings |
| 4000–4999 | Revenue (R) | 4000 Product Revenue, 4100 Services & Subscription Revenue |
| 5000–6999 | Expenses (E) | 5000 COGS, 6000 Salaries, 6400 Travel & Entertainment |

## Rules

- New accounts require Controller approval and an effective-dated row in the
  ACCOUNT chartfield (never re-use a retired number).
- **Capitalization threshold: $5,000** — purchases below it are expensed
  (typically 6900 Miscellaneous), at or above it go to 1500 Fixed Assets and
  depreciate monthly (6500 / 1590).
- Department is required on all P&L lines: 10000 Corporate, 20000 Sales,
  30000 Operations. Balance-sheet lines default to 10000.
- Contra accounts keep the sign of their section: 1590 Accumulated
  Depreciation is an asset account that normally carries a credit balance.
- Renames are done with a new effective-dated row (example: 4100 became
  "Services & Subscription Revenue" effective 2026-01-01).
