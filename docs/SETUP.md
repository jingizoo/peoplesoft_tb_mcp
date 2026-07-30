# Setup Guide

Getting the PeopleSoft trial-balance agent running on a new machine, including
locked-down corporate laptops. Everything below works against the bundled
sample ledger, so you can prove the whole stack before any PeopleSoft
connection or credential exists.

## 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| **Python 3.10+** | the only hard requirement | `python --version` (try `python3` on macOS/Linux) |
| Ollama *or* a GCP project | to run the chat client | see step 4 |
| Oracle read-only account | only for the real database | see step 6 |

If `python --version` reports 3.9 or older, install a current Python from
[python.org](https://www.python.org/downloads/). On Windows, tick **"Add
python.exe to PATH"** in the installer.

## 2. Get the code

Download the repository ZIP from GitHub and extract it, or clone it. Then open
a terminal **in the extracted folder** — the one containing `pyproject.toml`.

```bash
cd path/to/peoplesoft_tb_mcp
```

Windows PowerShell: if the ZIP came from a browser download, unblock it first
so Windows does not quarantine the files:

```powershell
Get-ChildItem -Recurse | Unblock-File
```

## 3. Run the bootstrap

```bash
python scripts/bootstrap.py
```

Roughly two minutes. It creates `.venv/`, installs the package and both LLM
clients, builds the sample ledger, and then verifies the engine (153 checks) and
the MCP server over real stdio. It is safe to re-run.

Useful flags:

```bash
python scripts/bootstrap.py --no-llm      # skip the LLM clients
python scripts/bootstrap.py --oracle      # also install the Oracle driver
python scripts/bootstrap.py --sqlserver   # also install the SQL Server driver
```

Expected ending:

```
[5/5] Verifying
  engine checks passed
  MCP server and tool discovery passed

Setup complete.
```

If it fails, the failing step prints the last 15 lines of output. Common causes
are in [Troubleshooting](#8-troubleshooting).

### Corporate proxy / restricted PyPI

If `pip` cannot reach PyPI, set the proxy before bootstrapping:

```bash
# macOS/Linux
export HTTPS_PROXY=http://proxy.company.com:8080
# Windows PowerShell
$env:HTTPS_PROXY = "http://proxy.company.com:8080"
```

For an internal package mirror, add it once and bootstrap will pick it up:

```bash
python -m pip config set global.index-url https://artifactory.company.com/api/pypi/pypi/simple
```

Fully offline? `python scripts/smoke_test.py` still runs the entire trial-balance
engine using the standard library alone — no virtualenv, no downloads. Only the
MCP server and chat client need installed packages.

## 4. Pick an LLM provider

Both are supported and switchable per run.

### Option A — Ollama (local, nothing leaves the machine)

```bash
ollama pull llama3.1:8b
.venv/bin/python -m pstb.client.chat
```

Windows: `.venv\Scripts\python -m pstb.client.chat`

`llama3.1:8b` is ~4.9 GB. If Ollama is not on the machine, install it from
[ollama.com/download](https://ollama.com/download). If `ollama` reports it
cannot connect, start the server with `ollama serve` and retry.

### Option B — Gemini on Vertex AI

No code changes are needed — only credentials and one config value. Four steps,
in order:

**1. Install the gcloud CLI**

```bash
# macOS
brew install --cask google-cloud-sdk
# Windows / Linux: https://cloud.google.com/sdk/docs/install
```

**2. Authenticate with Application Default Credentials**

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login
```

The third command is the one that matters — the SDK reads ADC, not your
`gcloud` login. Skipping it produces `DefaultCredentialsError`.

**3. Enable the API and grant yourself access**

```bash
gcloud services enable aiplatform.googleapis.com --project YOUR_PROJECT_ID
```

Your account needs **`roles/aiplatform.user`** on the project. Without it the
first call fails with `PermissionDenied: 403`, which reads like a bad model
name — check IAM before changing the model.

**4. Point the agent at your project**

In `.env`:

```
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
```

Then run it — no other change required:

```bash
.venv/bin/python -m pstb.client.chat --provider gemini
```

To make Gemini the permanent default instead of passing `--provider` each time,
set `llm.provider: gemini` in `config.yaml`. You can also switch mid-session in
the REPL with `/provider gemini`, or override per run with the
`PSTB_LLM_PROVIDER` environment variable.

**Choosing a model.** `config.yaml` ships `gemini-2.5-pro`. Newer models may
be available in your project and region; list what you can actually call with:

```bash
gcloud ai models list --region=us-central1 --project=YOUR_PROJECT_ID
```

Override without editing config using `--model`:

```bash
.venv/bin/python -m pstb.client.chat --provider gemini --model gemini-2.5-pro
```

Model availability is region-specific — if a model 404s, try
`GOOGLE_CLOUD_LOCATION=us-central1`, which has the broadest coverage.

**Gemini 2.5 Pro tuning built in.** The config defaults to `gemini-2.5-pro`.
Tool results are sized up to 120k characters for Gemini (vs 24k for local
models), transient 429/503 errors retry with backoff, and
`llm.gemini_thinking_budget` caps thinking tokens if you want lower
latency/cost (-1 = model default).

**Expect better results than a local model.** In testing, `llama3.1:8b`
invented parameter names and misstated verdicts even with correct tool data.
Gemini's tool-calling is substantially stronger, which matters for a 25-tool
surface. The trade-off is in the data note below.

**Data note:** with Gemini, tool results — ledger amounts, operator IDs, and
journal descriptions — are sent to Google Cloud. Ollama keeps everything local.
Confirm this against your data-handling policy before pointing Gemini at
production financials.

## 5. Verify it works

```bash
.venv/bin/python -m pstb.client.chat --ask "Does the trial balance balance for period 6?"
```

Expected: the trial balance balances, with debits and credits both
6,419,357.27. Other questions worth trying:

- "Why did travel expense spike in April?"
- "Show me the trial balance by department for period 6."
- "Is the suspense balance within our policy?" (combines ledger and wiki)
- "What are total assets as of period 6?"

A ~50-question catalog is in [QUESTIONS.md](QUESTIONS.md).

## 5b. Use the web UI

```bash
.venv/bin/python -m pstb.gui --open
```

Windows: `.venv\Scripts\python -m pstb.gui --open`. Opens
http://127.0.0.1:8000 (add `--port 8777` to change it, `--host 0.0.0.0` to
expose it on the network — see the security note below).

Five views sharing one scope bar (business unit, ledger, fiscal year, period):

| View | What it gives you |
|---|---|
| Trial balance | Grid with pinned DR/CR totals, chartfield grouping, account filter, CSV export. Click any row for the account drawer. |
| Close & controls | One card per control: balance, suspense, unposted journals, out-of-balance journals, inactive/orphan accounts, retained-earnings roll. |
| Statement rollup | Assets / liabilities / equity / revenue / expenses by tree node, with net income and an A = L + E check. |
| Variance | Largest movers between two periods with change, % and a magnitude bar. |
| Reports | nVision-style statements (income statement, balance sheet, quarterly trend): tree/account rows × ledger + timespan columns, budget vs actuals. See [NVISION.md](NVISION.md). |
| Receivables | AR aging by customer with GL tie-out, customer drill-down, billing pipeline health (stuck invoices, interface errors, finalized-not-in-AR). |
| Ask | Chat that renders each tool result inline as a table, chart or control card. |

Every figure on screen is computed by the engine and rendered by the browser.
The model never produces a number that is displayed — in the Ask view its reply
is limited to a short comment and labelled as commentary. This is deliberate:
in testing, a small local model produced incorrect prose next to a correct
table.

**Security:** the UI has no login and no per-user authorization. Bind it to
`127.0.0.1` (the default) and treat it as a single-user tool until an
authenticated gateway exists — see `docs/REVIEW_RESPONSE.md`.

## 6. Point it at a real PeopleSoft database

1. Get a **read-only** Oracle account with SELECT on the GL tables.
2. `python scripts/bootstrap.py --oracle` (installs `python-oracledb`, thin
   mode — no Oracle client install needed).
3. In `.env`:
   ```
   ORACLE_DSN=host:1521/SERVICE_NAME
   ORACLE_USER=readonly_user
   ORACLE_PASSWORD=...
   ```
4. In `config.yaml`, switch the backend and set your real conventions:
   ```yaml
   db:
     backend: oracle
     schema: SYSADM
   defaults:
     business_unit: YOUR_BU
     ledger: ACTUALS
     setid: SHARE
     calendar_id: "01"
     adjustment_periods: [998]
     suspense_accounts: ["1999"]
     retained_earnings_account: "3500"
   ```
5. The scope bar and blank tool arguments are **validated against the
   database**: if the config defaults (business unit, ledger) don't exist in
   your instance, the agent discovers real ones and says so — but set your
   true defaults here anyway so the discovery note goes away.
6. Sanity-check before trusting anything:
   ```bash
   .venv/bin/python -m pstb.client.chat --ask "List the business units and ledgers."
   ```

Optionally have a DBA deploy the views in [`sql/oracle/`](../sql/oracle) and set
`db.use_views: true` — see [VIEWS.md](VIEWS.md). The agent works without them.

**Before anyone else uses it,** read the open items in
[REVIEW_RESPONSE.md](REVIEW_RESPONSE.md). Notably: there is no currency/amount
basis contract, no user authorization boundary, and raw SQL is enabled in the
shipped config (`tools.allow_raw_sql: false` turns it off).

## 7. Connect the company wiki (Confluence)

The wiki supplies policy context — suspense rules, capitalization thresholds,
close checklists — so the agent can answer "is this within policy", not just
"what is the balance".

### 7.1 Get credentials

**Confluence Cloud:** create an API token at
https://id.atlassian.com/manage-profile/security/api-tokens, then in `.env`:

```
CONFLUENCE_BASE_URL=https://yourco.atlassian.net/wiki
CONFLUENCE_EMAIL=you@yourco.com
CONFLUENCE_API_TOKEN=<token>
```

**Confluence Data Center / Server:** create a personal access token in your
profile settings, leave the email blank, and use the site root:

```
CONFLUENCE_BASE_URL=https://wiki.yourco.com
CONFLUENCE_EMAIL=
CONFLUENCE_API_TOKEN=<personal access token>
```

The token inherits *your* permissions. Use a service account limited to the
finance space if the agent will be shared.

### 7.2 Scope it — do this, don't skip it

Unscoped, a question like "what is our suspense policy" runs a full-text search
across everything you can read and trusts whatever ranks first. Two filters fix
that, in `config.yaml`:

```yaml
wiki:
  provider: confluence        # not "auto" — see 7.4
  confluence_space: FIN       # space key
  confluence_labels: "gl-policy,gl-close,gl-coa"
```

`confluence_space` restricts lookups to one space. `confluence_labels` further
restricts them to pages carrying a label, which is the part that makes answers
predictable.

### 7.3 What to do in Confluence itself

This is the manual step, and it is what turns search into mapping:

1. Pick the space that holds finance policy (or create one).
2. Label the pages the agent should be allowed to cite. A workable starter set:

   | Label | Apply to |
   |---|---|
   | `gl-policy` | suspense rules, capitalization threshold, adjustment-period rules, materiality |
   | `gl-close` | month-end checklist, close calendar, sign-off procedure |
   | `gl-coa` | chart-of-accounts policy, account ranges, naming conventions |

3. Make sure each page states its rule explicitly ("cleared within 30 days",
   "capitalization threshold is $5,000"). The agent quotes what is written; a
   page that only implies a threshold produces a vague answer.
4. Keep one page per topic. Two pages describing the same rule differently is
   the most common source of a wrong policy answer.

Start with three or four pages covering the questions people actually ask, then
widen.

### 7.4 Fail closed

With `provider: confluence`, the server reports the wiki as unavailable if
Confluence cannot be reached — it will **not** fall back to the sample pages in
`sample_wiki/`. That fallback exists only under `provider: auto`, for local
development. Never leave `auto` set against a production ledger: it can pair
real balances with the demo thresholds shipped in this repo.

Page fetches are re-checked against the configured space after retrieval, so a
page id outside the allowed space is rejected rather than returned.

### 7.5 Verify — run the diagnostic

```bash
.venv/bin/python scripts/diagnose_wiki.py
```

Staged output: which provider is **actually** active, credential presence
(never the token), connectivity and auth, whether the configured space and
labels exist and match pages, a real search, and a real page fetch with
provenance plus a content excerpt to compare against Confluence in a browser.
It exits non-zero and lists fixes when anything is off.

Test a specific question or page:

```bash
.venv/bin/python scripts/diagnose_wiki.py --query "capitalization threshold"
```

**The failure it is designed to catch:** with `provider: auto` and missing or
partial credentials, the agent silently serves the fictional sample pages in
`sample_wiki/` — the 30-day suspense rule and $5,000 threshold in them are
made up. The diagnostic reports `bundled demo content` and refuses to call the
setup trustworthy. In the app, the sidebar shows
`wiki: localdocs (demo pages — not your wiki)`, the `wiki_health` tool reports
it, and every `wiki_search` result carries a `demo_content_warning` so the
agent states the wiki is not connected instead of quoting fake policy.

Final confirmation once green: ask a policy question and check the cited page
title is one of yours. Each page carries `space`, `version`, `last_modified`,
`retrieved_at`, and a content hash for later audit.

### 7.6 Known gap

Concept-to-page binding is by label and search, not an explicit map. If you want
deterministic lookups for recurring questions — always read page 12345 for
"suspense policy" — that mapping file does not exist yet. Labels get you most of
the way; tell me your space and page ids if you want the explicit binding.

## 7b. When a query hangs

If the UI or chat sits on a tool call and never returns, time each database
step:

```bash
.venv/bin/python scripts/diagnose_db.py --bu YOUR_BU --ledger ACTUALS --fy 2024 --period 6
```

It reports elapsed time per statement and flags anything over 5 seconds, then
tells you how to read the result. Add `--sql` to print the statement itself.

Queries now abort rather than hang: `db.query_timeout_seconds` (default 120)
becomes an Oracle `call_timeout`, and the browser gives up after 180 s with an
explanation. Raise the config value for genuinely large scopes.

If the aggregate is slow on a real ledger, confirm the delivered PS_LEDGER index
on `(BUSINESS_UNIT, LEDGER, FISCAL_YEAR, ACCOUNTING_PERIOD, ACCOUNT)` exists and
that optimiser statistics are current, then narrow the scope (one fiscal year,
one period range, an account filter) before widening again.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `python: command not found` | try `python3`; on Windows reinstall Python with "Add to PATH" ticked |
| `No module named pstb` | run commands from the project root, and use `.venv` python, not the system one |
| `SQLite sample database not found` | `python scripts/seed_sample_data.py` |
| `could not connect to ollama server` | `ollama serve`, then retry — this usually is *not* a missing model |
| `model requires more system memory` | use a smaller model: `ollama pull llama3.2:3b` and `--model llama3.2:3b` |
| `Set GOOGLE_CLOUD_PROJECT in .env` | fill it in, then `gcloud auth application-default login` |
| `ORA-12541` / `ORA-12154` | DSN wrong or blocked; confirm `host:port/service_name` and firewall access |
| Tool call hangs, no result | run `scripts/diagnose_db.py` (section 7b); check the PS_LEDGER index and `db.query_timeout_seconds` |
| pip TLS/certificate errors | corporate TLS inspection — point pip at the internal mirror (step 3) |
| PowerShell blocks activation | you do not need to activate; call `.venv\Scripts\python` directly |

## 9. What is safe to commit

`.gitignore` already excludes `.venv/`, `.env`, the generated sample database,
and build artifacts. **`.env` holds credentials — never commit it.** Share
`.env.example` instead.
