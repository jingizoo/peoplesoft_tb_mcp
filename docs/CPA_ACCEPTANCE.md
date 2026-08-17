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
| B8 | **Find the custom billing-interface record for rejected rows; do not assume it starts with PS_.** | `search_metadata` → `get_metadata_context` → `profile_record`/`compare_records` when live discrimination is needed | Source/schema, logical record, physical object, match reason, confidence and provenance shown separately; an unresolved or ambiguous mapping stops before a live query | `search_records` is used while a current catalog is available, a prefix is invented, the first name match is trusted without context, or an unresolved physical object is queried |

## Metadata/custom discovery questions

These are structural acceptance questions. They need no financial scope and
must not read transaction rows unless the question explicitly asks for a live
population. Ask Gemini exactly this:

| ID | Ask Gemini exactly this | Expected path | Evidence required for a pass | Fail if |
|---|---|---|---|---|
| M1 | **Which record contains the field whose label is Approval Status? Show the exact label evidence. Do not query transaction rows.** | `search_metadata` and, only for a defensible candidate, `get_metadata_context` | The matched field/label facet, source/schema and confidence are shown; no exact match is reported as inconclusive rather than converted into a guessed record | A status-looking column is presented as an exact label match, `search_records` replaces an available metadata catalog, or any live row tool is called |
| M2 | **Find VENDOR across every configured database. If it exists in more than one source or schema, do not choose one silently; show each candidate and tell me which source/schema I must specify.** | `search_metadata` → `get_metadata_context(identifier="VENDOR")` | Every distinct source/schema candidate is retained; one source/schema is selected only if unique or explicitly supplied by the user | Same-named objects are merged, sort order silently chooses one, or a live query runs before ambiguity is resolved |
| M3 | **Find the physical table behind the custom record TU_FILE_INTFC. Do not add PS_ or any company prefix. If metadata cannot prove the mapping, stop and say it is unresolved.** | `search_metadata` → `get_metadata_context(identifier="TU_FILE_INTFC")` | Returned logical and physical identities are kept distinct; the confidence basis is quoted; the sample catalog correctly leaves the physical mapping unresolved | `PS_TU_FILE_INTFC` or another physical name is manufactured, an inconclusive mapping is described as confirmed, or the object is queried |
| M4 | **How many billing-interface rows are rejected for US001 right now? First identify the record without assuming a prefix, then use live scoped data; metadata alone is not an answer.** | `search_metadata` → `get_metadata_context` → `profile_record`/`compare_records` if needed → scoped `run_sql` or a curated live billing tool | A successful live call supplies the count and applies `BUSINESS_UNIT='US001'`; metadata explains object selection only | A catalog hit is narrated as the rejected-row count, the live call is missing/failed, or an unscoped population supports the answer |

## AP/GL questions

| ID | Ask Gemini exactly this | Expected path | Evidence required for a pass | Fail if |
|---|---|---|---|---|
| A1 | **How much did we owe vendors at the selected period end?** | Resolve the selected period end, then `get_open_payables(as_of_date="2026-06-30")` for the sample FY2026 P6 scope | Voucher date cut-off, current open-status basis, point-in-time completeness disclosure, amount/count by vendor, and either one currency basis or a prominent mixed-currency limitation | A voucher dated after the resolved cut-off is included, today's open AP is presented as a reconstructed historical subledger, or mixed face amounts are presented as one consolidated-currency total |
| A2 | **Which vendors had we paid through June 30, 2026?** | `get_vendor_payments(as_of_date="2026-06-30")` | Inclusive payment-date cut-off; void treatment; whether the payment population was actually scoped to the BU | A later payment is included, an unscoped installation-wide total is labelled as the BU, or void support is assumed |
| A3 | **Any confirmed duplicate vendor payments?** | `get_duplicate_payments` | Confirmed population reported separately from duplicate-voucher and same-amount review candidates; distinct non-void payment IDs and paid amount for each confirmation | A repeated invoice number with no payment header is called a duplicate payment |
| A4 | **Is AP complete for month-end?** | `run_playbook(playbook="ap_completeness")` | Exact period/cut-off; AP posting/pipeline exceptions; approved procurement items missing from AP; RNI/accrual population; composed verdict | Current trailing activity is labelled FY/period-end evidence, one unavailable step still yields `passed`, or booked AP and accrual candidates are added together without explaining the populations |
| A5 | **What should we accrue for AP at month-end?** | `run_playbook`, `get_coupa_rni`, or `coupa_to_ap_tie` | Uninvoiced received commitments separated from booked vouchers; source and currency; missing connector/check disclosed | Current open vouchers alone are called the accrual, or policy/procurement prose substitutes for transaction evidence |
| A6 | **Does AP accounting activity reconcile to the Finance-approved GL control accounts for this period?** | `reconcile_ap_to_gl(control_accounts="<approved comma-separated list>", fiscal_year=2026, period=6, as_of_date="2026-06-30")` | One AP/GL BU, ledger, approved account set, fiscal period and cut-off; an account-attributed `VCHR_ACCTG_LINE` population across all posting processes; AP-posted + Journal Generator-distributed status; signed base-currency amounts; complete JGEN journal identity matched to posted `JRNL_HEADER`/`JRNL_LN`; numeric and exact-key verdict plus observed exception categories | Voucher gross/payment arithmetic or a `PS_LEDGER` ending balance substitutes for period journal activity; another liability account enters the AP side; a missing/null amount, blank JGEN key, mixed currency, non-distributed AP row, unposted GL journal, duplicate key, empty population or capped result becomes a pass; or an observed residual is assigned a cause the tool did not prove |
| G1 | **Does the trial balance balance?** | `tb_integrity_check` | BU, ledger, FY/period; total debits, total credits, difference, and balance verdict | A zero/no-data scope is balanced, or totals are omitted while a verdict is asserted |
| G2 | **Are there unposted journals?** | `tb_integrity_check`, `run_playbook`, or a scoped journal drill | BU, ledger, FY/period and journal population; count/detail or explicit incomplete reason | Another ledger or period leaks in, or unreadable journal data becomes “none found” |
| G3 | **Are we ready to close GL?** | `run_playbook(playbook="close_readiness")` | Balance, suspense, unposted and unbalanced journals, retained-earnings roll, AR tie, billing handoff, period position, and composed verdict | A balanced TB alone is called ready, or any skipped step is hidden |
| G4 | **Our asset accounts 1000-1999 are up versus last year end. What drove it, and does the breakdown add up?** | `explain_balance_change` | Start/end basis, bridge drivers, residual/reconciliation, and exception-first explanation | Gemini calculates a bridge in prose, quotes a number absent from tool evidence, or omits the reconciliation |

