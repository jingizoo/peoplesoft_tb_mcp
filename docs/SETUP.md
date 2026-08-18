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
clients, builds the sample ledger, and then verifies the engine (202 checks) and
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

All three are supported and switchable per run.

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

The same applies to Claude below, with Anthropic in place of Google Cloud.

`profile_record` and `compare_records` add row-level samples to that path
whichever cloud provider is selected.
Personal columns (names, contacts, addresses, tax and bank identifiers) are
masked to a shape-preserving placeholder before they leave the database, and
identifiers, codes, dates and amounts are not — see
[RECORD_SELECTION.md](RECORD_SELECTION.md). Set `tools.sample_rows: 0` in
`config.yaml` to send no rows at all; record selection still works from shape,
fill rates and status-code counts.

### Option C — Claude on the Anthropic API

Two things and no CLI required: the package (already installed by
`pip install -e ".[llm]"`) and a credential.

**1. Give it a credential — one of two ways**

Put the key in `.env`:

```
ANTHROPIC_API_KEY=...
```

`.env` is mode 600 and is never committed. If you would rather not edit a
file on the box, the configuration console does it for you: open `/console`
over the tunnel, enter the confirmation code, and set **Anthropic API key**.
Nothing echoes the value back, there or in the logs.

Or sign in as yourself, if the Anthropic CLI is installed. Over SSH the
browser cannot open, so the flag is required:

```bash
ant auth login --no-launch-browser
```

That writes a profile under `~/.config/anthropic/` which the SDK reads on its
own — no environment variable at all.

**2. Run it**

```bash
.venv/bin/python -m pstb.client.chat --provider claude
```

To make it the permanent default, set `llm.provider: claude` in `config.yaml`
(or from the console). `/provider claude` switches mid-session in the REPL,
and `PSTB_LLM_PROVIDER` overrides per run.

**What is tuned, and what is deliberately ignored.** `llm.temperature` does
not apply — Claude Opus 5 rejects it outright — so tool selection is made
firm by *forcing* a call on the first turn of a question that needs one,
which is the same discipline Gemini gets from mode ANY. Depth is
`llm.claude_effort` (`low`, `medium`, `high`, `xhigh`, `max`; `high` is the
default and a starting point, not a measured one). `llm.claude_max_tokens`
covers thinking **and** the answer together, because thinking is on by
default on this model family. Tool results are sized to 120k characters as
they are for Gemini, the system prompt and tool list are cached so repeat
turns bill a fraction of the input, and a request the safety classifiers
decline is retried once on Anthropic's recommended substitute model rather
than coming back empty.

If the credential is missing the provider refuses to start and prints the
three ways to fix it — it does not fail on the first question.

**Choosing a model.** `config.yaml` ships `claude-opus-5`. Override without
editing it:

```bash
.venv/bin/python -m pstb.client.chat --provider claude --model claude-sonnet-5
```

**Data note:** with Claude, tool results — ledger amounts, operator IDs, and
journal descriptions — are sent to Anthropic. This is a separate vendor
decision from the Gemini one above, not covered by it; confirm it against
your data-handling policy before pointing Claude at production financials.
Ollama keeps everything local.

**Which one is better here?** Measure rather than assume — the eval harness
replays the same graded questions through whichever provider you name:

```bash
.venv/bin/python scripts/eval.py --provider claude
.venv/bin/python scripts/eval.py --provider gemini
```

## 4b. Restrict data by business unit (optional)

Off by default. Switched on, each person signs in with their user ID and
sees only the business units the finance system grants that ID — everyone
else's units disappear from the chooser, from answers, and from the
catalog the model reasons over. One or more IDs can be named privileged
and see everything.

**Read this first.** The sign-in asks for a user ID and no password, so it
identifies nobody: anyone who can reach the page can type any ID,
including a privileged one. What it gives you is PeopleSoft's own row
rules applied to the session — an honest user cannot stray into another
unit and neither can the model. It is a scope selector, not an access
control, and the page says so where people can read it. SSO replaces
`resolve_operator()` in `pstb/gui/app.py`; nothing that enforces anything
has to move when it does.

**1. See what your instance actually has**

```bash
.venv/bin/python scripts/diagnose_bu_security.py --user SOMEUSER
```

