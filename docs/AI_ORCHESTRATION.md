# AI retrieval and controller workflows

The application uses AI where it improves recognition and explanation, while
keeping financial arithmetic, scope rules and control verdicts deterministic.
It does not need a LangChain or LangGraph rewrite.

## Hybrid metadata retrieval

`search_metadata` remains the authority for the candidate set. It searches the
offline, structure-only catalog and explains why a logical record maps to a
physical object. Optional semantic re-ranking can improve questions whose
business wording does not literally match a custom field or table name.

Enable it explicitly:

```yaml
semantic_retrieval:
  enabled: true
  provider: vertex
  model: gemini-embedding-001
  location: global
  output_dimensionality: 768
  candidate_limit: 20
  semantic_weight: 0.35
  timeout_seconds: 15
```

The provider receives only the query and an allow-listed structural summary:
source, schema, object/record/field names, labels and match facets. Transaction
rows, sample values, amounts, parties, arbitrary node attributes and database
credentials are excluded. Vertex ordering is advisory: it cannot add a
candidate, change relationship confidence, satisfy financial evidence, or
replace a live finance tool. If Vertex is unavailable, the original catalog
order is returned with an `unavailable` reason.

This uses the same Google Cloud project and Application Default Credentials as
the Gemini provider. Enabling it is therefore a data-egress decision: metadata
names and the user's search phrase leave the application host for Google
Vertex AI. It is disabled by default, and only the literal YAML boolean `true`
enables it. Each request has a bounded timeout (default 15 seconds, runtime
range 1-60 seconds), after which deterministic catalog order is preserved.
Google's `global` endpoint does not provide control or visibility over the
processing region; regulated deployments should choose a supported region
approved by their data-residency policy instead of accepting this example
default.

## Resumable controller workflows

The existing playbooks are deterministic and already enforce the critical
rule that an unavailable control makes the result `incomplete`, never passed.
`pstb.workflows` sequences those playbooks into multi-phase reviews and pauses
after every phase for a human acknowledgment.

Available initial workflows:

- `month_end_close`: AP completeness, close readiness, post-close watch.
- `daily_controller_review`: daily exception brief, receivables health.
- `receivables_review`: receivables health by itself.

Start and advance a workflow from the deployment host:

```bash
.venv/bin/python scripts/workflow.py list-specs
.venv/bin/python scripts/workflow.py start month_end_close \
  --bu US001 --ledger ACTUALS --fy 2026 --period 6
.venv/bin/python scripts/workflow.py run <workflow-id>
.venv/bin/python scripts/workflow.py review <workflow-id> accept --revision 3
```

`accept` means only that a human reviewed the live evidence. It does not post
or approve a journal, voucher or payment, and it is not approval of an
accounting conclusion. `rerun` discards the old checkpoint outcome and reads
the source systems again; `cancel` ends the workflow.
Every review mutation requires the exact positive `revision` from the latest
run or status response; stale or omitted revisions are refused.

Checkpoints under `logs/workflows/` are atomic mode-0600 JSON files. They store
scope, phase status, verdict/counts, timestamps and a SHA-256 result digest.
They deliberately do not store live tool results, amounts, rows, party names,
journal or voucher identifiers, free-form operator/reviewer names, or model
prose. Until the app has real authentication, a review records only that a
local human acknowledged it and when; it is not an identity audit trail. A
result needed after restart must be rerun from the source. An optimistic
revision check prevents a stale review screen from advancing a workflow that
changed elsewhere. Each playbook execution also holds a unique, expiring lease
token, so a slow superseded worker cannot overwrite the result of a newer run.

## Why not LangGraph yet?

The needed behavior is a small state machine around known playbooks, not an
open-ended agent graph. This implementation is easier to audit, has no second
tool-routing stack, and works without an LLM. A graph framework becomes useful
only if deployments later require distributed workers, many conditional
branches, external approval systems, or long-running event subscriptions.
Those can be added behind this workflow contract without changing the finance
tools or their evidence rules.
