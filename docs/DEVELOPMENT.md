# Development Notes

MCP server + LLM chat client answering PeopleSoft trial-balance questions.
Python package `pstb`; runs against a bundled SQLite sample ledger or a real
PeopleSoft Oracle database.

## Commands

Cross-platform (Windows, macOS, Linux):

```
python scripts/setup.py              # interactive: venv, credentials, discovery, pre-flight
python scripts/bootstrap.py          # unattended: venv + install + seed + verify
python scripts/seed_sample_data.py   # rebuild the sample ledger (stdlib only)
python scripts/smoke_test.py         # engine tests, stdlib only, no install needed
```

With the virtualenv active:

```
python scripts/mcp_probe.py                    # spawn the server over stdio, no LLM
python scripts/diagnose_db.py                  # time each DB step (find slow queries)
python scripts/diagnose_wiki.py                # prove the wiki is connected and real
python scripts/build_metadata_catalog.py       # atomically refresh structural discovery
python -m pstb.client.chat                     # chat REPL
python -m pstb.client.chat --provider gemini   # chat via Gemini on Vertex AI
python -m pstb.client.chat --provider claude   # chat via Claude on the Anthropic API
python -m pstb.client.chat --ask "..."         # one-shot question
python -m pstb.server                          # run the server standalone
python -m pstb.gui --open                      # web UI on 0.0.0.0:8016
```

macOS/Linux shortcuts: `make venv`, `make seed`, `make smoke`, `make probe`,
`make chat`, `make chat-gemini`, `make chat-claude`.

## Architecture

- `pstb/server.py` — MCP stdio server; thin tool wrappers whose docstrings are
  what the model reads. Errors are returned as `{"error": ...}` dicts so the
  agent loop survives them.
- `pstb/engine.py` — trial-balance math (pure logic, stdlib only). Pivots
  `PS_LEDGER` period buckets into beginning / activity / ending.
- `pstb/queries.py` — SQL builders. Inline base-table mode, or `XX_TB_*` view
  mode via `db.use_views`. Bind parameters always; identifiers are formatted in
  only from allowlists.
- `pstb/db.py` — SQLite / Oracle / SQL Server connections; rows come back as
  lowercase-keyed dicts.
- `pstb/wiki.py` — Confluence REST or a local markdown folder, with automatic
  fallback; `lookup()` searches, fetches and returns ranked passages.
- `pstb/retrieve.py` — heading-aware passage splitting + BM25 (stdlib).
- `pstb/guards.py` — structural answer guards, including MECHANICAL number
  grounding: every figure in an answer must appear in a tool payload from the
  same turn or the answer is withheld. Prompts and verdict checks reduce
  fabrication; this makes it impossible. Other guards: continue on a promised-but-
  unmade tool call; flag a compliance verdict missing rule or figure.
- `pstb/ar.py` — AR aging (with GL control tie-out) and billing pipeline
  over PS_ITEM / PS_CUSTOMER / PS_BI_HDR / INTFC_BI. Record shapes are
  introspected at runtime (ACCTG_DT vs ASOF_DT dating, optional
  DISPUTE_STATUS/BAL_CURRENCY) and adaptations disclosed via record_notes —
  never assume the reference layout survives contact with a real site.
- `pstb/memory.py` — site memory: facts about THIS installation (aliases,
  calendar conventions, exclusions). Proposed by the model, ACTIVE ONLY
  after a human approves — `python -m pstb.memory` reviews the queue.
  Approved facts enter the prompt as context explicitly subordinate to
  tool results, so a remembered alias can never outrank the database.
- `pstb/playbooks.py` — accountant workflows (close readiness,
  receivables health) composed from curated tools and run
  server-side. The SEQUENCE lives in Python so the model cannot
  reorder or forget a step; it triggers and narrates only.
- `pstb/anomalies.py` — read-only, metadata-led daily volume, related-table,
  and process-performance anomaly detection. Physical names come from the
  live catalog/PeopleTools metadata rather than a prefix convention; robust
  weekday-aware/active-day baselines disclose sparse or incomplete evidence;
  caller business-unit scope and per-feed freshness are enforced in-module.
- `pstb/procgraph.py` — offline process-relationship index. PeopleTools
  catalogs are keyset-paginated to configurable 100k defaults; SQLite writes
  are batched and atomic, with explicit partial-source metadata and guarded
  node/edge/memory ceilings. Memory is preflighted before merge allocation;
  query-time walks stay separately bounded.