It reports which security records the app's account can read, which model
this site uses — by user ID (`PS_SEC_BU_OPR`) or by row-security
permission list (`PS_SEC_BU_CLS` through `PSOPRDEFN.ROWSECCLASS`) — how
many rows are in it, and what one real user resolves to. Exit code 1 when
security is on and unreadable, so it can gate a deployment.

Do this before switching anything on. A read-only reporting account
frequently has SELECT on the ledger and not on the security records, and
that is the single most common reason this feature shows nobody anything.

**2. Switch it on**

```yaml
security:
  enabled: true
  privileged_users: ["FINADMIN"]     # keep at least one
  on_unavailable: refuse
```

Or from `/console`, which has the same three switches.

- `privileged_users` is read from config, not from the database, on
  purpose: it is the account that still works while a grant is being
  fixed.
- `on_unavailable: refuse` means security that is on but unreadable shows
  **nothing** and names the missing grant. `allow` degrades to full access
  instead — a decision that should be typed rather than reached.
- `unit_record` / `unit_key` point at a custom record if this site keeps
  unit security somewhere other than the stock ones.
- Ad-hoc SQL (`run_sql`) is off for restricted users, because a query
  writes its own `WHERE` clause and no argument check can bound it to a
  unit. `raw_sql_for_restricted: true` overrides that.

**What is enforced, and where**

| | |
|---|---|
| The chooser and the catalog | filtered where they are built, so a foreign unit never becomes the discovered default |
| Every `/api/*` data route | one gate in front of all of them — a named unit must be granted, and an **omitted** one resolves inside the user's reach rather than falling through to the site default |
| Every tool the model calls | checked before the call leaves the client, with a refusal the model can act on |
| The scope catalog the model reads | narrowed on the way back, and told that it was |
| CSV export | checked against the body, where the query-string gate cannot see it |

The bundled sample ships users to try this with: `FIN_US001` (one unit),
`FIN_CA001` (a unit with no ledger data — the honest empty case),
`AP_CLERK` (class-based), `AUDITOR` (two units), `NOACCESS` (none).

## 4c. Join paths (`join_path`)

Ad-hoc SQL has always had the record names, the columns and the
optimizer's opinion of a *finished* query — but nothing that says how to
get from one record to another. So the model guessed, and a guessed join
against a transaction table does not come back wrong, it comes back in
four minutes.

`join_path("PS_ITEM", "PS_CUSTOMER")` answers that from **this instance's
own catalog**: shared key columns, weighted by whether those columns lead
a real index on each side.

```
PS_ITEM -> PS_CUSTOMER on CUST_ID
  indexed once you also pin BUSINESS_UNIT, SETID as a constant
  confidence: high   cost: 2.0

FROM PS_ITEM T0
  JOIN PS_CUSTOMER T1 ON T0.CUST_ID = T1.CUST_ID
```

The second line is the useful half. PeopleSoft leads its indexes with
SETID and BUSINESS_UNIT — values the selected scope has already fixed — so
a join that looks unindexed is usually one constant away from a range
scan. Nothing else in the app could tell you that.

What it deliberately does **not** do:

- It does not run anything, and it returns a FROM/JOIN skeleton rather
  than a whole SELECT. What to select and how to filter is the question's
  business; a half-guessed SELECT list is how a plausible wrong answer
  starts.
- It does not claim the join is *correct*. Shared column names are strong
  evidence and are not a declared foreign key, so every payload carries
  that caveat and points at `explain_query` for the finished statement.
- It refuses to invent a bridge. Two records with nothing in common come
  back `found: false` with what the source *can* reach, instead of a
  creative path through a coincidence.

Junk edges are excluded by construction: a join column has to identify
something (dates, amounts, rates, descriptions are values, not keys — two
records agreeing on `INVOICE_DT` is a cartesian product with an ON
clause), and columns every record carries (`SETID`, `EFFDT`,
`BUSINESS_UNIT`) never make an edge on their own.

The graph is derived at runtime from `db.columns()` and `db.indexes()`,
cached for 15 minutes, and bounded to the curated record map plus whatever
records you name — a live instance has tens of thousands of records, and
building every pair would be its own outage.

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
http://<this-host>:8016 (`--port` to change it).

### Who can reach it

