# Metadata intelligence catalog

The metadata catalog is a local, read-only index of the database structure
available to this deployment. It helps the agent find delivered and custom
records without guessing a `PS_` prefix, and without sending an entire
PeopleSoft catalog to the language model.

It is deliberately an **offline structural snapshot**. It contains names,
definitions and relationships, not transaction rows, balances or financial
evidence. Use it to choose the right source and object; use a live, scoped tool
to answer the business question.

## Build and refresh it

From the project root:

```bash
.venv/bin/python scripts/build_metadata_catalog.py
```

The default build reads the primary `db:` connection as source `default`, plus
every database configured under `sources:`. It applies the PeopleTools overlay
to `default` unless told otherwise. Each source is published to its own atomic
SQLite artifact. The file holds both semantic-search nodes and relationship
edges; there is no second process graph or shared cross-database catalog.

`--source` chooses which independent artifacts to refresh. It never patches or
replaces an unmentioned source's file. Omit it in the normal scheduled job to
refresh every configured database.

```bash
# Refresh only these two source artifacts; every other source is unchanged
.venv/bin/python scripts/build_metadata_catalog.py --source default,warehouse

# PeopleTools is on a different selected source
.venv/bin/python scripts/build_metadata_catalog.py \
  --source default,psft --peopletools-source psft

# Build native database structure only
.venv/bin/python scripts/build_metadata_catalog.py --peopletools-source none

# Put one selected source's artifact in a separately protected location
.venv/bin/python scripts/build_metadata_catalog.py --source warehouse \
  --peopletools-source none --out /secure/path/warehouse_metadata.db
```

For a primary-only deployment, `default` retains the legacy
`metadata_catalog.db` path beside the active configuration. In a multi-source
deployment, every source including `default` is stored under
`metadata_catalogs/<safe-slug>-<source-hash>.db`. The source name is hashed
before it enters a filename, so a configured name cannot escape that directory
or collide after sanitization. `--out` is accepted only when exactly one source
is selected.

Artifacts are git-ignored and written with owner-only permissions (`0600`).
The builder writes a `.building` sibling first and atomically replaces only the
selected source after a usable snapshot commits. A failed or empty rebuild
does not destroy that source's last good catalog, and cannot affect another
database's artifact.

Each artifact stores a one-way, secret-free fingerprint of its backend,
configured schema, and non-secret database locator. Passwords, wallet secrets,
access tokens, timeout settings, and pool sizing are excluded. When schema is
blank, the configured login identity is included only inside the one-way hash
because it determines `USER_*`/default-schema visibility; the username itself
is not stored in the artifact. A bare Oracle TNS alias is also bound to the
hashed contents of the readable `tnsnames.ora` in its configured network or
wallet directory. SQL Server metadata sources must expose explicit
`Server`/`Address` and `Database`/`Initial Catalog` values. DSN-only and
FILEDSN locators, attached database files without an explicit database, and
connection strings that rely on the login's mutable default database are
refused because those targets can change without changing `config.yaml`.
In a multi-source deployment the runtime compares the configured fingerprint
before every read. Repointing `p2go` (or changing its schema) therefore makes
the old graph unavailable until `--source p2go` rebuilds it; it cannot silently
answer with stale semantics from the former endpoint.

Build once during deployment and after a PeopleSoft customization, schema,
key/constraint, index, view, field-label, translate-value or saved-query
change. Catalog schema version 2 adds native keys and view lineage, so an
artifact built by the first release must be rebuilt. A weekly rebuild is
a reasonable baseline for stable production metadata; schedule it nightly when
warehouses or custom integration schemas change frequently. The default
freshness target is seven days (`168` hours), so choose a cadence at least as
frequent as that target.

## Source and schema scope

Source names are a hard namespace and file boundary. The same table name in
`default` and `warehouse` is stored in two different SQLite files. At runtime,
`describe_metadata_catalog`, `search_metadata`, and `get_metadata_context`
resolve one canonical `source=` and open only its bound file. The reader checks
the file's `sources` table on every open; a copied or misrouted artifact fails
closed with a rebuild instruction instead of searching the wrong database.
It also verifies the endpoint fingerprint, so the same source name cannot be
reused for another locator/schema while retaining the old artifact. The source
exact match, build time, snapshot age, and fingerprint are visible through
`describe_metadata_catalog(source="...")`.
Add intended secondary databases under `sources:` in `config.yaml`, for
example:

