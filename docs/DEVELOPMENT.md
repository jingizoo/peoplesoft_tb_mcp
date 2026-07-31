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
python -m pstb.client.chat                     # chat REPL
python -m pstb.client.chat --provider gemini   # chat via Gemini on Vertex AI
python -m pstb.client.chat --ask "..."         # one-shot question
python -m pstb.server                          # run the server standalone
python -m pstb.gui --open                      # web UI on 127.0.0.1:8000
```

macOS/Linux shortcuts: `make venv`, `make seed`, `make smoke`, `make probe`,
`make chat`, `make chat-gemini`.

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
- `pstb/report.py` — nVision-style report runner: timespan resolver (YTD/BAL/
  PER/QTD/Qn/ROLL12/-1Y) plus a grid engine over report JSONs in reports/.
- `pstb/client/` — provider-agnostic agent loop plus `llm_ollama.py` and
  `llm_gemini.py` (google-genai with `vertexai=True`; the older
  `vertexai.generative_models` module was retired in June 2026).
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
from the web UI. Review the failure backlog with `python -m pstb.qlog`; each
flagged question is a candidate for a new curated tool or record-map entry.

Multi-step chaining is deliberately NOT LangChain/LangGraph: the agent loop
already feeds tool results back for up to 10 rounds, and the reliable way to
reduce chain errors is to move routing and arithmetic INTO deterministic
tools — get_record_map kills table-guessing before run_sql, and
get_exchange_rate converts amounts server-side so the model never multiplies.
A framework on top of MCP would add a dependency without fixing either
failure mode.

## Evals — pinning MODEL behavior

```
.venv/bin/python scripts/eval.py              # every case, exit 1 on failure
.venv/bin/python scripts/eval.py --case ar-aging
.venv/bin/python scripts/eval.py --from-qlog  # seed cases from real failures
```

The suites pin SQL and engine behavior; `evals/cases.json` pins what the
MODEL does — which tool it picks, whether it refuses, whether it reaches for
the wiki when it should query the ledger. Assertions are structural
(`any_tool`, `not_tool`, `tool_args_contain`, `answer_lacks`, `not_refused`),
never "does this read well", so a pass means the same thing every run. Run it
after any change to prompts, tool docstrings, or the model — it caught a
false positive in the number guard on its very first run.

Real failures are the best eval material: `--from-qlog` turns flagged turns
into pending cases for a human to grade.

## Testing

`scripts/smoke_test.py` runs 202 checks on the stdlib alone (no venv required)
and covers ledger math, effective dating, journal tie-out, integrity controls,
tree rollups, SQL guards, no-data scope handling, and adjustment-period basis.
Run it plus `scripts/mcp_probe.py` before any commit.
