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
to `default` unless told otherwise. Useful scoped builds are:

`--source` REPLACES the whole artifact — it is not an incremental refresh.
A build naming one source produces a catalog containing only that source, and
because the write is atomic the result looks complete. Name every source you
want indexed, or omit the flag entirely (the normal case, and what cron uses).

```bash
# Only the PeopleSoft primary and a named warehouse — anything else
# configured is DROPPED from the rebuilt catalog
.venv/bin/python scripts/build_metadata_catalog.py --source default,warehouse

# PeopleTools is on a different selected source
.venv/bin/python scripts/build_metadata_catalog.py \
  --source default,psft --peopletools-source psft

# Build native database structure only
.venv/bin/python scripts/build_metadata_catalog.py --peopletools-source none

# Put the artifact in a separately protected location
.venv/bin/python scripts/build_metadata_catalog.py --out /secure/path/metadata_catalog.db
```

Without `--out`, the artifact is `metadata_catalog.db` beside the active
configuration. It is git-ignored and written with owner-only permissions
(`0600`). The builder writes `metadata_catalog.db.building` first and atomically
replaces the readable artifact only after a usable snapshot commits. A failed
or empty rebuild does not destroy the last good catalog.

Build once during deployment and after a PeopleSoft customization, schema,
key/constraint, index, view, field-label, translate-value or saved-query
change. Catalog schema version 2 adds native keys and view lineage, so an
artifact built by the first release must be rebuilt. A weekly rebuild is
a reasonable baseline for stable production metadata; schedule it nightly when
warehouses or custom integration schemas change frequently. The default
freshness target is seven days (`168` hours), so choose a cadence at least as
frequent as that target.

## Source and schema scope

Source names are a hard namespace boundary. The same table name in `default`
and `warehouse` remains two different objects, and an ambiguous lookup asks for
`source=` instead of choosing one. Add intended secondary databases under
`sources:` in `config.yaml`, for example:

```yaml
db:
  backend: oracle
  schema: SYSADM

sources:
  warehouse:
    backend: oracle
    schema: FIN_DW
```

Keep passwords in `.env`; they are not copied into the catalog. Per-source
Oracle credentials use `PSTB_SRC_<SOURCE>_DSN`,
`PSTB_SRC_<SOURCE>_USER`, and `PSTB_SRC_<SOURCE>_PASSWORD`.

Schema behavior is intentionally bounded:

- **Oracle:** when `db.schema` is set, the collector uses owner-filtered
  `ALL_*` catalog views. When it is blank, it uses `USER_*`. It never performs
  an unowned crawl of everything visible to the account.
- **SQL Server:** objects are joined to `sys.schemas`. A configured schema is
  matched case-insensitively; with no configured schema, every schema visible
  in that configured database is collected and kept distinct.
- **SQLite:** the collector reads the selected database's `sqlite_master` and
  PRAGMA metadata under schema `MAIN`.

Select sources deliberately. Catalog structure can itself reveal custom object
and field names even though it contains no business rows.

### Minimum read-only access

The build uses only catalog reads and `SELECT`/PRAGMA operations. Ask the DBA
for the narrowest metadata visibility that covers the intended database and
schema:

- Oracle: visibility of objects, columns, indexes, constraints and dependencies
  through `USER_*`, or `ALL_*` for the single configured owner. Object
  visibility still follows the privileges of the build account.
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

For every selected database source, the catalog stores:

- tables and views, with source and schema;
- columns with ordinal, data type, length and nullability;
- indexes with ordered key columns, uniqueness, and a note for expression or
  filtered indexes when available;
- native primary-key, unique and foreign-key constraints with ordered local and
  referenced columns, enable/trust or validation status when the database
  exposes it, and delete/update rules when available;
- native view-to-table/view dependencies on Oracle and SQL Server.

Foreign-key and view targets are resolved only when the exact source/schema
object was observed in the same build. A catalog reference to an object outside
that scope remains an `external_object` with `resolution_status: unresolved`;
it is never silently dropped or promoted to a live table. Cross-database,
SQL Server linked-server and Oracle database-link names remain structural
reference attributes, not a guessed mapping to another configured source.

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

## Gemini 2.5 Pro workflow

For an unfamiliar delivered or custom concept, the intended sequence is:

```text
search_metadata(query="Phoenix interface")
    -> ranked structural candidates with source, confidence and provenance

get_metadata_context(identifier="ACME_TXN_HDR", source="default")
    -> logical/physical mapping, columns, declared keys, foreign-key targets,
       native view dependencies, ordered indexes, labels and codes

profile_record(table="ACME_TXN_HDR", source="default")
    or describe_table / compare_records / explain_query / join_path
    -> live shape, population and query-plan evidence

curated financial tool or guarded run_sql
    -> the scoped, dated business answer
```

Gemini 2.5 Pro is prompted to use this discovery chain before querying an
unfamiliar object. It receives only the bounded search/context results, not the
whole artifact. If the catalog is unavailable it can fall back to
`search_records`; when several live candidates remain plausible it should use
`compare_records` rather than guess.

The final live call must still apply the caller's authorized business-unit
scope and the question's date, status, ledger and currency basis. The metadata
tools are classified as structural tools and cannot satisfy the financial
evidence gate.

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
when a name exists in multiple sources or schemas. Pass the returned source and
qualified physical object to live tools exactly as reported.

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

Run `describe_metadata_catalog` before relying on an absence. It reports the
snapshot time and age, selected sources, source/layer status, configured limit
hits, collector notes, search mode and schema version.

- **Partial:** a source or layer failed, or a configured harvest limit was
  reached. Available facts remain searchable, but a missing object or
  relationship is inconclusive.
- **Stale:** the artifact is older than `stale_after_hours`. It remains
  readable and every result discloses the age; rebuild it before treating it
  as a picture of the current customization.
- **Unavailable or incompatible:** search/context return the exact rebuild
  command. Source databases are not queried as an implicit fallback.
- **Layer unavailable:** `notes[].status` says `unavailable` when a platform
  has no structured layer (notably SQLite view dependencies) or an optional
  privacy-safe PeopleTools shape is absent. Other complete layers remain
  usable and the unsupported layer alone does not make them partial.
- **Failed rebuild:** the `.building` file is removed and the prior artifact,
  if any, remains readable. A successful partial rebuild can replace it because
  its coverage gaps are explicit; a completely empty harvest cannot.

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
  PeopleTools logical index/key definitions are a later layer. Use `join_path`
  and `explain_query` for live, bounded join-plan verification.
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