- `pstb/metadata.py` — versioned, offline structural catalog across the primary
  and named database sources. Native tables/views, columns and ordered indexes
  are joined to available PeopleTools records, fields, labels, translate
  values, pages and public saved-query use. Search is local FTS5 (substring
  fallback), mappings carry categorical confidence/provenance, and atomic
  replacement preserves the last good artifact on a failed build.
- `pstb/report.py` — nVision-style report runner: timespan resolver (YTD/BAL/
  PER/QTD/Qn/ROLL12/-1Y) plus a grid engine over report JSONs in reports/.
- `pstb/client/` — provider-agnostic agent loop plus `llm_ollama.py`,
  `llm_gemini.py` (google-genai with `vertexai=True`; the older
  `vertexai.generative_models` module was retired in June 2026) and
  `llm_claude.py` (the anthropic SDK; note it ignores `llm.temperature`,
  which Opus 5 rejects, and keeps a stricter transcript than the other two —
  every tool_use must be answered). `llm_base.py` owns the PROVIDERS tuple
  that every surface reads, so a new provider is reachable everywhere or
  nowhere.
- `scripts/seed_sample_data.py` — builds `PS_LEDGER` *from* generated balanced
  journals, so drill-downs tie to the penny. FY2025 closes into FY2026 period 0.

## Conventions

- Ledger amounts are signed: debits positive, credits negative.
- Period 0 = beginning balances; adjustment periods are a reporting basis, not
  a point on the monthly trend; ending(P) = sum of periods 0..P.
- Tool arguments are primitives only (str/int/float/bool) — this keeps the JSON
  schemas simple enough for small local models and for Gemini's validator.
  `fiscal_year=0` / `period=0` mean "current".
- The server must never print to stdout; stdio carries the MCP protocol.
  Diagnostics go to stderr.
- Never interpolate user-supplied values into SQL.
- An empty result is never a clean or balanced ledger — return a scope
  diagnosis with null verdicts instead.

## MCP SDK compatibility

The SDK renamed several fields between 1.x and 2.x (`inputSchema` →
`input_schema`, `isError` → `is_error`, `structuredContent` →
`structured_content`) and moved `FastMCP` to `mcp.server.mcpserver.MCPServer`.
Both are handled: `pstb/server.py` has a compat import and
`pstb/client/chat.py` reads fields through `_field()`.

`scripts/mcp_probe.py` builds the provider tool specs the same way the chat
client does. Keep it that way — an earlier version checked only tool names and
missed a break that made the client unusable for every question.

Dependencies are upper-bounded in `pyproject.toml` so a fresh install cannot
silently pick up the next breaking SDK major.

## Question log & multi-step chaining

Every chat turn is appended to `logs/questions.jsonl` with auto failure flags
(tool_error / no_tool_calls / max_rounds / gave_up) plus thumbs-down feedback
from the web UI. Review the failure backlog and deterministic, source-separated
summary with:

```bash
.venv/bin/python -m pstb.qlog logs/questions.jsonl
.venv/bin/python -m pstb.qlog_report logs/questions.jsonl
```

The Diagnostics view reads the same summary through `/api/question-report`.
Turns are grouped by their resolved canonical source (`default` for Finance,
`p2go` for P2Go), and repeated failures, tool error/latency counts, catalog
state and relationship-path evidence stay in that source's bucket. Use the
per-source summaries as the operational rates; the top-level total is only a
volume/backlog count and must not blend Finance and P2Go quality.

The local JSONL record deliberately keeps a narrow per-tool observability
envelope:

- canonical source, default schema and configured schema allowlist;
- Finance scope when present (business unit, ledger, fiscal year and period);
- tool name, success, elapsed milliseconds and whether its result source
  exactly matched the expected canonical source;
- declared completeness/truncation state and a categorized refusal reason;
- catalog fingerprint/snapshot/version/time plus complete/partial/stale state;
- per-allowed-schema TABLE/VIEW counts, missing owners and coverage status;
- relationship-path found/confidence/evidence class and target schema owners.

