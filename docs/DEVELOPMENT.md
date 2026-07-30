# Development Notes

MCP server + LLM chat client answering PeopleSoft trial-balance questions.
Python package `pstb`; runs against a bundled SQLite sample ledger or a real
PeopleSoft Oracle database.

## Commands

Cross-platform (Windows, macOS, Linux):

```
python scripts/bootstrap.py          # venv + install + seed + verify, one step
python scripts/seed_sample_data.py   # rebuild the sample ledger (stdlib only)
python scripts/smoke_test.py         # engine tests, stdlib only, no install needed
```

With the virtualenv active:

```
python scripts/mcp_probe.py                    # spawn the server over stdio, no LLM
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
  fallback.
- `pstb/ar.py` — AR aging (with GL control tie-out) and billing pipeline
  over PS_ITEM / PS_CUSTOMER / PS_BI_HDR / INTFC_BI.
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

## Testing

`scripts/smoke_test.py` runs 103 checks on the stdlib alone (no venv required)
and covers ledger math, effective dating, journal tie-out, integrity controls,
tree rollups, SQL guards, no-data scope handling, and adjustment-period basis.
Run it plus `scripts/mcp_probe.py` before any commit.