**The default is the shared box.** `python -m pstb.gui` binds `0.0.0.0` on
port 8016, so colleagues open `http://<this-host>:8016` and nothing else
has to be arranged. There is no token unless you set one.

Every routable start prints what that exposes: anyone who can route to the
host can read every balance, every customer and use the ad-hoc SQL tool,
over cleartext HTTP. **Keep it inside the VPN.**

`--share` is still accepted and does nothing. It used to be a required
acknowledgement, which was worth it while loopback was the default — a
routable bind could then only happen on purpose. Once the shared bind is
the default, that flag would refuse the intended command, and a gate that
fires on the intended path is a gate people delete rather than read. The
disclosure it used to guard is printed either way.

**To keep it on this machine only**, ask for loopback and forward the port:

```bash
.venv/bin/python -m pstb.gui --host 127.0.0.1
ssh -L 8016:localhost:8016 <this-host>     # from your laptop
```

then open http://localhost:8016.

Two things stay true whatever you choose:

- **The configuration console is never shared.** `/console` — which changes
  settings and rotates credentials — answers only from the machine itself
  (SSH tunnel). Letting colleagues read the dashboards must not also let
  them rotate the Oracle password.
- **The Host check and the optional token are unaffected.** `--share` never
  controlled either of them.

**If you do want a token**, set `PSTB_AUTH_TOKEN` and restart; every request
then needs it, including from `127.0.0.1`, and the startup banner prints a
URL with the token in it to paste once. It must be 16–128 characters of
`A–Z a–z 0–9 - _` (it travels inside a URL and a cookie), and the console
can generate and store one for you. Changing it invalidates every link
already handed out — but a stale cookie never locks anyone out: the fresh
link wins and quietly replaces it.

`--allow-host <name>` (repeatable) restricts which `Host` names are
accepted. Without it any name is accepted.

Requests carrying a proxy's `X-Forwarded-For` are refused in both modes,
and in loopback mode an unexpected `Host` header is refused as well: a page
on the open web can otherwise resolve its own hostname to 127.0.0.1 and
read your ledger through the browser you left the tunnel open in.

### Why a question times out at 180s

The browser gives up on a chat request after 180 seconds. Everything the
turn does has to fit inside that, and the defaults do not add up on a slow
instance — this is arithmetic worth doing before blaming the model:

| Budget | Default | Where |
|---|---|---|
| Browser request | 180s | `REQ_TIMEOUT_MS`, `pstb/gui/static/index.html` |
| One SQL query | 120s | `db.query_timeout_seconds`, `config.yaml` |
| One Query Access Service call | 60s | `ps_api.timeout_seconds`, `config.yaml` |

A turn makes **several** tool calls. Two slow queries, or one slow query
plus one QAS call to a gateway that is not answering, already exceed the
browser's patience — and the turn keeps running server-side after the
browser has stopped listening. If PeopleSoft Query Access Services is
configured but its REST listening connector is down, every turn that
touches it spends 60s per call finding that out; `ps_api.enabled: false`
in `config.yaml` removes that cost entirely, and the curated database tools
are unaffected because they read the database directly.

A question asked while a previous one is still running is refused with an
explanation rather than queued into its own timeout, and **Clear** starts a
fresh conversation without waiting for the stuck query. Tune the two
thresholds with `PSTB_TURN_ABANDONED_SECONDS` (default 180, match it to the
browser) and `PSTB_QUEUE_WAIT_SECONDS` (default 45).

When a query is genuinely the problem, **Close & controls → diagnostics**
with timings measures the real inputs, and `scripts/diagnose_db.py` breaks
one playbook down to individual statements with their plans.

### While it is starting

Opening the page reports what the server is doing — connecting the answer
engine, reading ledger defaults, finding the last posted period,
discovering business units — with the elapsed time of the step in flight.
A slow instance genuinely takes a while at "Finding the last posted
period"; what that screen tells you is that it is working rather than
hung, and if a step fails it shows the database's own error (`ORA-…`) with
a retry button instead of sitting there.

