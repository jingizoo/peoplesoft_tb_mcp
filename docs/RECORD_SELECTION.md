# Choosing the right record

`search_records` finds candidates by name and PeopleTools description. That is
often not enough to pick between them.

PeopleSoft ships many records with near-identical names, and every site adds
more. A question about open invoices might plausibly mean `PS_ITEM`,
`PS_BI_HDR`, a history shell, a staging table, or a custom `PS_XX_*` record.
Names cannot separate those. Contents can.

`profile_record` and `compare_records` supply the contents.

## The sequence

```
search_records("open invoice")   ->  candidates, by description
compare_records([...])           ->  which are readable, populated, and shaped right
run_sql(...)                     ->  the actual question
```

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

Profiling answers "which of these candidate tables fits". It cannot answer
"which table is it" when the table is client-specific: no PeopleTools
description, a name that encodes nothing, no wiki page. `search_records("interface
file")` returns nothing, and no improvement to retrieval changes that — the
information was never written anywhere the agent can read.

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