```yaml
db:
  backend: oracle
  schema: SYSADM

sources:
  warehouse:
    backend: oracle
    schema: FIN_DW
  p2go:
    backend: oracle
    schema: P2GO
    schemas: [P2GO, TUSINVC]
```

Keep passwords in `.env`; they are not copied into the catalog. Per-source
Oracle credentials use `PSTB_SRC_<SOURCE>_DSN`,
`PSTB_SRC_<SOURCE>_USER`, and `PSTB_SRC_<SOURCE>_PASSWORD`.

Schema behavior is intentionally bounded:

- **Oracle:** when `schema`/`schemas` are set, the collector uses safely bound
  owner filters on `ALL_*` catalog views. `schema` is the default for
  unqualified live names; `schemas` is the complete allowlist stored in that
  source's one artifact. When both are blank, it uses `USER_*`. It never
  performs an unowned crawl of everything visible to the account.
- **SQL Server:** objects are joined to `sys.schemas`. A configured schema is
  matched case-insensitively; with no configured schema, every schema visible
  in that configured database is collected and kept distinct. Explicit
  multi-schema allowlists are Oracle-only in this release; use separate
  sources for other governed database boundaries.
- **SQLite:** the collector reads the selected database's `sqlite_master` and
  PRAGMA metadata under schema `MAIN`.

Each multi-schema Oracle build records `schema_coverage` in the source's
snapshot: the configured default, the complete configured owner list, a
TABLE/VIEW object count per owner, the owners with a zero count, and a
`complete` boolean. Thus a P2Go source configured as `schema: P2GO` with
`schemas: [P2GO, TUSINVC]` has one catalog and relationship graph, but the
operator can still prove that both owners contributed objects. Declared native
relationships may cross those two owners; they never cross into Finance or a
different source artifact.

A configured owner returning zero TABLE/VIEW metadata marks the source and
snapshot **partial**, and the build prints `MISSING SCHEMAS <source>: ...`.
It does not prove that the owner is empty. Verify the Oracle service/PDB, exact
owner spelling, and the build account's normal-session `ALL_*` visibility. Do
not treat absence from that owner as evidence until a fresh build reports
nonzero coverage for it.

Each CLI refresh also writes an adjacent owner-only (`0600`), git-ignored
`*.db.status.json`. Before collection starts, the builder durably records a
`building` attempt; if that marker cannot be written, the build stops before
touching the catalog. The status records a random build-run ID, attempt time,
canonical source, building/published/failed/partial state, current and previous
snapshot IDs, and the bounded schema coverage above. It stores only a
categorized failure reason, never the Oracle error, SQL, credentials, login,
DSN or object names.
`describe_metadata_catalog` exposes this as `latest_build`. A failed refresh
still preserves the prior readable artifact atomically, but
`latest_build.published: false` makes that failure visible and prevents a model
eval from mistaking the older snapshot for a successful current refresh. A
successful status is accepted only when its snapshot ID matches the catalog
being served; a mismatch is disclosed as a failed/unknown refresh rather than
borrowed as health evidence.

Select sources deliberately. Catalog structure can itself reveal custom object
and field names even though it contains no business rows.

### Governed local meanings for cryptic custom objects

A native catalog can prove that `P2GO.X9_HDR` exists without explaining what
the name means. In a secondary workspace, an explicit user correction such as
“remember that `P2GO.X9_HDR` is our inbound job header table” may create a
source-bound proposal. It is deliberately **not learned immediately**:

1. the named object must resolve unambiguously in the current source catalog;
2. the proposal is stored pending in `source_knowledge/<source-hash>.db`;
3. a host operator opens **Metadata meanings** in the visible Ask context bar
   and explicitly approves or rejects it; the same drawer can submit an exact
   `schema.object`, meaning and aliases directly without sending that wording
   through the chat model;
4. an approved **prefer** meaning may improve `search_metadata` or
   `get_metadata_context` for that same source; an approved **exclude** rule
   vetoes that exact object instead.