For A6, Finance may govern the account list once in
`defaults.ap_control_accounts`; leaving it empty makes the tool refuse rather
than guess. `tools.ap_reconciliation_line_cap` defaults to 50,000 rows per
side and has a runtime hard ceiling of 100,000. Reaching either cap returns an
incomplete result with no totals or verdict. A6 is the APY1410/APY1420-style
period-activity control; APY1400/APY1405 remains the separate ending
open-liability reconciliation.

## Custom-record discipline

Custom does not mean “starts with `XX_`,” and delivered does not mean the
physical table necessarily starts with `PS_`. For every unfamiliar record,
Gemini should follow this evidence chain while the metadata catalog is
available:

```text
search_metadata(question terms)
  -> get_metadata_context(identifier, source when known)
  -> compare_records(candidates) when live discrimination is necessary
  -> describe_table/profile_record(returned physical object)
  -> curated financial tool or scoped run_sql for the requested population
```

`search_records` is the live PeopleTools fallback only when
`describe_metadata_catalog` reports `available=false`; it is not the primary
path when a current catalog exists. The logical record is the App Designer
identity. `physical_object`, `source` and `schema` identify the query target.
Gemini must use those values exactly as returned; it must not prepend `PS_`,
replace a company prefix, merge same-named objects across sources, or guess
when resolution is ambiguous. Match facets, term coverage, relevance,
confidence basis, provenance, populated status and observed status values are
selection evidence and should be summarized in plain language.

Metadata stops at structure. It contains no transaction rows, balances,
customer/supplier values or document-identifier values. A successful
`search_metadata`, `get_metadata_context` or `describe_metadata_catalog` call
therefore cannot support a billing, AR, AP or GL conclusion. The turn must
continue to a successful live financial tool or guarded `run_sql` call with
the caller's authorized business-unit and date/period scope. If that call is
missing, fails or is blocked, Gemini must withhold the conclusion.

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
- custom-record results show source/schema, logical record, physical object,
  match reason, relevance, confidence/provenance and whether ambiguity remains;
- large detail populations disclose truncation and remain exportable without
  implying that a displayed slice is complete.

## Run against Gemini 2.5 Pro

The case definitions and routing contracts can be validated without a live
database, Vertex credentials or an LLM:

```bash
.venv/bin/python -m json.tool evals/cases.json >/dev/null
.venv/bin/python -m unittest \
  tests.test_metadata_tool_contract tests.test_gemini_tuning
```

To replay the same prompts with a local model and the bundled sample database,
copy `config.example.yaml` to `config.yaml`, keep the sample SQLite connection,
build its metadata artifact, and select the local provider. This requires a
running Ollama model but no cloud credentials:

```bash
.venv/bin/python scripts/build_metadata_catalog.py
.venv/bin/python scripts/eval.py --provider ollama \
  --case custom-record
```

For the production replay, use the production read-only configuration and
refresh the artifact first. Do not point the sample-only `mcp_probe.py` at
production:

```bash
export PSTB_CONFIG=/etc/peoplesoft_tb_mcp/config.yaml
export GOOGLE_CLOUD_PROJECT=your-gcp-project
export GOOGLE_CLOUD_LOCATION=us-central1
gcloud auth application-default print-access-token >/dev/null

.venv/bin/python scripts/build_metadata_catalog.py
.venv/bin/python scripts/eval.py --provider gemini \
  --case metadata-field-label-discovery \
  --json eval-gemini-metadata-field-label.json
.venv/bin/python scripts/eval.py --provider gemini \
  --case metadata-ambiguous-source-schema \
  --json eval-gemini-metadata-ambiguity.json
.venv/bin/python scripts/eval.py --provider gemini \
  --case custom-billing-record-no-prefix \
  --json eval-gemini-custom-no-prefix.json
.venv/bin/python scripts/eval.py --provider gemini \
  --case metadata-live-evidence-boundary \
  --json eval-gemini-metadata-live-boundary.json
```

Before the ambiguity replay, confirm whether `VENDOR` actually exists in more
than one configured source/schema. If it does not, the case still verifies the
unique-source path; separately repeat M2 with an identifier that the first
`search_metadata` call shows in multiple sources. The pass criterion is the
same: Gemini must ask for or retain the source/schema instead of choosing by
sort order.

The production config must resolve to `llm.provider: gemini`,
`llm.gemini_model: gemini-2.5-pro`, and the intended read-only database
connections. Then run the full acceptance suite against the real MCP server:

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

The automated cases prove the required terminal tool, prohibited tools,
required arguments, refusals, and a small number of critical answer phrases.
For M4, also inspect the retained JSON call trace and require
`search_metadata` before `get_metadata_context` before the successful live
call; the current grader does not assert multi-call order. The cases do **not**
prove population math or visual quality. Those remain gated by the focused
unit tests and the manual evidence/presentation checks above.
