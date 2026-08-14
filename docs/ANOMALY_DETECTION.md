# Transaction and Process Anomaly Detection

`detect_transaction_anomalies` answers three related operational questions for
one selected date:

1. Did a transaction/interface table's daily volume move materially away from
   its three- or six-month baseline?
2. Did one related table receive rows while a counterpart that normally moves
   with it received none?
3. Did an operational process take materially longer, or meet its successful
   status materially less often, than its own history?

The tool is read-only. It issues catalog reads and bounded, bind-parameterized
daily aggregate SELECTs. Identifiers enter SQL only after validation against
the live catalog. The configured database account should still be a
SELECT-only account, as it should be for every tool in this server.

## Discovery does not assume `PS_`

The detector starts with physical objects visible in the database catalog. When
PeopleTools metadata is readable, it maps records to those objects in this
order:

1. `PSRECDEFN.SQLTABLENAME` names an object that exists;
2. the record name itself exactly matches an object;
3. exactly one physical object ends in `_<record name>`.

The last rule finds both delivered physical names and company-prefixed names
without manufacturing either prefix. Ambiguous suffixes are not guessed.
`PSQRYRECORD` and `PSPNLFIELD` add evidence when two records are used by the
same saved query or page. Shared identifying fields and historical daily
co-activity/correlation supply the other relationship evidence. Every inferred
rule reports those inputs and a confidence; it is evidence, not a foreign-key
claim.

Automatic discovery is bounded by `candidate_limit`. A large table with no
catalog-supported date-range index is not scanned automatically unless its
optimizer row estimate is at or below `max_unindexed_rows`. It appears under
`discovery.skipped_for_scan_safety` instead. An explicit rule is the operator's
way to opt an important known table into the check.

## Configure important flows

Inference is useful for orientation, but explicit rules carry the site's actual
business semantics. Add them to the per-deployment `config.yaml` (never edit the
tracked example in place):

```yaml
anomalies:
  infer_tables: true
  infer_processes: true
  candidate_limit: 20
  max_unindexed_rows: 50000
  material_count: 10
  material_pct: 0.50
  z_threshold: 3.5

  table_rules:
    - name: invoice interface headers
      table: ACME_INV_HDR
      date_column: CREATED_DTTM
      scope_column: BUSINESS_UNIT
    - name: invoice distributions
      table: FIN_INV_DIST
      date_column: CREATED_DTTM
      scope_column: BUSINESS_UNIT

  relationship_rules:
    - name: accepted header creates distributions
      left_table: ACME_INV_HDR
      right_table: FIN_INV_DIST
      direction: left_requires_right
      minimum_trigger_count: 10
      confidence: 1.0
      explanation: accepted interface headers should create accounting distributions

  process_rules:
    - name: invoice loader
      table: ACME_PROCESS_RUN
      date_column: START_DTTM
      start_column: START_DTTM
      end_column: END_DTTM
      process_name_column: PROCESS_NAME
      status_column: RUN_STATUS
      success_values: [SUCCESS]
      scope_column: BUSINESS_UNIT
```

Names are physical table/column names exactly as the catalog exposes them.
They may be delivered, custom, or company-prefixed. Invalid/unreadable names
are returned in `configuration_errors`; they are never interpolated and tried.

Relationship directions are:

- `left_requires_right`: left-side activity expects right-side activity;
- `right_requires_left`: the reverse;
- `mutual`: activity on either side expects the other.

A process rule can use `duration_column` with `duration_unit: seconds`,
`minutes`, or `hours` instead of start/end timestamps. `success_values` should
contain the site's actual successful terminal codes. Without configured codes,
inference can compare the historically dominant status, but labels that result
as behavioral evidence rather than claiming the code means success.

## Statistical behavior

Call with `history_months=3` or `history_months=6`; other windows are rejected.
The baseline is robust rather than mean/standard-deviation only:

- missing dates are represented as zero for volume analysis;
- the same weekday is preferred when at least eight observations and enough
  active days exist, avoiding routine weekend/weekday false positives;
- otherwise a dense all-calendar-day baseline is used;
- sparse/month-end-only histories return `sparse_history` and do not turn a
  missing date into either a clean verdict or an alert;
- the expected value is the median, dispersion is median absolute deviation
  with a conservative count floor, and an alert must cross both the robust
  z-score threshold and absolute/percentage materiality.

Process duration baselines include only historical days on which that process
ran. A duration alert requires enough historical run days, a robust statistical
deviation, a percentage increase, and a minimum increase in seconds. Status
alerts require enough historical/today runs and a material rate drop.

## Reading the result

Each alert includes:

- `observed`: today's row counts, duration, run count, or status rate;
- `expected`: the relationship or historical baseline and its sample size;
- `severity`: `medium`, `high`, or `critical`;
- `confidence` and `confidence_score`;
- `evidence` where the relationship/process was inferred or configured;
- a plain-language `explanation` containing the observed and expected values.

Always inspect `status`, `checks_incomplete`, `configuration_errors`, table
baseline status, and `discovery.skipped_for_scan_safety`. Zero alerts with an
incomplete catalog/query or sparse history is not a clean operational verdict.

Example call:

```text
detect_transaction_anomalies(
  as_of_date="2026-07-31",
  history_months=6,
  business_unit="US001",
  include_inferred=true
)
```