The web server now accepts connections before the answer engine has
finished connecting, so the URL in the terminal is live immediately.
Questions asked in that first moment fall back to a per-question server,
which is slower but works; the page says so only if the shared engine
actually fails.

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
`--host 127.0.0.1` and treat it as a single-user tool until an
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

   **Where the password lives, and the one way it bites.** It is read from
   `ORACLE_PASSWORD` in the `.env` file that sits beside `config.yaml`,
   falling back to `db.oracle_password` in `config.yaml` itself. The
   environment variable wins, so a value exported in the shell or set by a
   systemd unit silently overrides the file. `.env` is kept at mode 600 and
   is repaired to 600 on every load; it is git-ignored. The configuration
   console writes secrets to that same `.env` and nowhere else. The password
   is never logged and never appears in an error message.

   Setting it outside `.env` is fine — but it cannot be set *wrong* outside
   `.env` without consequences, because Oracle counts every rejected logon
   against `FAILED_LOGIN_ATTEMPTS` in the account's profile (10 in the
   shipped `DEFAULT` profile). This app connects lazily, per query, so a
   rejected password would be re-offered on every query and could lock a
   shared service account within a few minutes of ordinary use. It no longer
   does: the first `ORA-01017` (or `ORA-28000`/`ORA-28001`) latches, and no
   further connection is attempted until the process restarts or you use the
   console's reload. Transient failures — `ORA-12541`, `ORA-12170`, a
   dropped session — still retry normally; they cost the account nothing.

   Two ways a *correct* password arrives wrong, both of which used to look
   identical to a typo:

   - **`$` or `{}` in a shell.** `export ORACLE_PASSWORD=Pa$sword` gives
     Oracle `Pa` — the shell ate the rest. Single-quote it:
     `export ORACLE_PASSWORD='Pa$sword'`.
   - **`$` or `{}` in `.env`.** The loader runs with `interpolate=False`
     specifically so `${...}` survives, and the console refuses to write a
     value containing `${` rather than mangle it. If you hand-edit, quote
     it.

   If the account does lock, `ORA-28000` is what you will see; a DBA
   clears it with `ALTER USER <user> ACCOUNT UNLOCK`.
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

### Add P2Go as an isolated Ask workspace

Ask workspaces are populated from configured runtime sources, not from the
offline metadata artifact. Add P2Go under `sources:` in `config.yaml`:

```yaml
db:
  backend: oracle
  schema: SYSADM

sources:
  p2go:
    backend: oracle
    schema: P2GO_SCHEMA
```

Keep its read-only credentials in `.env`:

```dotenv
PSTB_SRC_P2GO_DSN=host:1521/SERVICE_NAME
PSTB_SRC_P2GO_USER=readonly_p2go_user
PSTB_SRC_P2GO_PASSWORD=...
```

Restart the GUI process after changing either file. Then open `/api/meta` on
that process (for the default port,
`http://127.0.0.1:8016/api/meta`) and confirm that `sources` contains both
`default` and `p2go`, with commands `finance` and `p2go`.

Ask now shows separate `/finance` and `/p2go` workspaces. Type `/` to open the
workspace menu, `/p2go` to switch, or `/p2go show failed interface jobs` to
switch and ask in one step. Each workspace keeps its own visible transcript,
draft, in-flight work, activity and model session. **Clear** resets only the
active workspace.

`/finance` stays on the primary PeopleSoft connection and retains its curated
financial controls and business-unit/ledger/time scope. `/p2go` is hard-bound
to the P2Go connection and exposes no PeopleSoft, Coupa, wiki, memory or
P2Go-specific business tools. It can use only generic source-bound metadata
search, relationship paths, live table shape, query explanation, and guarded
read-only SQL. A P2Go workspace therefore has no PeopleSoft business unit,
ledger, fiscal year or period controls. An unknown slash command is rejected
in the browser without sending a model or database request.

Build every configured source once after enabling multi-source mode. This
migrates Finance from the single-source legacy path and creates P2Go's separate
semantic catalog and relationship graph:

```bash
.venv/bin/python scripts/build_metadata_catalog.py
```

After that initial build, a P2Go-only refresh leaves Finance untouched:

```bash
.venv/bin/python scripts/build_metadata_catalog.py \
  --source p2go --peopletools-source none
```

