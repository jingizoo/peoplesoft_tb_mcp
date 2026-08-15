# Controller Acceptance: Billing/AR and AP/GL

This is the production acceptance pack for the finance assistant. It is
deliberately limited to **Billing/AR and AP/GL** and is written from a
controller's perspective: define the population, preserve the accounting
cut-off, reconcile where possible, and distinguish an exception from a check
that did not run.

The target provider is **Gemini 2.5 Pro on Vertex AI**. These questions test
the model and the MCP tools together. Unit tests remain the authority for the
underlying calculations; this pack checks whether Gemini selects the right
controlled calculation and presents its evidence honestly.

## What a passing answer looks like

Every financial answer should make the following visible without requiring the
reader to infer it from a table dump:

1. **Conclusion** — `passed`, `exceptions found`, `incomplete`, or `no data`.
   A zero is not a pass until the population was successfully read.
2. **Scope and cut-off** — business unit, ledger when relevant, fiscal period
   or as-of date, and whether the result is current-state or historical.
3. **Population** — what was included and excluded. Examples: finalized bills
   exclude cancelled and pipeline bills; confirmed duplicate payments require
   distinct non-void payment evidence.
4. **Amounts and basis** — currency beside every amount, no raw sum across
   currencies, and no unexplained sign conversion.
5. **Control evidence** — the expected counterpart or reconciliation, its
   difference, and whether it was evaluated.
6. **Exceptions and limitations** — the records that need attention and any
   missing table, column, date, rate, row scope, or truncated population.

An invalid business unit must read as **NO DATA / business unit not found**, not
as a clean zero. A skipped or unsupported check must read as **INCOMPLETE**, not
as passed. “The trial balance balances” is not, by itself, a close-readiness
conclusion.

## BI/AR questions

| ID | Ask Gemini exactly this | Expected path | Evidence required for a pass | Fail if |
|---|---|---|---|---|
| B1 | **Give me total finalized invoice amount** | `get_invoice_totals` | BU and FY basis; governed finalized predicate; totals by currency; pipeline and terminal/cancelled exclusions | Cancelled or in-process bills are included, currencies are combined, or raw SQL recreates the status rule |
| B2 | **For customer C1005, separate finalized, cancelled, and still-in-process billing.** | `get_customer_financial_360`, `get_invoice_lifecycle`, or `get_billing_workbench` | Each observed status is labelled finalized, terminal, or pipeline using the governed billing semantics | `CAN` is described as finalized revenue, or an unknown active status is silently discarded |
| B3 | **Show AR aging and reconcile it to the GL control account** | `get_ar_aging` | Open-item total and buckets; as-of/current-state disclosure; base/display currency; GL control balance, difference, and `evaluated`/tie result | Two unlike dates are compared, a missing tie is called a pass, or mixed currencies are summed raw |
| B4 | **Which customers are overdue or in dispute?** | `get_ar_aging` | Open items only; due-date aging; overdue and dispute amounts kept distinct; credits shown separately | Total billing is substituted for open AR, credits are hidden, or a missing dispute field is treated as no disputes |
| B5 | **Where is billing stuck before AR?** | `get_invoice_lifecycle` or `get_billing_workbench` | Interface waiting/errors; bills still in pipeline; finalized bills missing from AR; counts and amounts with age where available | A cancelled bill is a bottleneck, an unavailable stage is reported clean, or only a status dump is narrated |
| B6 | **Did every finalized invoice reach AR?** | `get_billing_workbench` | Finalized population and the anti-join to AR; lookback/date basis; explicit incomplete note if required columns are absent | Zero exceptions is called clean when the check could not run |
| B7 | **Who are the top billing customers in USD, and how much of their AR is overdue?** | `get_customer_intelligence` | Finalized billing basis; open AR converted to USD before aggregation; overdue amount explicitly labelled USD; FX evidence or missing-rate failure | Billing and AR currencies are mixed, or open balances in different currencies are added without conversion |
| B8 | **Find the custom billing-interface record for rejected rows; do not assume it starts with PS_.** | `search_records` → `compare_records`/`describe_record`/`profile_record` | Search matches on meaningful tokens, descriptions, fields, labels, or observed content; logical record and physical table shown separately; match reason/relevance disclosed | A `PS_` or company prefix is invented, the first name match is trusted without evidence, or an unresolved physical object is queried |

## AP/GL questions

