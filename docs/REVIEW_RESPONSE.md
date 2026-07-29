# Response to TRIAL_BALANCE_MCP_REVIEW.md

**Date:** 2026-07-29 · **Status of review claims:** independently re-verified

Every claim I tested reproduced. Three defects are now fixed and covered by
regression tests; the rest are accepted as open and unfixed.

## Claims verified before changing anything

| Review claim | Verified | Evidence |
|---|---|---|
| Chat host broken on MCP 2.0 (`inputSchema`) | **Confirmed** | SDK 2.0 `Tool` exposes `input_schema`; result object exposes `is_error` / `structured_content` |
| Empty scope reports `in_balance=true`, `clean=true` | **Confirmed** | bogus BU returned 0 rows, `in_balance=True`, `clean=True`, `issues=[]` |
| `through_period=998` double-counts adjustments | **Confirmed** | acct 6900 returned 998 trend rows, ending 34,000, adj 25,000, incl 59,000 |
| `use_views=true` + `group_by=FUND_CODE` fails | **Confirmed** | `OperationalError: no such column: L.FUND_CODE` |
| View mode only routes `XX_TB_BAL_VW` | **Confirmed** | 4 of 5 shipped views unreferenced at runtime |
| Unposted-journal check not ledger-scoped | **Confirmed** | no `LEDGER` predicate in the SQL |
| Tree join omits `SETCNTRLVALUE` | **Confirmed** | absent from `tree_rollup` |
| Historical effective dating uses "as of today" | **Confirmed** | FY2025 TB shows the FY2026 rename of acct 4100 |
| Local wiki path containment | **Partly** — traversal probes (`../config.yaml`, `../pstb/server.py`) were already blocked; `is_relative_to` + regular-file + extension allowlist remain valid hardening |

## Fixed

**1. MCP 1.x/2.x client compatibility** (`pstb/client/chat.py`)
Field access normalized via `_field()` across both SDK majors. `mcp_probe.py`
now builds the provider `ToolSpec` list and runs `clean_schema` over every
tool — the step whose absence let this ship. Also replaced the blind
24,000-character slice with `_truncate_json`, which drops whole rows and
records `rows_omitted_for_context` rather than cutting JSON mid-object.

**2. No-data scopes can no longer read as clean** (`pstb/engine.py`)
New `_scope_diagnosis()` distinguishes `business_unit_not_found`,
`ledger_not_found`, and `no_data_for_period`, listing valid values.
`get_trial_balance` returns `scope_status` and sets `in_balance=null`;
`tb_integrity_check` returns `control_status="not_run"` with `balanced=null`
and `clean=null`.

**3. Adjustment-period basis** (`pstb/engine.py`)
`through_period=998` is now a reporting basis, not a trend point: the trend
runs to the last regular period (derived from the calendar via
`_max_regular_period()`, so 13-period calendars work), adjustments are counted
once, and the response carries `basis` and `requested_period`.
Account 6900 FY2025 now returns 9,000 regular + 25,000 adjustment = 34,000.

**4. Two follow-on defects found while testing the above**
- `tb_integrity_check` never returned debit/credit totals, so "does it balance,
  what are DR and CR" routed to a tool answering half the question. Added
  `total_debits` / `total_credits` / `account_count`.
- My own first fix introduced `status="failed"` for exception-found runs, which
  reads as "the trial balance failed". Renamed to
  `control_status="exceptions_found"` and added a plain-language `summary`
  keeping the balance verdict separate from the control verdict.

Smoke suite: **28 → 39 checks, all passing.**

## Live end-to-end results (ollama llama3.1:8b, real stdio MCP)

The review noted no live provider answer had been proven. It now has been —
with results that support the review's central architectural point.

| Question | Before fixes | After fixes |
|---|---|---|
| "Does the TB balance, totals?" | crash at tool discovery | correct: balances, DR = CR = 6,419,357.27 |
| Same, first run after unblocking | **fabricated $1,234,567.89 / $1,245,678.90** and contradicted `balanced=true` | correct |
| "TB for business unit UK002" | would have reported a clean, balanced, zero TB | "not available — the business unit does not exist in the ledger" |
| "Is suspense within policy?" | — | correct figure (15,000 CR) after prompt hardening; first attempt asserted a verdict with no balance lookup |

`pstb/client/prompt.py` now carries an explicit anti-fabrication rule, a
never-contradict-a-tool-result rule, and a never-promise-a-future-tool-call
rule. **These reduce but do not eliminate the failure mode.** In the same runs
llama3.1:8b invented parameter names (`account_id`, `omit_zeroes`), repeated
wiki calls, and characterized a 30-day aging policy as a "policy limit".

This is direct evidence for review finding #10 and its recommendation: render
financial amounts and verdicts deterministically, and let the model explain
already-verified data. A small local model is not trustworthy as the control
surface for financial figures. A numeric-citation validator is the right next
control.

## Accepted and open — not fixed

P0 #3 currency/amount-basis contract · P0 #4 identity and entitlements ·
P0 #5 raw SQL and untrusted-wiki exposure (still enabled in shipped config) ·
P1 #6 genuine view-only mode and ChartField alignment · P1 #7 historical
effective dating and BU/ledger-aware calendar · P1 #8 901-912 adjustment
mapping and period 999 · P1 #9 scoped control results · P1 #11 wiki fail-closed
· all P2 items.

These are product and platform scope, not defects in the delivered path. The
review's ordered backlog is a reasonable sequence; items 3, 4, and 7 are the
ones I would not ship a shared deployment without.

## Correction to my own earlier statements

- I told the user the chat path was ready to run. It was broken for every
  question. The probe I wrote gave false assurance by checking tool names only.
- I reported "no model pulled" for Ollama. `llama3.1:8b` had been present for
  seven days; the Ollama **server** was not running, and I misread the error.