The envelope does **not** persist tool arguments or extract SQL text, binds,
result rows, object/column names, raw database errors, credentials, usernames,
passwords, access tokens or DSNs from tool payloads. The turn record also
stores no answer text, only its character count. The user's question (capped
at 8,000 characters) and an optional free-form feedback note (capped at 4,000)
are stored locally so a failure remains actionable. Credential assignments,
connection locators, bearer tokens, qualified object-name disclosures and
SQL-shaped text are redacted before persistence. Ordinary business identifiers
can still be sensitive, so neither the CLI summaries nor the Diagnostics
API/UI return that private text: they expose
the random local turn ID, source, structural flags and tools so an authorized
operator can locate the protected JSONL record when necessary. Active and
rotated files are owner-only (`0600`); the default rotation is 10 MiB with
three backups (`questions.jsonl.1` through `.3`). Treat the file and that
endpoint as sensitive: keep them on a restricted host/path and apply your
normal access and retention policy. With row security enabled, the web report
is privileged-operator-only. There is no OTLP, hosted collector or
other external telemetry exporter in this implementation; logging is the
configured local JSONL file only. A generic `truncated: false` does not become
a completeness claim: without an explicit evidence contract, its status
remains `unknown`.

Each auto-flagged or thumbed-down question is a candidate for a source-specific
eval, curated tool or record-map entry. `scripts/eval.py --from-qlog` joins
feedback to its turn and writes an owner-only, git-ignored review queue at
`logs/eval-pending.json`, preserving the source/scope needed to reproduce it.
It never edits tracked eval packs. A human must review and redact private
identifiers, state the expected behavior, and explicitly promote an approved
case into the Finance or P2Go pack; the command never decides the expected
answer automatically.

Multi-step chaining is deliberately NOT LangChain/LangGraph: the agent loop
already feeds tool results back for up to 10 rounds, and the reliable way to
reduce chain errors is to move routing and arithmetic INTO deterministic
tools. For unfamiliar structure, Gemini 2.5 Pro follows
`search_metadata` → `get_metadata_context` → a live profiling/query tool;
`get_exchange_rate` converts amounts server-side so the model never
multiplies. A framework on top of MCP would add a dependency without fixing
either failure mode.

### Metadata discovery contract

`scripts/build_metadata_catalog.py` reads catalog structure with SELECT/PRAGMA
only and publishes one atomic mode-`0600` artifact per configured database.
Single-source installs keep `metadata_catalog.db`; multi-source installs use
`metadata_catalogs/<safe-source>-<hash>.db`. Source selection chooses one whole
artifact, so same-named objects from different databases are never combined.
Never infer a `PS_` prefix or treat lexical relevance as mapping confidence.
The four mapping tiers are `confirmed`, `corroborated`, `candidate` and
`inconclusive`, each with an evidence basis.

The MCP sequence is intentionally split:

1. `describe_metadata_catalog` establishes version, freshness, coverage and
   limit hits.
2. `search_metadata` returns explainably ranked structural candidates.
3. `get_metadata_context` resolves an exact candidate and returns columns,
   ordered indexes, labels/codes and mapping provenance.
4. A live tool applies caller business-unit scope and date/status/currency
   basis. Metadata tools are structural and cannot satisfy financial evidence.

Partial and stale artifacts remain readable with disclosure. Tests and callers
must treat absence from a partial layer as inconclusive and must expect
ambiguity responses rather than sort-order selection. Schema version 2 records
native PK/FK constraints and view dependencies; relationship paths use only
that declared evidence and never infer a join from matching column names.
Composite keys collected incompletely remain inconclusive. The catalog does
not use embeddings or support quoted identifiers that differ only by case. See
[METADATA_CATALOG.md](METADATA_CATALOG.md) for exact build limits, grants and
source/dialect rules.

## Monitoring — from answering to noticing

```
.venv/bin/python scripts/monitor.py                 # run + diff + report
.venv/bin/python scripts/monitor.py --quiet         # silent unless something changed
.venv/bin/python scripts/monitor.py --history       # what has been recorded
```

Runs a playbook, compares it to the previous run, and reports what CHANGED —
new findings, findings that moved (suspense grew from 15,000 to 40,000),
findings that cleared, and checks that STOPPED RUNNING, which is the loudest
signal because the verdict is no longer comparable. No language model is
involved: the diff is deterministic, so it cannot drift or hallucinate and it
runs on a box with no LLM configured.

Exit codes are for schedulers: `0` nothing changed, `1` something changed,
`2` the run failed. A nightly cron that mails only on change:

```
30 6 * * * cd /opt/peoplesoft_tb_mcp && .venv/bin/python scripts/monitor.py --quiet | mail -E -s "PeopleSoft close readiness" finance-team@yourco.com
```

Snapshots and an append-only history live in `logs/monitor/`.

## Source-aware evals — pinning MODEL behavior