| ID | Ask Gemini exactly this | Expected path | Evidence required for a pass | Fail if |
|---|---|---|---|---|
| A1 | **How much did we owe vendors at the selected period end?** | Resolve the selected period end, then `get_open_payables(as_of_date="2026-06-30")` for the sample FY2026 P6 scope | Voucher date cut-off, current open-status basis, point-in-time completeness disclosure, amount/count by vendor, and either one currency basis or a prominent mixed-currency limitation | A voucher dated after the resolved cut-off is included, today's open AP is presented as a reconstructed historical subledger, or mixed face amounts are presented as one consolidated-currency total |
| A2 | **Which vendors had we paid through June 30, 2026?** | `get_vendor_payments(as_of_date="2026-06-30")` | Inclusive payment-date cut-off; void treatment; whether the payment population was actually scoped to the BU | A later payment is included, an unscoped installation-wide total is labelled as the BU, or void support is assumed |
| A3 | **Any confirmed duplicate vendor payments?** | `get_duplicate_payments` | Confirmed population reported separately from duplicate-voucher and same-amount review candidates; distinct non-void payment IDs and paid amount for each confirmation | A repeated invoice number with no payment header is called a duplicate payment |
| A4 | **Is AP complete for month-end?** | `run_playbook(playbook="ap_completeness")` | Exact period/cut-off; AP posting/pipeline exceptions; approved procurement items missing from AP; RNI/accrual population; composed verdict | Current trailing activity is labelled FY/period-end evidence, one unavailable step still yields `passed`, or booked AP and accrual candidates are added together without explaining the populations |
| A5 | **What should we accrue for AP at month-end?** | `run_playbook`, `get_coupa_rni`, or `coupa_to_ap_tie` | Uninvoiced received commitments separated from booked vouchers; source and currency; missing connector/check disclosed | Current open vouchers alone are called the accrual, or policy/procurement prose substitutes for transaction evidence |
| G1 | **Does the trial balance balance?** | `tb_integrity_check` | BU, ledger, FY/period; total debits, total credits, difference, and balance verdict | A zero/no-data scope is balanced, or totals are omitted while a verdict is asserted |
| G2 | **Are there unposted journals?** | `tb_integrity_check`, `run_playbook`, or a scoped journal drill | BU, ledger, FY/period and journal population; count/detail or explicit incomplete reason | Another ledger or period leaks in, or unreadable journal data becomes “none found” |
| G3 | **Are we ready to close GL?** | `run_playbook(playbook="close_readiness")` | Balance, suspense, unposted and unbalanced journals, retained-earnings roll, AR tie, billing handoff, period position, and composed verdict | A balanced TB alone is called ready, or any skipped step is hidden |
| G4 | **Our asset accounts 1000-1999 are up versus last year end. What drove it, and does the breakdown add up?** | `explain_balance_change` | Start/end basis, bridge drivers, residual/reconciliation, and exception-first explanation | Gemini calculates a bridge in prose, quotes a number absent from tool evidence, or omits the reconciliation |

## Custom-record discipline

Custom does not mean “starts with `XX_`,” and delivered does not mean the
physical table necessarily starts with `PS_`. For every unfamiliar record,
Gemini should follow this evidence chain:

```text
search_records(question terms)
  -> compare_records(candidates) when more than one is plausible
  -> describe_record/profile_record(selected candidate)
  -> run_sql against the returned physical table name
```

The `record` value is the PeopleTools logical name. The `table` value is the
catalog-resolved physical object. Gemini must use the latter exactly as
returned; it must not prepend `PS_`, replace a company prefix, or guess when
resolution is ambiguous. `matched_terms`, `matched_on`, `relevance`, taught
facts, populated status, and observed status values are evidence for the
selection and should be summarized in plain language.

An operator-supplied table purpose may be proposed with
`remember_record_fact`, but remains pending until approved. It is a discovery
hint, not authority over the live catalog or contents.

## Presentation check

The GUI passes the controller review when:

- the starter questions are focused on Billing/AR and AP/GL;
- the conclusion, scope, as-of basis, currency, and primary exception are
  visible before lower-level detail;
- `NO DATA` and `INCOMPLETE` are visually distinct from a successful zero;
- exception and custom-record evidence opens without requiring the user to
  inspect raw JSON;
- custom-record results show logical record, physical table, match reason,
  relevance, and whether the object is populated;
- large detail populations disclose truncation and remain exportable without
  implying that a displayed slice is complete.

## Run against Gemini 2.5 Pro

Set `llm.provider: gemini` and `llm.gemini_model: gemini-2.5-pro`, then run the
structural suite against the real MCP server:

```bash
.venv/bin/python scripts/eval.py --provider gemini
```

Run an individual controller-risk case while tuning:

```bash
.venv/bin/python scripts/eval.py --provider gemini --case ap-open-as-of-cutoff
.venv/bin/python scripts/eval.py --provider gemini --case custom-billing-record-no-prefix
```

Before promoting a prompt, model setting, or tool-description change, run the
full suite three times in clean sessions and retain the JSON evidence:

```bash
for run in 1 2 3; do
  .venv/bin/python scripts/eval.py --provider gemini \
    --json "eval-gemini-${run}.json" || exit 1
done
```

The automated cases prove tool selection, required arguments, refusals, and a
small number of critical answer phrases. They do **not** prove the population
math or visual quality. Those remain gated by the focused unit tests and the
manual evidence/presentation checks above.