This publishes only P2Go's atomic knowledge SQLite file under
`metadata_catalogs/`; it does not rebuild or replace Finance. The artifact
contains structural names, keys, foreign-key column pairs and view dependency
edges—not transaction rows. Missing or partial native relationships remain
inconclusive, and matching column names alone are never promoted to a join.
If `tools.allow_raw_sql: false`, `/p2go` remains useful for structural semantic
and relationship questions but cannot answer row/count/amount questions.

Optionally have a DBA deploy the views in [`sql/oracle/`](../sql/oracle) and set
`db.use_views: true` — see [VIEWS.md](VIEWS.md). The agent works without them.

**Before anyone else uses it,** read the open items in
[REVIEW_RESPONSE.md](REVIEW_RESPONSE.md). Notably: there is no currency/amount
basis contract, no user authorization boundary, and raw SQL is enabled in the
shipped config (`tools.allow_raw_sql: false` turns it off).

## 6a. Connect Coupa as the PO/receipt authority

Use this profile when every purchase order and receipt lives in Coupa and only
invoices or accounting may reach PeopleSoft. The connector is read-only. Give
its service identity only the Coupa read scopes it needs.

Put the instance URL and one authentication method in `.env`:

```dotenv
COUPA_BASE_URL=https://your-company.coupahost.com
COUPA_CLIENT_ID=<read-only OAuth client id>
COUPA_CLIENT_SECRET=<read-only OAuth client secret>
COUPA_SCOPE=core.invoice.read core.purchase_order.read core.inventory.receiving.read core.supplier.read
```

An existing read-only API key can be used instead:

```dotenv
COUPA_BASE_URL=https://your-company.coupahost.com
COUPA_API_KEY=<read-only API key>
```

Choose OAuth or the API key, not both. The receiving scope is required for
dated receipt-event evidence; PO-line summary amounts are not a substitute.
Then configure the tenant-specific business-unit semantics in `config.yaml`:

```yaml
coupa:
  po_receipt_authority: true
  # Required: the Coupa company/calendar IANA timezone, not the server zone.
  business_timezone: "America/New_York"
  # Example only. Verify this exact dotted path against your Coupa receipt JSON.
  business_unit_path: "custom-fields.erp-business-unit"
  # Live controls require exact tenant-tested server query keys. Coupa query
  # spelling varies by resource/release; never infer a custom field's exposed
  # API spelling or a company prefix from the JSON response path.
  receipt_business_unit_filter: "custom_fields[erp_business_unit]"
  invoice_business_unit_filter: "invoice_lines[custom_fields][erp_business_unit]"
  # Enable only after proving the invoice query returns every header with an
  # in-scope PO/order-line even after an invoice distribution-account change.
  invoice_scope_order_line_invariant: true
  business_unit_map:
    US001: US_CORP
  invoice_eligible_statuses: [approved]
  receipt_eligible_statuses: [created]
  rni_max_rows: 50000
```

`business_timezone` must be the Coupa company's IANA timezone. The current-only
control calculates “today” in that governed zone; it does not inherit the app
server's local date around midnight. `business_unit_path` is not portable
across Coupa tenants. It must identify one
unambiguous Coupa value on each receiving transaction. `business_unit_map`
maps the caller's authorized PeopleSoft BU to that value. Leave neither to a
guess: a blank/unreadable path or unmapped unit must return `incomplete`, never
an all-company scan. Live evaluation requires the two exact server filter
keys even for a standard account segment. `invoice_scope_order_line_invariant`
is a reviewed tenant assertion, not
a convenience switch: leave it false until a Coupa API test proves that an
invoice-account reassignment cannot hide a header that still references an
in-scope order line. Coupa `paid` is a boolean, not a delivered invoice-header
status; the default eligible header status is `approved`. Receipt status is
tenant-extensible text, so add only tested values to
`receipt_eligible_statuses`. `rni_max_rows` is a configurable safety cap; the
runtime hard ceiling remains 100,000 rows per endpoint.

Restart the server after changing `.env` or `config.yaml`, then run
`coupa_health`. Production acceptance requires both:

```text
mode: live
ok: true
```

A blank `COUPA_BASE_URL` deliberately selects bundled fixtures. That is useful
for a demonstration, but `mode: fixtures` fails production acceptance.

Next ask, in the active BU and selected date:

> Show Coupa PO lines with net receipt activity above eligible invoice
> coverage at the selected period end, by currency.

