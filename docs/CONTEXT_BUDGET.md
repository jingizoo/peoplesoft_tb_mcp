# The fixed prompt does not fit the local model's window

Measured 2026-08-08 with `scripts/context_budget.py`, llama3.1:8b, 70 tools.

Every turn pays a fixed cost before the user has asked anything: the system
prompt, plus every tool's description, plus every tool's JSON schema. Nothing
was measuring it. It crossed `llm.ollama_num_ctx` unnoticed, Ollama truncated
silently at an end nothing controls, and routing degraded across the product
with no error, no warning and no failing test.

This is the second time this failure has happened here. The first was the
`num_ctx=2048` default silently truncating a ~5,574-token system prompt, which
meant every local-model routing decision was measured against a prompt that
never arrived.

## What it costs

| | chars | tokens |
|---|---:|---:|
| system prompt | 24,946 | 6,929 |
| tool descriptions | 26,122 | 7,256 |
| tool schemas | 13,016 | 3,615 |
| **fixed, every turn** | **64,084** | **17,801** |
| + one tool result (24,000-char local cap) | 24,000 | 6,666 |
| + question, reasoning and answer | | ~1,500 |
| **needed** | | **~25,967** |
| `ollama_num_ctx` | | **16,384** |

Tokens are estimated at 3.6 chars/token, measured against this prompt's
register rather than the usual 4.0, which flatters the number.

## What was already cut

Both of these shipped and neither cost routing accuracy:

* **Schema titles.** FastMCP derives `"title": "Business Unit"` for a
  parameter already keyed `business_unit`. Nothing reads it. Across 70 tools
  that was 7,412 characters — 37% of all schema bytes.
* **Docstring duplication.** 31,151 → 26,122 chars. `run_sql` alone went
  3,904 → 2,790, almost entirely by deleting text `prompt.py` already states
  in full. Verified: the eval suite scored 25/30 afterwards, its best result,
  with no case regressing.

## What is left, and why trimming cannot finish the job

The audit found roughly 4,815 further characters safely cuttable from
`prompt.py`. Applying all of them lands at **~24,630 tokens against a 16,384
window** — still over by ~8,200.

Reaching 16,384 from here would additionally require dropping the local
tool-result cap from 24,000 to about 6,000 characters *and* removing roughly
half the tools. Neither is a trim; both change the product.

Two proposed prompt cuts were deliberately **not** applied, because cutting
behaviour no test covers is how it breaks silently:

* `## Performance: plan before you join` (−550) — no eval case covers
  `explain_query`, partitioning, or timeout recovery.
* `## Chaining across modules` (−443) — `tests/test_chaining.py` covers the
  server-side substitution, not whether the model ever chains.

## The decision

| | latency (warm) | routing |
|---|---|---|
| `num_ctx` 16384 | 2.5s · 10.0s · 13.9s | tool list truncated; `tb_integrity_check` and `get_ar_aging` misroute to `run_report`/`run_sql` |
| `num_ctx` 32768, before the cuts | 27.3s · 37.3s · 32.1s | correct |
| `num_ctx` 32768, after the cuts | 27.3s · 29.6s · 24.4s | correct — eval 25/30, best recorded |

The cuts do not remove the need for a larger window; they reduce its price by
15–24% and leave real headroom inside 32,768.

`config.yaml` currently ships **16384**, unchanged, because this is a
deployment tuning decision rather than a code one. Raising it is a one-line
change. **Gemini is unaffected** either way — a 1M window, and it is the
deployment target.

One eval case, `explain-why-it-moved`, is marked `skip` for this reason: it
passes at 32768 and fails at 16384. Unskip it when the window is raised.

## Keeping it honest

```bash
.venv/bin/python scripts/context_budget.py
```

Exits 1 when the fixed prompt plus one full-size tool result will not fit, so
it can gate a merge. Run it after adding a tool — a new tool is not free, and
the cost is paid on every turn by every question, including the ones that
never call it.
