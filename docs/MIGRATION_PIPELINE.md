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
| `delivered_missing` | Delivered dependency **gone** in 9.2 | blocker: retarget the custom object | none |
| `unknown_source` | Referenced but not found in 9.1 metadata | resolve or ignore explicitly | none |

### Emitted artifacts (`migrate_out/`)

- `README.txt` — the runbook, in run order
- `01_project_records.txt` — record list for the App Designer project
- `02_export_records.dms` / `03_import_records.dms` — Data Mover; import uses
  `REPLACE_DATA` so re-running is safe
- `drift/<REC>.sql` — per-drifted-record INSERT‑SELECT template with the
  common columns pre-mapped and `TODO` markers for 9.2-only columns
- `04_reconcile.sql` — DBA spot-check probes (the pipeline runs the same
  checks live)
- `plan.json` / `plan.md` — machine and human forms of the plan

## Setup

1. Add the 9.1 database under `sources:` in `config.yaml` and point
   `migrate.source` at it (see the commented block there). The primary `db:`
   is normally your 9.2 instance and is the default target. Credentials go in
   `.env` as `PSTB_SRC_<NAME>_DSN/_USER/_PASSWORD` — read-only accounts with
   SELECT on `PSRECDEFN`, `PSRECFIELD`, `PSDBFIELD`, `PSSQLTEXTDEFN`, and the
   `PS_%` tables being ported.
2. CLI: `python -m pstb.migrate discover | plan | show REC | emit |
   verify-build | reconcile | mark REC STATUS | status`
3. Agent-driven: run `python -m pstb.migrate.server` and attach the chat
   client (or any MCP host — Gemini CLI included) to drive the same steps as
   tools: `migrate_discover`, `migrate_plan`, `migrate_show_record`,
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
- **Delivered data never copies.** Setup/config content for delivered records
  comes from 9.2 itself or from the delivered conversion path.
- **Cross-release value drift** (chartfield values, SetIDs, translate values
  referenced by ported rows) is validated only indirectly by reconciliation;
  functional testing on 9.2 is still yours.
- **Every drift merge is a human decision.** The pipeline (and the LLM)
  drafts; App Designer applies; reconcile proves.