For a historical selected period end, that question is expected to return
`incomplete`: the standard Coupa invoice API does not reconstruct approval
status at a past cut-off. For a current-date operational collection, before
using an observed candidate amount require `source=coupa`, `mode=live`, requested and
mapped BU/path, requested/data-as-of dates, receipt and invoice event IDs and
dates, eligible status basis, same-PO-line matching precision, per-currency
counts and amounts, page/cap/freshness coverage, and missing-key/date/currency
exceptions. The answer must call these **Coupa PO-line review candidates**,
label receipt IDs as contributing evidence rather than exact matches, and
state **booked status not evaluated**. Receipt and invoice
endpoints are paged sequentially, so `collection_complete=true` is still
`point_in_time_complete=false` and `atomic=false`; say “observed in the
completed sequential collection,” never “the exact current population.” A
nonzero `min_amount` is the same nominal native-unit threshold in every
currency, not one comparable materiality threshold.

Two controls remain deliberately outside that candidate result:

- The current `ap_completeness` / `coupa_to_ap_tie` path is a diagnostic, not
  production period-end completeness evidence: it does not yet prove complete
  Coupa pagination, the same BU, and the selected as-of cut-off on both sides.
  `coupa_to_ap_tie` remains a **current diagnostic only**.
  The broad control therefore remains `incomplete`; the distinct business fact
  “Did every approved Coupa invoice become a PeopleSoft voucher?” is `not
  established` until a governed cross-system bridge is added.
- “What booked receipt-accrual liability posted to PeopleSoft GL?” requires a
  separately governed Coupa-interface → PeopleSoft accounting/JGEN → posted-GL
  bridge. Neither `get_coupa_rni` nor `get_po_grni_candidates` can establish
  it. Without that bridge, answer `not established`; do not prepare a journal
  entry from the candidate total.

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

## 7a. Build the process graph (optional, recommended)

"How do we do invoicing?" is a different kind of question from "what is the
AR balance?" — it asks about the menu path, the pages, the records those
pages write, and the procedure that describes them. The agent answers it
from a **process graph** built offline from this instance's own metadata:

```bash
.venv/bin/python scripts/build_process_graph.py
```

Run it once after setup and again whenever the instance is customized (new
pages, new custom records, rewritten procedures). It reads structure only —
PeopleTools page/component/navigation definitions (`PSPNLDEFN`,
`PSPNLFIELD`, `PSPNLGROUP`, `PSPRSMDEFN`), the record catalog, saved
queries, business-unit countries, and the wiki — and writes one local
SQLite file, `process_graph.db` (git-ignored, no amounts, no customer or
supplier data). Question-time reads are milliseconds; nothing is added to
page load.

The build report names every metadata table it could not read and what that
costs ("pages are missing — answers name records but not screens"). Grant
SELECT on the four `PSPNL*`/`PSPRSM*` tables above to get the full chain.
Without a build, `trace_process` answers that the graph has not been built
and names the script — nothing else degrades.

Large catalogs are read with keyset pagination rather than one materialized
query. The shipped build limits are 100,000 rows for each PeopleTools source,
100,000 finished nodes, 100,000 finished edges, and a 512 MiB estimated-memory
budget. They are deployment settings, not query-time answer limits:

```yaml
process_graph:
  max_records: 100000
  max_pages: 100000
  max_page_fields: 100000
  max_components: 100000
  max_navigation: 100000
  max_queries: 100000
  query_page_size: 5000
  max_nodes: 100000
  max_edges: 100000
  memory_budget_mb: 512
  write_batch_size: 2000
```

Use `--row-limit`, `--page-size`, `--max-nodes`, `--max-edges`, or
`--memory-mb` for a one-build override. A source that reaches its catalog
limit is kept but marked `PARTIAL`/`DEGRADED`; `describe_process_graph`
returns structured `limit_hits`. A finished graph that exceeds the global
node, edge, or memory guard is not written: the prior `process_graph.db`
remains in place and the command explains which setting to review. Absolute
hard ceilings still prevent an accidental unbounded catalog mirror. The
memory guard preflights the raw harvests before allocating the merged graph,
then validates again after implied-node creation and canonicalization.

