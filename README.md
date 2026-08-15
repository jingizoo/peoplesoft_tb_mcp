# PeopleSoft Trial Balance MCP Agent

An MCP server + chat client that answers trial-balance questions against
PeopleSoft Finance (GL), using **Ollama** (local), **Gemini on Vertex AI**, or
**Claude on the Anthropic API** — switchable per run. It ships with a realistic
SQLite sample GL so everything works before you connect a real database, and it
enriches answers with company documentation from **Confluence** (or a local docs
folder).

```
┌──────────────────────┐   stdio (MCP)   ┌──────────────────────────┐
│ dynamic chat / REPL  │◄───────────────►│  MCP server (pstb.server) │
│ GUI + client agent   │    MCP tools    │  TB engine + guarded SQL  │
│                      │                 │  + wiki tools             │
│  LLM providers:      │                 └─────┬──────────────┬─────┘
│   • Ollama (local)   │                       │              │
│   • Gemini (Vertex)  │              Oracle / SQLite      Confluence /
│   • Claude (API)     │              (PS_LEDGER, PS_JRNL_*) local docs
└──────────────────────┘
```

The server speaks standard MCP over stdio — the bundled chat client is one
consumer, and any MCP-compatible host can connect to it instead (see below).

## Quickstart (no credentials, no database needed)

Requires **Python 3.10+** and nothing else. Works on Windows, macOS, and Linux:

```bash
python scripts/setup.py
```

That interactive wizard is the recommended path for a real deployment: it
checks the Python version, builds the venv, verifies the sample, collects and
**tests** your Oracle credentials, discovers business units / ledgers /
calendar / SetID from the database itself, reports any missing SELECT grants
as a ready-to-send GRANT statement, writes `.env` (mode 600) and
`config.yaml`, and finishes with the pre-flight gate. It is safe to re-run —
existing values become the defaults.

For an unattended sample-only install:

```bash
python scripts/bootstrap.py
```

That creates a virtualenv, installs the package, builds the sample ledger, and
verifies both the engine (202 checks) and the MCP server end to end. Then install
a local model and start asking questions:

```bash
ollama pull llama3.1:8b
```

Then either the web UI:

```bash
.venv/bin/python -m pstb.gui --open
```

The web product is one **Ask PeopleSoft Finance** conversation, not a set of
fixed dashboards. It discovers authorized BU/ledger combinations from
`PS_LEDGER`, asks for scope when the database is ambiguous, and renders the
appropriate trial balance, reconciliation, statement, AR, billing-customer,
or wiki-evidence card inline. Each browser tab and selected scope has isolated
model history.

Conversation history is currently in-process. Run one GUI worker for the pilot,
or use sticky routing if a gateway starts multiple replicas; an external
session store is a production-scale follow-up.

or the terminal chat:

```bash
.venv/bin/python -m pstb.client.chat
```

On Windows use `.venv\Scripts\python` in place of `.venv/bin/python`.

### Which build am I running?

Every page shows it in the context bar (`v0.1.0 · 5e61755`), hover for detail.
From the shell:

```bash
python -m pstb.version
```

`scripts/diagnose_db.py` prints the same line first. A ZIP deployment has no
git metadata, so the fingerprint — a hash of the source on disk — is the
identity that always works: two deployments showing the same fingerprint are
running the same code. `+local` means the working tree differs from the
commit.

Full step-by-step instructions, including work-laptop and offline notes, are in
[docs/SETUP.md](docs/SETUP.md). On macOS/Linux there are also `make` shortcuts:
`make venv`, `make seed`, `make smoke`, `make probe`, `make chat`.

Try: *"Does the trial balance balance for period 6?"*, *"Why did travel spike
in April?"*, *"Is the suspense balance within policy?"* — the last one combines
a ledger number with the wiki's 30-day suspense rule. For combined questions,
the client enforces PeopleSoft evidence first and retrieves wiki policy only
after the database result succeeds. More in
[docs/QUESTIONS.md](docs/QUESTIONS.md) (a ~50-question catalog).

