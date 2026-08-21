# Choosing the right record

`search_metadata` is the preferred first step for an unfamiliar delivered or
custom concept inside the active database workspace. Each workspace opens only
its own offline artifact. `/finance` adds PeopleTools logical records, fields,
labels, translate values, page use and public saved-query use; a generic
workspace such as `/p2go` contains that database's native objects, columns,
keys, indexes and view dependencies.

`search_records` remains the live PeopleTools fallback when that artifact has
not been built, and it includes approved site-memory facts. A name or
description match alone is often not enough to pick between candidates.

PeopleSoft ships many records with near-identical names, and every site adds
more. A question about open invoices might plausibly mean `PS_ITEM`,
`PS_BI_HDR`, a history shell, a staging table, or a custom `PS_XX_*` record.
Names cannot separate those. Contents can.

`get_metadata_context` supplies explainable structural context;
`profile_record` and `compare_records` supply live population evidence.

## The sequence

In `/finance` the full discrimination sequence is:

```
search_metadata(query="open invoice", source="default")
get_metadata_context(identifier="ACME_ITEM", source="default")
profile_record(table="ACME_ITEM", source="default")
compare_records(tables=[...], source="default")
curated tool or run_sql(...)
```

In a generic source silo the sequence remains inside that source:

```
search_metadata(query="failed interface", source="p2go")
get_metadata_context(identifier="JOB_HDR", source="p2go")
join_path(from_record="JOB_HDR", to_record="JOB_LINE", source="p2go")
describe_table(table="JOB_HDR", source="p2go")
explain_query(...) / run_sql(...)                 # when raw SQL is enabled
```

On Gemini 2.5 Pro this sequence is part of the tool-routing prompt. The model
must use the physical object and source returned by the active artifact; it
must not add `PS_` or guess a company prefix. If an identifier is ambiguous
across schemas within that source, `get_metadata_context` returns candidates
rather than choosing by sort order. To inspect another database, switch
workspaces and run a separate search; one turn never merges source catalogs.

Catalog confidence is separate from search relevance:

- `confirmed` is observed or directly declared metadata;
- `corroborated` is a unique live-catalog suffix match with no assumed prefix;
- `candidate` is a useful but unverified declared/type-mismatched lead;
- `inconclusive` means ambiguity, partial coverage or no defensible mapping.

A stale or partial snapshot remains useful for positive matches, but a miss is
not evidence that a record does not exist. Check `describe_metadata_catalog`
and see [METADATA_CATALOG.md](METADATA_CATALOG.md) for the complete contract.

## What a profile reports

| field | what it tells you |
|---|---|
| `columns`, `column_count` | the real shape on **this** instance |
| `fill_percent` | how many sampled rows populate each column |
| `always_null` | columns this site never populates, whatever the catalog claims |
| `value_counts` | the actual codes in status/type columns |
| `sample` | a few real rows, masked |
| `populated` | `false` means the record is readable but empty |

`value_counts` is the strongest signal. Knowing `ITEM_STATUS` really holds `O`
and `C` on this instance turns a guessed predicate into a correct one — and
guessing `ITEM_STATUS = 'OPEN'` is exactly the error that returns zero rows and
gets narrated as "you have no open items".

`always_null` matters nearly as much. A column the catalog defines but the site
never populates will silently produce empty results forever, and no amount of
SQL skill finds that from the outside.

An empty record is rarely what a question means, however well its name matches.
`compare_records` separates candidates into `readable_and_populated` and
`empty_or_unreadable` for that reason.

## Masking

The offline metadata catalog stores and returns structure only, so its search
and context calls contain no sampled source rows. In `/finance`, the first
row-bearing step may be `profile_record` or `compare_records`; those tools are
not exposed in a generic source silo, whose first row-bearing step is a guarded
`run_sql` when raw SQL is enabled.

Sample rows leave this process and reach whichever model is configured. On the
Gemini provider that means they leave your network.

So values are masked before they go anywhere:

```
"ACME Industrial"  ->  "A************** (15 chars)"
```

Shape survives, the value does not. Shape is kept deliberately — a 15-character
name column and a 3-character code column are different findings, and the model
needs to tell them apart.

**Masked**: names, contacts, addresses, cities, postcodes, phone, fax, email,
national and tax identifiers, dates of birth, bank and card details, passwords
and tokens, salary and pay rates. Numbered variants are covered, so `ADDRESS1`
through `ADDRESS4` mask exactly as `ADDRESS` does.

**Not masked**: business units, SetIDs, ledgers, accounts, departments,
customer and vendor IDs, item and journal IDs, currency codes, fiscal years and
periods, status flags, dates and amounts. These identify records rather than
people, and masking them would defeat the point of the feature.

To send no rows at all, set `tools.sample_rows: 0` in `config.yaml`. Shape,
fill rates and status-code counts still come back, so record selection keeps
working — only the row sample is withheld.

