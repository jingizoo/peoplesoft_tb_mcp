# PeopleSoft Trial Balance MCP Agent

An MCP server + chat client that answers trial-balance questions against
PeopleSoft Finance (GL), using **Ollama** (local) or **Gemini on Vertex AI** —
switchable per run. It ships with a realistic SQLite sample GL so everything
works before you connect a real database, and it enriches answers with company
documentation from **Confluence** (or a local docs folder).

```
┌──────────────────────┐   stdio (MCP)   ┌──────────────────────────┐
│  chat client (REPL)  │◄───────────────►│  MCP server (pstb.server) │
│  pstb.client.chat    │    30 tools     │  TB engine + guarded SQL  │
│                      │                 │  + wiki tools             │
│  LLM providers:      │                 └─────┬──────────────┬─────┘
│   • Ollama (local)   │                       │              │
│   • Gemini (Vertex)  │              Oracle / SQLite      Confluence /
└──────────────────────┘              (PS_LEDGER, PS_JRNL_*) local docs
```

The server speaks standard MCP over stdio — the bundled chat client is one
consumer, and any MCP-compatible host can connect to it instead (see below).

## Quickstart (no credentials, no database needed)

Requires **Python 3.10+** and nothing else. Works on Windows, macOS, and Linux:

```bash
python scripts/bootstrap.py
```

That creates a virtualenv, installs the package, builds the sample ledger, and
verifies both the engine (179 checks) and the MCP server end to end. Then install
a local model and start asking questions:

```bash
ollama pull llama3.1:8b
```

Then either the web UI:

```bash
.venv/bin/python -m pstb.gui --open
```

or the terminal chat:

```bash
.venv/bin/python -m pstb.client.chat
```

On Windows use `.venv\Scripts\python` in place of `.venv/bin/python`.

Full step-by-step instructions, including work-laptop and offline notes, are in
[docs/SETUP.md](docs/SETUP.md). On macOS/Linux there are also `make` shortcuts:
`make venv`, `make seed`, `make smoke`, `make probe`, `make chat`.

Try: *"Does the trial balance balance for period 6?"*, *"Why did travel spike
in April?"*, *"Is the suspense balance within policy?"* — the last one combines
a ledger number with the wiki's 30-day suspense rule. More in
[docs/QUESTIONS.md](docs/QUESTIONS.md) (a ~50-question catalog).

One-shot mode: `.venv/bin/python -m pstb.client.chat --ask "total assets as of period 6?"`

## LLM providers

| | Ollama | Gemini on Vertex AI |
|---|---|---|
| Setup | `ollama pull llama3.1:8b` (or qwen3 etc.) | GCP project + `gcloud auth application-default login` |
| Data | stays on your machine | tool results are sent to Google Cloud — check data governance before pointing at production |
| Select | `make chat` / `--provider ollama` | `make chat-gemini` / `--provider gemini` |

Gemini uses the **google-genai** SDK with `vertexai=True` (Google retired the
old `vertexai.generative_models` module in June 2026 — google-genai *is* the
current Vertex AI library). Set `GOOGLE_CLOUD_PROJECT` in `.env`; install the
gcloud CLI if you don't have it (`brew install google-cloud-sdk`), then:

```bash
gcloud auth application-default login
```

Models are set in `config.yaml` (`gemini-2.5-pro` default, with thinking-budget
and retry tuning built in — `gemini-2.5-flash` is the cheaper/faster option; `llama3.1:8b` default for Ollama, with
`qwen3` / larger llama models giving better tool use).

## Connecting your real PeopleSoft database (Oracle)

1. Get a **read-only** Oracle account with SELECT on the GL tables (or the
   `XX_TB_*` views), e.g. from your DBA.
2. `pip install -e ".[oracle]"` inside the venv.
3. In `.env`: `ORACLE_DSN=host:1521/SERVICE`, `ORACLE_USER`, `ORACLE_PASSWORD`.
4. In `config.yaml`: `db.backend: oracle`, `db.schema: SYSADM`, and your real
   `defaults:` (business unit, ledger, setid, calendar, adjustment periods,
   suspense + retained-earnings accounts).
5. Optional but recommended: have a DBA run [sql/oracle/](sql/oracle/)
   `01..06` and set `db.use_views: true`. Rationale, semantics, and deployment
   notes (App Designer vs direct DDL, materialized-view option) are in
   [docs/VIEWS.md](docs/VIEWS.md).

The agent works without the views (its inline SQL encodes the same effective
dating / setid / tree logic), so you can pilot first and deploy views later.

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

On Windows the command is `C:\path\to\.venv\Scripts\python.exe`. Same 30 tools,
no chat client needed.

## Tools exposed by the server

`get_trial_balance` · `get_account_balance` · `compare_trial_balance` ·
`drill_to_journals` · `search_accounts` · `resolve_period` · `list_periods` ·
`tb_integrity_check` · `rollup_trial_balance` · `list_trees` ·
`list_financial_scopes` · `list_business_units` · `list_ledgers` ·
`list_reports` · `run_report` · `resolve_timespan` (nVision-style statements —
see [docs/NVISION.md](docs/NVISION.md)) · `get_ar_aging` · `get_customer_ar` ·
`search_customers` · `get_billing_workbench` (AR aging with GL tie-out and
billing pipeline — see [docs/BILLING_AR.md](docs/BILLING_AR.md)) ·
`get_top_billing_customers` · `get_exchange_rate` (effective-dated
PS_RT_RATE_TBL, server-side conversion, base-currency triangulation) ·
`get_record_map` (semantic record dictionary with live row counts) ·
`wiki_lookup` (searches, fetches and returns the actual passages — see
[docs/RAG.md](docs/RAG.md)) · `wiki_search` · `wiki_get_page` · `wiki_health` ·
and (config-gated) `run_sql` / `list_tables` / `describe_table` — the SQL tool
is SELECT-only, single-statement, row-capped; pair it with a read-only DB user.

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
- **Semantic layer:** `list_tables`/`describe_table` already let the model
  explore; next step is a curated record dictionary (PSRECDEFN-driven) so
  free-form questions ground in the right records.
- **Wiki-augmented answers (shipped):** `wiki_lookup` returns ranked passages
  with page/section provenance so policy answers quote real text and combine
  with ledger figures. Whether to add embeddings — and the evidence that would
  justify it — is worked through in [docs/RAG.md](docs/RAG.md).

## Layout

```
pstb/            server.py (MCP) · engine.py (TB math) · queries.py · db.py ·
                 wiki.py · client/ (chat REPL + providers) · gui/ (web UI)
scripts/         seed_sample_data.py · smoke_test.py · mcp_probe.py
sql/oracle/      XX_TB_* view DDL for the real database
docs/            SETUP.md (install) · QUESTIONS.md (catalog) · VIEWS.md ·
                 REVIEW_RESPONSE.md (open items) · DEVELOPMENT.md
sample_wiki/     sample policy pages served by the local wiki provider
```
