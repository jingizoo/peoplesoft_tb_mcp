# Wiki Grounding: Do We Need a Vector Database?

**Short answer: not yet, and possibly never for this corpus.** The failure you
saw — "I get a list of hyperlinks, it doesn't seem to read the wiki" — was not
a retrieval-quality problem. It was that the search tool returned no text at
all. This document records the diagnosis, the fix, and the honest decision
criteria for embeddings, so the choice can be revisited with evidence rather
than fashion.

## What was actually wrong

`wiki_search` against Confluence returned `id`, `title`, `space`, `version`,
`url` — and **no body**. The model received a list of hyperlinks and had
nothing to reason over. It could only relay the links or, worse, infer policy
from a page title. No amount of embedding would have fixed that: the right
page was already being found; its content was never fetched.

Two contributing factors made it worse:

1. **Search-then-read is two decisions.** Models routinely make the first and
   skip the second. Any design that requires the model to remember to fetch
   will fail some fraction of the time.
2. **Whole pages are the wrong unit.** Even when fetched, a 12,000-character
   policy page buries the one sentence that answers the question.

## What we built instead

| Layer | What it does | Why |
|---|---|---|
| `wiki_search` | returns title, URL **and a snippet**, plus `next_step` pointing at `wiki_lookup` | pointers should look like pointers |
| **`wiki_lookup`** | searches → **fetches** top pages → splits into heading-aware passages → BM25-ranks them → returns the passages | one call, grounded content; removes the skipped second step |
| `pstb/retrieve.py` | passage splitting + BM25 (stdlib) | passage-level relevance, no infrastructure |
| Answer guards | continue the loop on "I will call…"; flag a compliance verdict lacking either the rule or the figure | structure, not prompting |

Every passage carries page title, section heading, URL and version, so an
answer can quote a sentence and name where it came from.

For a combined data-and-policy question, the agent loop now enforces the source
order rather than trusting the model to remember it:

1. validate the user-selected PeopleSoft scope;
2. retrieve successful financial evidence from the database;
3. only then run `wiki_lookup`;
4. synthesize the two separately identified sources.

An error, invalid scope, or `NO DATA` result stops the chain. Wiki text is never
used as a numerical fallback. Pure policy questions may still use the wiki
directly, and data-only questions cannot call wiki tools.

## When a vector database *is* the right call

Adopt embeddings when you can point at one of these, measured — not assumed:

- **Vocabulary mismatch.** Users ask "how long can money sit unidentified?"
  and the page says "unapplied receipts must be researched within 30 days."
  Keyword search misses; semantic search finds it. This is the strongest
  genuine case, and it is the one to measure first.
- **Corpus scale.** Thousands of pages across many spaces, where CQL's
  full-text ranking returns too many weak candidates to fetch.
- **Cross-document synthesis.** "What controls apply to period close?" where
  the answer spans six pages and no single page is the answer.

## When it is a liability

- **Small, well-labelled corpus.** A few hundred finance policy pages that
  Finance owns and labels. Confluence already indexes them.
- **Staleness becomes a correctness bug.** A policy changes; the index lags;
  the agent quotes a superseded threshold with full confidence. Live CQL has
  no such window. If you index, you own a sync pipeline and its SLA.
- **Data governance.** Embedding finance policy means sending it to an
  embedding endpoint. That is a new data-egress path requiring review — the
  same conversation as the Gemini one, but for documents rather than ledger
  rows.
- **New failure mode.** Semantic search returns the *plausibly similar* page.
  For "what is the capitalization threshold", retrieving the fixed-asset
  disposal policy instead of the acquisition policy is a confident wrong
  answer. Lexical search fails more visibly, which in finance is safer.

## Staged path (recommended)

1. **Now — connect the wiki properly.** `scripts/diagnose_wiki.py`, explicit
   `provider: confluence`, space + label scoping. Most "bad retrieval" on day
   one is a scoping problem.
2. **Now — measure.** `logs/questions.jsonl` records every turn and flags
   failures; the UI has a thumbs-down. After two weeks of real use you will
   have a list of questions that retrieval got wrong. **That list decides
   step 3, not intuition.**
3. **If the failures are vocabulary mismatch** — add embeddings as a
   *reranker over CQL results*, not as a replacement index. Keep Confluence
   as the source of truth and the security boundary; embed only the top ~20
   candidates per query. No sync pipeline, no stale index, no bulk egress.
   `pstb/retrieve.py` is the seam: swap `rank_passages` for a hybrid scorer.
4. **Only if scale demands it** — a persistent vector store, with an explicit
   owner for freshness, a re-index trigger on page update, and a documented
   egress approval.

Skipping to step 4 is the common, expensive mistake: it buys infrastructure
before the failure it addresses has been observed.

## What actually moves accuracy on this corpus

In rough order of impact for a PeopleSoft finance wiki:

1. **Fetching the content at all** (done — this was the whole bug).
2. **Label scoping** so the agent only reads pages Finance owns (`gl-policy`,
   `gl-close`, `gl-coa`) — see `docs/SETUP.md` §7.3.
3. **Passage-level ranking** so the answer quotes a sentence, not a page.
4. **One page per topic.** Two pages describing the same rule differently is
   the single largest source of wrong policy answers, and no retrieval
   technology fixes it.
5. **Requiring both halves** for a compliance question — rule *and* figure —
   enforced by the answer guard rather than by asking the model nicely.
6. A stronger model. Gemini 2.5 Pro chains retrieval → data → verdict far more
   reliably than an 8B local model, which in testing quoted a rule correctly
   and then fabricated the balance it compared against.

Embeddings appear nowhere in that list until step 3's evidence exists.