## Cost

Profiling reads at most 50 rows per record, always with `ROWNUM` / `LIMIT` /
`TOP` applied, and `compare_records` accepts at most 6 candidates. It never
scans a record, so it is safe to point at a table with a hundred million rows.
`fill_percent` and `value_counts` are measured over the rows sampled — they are
evidence about shape, and must never be reported as totals.


## Records nobody could have guessed

The metadata catalog materially widens discovery: a client-specific physical
name can be found through a logical record, field label, translate label, page
or public saved query even when the name itself is opaque. Profiling then
answers "which of these candidate tables fits".

It still cannot recover meaning that was never recorded anywhere. A table with
no useful name, PeopleTools description, field/label/code wording, page,
public-query use or wiki documentation gives lexical search nothing to match.
In that case `search_metadata("interface file")` and
`search_records("interface file")` return nothing; embeddings would only make
an unsupported guess sound more plausible.

What exists instead is somebody saying it out loud, once:

> "the interface file info is in TU_FILE_INTFC"

`remember_record_fact(table="TU_FILE_INTFC", fact="holds inbound interface
file headers and load status")` proposes it for review. After an operator
approves the proposal:

- `search_records("interface file")` returns it, **ranked first** — a human
  naming a table for this purpose outranks a substring match on a name
- so does `search_records("load status")` — matching is on the explanation, not
  on the words the question happened to use
- `describe_record` carries a `taught_here` block
- `what_do_we_know_about(table)` reports it directly

### Why approval comes before use

An unreviewed claim must not shape either a conclusion or the choice of table
used to reach that conclusion. Pending record facts therefore have **zero**
effect on `search_records`, `describe_record`, prompts, joins, or queries. The
memory CLI records an explicit approved/rejected decision before retrieval can
use the pointer.

The boundary that does hold: a taught fact is a **pointer, never authority**.
Every surface that shows one says so, and the columns always come from the
catalog. Reject a pending proposal—or remove an obsolete approved one—with
`python -m pstb.memory`; it then has no retrieval effect.

## Records that must not be used

Approval answers "is this statement correct?"; it does not always mean
"promote this table". In **Metadata meanings**, choose one of two explicit
selection effects:

- **Prefer for matching questions** adds governed business vocabulary.
- **Exclude from answers** creates a hard veto for a junk, obsolete, duplicate,
  scratch, or non-reporting staging object.

After an exclusion is approved, the object is removed from `search_metadata`,
`search_records`, and `list_tables`; omitted from `compare_records` and join
paths; and refused before `describe_record`, `describe_table`,
`profile_record`, `explain_query`, or `run_sql` can inspect it. The tool result
names the operator exclusion instead of letting the model silently choose the
next similar name. The same check covers interactive results and large CSV
exports.

Existing approved lessons beginning with an explicit instruction such as
"don't use", "do not query", or "never select" are recognized as exclusions
automatically. The word **staging by itself is not an exclusion**: an interface
staging table may be exactly the record an operational question needs.

Enforcement reads a private, source-bound local index cached by the sidecar's
file signature. It adds no Oracle query and cannot cross from one configured
database workspace into another. A local operator can use **Restore as a
candidate** in the review history (or revoke the proposal with
`python -m pstb.source_knowledge --source <name> --revoke <id>`) when the veto
is no longer valid.

## Planning a join before running it

Large-table queries on a real instance time out rather than erroring, and a
timeout teaches nothing. Three mechanisms turn "it hung" into "here is what to
change":

**The index catalog.** `get_metadata_context` carries the snapshot's indexes,
and `describe_table` returns the live catalog; both keep columns IN ORDER,
because order is the whole game: the optimizer can only use an index whose
leading columns appear in your predicates. A table with no readable index says
so plainly — every query there is a full scan and no rewrite changes that.
Metadata schema version 2 collects native PK/FK and view-dependency edges.
`join_path` may present those declared relationships with their exact columns;
an ordered index or a same-named column alone is still not a declared join.

**`explain_query`.** Asks the optimizer how it WOULD run a SELECT, without
executing it. Returns the plan, each referenced table's rows and indexes, and
concrete advice: which full scans are planned and which index's leading
columns the WHERE/JOIN must include to avoid them. The agent is instructed to
call it before any join over large tables and immediately after any timeout —
rewrite, re-explain, then run. The cost-gate refusal for unfiltered scans now
names the available indexes too, so the moment the model needs the catalog is
the moment it has it.

**Adaptive integrity probes.** The two journal checks in `tb_integrity_check`
pre-flight their plan. A full-year scan over a big instance gets narrowed to
the current period — disclosed in the check's headline, never silently — and
when even the narrowed plan would scan everything, the check refuses in
milliseconds with the index that would restore it, instead of burning two
minutes into a timeout that takes the whole playbook down. The balance verdict
is never affected; a narrowed or refused control check cannot impersonate a
full one.