Question-time traversal remains deliberately small (`3` hops, `12` seeds,
`400` visited nodes, and `40` results per layer). Those response limits keep
chat output relevant and are independent of how large the offline index is.

## 7b. Build the metadata catalog (recommended)

The metadata catalog lets Gemini find delivered and custom records across the
PeopleSoft database and other configured databases without guessing a `PS_` or
company prefix. Build it after the source connections work:

```bash
.venv/bin/python scripts/build_metadata_catalog.py
```

The default includes the primary `db:` connection as `default` and every
database under `sources:`. Only `default` receives the PeopleTools overlay by
default. Narrow or redirect that explicitly when required:

```bash
.venv/bin/python scripts/build_metadata_catalog.py --source default,warehouse
.venv/bin/python scripts/build_metadata_catalog.py \
  --source default,psft --peopletools-source psft
.venv/bin/python scripts/build_metadata_catalog.py --peopletools-source none
```

Oracle collection is owner-scoped: a configured `db.schema` uses filtered
`ALL_*` views and a blank schema uses `USER_*`. SQL Server retains the
`sys.schemas` owner and filters to a configured schema when present; SQLite
uses `MAIN`. Same-named objects in different sources or schemas never merge.

The build account needs read-only metadata visibility for objects, columns and
indexes in each selected source (`VIEW DEFINITION` on the intended SQL Server
database/schema; the corresponding `USER_*` or owner-filtered `ALL_*`
visibility on Oracle; file read access on SQLite). For the PeopleTools layer,
grant `SELECT` on `PSRECDEFN` and `PSRECFIELD`, plus the optional
`PSDBFIELD`, `PSDBFLDLABL`, `PSXLATITEM`, `PSPNLFIELD`, `PSQRYDEFN` and
`PSQRYRECORD` layers you want indexed. Saved-query use is collected only when
the local catalog shape can prove that the query is public.

The shipped limits are exact and per build:

```yaml
metadata_catalog:
  max_objects: 100000          # per source
  max_fields: 500000           # columns per source
  max_indexes: 250000          # index definitions per source
  max_peopletools_rows: 500000 # per PeopleTools layer
  query_page_size: 5000
  stale_after_hours: 168
```

Use `--max-objects`, `--max-fields`, `--max-indexes`, `--max-constraints`,
`--max-constraint-columns`, `--max-dependencies`,
`--max-peopletools-rows` or `--page-size` for a one-build override. The
builder writes a mode-`0600` `.building` file and atomically publishes one
artifact per selected source. A primary-only installation keeps the legacy
`metadata_catalog.db`; a multi-source installation uses
`metadata_catalogs/<safe-source>-<source-hash>.db` for every source, including
`default`. A failed or empty build preserves that source's prior artifact, and
refreshing `p2go` cannot replace `default`, `warehouse`, or any other source.
A layer that reaches a configured limit remains usable but is marked partial.
`describe_metadata_catalog` reports every limit hit, source/layer error,
snapshot age and search mode. Stale snapshots remain readable with a warning;
absence in a partial or stale layer is never evidence that an object does not
exist.

Rebuild after customizations, DDL/index changes, field-label/translate changes
or saved-query changes. Rebuild a source immediately after changing its
connection locator, configured schema, or (when schema is blank) configured
login identity. Each artifact carries a one-way fingerprint of that namespace;
passwords and tokens are excluded, and a multi-source runtime fails closed
instead of reusing an artifact whose fingerprint no longer matches. For an
Oracle TNS alias, configure a readable `oracle_config_dir` or
`oracle_wallet_dir`; the `tnsnames.ora` content is part of the hash. For SQL
Server, use explicit `Server`/`Address` plus `Database`/`Initial Catalog`
values in the connection string. DSN-only/FILEDSN sources, attached database
files without an explicit database, and login-default databases are refused
because their targets are mutable external indirection. Weekly
matches the default seven-day freshness target for a stable
PeopleSoft environment; schedule nightly if custom integration or warehouse
metadata changes frequently.

### 7b.1 Keep the offline refreshes in one schedule

The local snapshots have different truth clocks. Keep their jobs together so a
new deployment does not refresh the transaction-derived entity graph while
leaving structural discovery stale. For a stable installation, this is a
reasonable starting crontab (adjust `/opt/peoplesoft_tb_mcp` and the times):

