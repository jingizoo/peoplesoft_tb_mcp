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
clients, builds the sample ledger, and then verifies the engine (39 checks) and
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

**Choosing a model.** `config.yaml` ships `gemini-2.5-flash`. Newer models may
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

**Expect better results than a local model.** In testing, `llama3.1:8b`
invented parameter names and misstated verdicts even with correct tool data.
Gemini's tool-calling is substantially stronger, which matters for a 17-tool
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
5. Sanity-check before trusting anything:
   ```bash
   .venv/bin/python -m pstb.client.chat --ask "List the business units and ledgers."
   ```

Optionally have a DBA deploy the views in [`sql/oracle/`](../sql/oracle) and set
`db.use_views: true` — see [VIEWS.md](VIEWS.md). The agent works without them.

**Before anyone else uses it,** read the open items in
[REVIEW_RESPONSE.md](REVIEW_RESPONSE.md). Notably: there is no currency/amount
basis contract, no user authorization boundary, and raw SQL is enabled in the
shipped config (`tools.allow_raw_sql: false` turns it off).

## 7. Connect the wiki (optional)

In `.env`:

```
CONFLUENCE_BASE_URL=https://yourco.atlassian.net/wiki
CONFLUENCE_EMAIL=you@yourco.com
CONFLUENCE_API_TOKEN=...
```

Confluence Data Center instead of Cloud: leave `CONFLUENCE_EMAIL` empty and use
a personal access token. Until these are set, the agent serves the sample
policy pages in `sample_wiki/`, so policy questions still work.

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
| pip TLS/certificate errors | corporate TLS inspection — point pip at the internal mirror (step 3) |
| PowerShell blocks activation | you do not need to activate; call `.venv\Scripts\python` directly |

## 9. What is safe to commit

`.gitignore` already excludes `.venv/`, `.env`, the generated sample database,
and build artifacts. **`.env` holds credentials — never commit it.** Share
`.env.example` instead.
