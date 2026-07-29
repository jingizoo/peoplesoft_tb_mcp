# peoplesoft-tb-mcp

MCP server + LLM chat client answering PeopleSoft trial-balance questions.
Python package `pstb`; runs against a bundled SQLite sample GL or a real
PeopleSoft Oracle database.

## Commands
- `make seed` / `make smoke` — build sample DB, run stdlib-only engine tests (no install needed)
- `make venv` — venv + `pip install -e ".[llm]"`
- `make probe` — spawn the MCP server over stdio and call tools (no LLM)
- `make chat` / `make chat-gemini` — chat REPL (Ollama / Gemini via Vertex)
- One-shot: `.venv/bin/python -m pstb.client.chat --ask "..."`

## Architecture
- `pstb/server.py` — FastMCP stdio server; thin wrappers with docstrings (the
  LLM reads those), all errors returned as `{"error": ...}` dicts.
- `pstb/engine.py` — TB math (pure logic, stdlib only). Pivots PS_LEDGER
  period buckets into beginning/activity/ending.
- `pstb/queries.py` — SQL builders; inline base-table mode or `XX_TB_*` view
  mode (`db.use_views`). Bind params always; identifiers via allowlists only.
- `pstb/db.py` — sqlite/oracle/sqlserver connections; lowercase dict rows.
- `pstb/wiki.py` — Confluence REST or local markdown folder (auto fallback).
- `pstb/client/` — provider-agnostic agent loop; `llm_ollama.py`,
  `llm_gemini.py` (google-genai with vertexai=True — the old
  `vertexai.generative_models` module is retired).
- `scripts/seed_sample_data.py` — builds PS_LEDGER **from** the generated
  journals, so drill-downs tie exactly. FY2025 closed into FY2026 period 0.

## Conventions
- Ledger amounts are signed: debits positive, credits negative.
- Period 0 = beginning balances; 998 = adjustments; ending(P) = Σ periods 0..P.
- Tool args are primitives only (str/int/float/bool) — keeps schemas clean for
  Gemini and small Ollama models. `fiscal_year=0` / `period=0` mean "current".
- The MCP server must never print to stdout (stdio protocol) — stderr only.
- Never interpolate user values into SQL; only allowlisted identifiers are
  formatted in.
