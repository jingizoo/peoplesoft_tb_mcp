# 9.1 → 9.2 record-porting pipeline

Ports **custom records and their data** from a PeopleSoft 9.1 instance into an
existing 9.2 instance. The 9.2 environment stays the system of record for
everything Oracle delivers; this pipeline moves only what your site built.

It is deliberately **not** an upgrade tool. Bringing full transactional
history from 9.1 into 9.2 is Change Assistant + Oracle's delivered data
conversion, and nothing here replaces that. What this pipeline automates is
the porting project around it: finding the custom records, proving what they
depend on, deciding record-by-record what has to happen, generating the apply
artifacts, and verifying — with numbers — that the port worked.

## Write policy (the rule everything else follows)

The pipeline **never writes to either database**. PeopleTools metadata is
managed — version counters, cached bytecode, and cache invalidation belong to
Application Designer, and direct DML on PeopleTools tables corrupts
environments. So:

- **Definitions** move through an Application Designer project (copy to file
  on 9.1 → copy from file on 9.2 → Build).
- **Data** moves through Data Mover scripts the pipeline generates.
- **Drifted records** get a mapping script a human reviews and a DBA runs.
- The pipeline itself only ever SELECTs — through the same guarded `Database`
  layer as every other tool in this repo — and verifies afterwards.

## The cycle

```
discover ──► plan ──► review ──► emit ──► APPLY (App Designer / Data Mover)
                ▲                              │
                └── reconcile ◄── verify-build ┘        state db tracks every record
```

| Step | What it does | Who acts |
|---|---|---|
| `discover` | Candidate custom records on 9.1: naming prefixes (`Z_`…) and/or `LASTUPDOPRID <> 'PPLSOFT'`. Both are heuristics; the output is a review list. | pipeline |
| `plan` | Dependency closure over the seeds — subrecords, audit records, related-language records, prompt (edit) tables, records referenced in view SQL — then classifies each record against 9.2. Persists to the state db. | pipeline |
| review | Walk the plan; `show` any record for a field-level 9.1↔9.2 diff. This is where the LLM (Gemini/Ollama via the MCP server) earns its keep: triaging drift, proposing mappings, spotting a prompt table you'd rather re-point than port. | you + LLM |
| `mapping` | For every record whose shape differs: where each 9.2 column's value comes from, and what that costs. `mapping-template` writes a starter overrides file with rename candidates pre-filled. | pipeline + you |
| `preflight` | Counts, on the **real 9.1 data**, the rows each mapping would truncate, round, overflow, or collide on the 9.2 key. Read-only. | pipeline |
| `emit` | Writes the apply artifacts (below). Files only. | pipeline |
| apply | App Designer: project + Build. Data Mover: export on 9.1, import on 9.2. Mark progress with `mark`. | operator |
| `verify-build` | Confirms each planned table physically exists in 9.2 with every expected column. | pipeline |
| `reconcile` | Row counts **and numeric-column sums** on both sides. Sums catch the partial load and the zeroed column that counts alone miss. Clean records advance to `reconciled`. | pipeline |

### Classifications

| Classification | Meaning | Definition | Data |
|---|---|---|---|
| `build_and_load` | Custom table, absent in 9.2 | App Designer project + Build | Data Mover |
| `build_definition` | Custom view/subrecord/work/temp, absent | project + Build | none |
| `load_only` | Custom table already in 9.2, identical shape | none | Data Mover |
| `already_present` | Custom non-table already in 9.2, identical | none | none |
| `drift_review` | In both, **shapes differ** | manual merge in App Designer | reviewed mapping SQL |
| `delivered_ok` | Delivered dependency, present in 9.2 | none — never copy delivered objects | none |
| `delivered_convert` | Delivered, in both, `delivered_data: convert` | none — 9.2 owns the definition | Data Mover if shapes match, else mapping SQL |
| `delivered_missing` | Delivered dependency **gone** in 9.2 | blocker: retarget the custom object | none |
| `unknown_source` | Referenced but not found in 9.1 metadata | resolve or ignore explicitly | none |

### Emitted artifacts (`migrate_out/`)

- `README.txt` — the runbook, in run order
- `01_project_records.txt` — record list for the App Designer project
- `02_export_records.dms` / `03_import_records.dms` — Data Mover; import uses
  `REPLACE_DATA` so re-running is safe
- `convert/<REC>.sql` — mapping-driven `INSERT … SELECT` for every reshaped
  record, with each risk restated as a comment above the statement
- `04_reconcile.sql` — DBA spot-check probes (the pipeline runs the same
  checks live)
- `05_staging_ddl.sql` — landing tables in the 9.1 shape (staging mode only)
- `resolved_mappings.json` — every column decision and risk, for review
- `plan.json` / `plan.md` — machine and human forms of the plan

## Delivered data: shape-aware conversion

Default is `migrate.delivered_data: skip` — 9.2 ships its own delivered
content and Oracle's upgrade path converts history through delivered
conversion programs.

Set it to `convert` when you are **reimplementing onto a standing 9.2
instance** and the data has to come across anyway. Understand what that
trades away: this path moves rows directly and therefore bypasses both 9.2's
delivered content and Oracle's delivered conversion App Engine programs,
which do more than reshape columns — they apply functional conversions
(new setup rows, changed status domains, restructured related tables). Before
converting a record, confirm 9.2 does not already populate it, or the load
will duplicate or contradict what is there.

With the switch on, seed the delivered records you want (`discover
--delivered-like 'JRNL%'` finds them by pattern) and they classify as
`delivered_convert`. Identical shapes get a straight Data Mover copy;
different shapes go through the mapping engine.

### The mapping engine

For each physical column of the **9.2** table it resolves a source, in order:

1. an operator override, 2. the same-named 9.1 column, 3. PeopleSoft's own
type default (`' '` for character, `0` for numbers, `NULL` for dates) — the
same values App Designer Build would write.

It then reports what each decision costs, tagged with a severity and a code:

| Code | Severity | Meaning |
|---|---|---|
| `type_family` | blocker | char↔number↔date change; needs an explicit `expr` |
| `unsourced_key` | blocker | a 9.2 **key** column with no 9.1 source — every row would get the same value |
| `bad_override` | blocker | an override points at a column 9.1 does not have |
| `key_set_change` | blocker | 9.2 dropped a key column, so distinct 9.1 rows collide |
| `truncation` | warning | the 9.2 column is shorter |
| `rounding` / `overflow` | warning | fewer decimals / fewer integer digits |
| `unsourced_column` | warning | new in 9.2, filled with a default |
| `dropped_columns` | warning | 9.1 columns with no home in 9.2 |

Unmapped 9.2 columns get **rename candidates**, ranked by type compatibility
and name similarity — suggestions only, never auto-applied, because a wrong
auto-rename moves data into the wrong column silently.

### The overrides file (`migrate_mappings.json`)

Operator-authored, reviewed like config. `mapping-template` writes a starter
version with every unresolved column and its rename candidates:

```json
{"JRNL_LN": {
   "where": "BUSINESS_UNIT = 'US002'",
   "columns": {
     "REFERENCE_ID":     {"from": "OLD_REF"},
     "AMOUNT_BASE":      {"expr": "TO_NUMBER(OLD_AMT_TEXT)"},
     "PROCESS_INSTANCE": {"default": "0"}}}}
```

`where` is what keeps a reimplementation tractable — migrate open items or a
date cutoff rather than all history. Expressions reach the generated SQL
verbatim; that is what makes arbitrary conversions possible and why the file
belongs under review.

### Pre-flight: predictions become counts

`mapping` says "this column may truncate". `preflight` answers the question
that actually decides the migration — *how many rows, right now*:

```
truncation      DEPTID                     at_risk=2   warning
rounding        MONETARY_AMOUNT            at_risk=1   warning
key_collision   BUSINESS_UNIT, JOURNAL_ID  at_risk=1   blocker
```

Probes are read-only aggregates against 9.1, filtered by the mapping's
`where` so the numbers describe the rows that will really move. Key collision
groups by the **source expressions behind the 9.2 key columns**, so it
measures what the insert will actually do.

Four of the risk codes are *measurable* (`truncation`, `rounding`,
`overflow`, `key_set_change`): a zero count clears them, which is the whole
point of measuring. The rest (`type_family`, `unsourced_key`,
`bad_override`) cannot be counted away and keep blocking however clean the
data looks. Blocked records are marked `blocked` in the state db.

Probes that a dialect cannot express are reported as `unavailable_probes`
rather than skipped, so an unrun check never reads as a passed one.
Type-conversion validity itself is not probed — an `expr` override is
operator-supplied SQL and is flagged for review, not verified.

### Reconciliation follows the mapping

Once a mapping is in play the two sides are not symmetric, so `reconcile`
compares the source **filtered by the same `where`**, pairs sums across
**renames** (`OLD_REF` ↔ `REFERENCE_ID`), and lists 9.2 columns with no 9.1
source under `unverifiable_columns` instead of silently implying they
matched.

## Setup

1. Add the 9.1 database under `sources:` in `config.yaml` and point
   `migrate.source` at it (see the commented block there). The primary `db:`
   is normally your 9.2 instance and is the default target. Credentials go in
   `.env` as `PSTB_SRC_<NAME>_DSN/_USER/_PASSWORD` — read-only accounts with
   SELECT on `PSRECDEFN`, `PSRECFIELD`, `PSDBFIELD`, `PSSQLTEXTDEFN`, and the
   `PS_%` tables being ported.
2. CLI: `python -m pstb.migrate discover | plan | show REC | mapping |
   mapping-template | preflight | emit | verify-build | reconcile |
   mark REC STATUS | status`
3. Agent-driven: run `python -m pstb.migrate.server` and attach the chat
   client (or any MCP host — Gemini CLI included) to drive the same steps as
   tools: `migrate_discover`, `migrate_plan`, `migrate_show_record`,
   `migrate_mapping`, `migrate_mapping_template`, `migrate_preflight`,
   `migrate_emit`, `migrate_verify_build`, `migrate_reconcile`,
   `migrate_mark`, `migrate_status`.

Progress lives in `migrate_state.db` (SQLite, one row per record) so the port
survives restarts, and replanning refreshes classifications without losing
recorded progress — unless a record's classification changed, in which case
its progress deliberately resets.

## Scope and honest limits

- **Records and data only.** PeopleCode, pages, components, App Engine and
  the rest of a retrofit are out of scope here (the compare/triage pattern
  extends to them, but text extraction and apply paths differ per type).
- **Delivered data is opt-in and lossy by nature.** `delivered_data: convert`
  moves rows, not meaning: it cannot perform the functional conversions
  Oracle's delivered programs do. It suits a reimplementation carrying master
  data, configuration, and bounded history — not a substitute for an upgrade.
- **Cross-release value drift** (chartfield values, SetIDs, translate values
  referenced by ported rows) is validated only indirectly by reconciliation;
  functional testing on 9.2 is still yours. Referential integrity **between**
  converted tables is not checked — reconciliation is per-record.
- **Row counts and sums are the proof, not correctness.** Matching totals mean
  the rows arrived; they do not mean the values mean the same thing in 9.2.
- **Every drift merge is a human decision.** The pipeline (and the LLM)
  drafts; App Designer applies; reconcile proves.