```cron
# Structural snapshots: weekly and after every customization/deployment.
0 2 * * 0 cd /opt/peoplesoft_tb_mcp && .venv/bin/python scripts/build_process_graph.py --quiet
30 2 * * 0 cd /opt/peoplesoft_tb_mcp && .venv/bin/python scripts/build_metadata_catalog.py --quiet

# Transaction-derived actor relationships: nightly.
0 3 * * * cd /opt/peoplesoft_tb_mcp && .venv/bin/python scripts/build_entity_graph.py --quiet

# Operational findings/history: each weekday morning; output only on change.
30 6 * * 1-5 cd /opt/peoplesoft_tb_mcp && .venv/bin/python scripts/monitor.py --quiet
```

Run both structural builders immediately after a PeopleTools migration rather
than waiting for Sunday. Sites with daily custom-schema changes should move the
metadata build to the nightly clock. Each builder publishes atomically, so a
failed refresh leaves its prior readable artifact in place; alert on a nonzero
exit code rather than deleting that artifact.

The resulting artifact contains structural metadata only — no source rows,
balances, credentials, private PSQuery names or full view SQL. It is not
business-unit row security and cannot substantiate a financial answer. Gemini
2.5 Pro uses `search_metadata` → `get_metadata_context` to select an object,
then a live tool must enforce caller scope and the required date/status basis.
The full contract, confidence tiers, source examples and first-slice
limitations are in [METADATA_CATALOG.md](METADATA_CATALOG.md).

## 7c. When a query hangs

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
| Tool call hangs, no result | run `scripts/diagnose_db.py` (section 7c); check the PS_LEDGER index and `db.query_timeout_seconds` |
| Metadata search says unavailable | run `scripts/build_metadata_catalog.py`; then inspect `describe_metadata_catalog` for missing source/schema grants |
| Metadata result is stale or partial | rebuild it; inspect `describe_metadata_catalog.limit_hits` and source/layer notes before raising only the affected configured limit or grant |
| pip TLS/certificate errors | corporate TLS inspection — point pip at the internal mirror (step 3) |
| PowerShell blocks activation | you do not need to activate; call `.venv\Scripts\python` directly |

## 9. Configuration files, and upgrading without losing them

Nothing that describes *this* machine is tracked by git, so `git pull` can
never overwrite it. Four layers, each one winning over the one above:

| File | Tracked? | Written by | Holds |
|---|---|---|---|
| `config.example.yaml` | **yes** | the project | shipped defaults; replaced on every upgrade |
| `config.yaml` | no | `scripts/setup.py`, or you | this installation's settings |
| `config.local.yaml` | no | the `/console` page | individual keys, overriding the two above |
| `.env` | no (mode 600) | `scripts/setup.py`, or you | passwords, API keys, DSN |

With no `config.yaml` at all the app reads `config.example.yaml`, so a fresh
clone runs immediately. Create yours by running `python scripts/setup.py`, or
by hand:

```bash
cp config.example.yaml config.yaml
```

An existing `config.yaml` always wins over a newer example — an upgrade
cannot silently repoint a live deployment at the sample ledger.

### Upgrading a machine that already has a hand-edited `config.yaml`

Earlier versions tracked `config.yaml`, so a machine set up before this change
has a *tracked, modified* file that `git pull` will stop on. Run this once,
before the pull:

```bash
cp config.yaml config.yaml.keep && git checkout -- config.yaml
```

Then pull, and put your settings back:

```bash
mv config.yaml.keep config.yaml
```

From then on `config.yaml` is ignored and every later pull leaves it alone.
Check what you have with `git status` — it should not list `config.yaml`.

## 10. What is safe to commit

`.gitignore` already excludes `.venv/`, `.env`, `config.yaml`,
`config.local.yaml`, the generated sample database, `logs/`, and build
artifacts. **`.env` holds credentials — never commit it.** Share
`.env.example` and `config.example.yaml` instead.

One trap when editing `.gitignore`: git has no inline comment there. A
trailing `# note` after a pattern becomes part of the pattern, the rule then
matches nothing, and the file it was meant to protect gets committed. Put
comments on their own lines. `tests/test_config_is_per_deployment.py` checks
each rule still matches the file it names.