```bash
.venv/bin/python scripts/eval.py --suite finance
.venv/bin/python scripts/eval.py --suite p2go
.venv/bin/python scripts/eval.py --suite all --json eval-all.json
.venv/bin/python scripts/eval.py --suite finance --case ar-aging
.venv/bin/python scripts/eval.py --from-qlog  # seed pending cases from failures
```

The suites pin SQL and engine behavior; the source-specific packs pin what the
MODEL does — which tool it picks, whether it refuses, whether it reaches for
the wiki when it should query the ledger, and whether each successful result
comes from the selected database. The runner mirrors the GUI runtime profile:

- Finance receives `system_prompt`, the provider's production prompt variant,
  the full Finance tool profile, and the selected BU/ledger/time scope.
- P2Go receives `source_silo_prompt("p2go")` and only
  `SOURCE_SILO_TOOLS`; it cannot pass by borrowing a PeopleSoft/Coupa/wiki or
  curated Finance tool.

Assertions are structural (`any_tool`, `all_tools`, `allowed_tools`,
`failed_tools`, ordered successful calls, argument values, result fields,
exact result sets, allowed result values, answer inclusions/exclusions and
refusal state), never "does this read well". Every successful source-aware tool
result must name the exact selected canonical source, including `default` for
Finance. Multiple contradictory calls cannot be stitched together to satisfy
one result contract.
P2Go's named argument and result assertions are tied to a successful call of
the expected tool, so an errored attempt or an unrelated successful call cannot
supply its evidence. Its expected boundary refusals are separately tied to the
named failed call.

The P2Go pack discovers safe object examples from P2Go's own offline artifact.
It covers an unqualified `P2GO` object, an explicitly qualified `TUSINVC`
object, semantic search, ambiguity across allowed owners, an allowed
cross-schema relationship, a guarded explain/read, an outside-owner refusal
and Finance/P2Go isolation. Its always-runnable health gate fails an
unavailable, partial or stale catalog, including a snapshot in which either
P2Go owner is missing, an extra owner enters the boundary, the latest refresh
failed, or the refresh status names a different snapshot. If the deployment
has no real catalog example for another structural scenario, that case is
reported **N/A**, not passed using an invented object. Build the P2Go catalog
before treating its suite as complete.

`--suite all` prints and writes separate `finance` and `p2go` summaries. Its
exit code is nonzero if either runnable source pack fails; never replace those
two pass rates with one blended percentage. The optional `--json` file is an
owner-only (`0600`), atomic local developer artifact and includes answer text
and observed tool arguments; its result excerpts are bounded to
structural/source facts and do not include transaction rows. Standard
eval-output names are git-ignored. Protect it like test evidence and do not
confuse it with the restricted question-log telemetry envelope described
above.

Real failures are the best eval material: `--from-qlog` copies redacted flagged
turns into the ignored owner-only review queue for a human to grade and promote.

### Operational acceptance checklist

1. Configure P2Go with `schema: P2GO` and
   `schemas: [P2GO, TUSINVC]`; keep Finance as canonical source `default`.
2. Run `.venv/bin/python scripts/build_metadata_catalog.py`. Reject the build
   for deployment acceptance if it prints `MISSING SCHEMAS p2go`, or if
   `describe_metadata_catalog(source="p2go")` reports either owner with zero
   objects, `schema_coverage.complete: false`, `partial: true`, or `stale: true`.
3. Run the Finance and P2Go suites separately (or `--suite all`) with the
   production provider. Require every runnable case in **each** source to pass;
   review every P2Go N/A rather than counting it as success.
4. Confirm the P2Go cases use only the source-silo tool profile, every
   successful structural/query result names `source_database: p2go`, both
   allowed owners are represented, and an outside owner is refused before a
   live database call. Confirm Finance still uses its curated controls and
   never resolves through P2Go.
5. Ask one representative question in `/finance` and `/p2go`, then run
   `.venv/bin/python -m pstb.qlog_report logs/questions.jsonl`. Require separate
   `default` and `p2go` summaries with the expected scope/schema context,
   timings and catalog/relationship status.
6. Inspect a sample JSONL record before production rollout. It may contain the
   bounded user question and feedback note, but its tool records must contain
   no SQL, arguments, binds, rows, raw errors, credentials or connection
   locators. Confirm the active file and any `.1`-`.3` backups are mode `0600`.

## Testing

`scripts/smoke_test.py` runs 202 checks on the stdlib alone (no venv required)
and covers ledger math, effective dating, journal tie-out, integrity controls,
tree rollups, SQL guards, no-data scope handling, and adjustment-period basis.
Run it plus `scripts/mcp_probe.py` before any commit.
