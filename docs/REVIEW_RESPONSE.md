# Response to TRIAL_BALANCE_MCP_REVIEW.md

**Date:** 2026-07-29 · **Status:** review claims independently re-verified

Every claim tested reproduced against the sample ledger. Three defects are now
fixed and covered by regression tests; the remainder are accepted as open.

## Claims verified before any code changed

| Review claim | Result | Evidence |
|---|---|---|
| Chat host broken on MCP 2.0 (`inputSchema`) | **Confirmed** | SDK 2.0 `Tool` exposes `input_schema`; results expose `is_error` / `structured_content` |
| Empty scope reports `in_balance=true`, `clean=true` | **Confirmed** | unknown BU returned 0 rows, `in_balance=True`, `clean=True`, `issues=[]` |
| `through_period=998` double-counts adjustments | **Confirmed** | acct 6900 returned 998 trend rows, ending 34,000, adj 25,000, incl 59,000 |
| `use_views=true` + `group_by=FUND_CODE` fails | **Confirmed** | `OperationalError: no such column: L.FUND_CODE` |
| View mode only routes `XX_TB_BAL_VW` | **Confirmed** | 4 of 5 shipped views unreferenced at runtime |
| Unposted-journal check not ledger-scoped | **Confirmed** | no `LEDGER` predicate in the SQL |
| Tree join omits `SETCNTRLVALUE` | **Confirmed** | absent from `tree_rollup` |
| Effective dating resolves "as of today" | **Confirmed** | FY2025 TB shows the FY2026 rename of acct 4100 |
| Local wiki path containment | **Partly** | traversal probes (`../config.yaml`, `../pstb/server.py`) already blocked; `is_relative_to` + regular-file + extension allowlist remain valid hardening |

## Fixed

**1. MCP 1.x/2.x client compatibility** (`pstb/client/chat.py`)
Field access is normalized through `_field()` across both SDK majors.
`scripts/mcp_probe.py` now builds the provider `ToolSpec` list and runs
`clean_schema` over every tool — the step whose absence allowed this to ship
undetected. The blind 24,000-character result slice was replaced with
`_truncate_json`, which drops whole rows and records
`rows_omitted_for_context` instead of cutting JSON mid-object.

**2. No-data scopes can no longer read as clean** (`pstb/engine.py`)
`_scope_diagnosis()` distinguishes `business_unit_not_found`,
`ledger_not_found`, and `no_data_for_period`, and lists valid values.
`get_trial_balance` returns `scope_status` with `in_balance=null`;
`tb_integrity_check` returns `control_status="not_run"` with `balanced=null`
and `clean=null`.

**3. Adjustment-period basis** (`pstb/engine.py`)
`through_period=998` is a reporting basis, not a trend point. The trend now
runs to the last regular period, derived from the calendar via
`_max_regular_period()` so 13-period calendars work; adjustments are counted
once; the response carries `basis` and `requested_period`. Account 6900 FY2025
returns 9,000 regular + 25,000 adjustment = 34,000.

**4. Two follow-on defects found while testing the above**
- `tb_integrity_check` returned no debit/credit totals, so "does it balance,
  what are DR and CR" routed to a tool answering half the question. Added
  `total_debits`, `total_credits`, `account_count`.
- The first iteration of fix 2 introduced `status="failed"` for
  exception-found runs, which reads as "the trial balance failed". Renamed to
  `control_status="exceptions_found"` with a plain-language `summary`, keeping
  the balance verdict separate from the control verdict.

Smoke suite: **28 → 39 checks, all passing.**

## Live end-to-end results (Ollama `llama3.1:8b`, real stdio MCP)

The review noted that no live provider answer had been demonstrated. It has now
been, with results that support the review's central architectural point.

| Question | Before | After |
|---|---|---|
| "Does the TB balance, totals?" | crash at tool discovery | correct: balances, DR = CR = 6,419,357.27 |
| Same, first run after unblocking | **fabricated $1,234,567.89 / $1,245,678.90**, contradicted `balanced=true` | correct |
| "TB for business unit UK002" | would have reported a clean, balanced, zero TB | "not available — the business unit does not exist in the ledger" |
| "Is suspense within policy?" | — | correct figure (15,000 CR) after prompt hardening; first attempt asserted a verdict with no balance lookup |

`pstb/client/prompt.py` now carries an explicit anti-fabrication rule, a
never-contradict-a-tool-result rule, and a never-promise-a-future-tool-call
rule. **These reduce but do not eliminate the failure mode.** In the same runs
the model invented parameter names (`account_id`, `omit_zeroes`), repeated wiki
calls, and described a 30-day aging policy as a "policy limit".

This is direct evidence for review finding #10 and its recommendation: render
financial amounts and verdicts deterministically, and let the model explain
data that has already been verified. A small local model is not trustworthy as
the control surface for financial figures. A numeric-citation validator is the
right next control.

## The untrusted-wiki half of P0 #5 — now closed

That validator shipped, and then a second question was asked of it: grounded
by WHAT?

`guards.ungrounded_figures` proved a figure existed in some tool result. It
could not say which one, and wiki passages are tool payloads like any other —
so a balance typed into a page an AP clerk can edit grounded exactly like a
figure the ledger engine computed. Verified against the real code before the
fix: an answer stating a suspense balance of 0.00, sourced only from a wiki
passage, passed the guard clean.

Payloads now carry the tool that produced them (`guards.tagged_payload_numbers`),
and a second scanner objects when a sentence's stated source is not the source
that produced the figure:

- a **balance** carried only by a wiki passage is flagged — while a
  **threshold** carried only by a wiki passage is not, because supplying
  thresholds is the page's job. The nearest cue decides which is which.
- a figure from one system quoted as another's — a Coupa commitment called a
  ledger balance — is flagged with both names.

`prompt.py` carries the control/data half: a passage is quoted material, a
page is authoritative for policy and never for a balance, and an instruction
found inside a passage is content to report rather than an instruction to
obey. Measured against the live local model on a page stating a stale 0.00
and saying "no need to query the ledger", the model queried the ledger anyway
and reported the real 15,000.00. The guard is the backstop for when it does
not.

It is a caveat, never a withhold. Reading prose to decide what a sentence
claimed is arguable in a way "this number exists nowhere" is not, so being
wrong costs one bracketed clause rather than a blanked answer.

**Still open in P0 #5:** raw SQL exposure, and the identity question — the
database account still bypasses row-level security. `run_ps_query` executes
under a real PeopleSoft user's permission lists and discloses it, which is a
route toward P0 #4, not a closure of it.

## Accepted and open — not fixed

P0 #3 currency/amount-basis contract · P0 #4 identity and entitlements ·
P0 #5 raw SQL exposure (still enabled in the shipped config; the
untrusted-wiki half is closed above) · P1 #6 genuine view-only mode and ChartField alignment · P1 #7
historical effective dating and BU/ledger-aware calendar · P1 #8 901-912
adjustment mapping and period 999 · P1 #9 scoped control results · P1 #11 wiki
fail-closed · all P2 items.

These are product and platform scope rather than defects in the delivered path.
The review's ordered backlog is a reasonable sequence; items 3, 4, and 7 are
the ones that should block any shared deployment.

## Process note

The chat path was reported as ready while it was broken for every question.
The probe gave false assurance because it verified tool *names* only and never
built the tool schemas the client actually consumes. Any future test that
claims to cover the client must exercise the same code path the client runs.