The review drawer is machine-local (opening it through an SSH tunnel still
arrives as loopback). When business-unit security is enabled, the selected
operator must also be listed in `security.privileged_users`; that configured
privilege is evaluated before PeopleSoft BU-security records, so the reviewer
does not need a row in those records. The `pstb.source_knowledge` CLI remains
available as a host-side fallback.

For an isolated test deployment that cannot use a tunnel, the default-off
`security.allow_unauthenticated_remote_approvals` exception permits a configured
privileged ID to review pending metadata from the remote GUI during isolated
testing. It requires `security.enabled: true` but deliberately requires neither
an application password, `--allow-host`, nor a timeout. It does not consult
BU-security rows, exposes only pending meanings for the active source, and
records the reviewer as unverified. This is not authentication:
anyone reaching the page can type the same ID. Question-log/answer-quality
review, site-memory facts, decision history, and `/console` remain
machine-local; ordinary database Diagnostics retain their existing signed-in
policy. See the exact setup and shutdown steps in
[SETUP.md](SETUP.md#temporary-passwordless-metadata-approval-from-a-remote-browser).

The overlay is bound to the canonical source, secret-free endpoint/schema
fingerprint, and exact catalog object ID. It is not merged into the structural
artifact and cannot change catalog confidence, columns, keys, native foreign
keys, view dependencies, SQL, policy, status semantics or row values.
Pending/rejected/revoked proposals have zero retrieval effect. `join_path`
continues to traverse only declared native FK and view-dependency evidence.
An active exclusion is removed before semantic ranking and is enforced again
before table browsing, description, profiling, comparison, join planning,
optimizer planning, guarded SQL, and streamed exports. Existing approved
wording that explicitly says “do not use” is read as an exclusion; “staging”
alone remains descriptive rather than prohibitive.

### Minimum read-only access

The build uses only catalog reads and `SELECT`/PRAGMA operations. Ask the DBA
for the narrowest metadata visibility that covers the intended database and
schema:

- Oracle: visibility of objects, columns, indexes, constraints and dependencies
  through `USER_*`, or `ALL_*` for every explicitly configured owner. Object
  visibility still follows the privileges of the build account; privileges do
  not add an owner to the artifact or live-query allowlist.
- SQL Server: `CONNECT` plus `VIEW DEFINITION` on the intended database or
  schema. Do not grant server-wide metadata visibility merely for this build.
- SQLite: read access to the selected database file.

For the PeopleTools overlay, the useful grants are read-only `SELECT` on:

```text
PSRECDEFN       PSRECFIELD       PSDBFIELD       PSDBFLDLABL
PSXLATITEM      PSPNLFIELD       PSQRYDEFN       PSQRYRECORD
```

`PSRECDEFN` and `PSRECFIELD` provide the core logical-record map. The other
layers add field definitions and labels, effective-dated translate values,
page use and **public** PSQuery use. If an optional table or shape is not
present, the builder records that layer as unavailable/inconclusive instead of
pretending the metadata does not exist; this alone does not mark the whole
snapshot partial. An actual read error or configured cap does. If public
visibility cannot be proven from the local PSQuery shape, saved-query
relationships are skipped so private query names cannot leak.

## What the catalog indexes

For its one database source, each catalog stores:

- tables and views, with source and schema;
- columns with ordinal, data type, length and nullability;
- indexes with ordered key columns, uniqueness, and a note for expression or
  filtered indexes when available;
- native primary-key, unique and foreign-key constraints with ordered local and
  referenced columns, enable/trust or validation status when the database
  exposes it, and delete/update rules when available;
- native view-to-table/view dependencies on Oracle and SQL Server.

The same file is both the semantic catalog and the relationship graph.
Secondary-source `join_path` performs a bounded shortest-path traversal in
that file using only native foreign keys and view dependencies. Foreign-key
steps include the literal ordered local/referenced column pairs. A composite
key whose collection was truncated remains visible as inconclusive structure,
but never emits a partial `ON` clause or a queryable join skeleton. Matching
column names alone are never promoted to a relationship.

Foreign-key and view targets are resolved only when the exact source/schema
object was observed in the same build. A catalog reference to an object outside
that scope remains an `external_object` with `resolution_status: unresolved`;
it is never silently dropped or promoted to a live table. Cross-database,
SQL Server linked-server and Oracle database-link names remain structural
reference attributes, not a guessed mapping to another configured source.
Two allowed schemas in one source can therefore produce a confirmed
cross-schema edge, while the same reference to Finance or any unlisted owner
cannot become a traversable relationship.

SQLite exposes primary, unique and foreign keys through structured PRAGMAs but
has no structured dependency catalog. The build therefore records SQLite view
lineage as `unavailable`; it does not parse or retain `sqlite_master.sql` merely
to produce a speculative edge.

For the selected PeopleTools source it can add:

- logical records and declared `SQLTABLENAME` values;
- record fields, descriptions, labels and effective-dated translate values;
- pages that reference a record;
- public saved queries that reference a record;
- explainable logical-to-physical mappings.

It does not manufacture a `PS_` or company prefix. A declared physical name is
kept even when it cannot be observed. Otherwise the mapper tries exact catalog
identity and then a unique catalog suffix, disclosing how it reached the
candidate.

Search uses local SQLite FTS5 when available and a deterministic substring
fallback otherwise. There are no embeddings, vector database, external
semantic-index service, or bulk metadata call to Gemini.

For a non-PeopleSoft source, the initial semantic vocabulary comes from native
object/column names and structural keys/view lineage. Oracle table/column
comments and SQL Server `MS_Description` values are not harvested in this
slice. The catalog does not ask a model to invent missing business meanings;
use reviewed site vocabulary when names alone are insufficient.

## Gemini 2.5 Pro workflow

For an unfamiliar delivered or custom concept, the intended sequence is:

```text
search_metadata(query="Phoenix interface", source="p2go")
    -> ranked structural candidates with source, confidence and provenance

get_metadata_context(identifier="ACME_TXN_HDR", source="p2go")
    -> logical/physical mapping, columns, declared keys, foreign-key targets,
       native view dependencies, ordered indexes, labels and codes

join_path(from_record="ACME_TXN_HDR", to_record="ACME_TXN_LINE", source="p2go")
    -> shortest native-FK/view-dependency path from P2Go's own artifact

describe_table / explain_query
    -> live shape and query-plan evidence from exactly P2Go

guarded run_sql
    -> the bounded live P2Go answer
```

Gemini 2.5 Pro is prompted to use this discovery chain before querying an
unfamiliar object. It receives only the bounded search/context results, not the
whole artifact. A secondary source workspace intentionally has no PeopleSoft
`search_records`, row profiler, comparison sampler, wiki, memory or curated
financial tools; if its catalog is missing or several candidates remain
plausible, it reports that limitation rather than borrowing another source or
sampling rows to guess. The `/finance` workspace retains its PeopleTools and
curated-tool discovery behavior.

The final live call must still apply every scope/filter the selected source
can actually prove. Finance includes the caller's authorized business unit,
date, status, ledger and currency basis; a generic source must not invent those
PeopleSoft dimensions. Metadata tools are structural only and cannot satisfy
a row/count/amount or financial-evidence claim.

## Confidence, relevance and provenance

Confidence is categorical and explainable. No model assigns a probability:

| Tier | Meaning |
|---|---|
| `confirmed` | Directly observed database structure, a declared PeopleTools relationship, a declared `SQLTABLENAME` matched to the expected live object type, or exact logical/physical catalog identity. |
| `corroborated` | A unique live-catalog suffix resolved the logical record when no exact or declared mapping did; the catalog was complete enough to establish uniqueness. No prefix was assumed. |
| `candidate` | A declared or unique-suffix name exists but is not visible as the expected live object type. It is a lead to verify, not a proven mapping. |
| `inconclusive` | The name is ambiguous across schemas, the relevant catalog layer is partial, a referenced constraint/dependency target was not observed in scope, the record is non-SQL, or no defensible physical mapping exists. |

`search_metadata` also returns an integer relevance score, term coverage,
matched facets and matched metadata. Relevance says why the words matched;
confidence says how strongly the metadata connects the logical concept to the
physical object. Neither proves that a future financial query is correct.

`get_metadata_context` returns bounded candidates instead of silently choosing
when a name exists in multiple schemas within the selected source. Pass the
returned source and qualified physical object to live tools exactly as
reported. Every metadata tool result also carries top-level
`source_database`; a source-silo chat rejects a missing or different value.

### How strongly a join is known

`join_path` walks four classes of relationship, and it does not mix them.
Each tier is exhausted before the next is consulted, because shortest-path
is the right tie-break only *within* a tier — across tiers it trades a
guarantee for a hop:

| Tier | Class | What it is | Compiles to join SQL |
|---|---|---|:-:|
| 0 | `foreign_key`, `view_dependency`, `same_object` | The database itself holds to it | yes (`foreign_key`) |
| 1 | `view_declared_join` | A person wrote the join in a view definition; nothing enforces it | no |
| 2 | `value_overlap` | Only measurement suggests it: sampled values line up | no |

Only an enforced key becomes join SQL. A tier-1 or tier-2 hop is reported
with its evidence and a caveat naming what it is not — a view author's
assertion is strong evidence of intent and no guarantee of integrity, and a
containment measurement is evidence of a reference and no evidence of intent.

Tier 1 comes from view definitions, which are read and discarded. A view is
often the only place a cryptically named schema writes down its own meaning,
so two things are extracted and nothing else: join predicates between two
qualified columns, and column aliases (`C1 AS INVOICE_NUMBER` becomes a
searchable term on that column). A column needs no table prefix when
exactly one object is in scope — a view over a single table, renaming its
columns, is the commonest shape there is and the one most worth reading —
but with two or more sources a bare column is genuinely ambiguous and is
refused. Expressions never name a column, because
`SUM(C4) AS TOTAL_DUE` describes a computation and calling it a name for
`C4` would be false. Both column names in a harvested join must exist on the
objects named — on Oracle a long definition arrives through a `VARCHAR2`
projection and is truncated, and a predicate cut mid-identifier would
otherwise mint an edge on a column that does not exist. A composite join is
one edge carrying every column of the condition; where a predicate had to be
dropped, the edge says the condition is incomplete rather than presenting the
survivors as the whole join.

Literals and comments are removed by a single left-to-right scanner, not by
ordered substitutions, because no ordering of independent substitutions is
correct: strip comments first and a string containing `--` swallows real
code; strip strings first and a quote inside a comment desynchronises
everything after it. The scanner tracks which construct it is inside, so it
also handles Oracle's alternative quoting (`q'[ ... ]'`, whose contents may
include apostrophes) and passes double-quoted identifiers through verbatim —
`"odd--name"` is code, not a comment. An unterminated literal or comment
yields **nothing at all** from that definition: past it, text cannot be told
from code, and guessing is precisely how a value becomes a join operand.

