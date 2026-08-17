# Choosing the right record

`search_metadata` is the preferred first step for an unfamiliar delivered,
custom or cross-database concept. It searches an offline snapshot of physical
objects, PeopleTools logical records, fields, labels, translate values, page
use and public saved-query use, while preserving source and schema identity.

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

```
search_metadata(query="open invoice")
    -> candidates across configured sources
get_metadata_context(identifier="ACME_ITEM", source="default")
    -> mapping, columns, indexes, labels/codes
profile_record(table="ACME_ITEM", source="default")
    -> live shape and population
compare_records(tables=[...], source="default")
    -> choose if several remain plausible
curated tool or run_sql(...)
    -> the scoped business question
```

On Gemini 2.5 Pro this sequence is part of the tool-routing prompt. The model
must use the physical object and source returned by the catalog; it must not add
`PS_` or guess a company prefix. If an identifier is ambiguous across sources
or schemas, `get_metadata_context` returns candidates rather than choosing by
sort order.

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
and context calls contain no sampled source rows. The first row-bearing step is
`profile_record` or `compare_records`.

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
file headers and load status")` keeps it. Afterwards:

- `search_records("interface file")` returns it, **ranked first** — a human
  naming a table for this purpose outranks a substring match on a name
- so does `search_records("load status")` — matching is on the explanation, not
  on the words the question happened to use
- `describe_record` carries a `taught_here` block
- `what_do_we_know_about(table)` reports it directly

### Why these are usable before approval

Site memory normally requires human approval before a fact enters prompt
context, because an unreviewed claim must not silently shape a conclusion.
Record facts do a different job and get a different rule: they help *find* a
record, and whatever is found is then read from the live catalog anyway. A
wrong one makes discovery worse; it cannot make a number wrong. Requiring a CLI
round trip before the agent can act on something the user just said is friction
with nothing on the other side of it.

The boundary that does hold: a taught fact is a **pointer, never authority**.
Every surface that shows one says so, and the columns always come from the
catalog. Reject one with `python -m pstb.memory` and it stops being used.

## Planning a join before running it

Large-table queries on a real instance time out rather than erroring, and a
timeout teaches nothing. Three mechanisms turn "it hung" into "here is what to
change":

**The index catalog.** `get_metadata_context` carries the snapshot's indexes,
and `describe_table` returns the live catalog; both keep columns IN ORDER,
because order is the whole game: the optimizer can only use an index whose
leading columns appear in your predicates. A table with no readable index says
so plainly — every query there is a full scan and no rewrite changes that.
The first metadata-catalog schema does not collect PK/FK or dependency edges,
so an ordered index must not be presented as a declared relationship.

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
