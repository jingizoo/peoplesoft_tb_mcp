# PeopleSoft Trial Balance MCP Agent

An MCP server + chat client that answers trial-balance questions against
PeopleSoft Finance (GL), using **Ollama** (local) or **Gemini on Vertex AI** —
switchable per run. It ships with a realistic SQLite sample GL so everything
works before you connect a real database, and it enriches answers with company
documentation from **Confluence** (or a local docs folder).

```
┌──────────────────────┐   stdio (MCP)   ┌──────────────────────────┐
│  chat client (REPL)  │◄───────────────►│  MCP server (pstb.server) │
│  pstb.client.chat    │    17 tools     │  TB engine + guarded SQL  │
│                      │                 │  + wiki tools             │
│  LLM providers:      │                 └─────┬──────────────┬─────┘
│   • Ollama (local)   │                       │              │
│   • Gemini (Vertex)  │              Oracle / SQLite      Confluence /
└──────────────────────┘              (PS_LEDGER, PS_JRNL_*) local docs
```

The server is a standard MCP server — the bundled client is one consumer, but
you can plug it straight into Claude Code / Claude Desktop too (see below).

## Quickstart (5 minutes, no credentials needed)

```bash
make seed        # build sample_data/ps_sample.db (2 fiscal years, closed FY2025)
make smoke       # engine self-tests — needs only python3, no packages
make venv        # install the package + ollama/google-genai clients
make probe       # spawn the MCP server and call tools end-to-end (no LLM)
ollama pull llama3.1:8b
make chat        # talk to your trial balance
```

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

Models are set in `config.yaml` (`gemini-2.5-flash` default — bump to a newer
Gemini as available in your region; `llama3.1:8b` default for Ollama, with
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
Optionally scope with `wiki.confluence_space: FIN`.

## Using the server from Claude Code / Claude Desktop

```bash
claude mcp add peoplesoft-tb -- /path/to/.venv/bin/python -m pstb.server
```

(or the equivalent `mcpServers` entry in Claude Desktop's config). Same tools,
no chat client needed.

## Tools exposed by the server

`get_trial_balance` · `get_account_balance` · `compare_trial_balance` ·
`drill_to_journals` · `search_accounts` · `resolve_period` · `list_periods` ·
`tb_integrity_check` · `rollup_trial_balance` · `list_trees` ·
`list_business_units` · `list_ledgers` · `wiki_search` · `wiki_get_page` ·
and (config-gated) `run_sql` / `list_tables` / `describe_table` — the SQL tool
is SELECT-only, single-statement, row-capped; pair it with a read-only DB user.

Key semantics baked in: signed amounts (credits negative), period 0 beginning
balances, adjustment period 998, ending(P) = Σ periods 0..P, effective-dated
chartfields, tree rollups, journal drill-down with ledger tie-out.

## Roadmap: from TB to all of PeopleSoft Finance

- **Tool packs per module:** AP open vouchers/aging, AR customer aging,
  Asset Management roll-forward tie-outs, budget vs actuals (KK/LEDGER_BUDG),
  allocations, intercompany eliminations (see the end of docs/QUESTIONS.md).
- **Semantic layer:** `list_tables`/`describe_table` already let the model
  explore; next step is a curated record dictionary (PSRECDEFN-driven) so
  free-form questions ground in the right records.
- **Wiki-augmented answers:** RAG over Confluence spaces for close calendars,
  policy thresholds, and runbooks.

## Layout

```
pstb/            server.py (FastMCP) · engine.py (TB math) · queries.py ·
                 db.py · wiki.py · client/ (chat REPL + ollama/gemini providers)
scripts/         seed_sample_data.py · smoke_test.py · mcp_probe.py
sql/oracle/      XX_TB_* view DDL for the real database
docs/            QUESTIONS.md (question catalog) · VIEWS.md (view design)
sample_wiki/     sample policy pages served by the local wiki provider
```