## Configuration and limits

The shipped settings are:

```yaml
metadata_catalog:
  max_objects: 100000
  max_fields: 500000
  max_indexes: 250000
  max_constraints: 250000
  max_constraint_columns: 1000000
  max_dependencies: 250000
  max_peopletools_rows: 500000
  query_page_size: 5000
  stale_after_hours: 168
  harvest_view_vocabulary: true
  max_view_definitions: 5000
```

Their exact scope is:

| Setting | Default | Scope |
|---|---:|---|
| `max_objects` | 100,000 | physical tables/views per selected source |
| `max_fields` | 500,000 | physical columns per selected source |
| `max_indexes` | 250,000 | index definitions per selected source |
| `max_constraints` | 250,000 | primary/unique/foreign-key definitions per selected source |
| `max_constraint_columns` | 1,000,000 | ordered column memberships across native constraints per selected source |
| `max_dependencies` | 250,000 | view-to-object dependency edges per selected source |
| `max_peopletools_rows` | 500,000 | rows per PeopleTools layer |
| `query_page_size` | 5,000 | rows in one keyset page |
| `stale_after_hours` | 168 | age after which readers disclose the snapshot as stale |
| `harvest_view_vocabulary` | true | read view definitions for declared joins and column vocabulary |
| `max_view_definitions` | 5,000 | view definitions read per selected source; a memory budget as much as a work budget, since an Oracle definition arrives as up to 4,000 characters. When the cap binds, the snapshot says so and is marked partial. |