One-shot mode: `.venv/bin/python -m pstb.client.chat --ask "total assets as of period 6?"`

## Monitoring

Playbooks can run on a schedule and report only what changed:

```bash
.venv/bin/python scripts/monitor.py --quiet
```

Exit code 1 means something moved since the last run — new findings, cleared
findings, or a check that stopped running. See docs/DEVELOPMENT.md for a cron
example.

## LLM providers

| | Ollama | Gemini on Vertex AI | Claude (Anthropic API) |
|---|---|---|---|
| Setup | `ollama pull llama3.1:8b` (or qwen3 etc.) | GCP project + `gcloud auth application-default login` | `ANTHROPIC_API_KEY` in `.env`, or `ant auth login` |
| Data | stays on your machine | tool results are sent to Google Cloud — check data governance before pointing at production | tool results are sent to Anthropic — the same check applies, and it is a separate vendor decision |
| Select | `make chat` / `--provider ollama` | `make chat-gemini` / `--provider gemini` | `make chat-claude` / `--provider claude` |

Gemini uses the **google-genai** SDK with `vertexai=True` (Google retired the
old `vertexai.generative_models` module in June 2026 — google-genai *is* the
current Vertex AI library). Set `GOOGLE_CLOUD_PROJECT` in `.env`; install the
gcloud CLI if you don't have it (`brew install google-cloud-sdk`), then:

```bash
gcloud auth application-default login
```

Claude uses the official **anthropic** SDK. The key is never a config value:
put `ANTHROPIC_API_KEY` in `.env`, set it from the configuration console at
`/console`, or sign in with `ant auth login --no-launch-browser` and let the
SDK read the profile. Two things differ from the other providers and are
deliberate — `llm.temperature` is ignored (Opus 5 rejects it, so routing is
tightened by forcing a tool call instead), and depth is set by
`llm.claude_effort` (`low`…`max`).

Models are set in `config.yaml` (`gemini-2.5-pro` default, with thinking-budget
and retry tuning built in — `gemini-2.5-flash` is the cheaper/faster option;
`claude-opus-5` default for Claude; `llama3.1:8b` default for Ollama, with
`qwen3` / larger llama models giving better tool use).

Whichever you pick, measure it rather than assuming:

```bash
.venv/bin/python scripts/eval.py --provider claude
```

## Restricting data by business unit

Off by default. Switched on, each person signs in with their user ID and
sees only the business units PeopleSoft grants that ID — the chooser, the
answers and the catalog the model reads are all narrowed, and named
privileged users see everything.

It is a **scope selector, not a login**: the sign-in takes a user ID and no
password, so it applies PeopleSoft's row rules to an honest session and
stops nobody who types someone else's ID. SSO replaces one function when
it arrives. Check what your instance supports before switching it on:

```bash
.venv/bin/python scripts/diagnose_bu_security.py --user SOMEUSER
```

