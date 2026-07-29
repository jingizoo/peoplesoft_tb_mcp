# Replacing nVision Reports with Natural-Language Reporting

PS/nVision reports are, mechanically, three things the agent already queries:

| nVision concept | Where it lives | Agent equivalent |
|---|---|---|
| Row criteria (tree node, detail values) | layout (.xnv Excel file) | `"node": "REVENUE"` or `"account": "5000-5999"` in a report JSON |
| Column criteria (ledger, timespan) | layout (.xnv Excel file) | `{"ledger": "ACTUALS", "timespan": "YTD"}` |
| Amounts | PS_LEDGER | same table, same sums, base currency |
| TimeSpans (YTD, PER, BAL, Q1…) | PSTREE/TIMESPAN setup | built-in resolver (`resolve_timespan`) |
| Trees / nPlosion | PSTREENODE / PSTREELEAF | node rows; `"expand": true` = nPlosion |
| Scope (one report per BU/dept) | scope definition (DB) | run per business unit / loop |
| DrillDown | drilldown layouts | account drawer → journal drill in the UI |

## The one honest caveat

**Layout criteria live inside Excel `.xnv` files on the report server, not in
database tables.** There is no table to read a layout's rows and columns from.
So each layout is *transcribed once* into a small JSON file in `reports/` —
typically 10–30 minutes per layout while looking at the nVision output or the
layout definition in Excel. After that, the report runs from natural language
("run the income statement for June", "balance sheet as of Q2") and stays
consistent with the ledger because the amounts come from the same PS_LEDGER
sums nVision used.

Report *requests* (which layout, scope, as-of date) do live in the database
(`PS_NVS_REPORT`); use `describe_table`/`run_sql` against it to inventory what
your organization actually runs, and transcribe in order of usage.

## Transcribing a layout

Take each nVision row and column and write it down:

```json
{
  "name": "income_statement",
  "title": "Income Statement",
  "tree": "ACCOUNT",
  "rows": [
    {"id": "revenue", "label": "Revenue", "node": "REVENUE",
     "flip_sign": true, "expand": true},
    {"id": "cogs", "label": "Cost of Goods Sold", "account": "5000-5999"},
    {"id": "gross", "label": "Gross Margin", "subtotal": ["revenue", "-cogs"]}
  ],
  "columns": [
    {"label": "Actuals YTD", "ledger": "ACTUALS", "timespan": "YTD"},
    {"label": "Budget YTD", "ledger": "BUDGET", "timespan": "YTD"},
    {"label": "Variance", "subtract": ["Actuals YTD", "Budget YTD"]},
    {"label": "Var %", "percent": ["Variance", "Budget YTD"]}
  ]
}
```

Row types:

- `node` — a tree node; amounts are every account in the node's leaf ranges
  (ancestors included via the node-number span, exactly like nVision).
- `account` — a range `"5000-5999"`, list `"6100,6200"`, prefix `"64%"`, or
  exact value.
- `subtotal` — sum of earlier rows by `id`, with `-id` to subtract. Computed on
  displayed values.
- `flip_sign: true` — show credit balances (revenue, liabilities, equity)
  positive, the way statements read.
- `expand: true` — nPlosion: adds an indented detail row per account with a
  non-zero cell.

Column types: value columns (`ledger` + `timespan`) and computed columns
(`subtract: [a, b]`, `percent: [a, b]` referencing earlier column labels).

## TimeSpans

Resolved relative to the requested (fiscal year, period), like nVision's
as-of date:

| TimeSpan | Meaning |
|---|---|
| `PER` | activity of the period |
| `YTD` | periods 1..P |
| `BAL` | period 0 (balance forward) + 1..P — use for balance sheets |
| `YR` | full-year activity |
| `QTD`, `Q1`–`Q4` | quarter to date / fixed fiscal quarters |
| `ROLL12` | trailing 12 periods (spans fiscal years) |
| `PER-1Y`, `YTD-1Y`, `BAL-1Y`, `YR-1Y` | prior-year variants |
| `"4-6"` | any explicit period range |

`include_adjustments=true` adds the configured adjustment periods (998 by
default) to the YTD/BAL/YR-type spans.

Not yet supported: custom timespans defined in `PS_TIMESPAN_DEFN` (the
built-in set covers the delivered ones); 13-period calendars get correct
YTD/BAL/PER but the fixed quarters assume periods 1–12; summary ledgers,
translation ledgers, and book codes are not modeled.

## Scope equivalent

An nVision scope that bursts one report per business unit or department is a
loop: run the same report with a different `business_unit` (or transcribe a
department-filtered variant). Ask the agent for each, or script it against
`GET /api/report`.

## Asking in natural language

Once transcribed, these all work — in the chat, the terminal client, or any
MCP host:

- "Run the income statement for period 6."
- "Balance sheet as of Q2 — how does it compare to last year?"
- "Show budget vs actuals YTD. Which lines are over budget?"
- "Quarterly expense trend for 2026."
- "Rolling 12-month revenue." (ad-hoc: `rows="node:REVENUE:flip"`,
  `columns="ACTUALS:ROLL12"`)
- "What does the BAL timespan include?" (`resolve_timespan`)

The grid is computed by the engine and rendered deterministically; the model
narrates but never produces the figures.