One-build overrides are available as `--max-objects`, `--max-fields`,
`--max-indexes`, `--max-constraints`, `--max-constraint-columns`,
`--max-dependencies`, `--max-peopletools-rows`, and `--page-size`. Put durable
site limits in `config.yaml`; use command-line overrides only for one measured
rebuild. The
defensive hard ceilings are 1,000,000 objects, 5,000,000 fields, 2,000,000
indexes, 2,000,000 constraints, 5,000,000 constraint-column memberships,
2,000,000 dependency edges, 5,000,000 PeopleTools rows per layer, and 25,000
rows per page. Raise a normal limit only
after `describe_metadata_catalog` identifies the exact layer that was
truncated; do not widen sources or schemas merely to make an absence go away.

Search results default to 20 and context to 40. Both are bounded to at most
100 items per call, independently of build size.

## Partial, stale and unavailable snapshots

Run `describe_metadata_catalog(source="...")` before relying on an absence. It
reports that source's snapshot time and age, layer status, configured limit
hits, collector notes, search mode and schema version.

- **Partial:** a source or layer failed, a configured harvest limit was
  reached, or a configured schema returned zero TABLE/VIEW objects. Available
  facts remain searchable, but a missing object or relationship is
  inconclusive. Inspect `schema_coverage.missing` as well as collector notes.