Full walkthrough in [docs/SETUP.md](docs/SETUP.md#4b-restrict-data-by-business-unit-optional).

## Connecting your real PeopleSoft database (Oracle)

1. Get a **read-only** Oracle account with SELECT on the GL tables (or the
   `XX_TB_*` views), e.g. from your DBA.
2. `pip install -e ".[oracle]"` inside the venv.
3. In `.env`: `ORACLE_DSN=host:1521/SERVICE`, `ORACLE_USER`, `ORACLE_PASSWORD`.
4. In `config.yaml` (`cp config.example.yaml config.yaml` if you have not run
   the wizard): `db.backend: oracle`, `db.schema: SYSADM`, and your real
   `defaults:` (business unit, ledger, setid, calendar, adjustment periods,
   suspense + retained-earnings accounts). This file is git-ignored, so an
   upgrade never overwrites it — see
   [docs/SETUP.md](docs/SETUP.md#9-configuration-files-and-upgrading-without-losing-them).
5. Optional but recommended: have a DBA run [sql/oracle/](sql/oracle/)
   `01..06` and set `db.use_views: true`. Rationale, semantics, and deployment
   notes (App Designer vs direct DDL, materialized-view option) are in
   [docs/VIEWS.md](docs/VIEWS.md).

The agent works without the views (its inline SQL encodes the same effective
dating / setid / tree logic), so you can pilot first and deploy views later.

## Offline metadata intelligence

Build the structural catalog after connecting the intended databases:

```bash
.venv/bin/python scripts/build_metadata_catalog.py
```

It indexes names, columns, ordered indexes, PeopleTools logical records,
labels, translate values, page use and public saved-query use across `default`
and every configured `sources:` database. It keeps source/schema identities
separate and resolves custom physical names from evidence — it never assumes
`PS_` or another company prefix.

For Gemini 2.5 Pro, unfamiliar-record discovery follows
`search_metadata` → `get_metadata_context` → a live scoped tool such as
`profile_record`, `compare_records`, a curated financial tool or guarded
`run_sql`. The catalog is structure only: it contains no source rows or
balances and cannot satisfy the financial-evidence gate. See
[docs/METADATA_CATALOG.md](docs/METADATA_CATALOG.md) for source/schema scope,
read-only grants, confidence tiers, exact limits, refresh cadence and partial
or stale behavior.

## Wiki context (Confluence)

Set in `.env`:

```
CONFLUENCE_BASE_URL=https://yourco.atlassian.net/wiki   # DC: https://wiki.yourco.com
CONFLUENCE_EMAIL=you@yourco.com                          # Cloud only; leave empty for DC PAT
CONFLUENCE_API_TOKEN=...
```

`wiki.provider: auto` uses Confluence once those are set; until then it serves
the sample pages in [sample_wiki/](sample_wiki/) so policy questions still work.

For production set `wiki.provider: confluence` explicitly — it then **fails
closed** rather than falling back to this repo's demo policy pages, which would
otherwise pair real balances with fictional thresholds. Verify with
`python scripts/diagnose_wiki.py`, which reports the *actually active* provider
and refuses to call demo content trustworthy. Scope lookups with
`wiki.confluence_space: FIN` and `wiki.confluence_labels: "gl-policy,gl-close"`,
and label the pages in Confluence accordingly. Full procedure, including the
manual labelling step, is in [docs/SETUP.md](docs/SETUP.md) section 7.

## Using the server from another MCP host

The server is a plain stdio MCP server, so any MCP-compatible host can launch
it directly instead of using the bundled chat client. Most hosts take a JSON
entry like this:

```json
{
  "mcpServers": {
    "peoplesoft-tb": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "pstb.server"],
      "env": { "PSTB_CONFIG": "/absolute/path/to/config.yaml" }
    }
  }
}
```

On Windows the command is `C:\path\to\.venv\Scripts\python.exe`. The same tools,
no chat client needed.

## Tools exposed by the server

`get_trial_balance` · `get_account_balance` · `compare_trial_balance` ·
`drill_to_journals` · `search_accounts` · `resolve_period` · `list_periods` ·
`tb_integrity_check` · `detect_transaction_anomalies` (metadata-discovered
table relationships, daily 3/6-month volume trends, and process performance —
see [docs/ANOMALY_DETECTION.md](docs/ANOMALY_DETECTION.md)) ·
`trace_process` / `describe_process_graph` (offline PeopleTools relationship
graph with configurable 100k-scale build limits and explicit partial coverage;
see [docs/SETUP.md](docs/SETUP.md#7a-build-the-process-graph-optional-recommended)) ·
`rollup_trial_balance` · `list_trees` ·
`list_financial_scopes` · `list_business_units` · `list_ledgers` ·
`list_reports` · `run_report` · `resolve_timespan` (nVision-style statements —
see [docs/NVISION.md](docs/NVISION.md)) · `get_ar_aging` · `get_customer_ar` ·
`search_customers` · `get_billing_workbench` (AR aging with GL tie-out and
billing pipeline — see [docs/BILLING_AR.md](docs/BILLING_AR.md)) ·
`get_top_billing_customers` · `get_exchange_rate` (effective-dated
PS_RT_RATE_TBL, server-side conversion, base-currency triangulation) ·
`get_record_map` (semantic record dictionary with live row counts) ·
`describe_metadata_catalog` / `search_metadata` / `get_metadata_context`
(offline, versioned structural discovery across configured databases and
PeopleTools metadata, with explainable confidence — see
[docs/METADATA_CATALOG.md](docs/METADATA_CATALOG.md)) ·
`search_records` / `describe_record` (find ANY record — including custom and
site-specific ones — by searching PeopleTools record descriptions and field
names, then list its fields) ·
`profile_record` / `compare_records` (choose between candidate records on
EVIDENCE — which columns this site actually populates, the real codes in its
status columns, and masked sample rows — see
[docs/RECORD_SELECTION.md](docs/RECORD_SELECTION.md)) ·
`wiki_lookup` (searches, fetches and returns the actual passages — see
[docs/RAG.md](docs/RAG.md)) · `wiki_search` · `wiki_get_page` · `wiki_health` ·
and (config-gated) `run_sql` / `list_tables` / `describe_table` — the SQL tool
is SELECT-only, single-statement, row-capped; pair it with a read-only DB user.
`list_financial_scopes` returns a fast BU/ledger inventory by default; request
activity detail only when fiscal-year ranges or latest periods are needed.

Key semantics baked in: signed amounts (credits negative), period 0 beginning
balances, adjustment period 998, ending(P) = Σ periods 0..P, effective-dated
chartfields, tree rollups, journal drill-down with ledger tie-out.

## Roadmap: from TB to all of PeopleSoft Finance

- **nVision replacement:** report definitions in [reports/](reports/) replicate
  layouts (tree/ledger/timespan) — see [docs/NVISION.md](docs/NVISION.md) for
  the migration guide. Sample income statement, balance sheet, and quarterly
  trend run against the bundled ACTUALS + BUDGET ledgers.
- **Billing & AR (shipped):** aging with GL tie-out, customer 360, billing
  pipeline health — [docs/BILLING_AR.md](docs/BILLING_AR.md). Next tier:
  delivery status, credit/rebill graph, customer hierarchies, payments.
- **Tool packs per module:** AP open vouchers/aging, Asset Management
  roll-forward tie-outs, commitment control, allocations, intercompany
  eliminations (see the end of docs/QUESTIONS.md).
- **Metadata intelligence (first slice shipped):** the versioned offline
  catalog grounds delivered and custom terminology in physical objects across
  configured databases, with PeopleTools labels/codes and explainable mapping
  confidence. PK/FK/dependency lineage and optional semantic reranking remain
  future layers — see [docs/METADATA_CATALOG.md](docs/METADATA_CATALOG.md).
- **Wiki-augmented answers (shipped):** `wiki_lookup` returns ranked passages
  with page/section provenance so policy answers quote real text and combine
  with ledger figures. Whether to add embeddings — and the evidence that would
  justify it — is worked through in [docs/RAG.md](docs/RAG.md).

## Layout

```
pstb/            server.py (MCP) · engine.py (TB math) · queries.py · db.py ·
                 metadata.py · wiki.py · client/ (chat REPL + providers) ·
                 gui/ (web UI)
scripts/         seed_sample_data.py · build_metadata_catalog.py ·
                 smoke_test.py · mcp_probe.py
sql/oracle/      XX_TB_* view DDL for the real database
docs/            SETUP.md (install) · QUESTIONS.md (catalog) · VIEWS.md ·
                 METADATA_CATALOG.md · RECORD_SELECTION.md · DEVELOPMENT.md
sample_wiki/     sample policy pages served by the local wiki provider
```