- **Stale:** the artifact is older than `stale_after_hours`. It remains
  readable and every result discloses the age; rebuild it before treating it
  as a picture of the current customization.
- **Unavailable, incompatible, or mismatched:** search/context return the exact
  source-specific rebuild command. Source databases are not queried as an
  implicit fallback, and an artifact built for another source is never opened.
- **Layer unavailable:** `notes[].status` says `unavailable` when a platform
  has no structured layer (notably SQLite view dependencies) or an optional
  privacy-safe PeopleTools shape is absent. Other complete layers remain
  usable and the unsupported layer alone does not make them partial.
- **Failed rebuild:** the catalog's temporary `.building` file is removed and
  the prior artifact, if any, remains readable. The adjacent status remains
  non-published (`failed`, or `building` if the final status update itself could
  not be persisted). A successful partial rebuild can replace the catalog
  because its coverage gaps are explicit; a completely empty harvest cannot.

This behavior is intentional: an older, labelled snapshot is safer than
destroying known-good discovery data during a transient grant or connection
failure.

## Security boundary

The artifact excludes transaction rows, balances, customer and supplier
values, credentials, operator grants and IDs, private query names, check
expressions, and full view SQL. It stores only the structural names, ordered
key columns, reference status and relationships needed for discovery and
explainable mapping.

Because the artifact has no business rows, it does not carry PeopleSoft
business-unit row security. That is not permission to scan all units: after
metadata selection, every live tool must apply the caller's authorized scope.
Keep the artifact and host restricted to the same administrators who may see
the selected schemas, and build only from intended sources.

## Current limitations

- Native primary/unique/foreign keys are collected, but database check/default
  expressions, triggers, grants and full definitions are intentionally not.
  Native Oracle comments and SQL Server extended descriptions, plus
  PeopleTools logical index/key definitions, are later layers. Secondary
  `join_path` uses the isolated native-edge graph; use `explain_query` for
  live, bounded plan verification.
- Oracle and SQL Server expose native view dependency catalogs. SQLite does
  not, so SQLite view lineage is explicitly unavailable rather than inferred
  from stored SQL. Dynamic SQL and dependencies the database itself cannot
  resolve remain explicit gaps.
- Search is lexical and explainable; there are no embeddings or semantic
  reranker. Add approved site-memory facts when the business meaning is absent
  from every name, description, label, translate value, page and public query.
- Quoted identifiers that differ only by case are unsupported. Names are
  normalized for cross-dialect matching, so such objects cannot be represented
  as distinct safe candidates in this first slice.
- The catalog is a scheduled snapshot, not change-data capture. It cannot show
  whether a table is populated today or prove a balance, status population, or
  financial conclusion.
- Application Engine, Process Scheduler, Integration Broker and end-to-end
  navigation are not part of this catalog slice. The separate process graph and
  `trace_process` tool cover the currently supported workflow relationships.
