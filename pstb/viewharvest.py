"""What a view's author knew, taken from its shape and not its text.

A view is the one place in a badly named schema where somebody wrote
down what they meant. `CREATE VIEW OPEN_INVOICES AS SELECT A.C1 AS
INVOICE_NUMBER ... FROM TU_X7 A JOIN TU_Q2 B ON A.C2 = B.C1` carries two
things no data dictionary holds: a JOIN a human asserted, and a
vocabulary mapping C1 to "invoice number". Both are exactly what the
custom schemas refuse to declare anywhere else.

The catalog has always refused to fetch or store view SQL, and that rule
stands. A definition can embed business rules, literal thresholds, and
comments nobody audited; storing it would put all of that into an
artifact that outlives the connection and travels to a model. So this
module reads a definition, extracts STRUCTURE, and keeps only that:

  * join predicates as column-name pairs, and
  * column aliases as name-to-name mappings.

Never a literal, never a WHERE clause, never a fragment of the text.
Literals are stripped before anything is matched, so a value cannot
reach an extractor even by accident.

Confidence follows the same doctrine as the value-overlap miner. A
view-declared join is INTENT -- a person wrote it -- which makes it
stronger evidence than measured containment and weaker than a foreign
key the database itself enforces. It sits between them, labeled, and it
never becomes a constraint.

Extraction is deliberately narrow. A predicate touching anything but two
qualified columns is skipped; an alias over anything but a plain column
reference is skipped. A wrong "these columns join" is worse than
silence, and silence is always available.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping


def normalize_bigquery_view_sql(text: str, project: str, dataset: str) -> str:
    """Make real BigQuery view text readable by this module's regexes.

    Real view definitions arrive backticked and project-qualified
    (`proj.ds.t`); the harvest's identifier grammar refuses backticks by
    doctrine and its alias map accepts at most schema.object. Two
    contained rewrites, nothing else:

    * strip backticks around identifier paths -- `proj.ds.t` becomes
      proj.ds.t, `ident` becomes ident;
    * collapse the CONFIGURED project's prefix so its references take
      the dataset.table shape the regexes parse.

    References to OTHER projects keep their three-part form, fail node
    resolution downstream, and drop -- silence over wrong edges, this
    module's own doctrine.
    """
    cleaned = re.sub(
        r"`([A-Za-z0-9_$#.\-]+)`", lambda m: m.group(1), str(text or ""))
    prefix = str(project or "").strip()
    if prefix:
        cleaned = re.sub(
            re.escape(prefix) + r"\.(?=[A-Za-z0-9_])", "", cleaned)
    _ = dataset  # reserved for a future dataset-collapse decision
    return cleaned

# Identifiers Oracle and friends actually allow, unquoted or quoted.
_IDENT = r'(?:"[^"]{1,128}"|[A-Za-z][A-Za-z0-9_$#]{0,127})'
_QUALIFIED = rf"(?P<a>{_IDENT})\s*\.\s*(?P<b>{_IDENT})"

_PLACEHOLDER = " ~L~ "
# Oracle's alternative quoting: q'[ ... ]' and friends. The delimiter is
# whatever follows the quote, and four of them are paired.
_Q_CLOSERS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$#]")
_SET_QUANTIFIER = re.compile(r"^\s*(?:DISTINCT|UNIQUE|ALL)\s+", re.I)
# Tokens that parse as a bare identifier and are not a column of anything.
# `NULL AS DISCOUNT_AMT` pads a UNION arm in a great deal of real view SQL;
# read as a column it asserts that the table has one called NULL.
_NOT_A_COLUMN = frozenset({
    "NULL", "SYSDATE", "SYSTIMESTAMP", "CURRENT_DATE", "CURRENT_TIMESTAMP",
    "USER", "UID", "ROWNUM", "ROWID", "LEVEL", "DUAL", "TRUE", "FALSE",
})

# Words that may never be read as a table alias: the parser walks a FROM
# clause token-wise, and treating a keyword as an alias would attach a
# join to an object that does not exist.
_NOT_AN_ALIAS = frozenset({
    "ON", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER",
    "CROSS", "GROUP", "ORDER", "HAVING", "UNION", "MINUS", "INTERSECT",
    "SELECT", "FROM", "AND", "OR", "AS", "WITH", "CONNECT", "START",
    "PARTITION", "NATURAL", "USING", "LATERAL", "PIVOT", "UNPIVOT",
    "FETCH", "OFFSET", "MODEL", "SAMPLE",
})
# Select-list aliases that carry no meaning worth storing.
_EMPTY_ALIASES = frozenset({
    "ID", "CD", "CODE", "NO", "NBR", "NUM", "VAL", "VALUE", "COL",
    "FIELD", "DATA", "ITEM", "NAME", "TYPE", "STATUS", "FLAG", "X", "Y",
})


def _unquote(name: str) -> str:
    text = str(name or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].strip().upper()
    return text.upper()


def scrub(sql: str) -> tuple:
    """(code with every literal and comment removed, scan_completed).

    A single left-to-right pass, because NO ordering of independent
    substitutions is correct. Comments-first loses a real join to a
    string containing `--`; strings-first lets a quote inside a comment
    swallow the rest of the text. Only a scanner that knows which
    construct it is inside can tell those apart.

    Four constructs are recognised. A double-quoted identifier passes
    through VERBATIM -- Oracle allows `"odd--name"`, and interpreting a
    comment or quote inside one would corrupt code that is not a
    literal. A `--` comment runs to the newline; a `/* */` comment to
    its terminator; a `'...'` string honours `''` escaping; and `q'X..X'`
    honours Oracle's alternative quoting with the four paired
    delimiters. Each removed literal becomes a placeholder rather than
    empty text, so a literal cannot weld two identifiers into one that
    never existed.

    An unterminated construct returns completed=False. There is no
    recovery: the remaining text cannot be classified as code or
    literal, and guessing is exactly how a value gets read as a join
    operand. Callers must extract NOTHING from an incomplete scan.
    """
    text = str(sql or "")
    out: list = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char == '"':
            close = text.find('"', index + 1)
            if close < 0:
                return "".join(out), False
            out.append(text[index:close + 1])
            index = close + 1
            continue
        if char == "-" and text.startswith("--", index):
            newline = text.find("\n", index)
            out.append(" ")
            index = size if newline < 0 else newline
            continue
        if char == "/" and text.startswith("/*", index):
            close = text.find("*/", index + 2)
            if close < 0:
                return "".join(out), False
            out.append(" ")
            index = close + 2
            continue
        if (char in "qQ" and index + 2 < size and text[index + 1] == "'"
                and not (index and _IDENT_CHAR.fullmatch(text[index - 1]))):
            delimiter = text[index + 2]
            closer = _Q_CLOSERS.get(delimiter, delimiter)
            close = text.find(closer + "'", index + 3)
            if close < 0:
                return "".join(out), False
            out.append(_PLACEHOLDER)
            index = close + 2
            continue
        if char == "'":
            cursor = index + 1
            while cursor < size:
                if text[cursor] == "'":
                    if text.startswith("''", cursor):
                        cursor += 2
                        continue
                    break
                cursor += 1
            if cursor >= size:
                return "".join(out), False
            out.append(_PLACEHOLDER)
            index = cursor + 1
            continue
        out.append(char)
        index += 1
    return "".join(out), True


def strip_noise(sql: str) -> str:
    """The scrubbed code alone, for callers that already know it parsed."""
    return scrub(sql)[0]


def table_aliases(sql: str) -> dict:
    """{alias -> (schema, object)} for every plainly named FROM/JOIN source.

    A subquery, a table function, or anything else that is not a bare
    (optionally schema-qualified) name yields nothing: its columns cannot
    be attributed to a catalog object, so a join through it would name
    the wrong table.
    """
    text, complete = scrub(sql)
    if not complete:
        return {}
    out: dict = {}
    pattern = re.compile(
        rf"\b(?:FROM|JOIN)\s+(?:(?P<schema>{_IDENT})\s*\.\s*)?"
        rf"(?P<object>{_IDENT})\s*(?:\bAS\b\s*)?(?P<alias>{_IDENT})?",
        re.I)
    for match in pattern.finditer(text):
        obj = _unquote(match.group("object"))
        schema = _unquote(match.group("schema") or "")
        if not obj or obj in _NOT_AN_ALIAS:
            continue
        alias = _unquote(match.group("alias") or "")
        if alias in _NOT_AN_ALIAS:
            alias = ""
        # An unaliased source is addressable by its own name, which is
        # how unaliased view SQL qualifies its columns.
        out[alias or obj] = (schema, obj)
        out.setdefault(obj, (schema, obj))
    return out


def join_predicates(sql: str, aliases: Mapping | None = None) -> list:
    """Column pairs a human wrote as equal, resolved to real objects.

    Only ``qualified = qualified`` survives. A predicate against a
    literal is a business rule, not a relationship; a predicate against a
    bind or a function is not attributable to a column. Both are skipped,
    and because literals were replaced before matching, neither can be
    read as an operand by accident.
    """
    text, complete = scrub(sql)
    if not complete:
        # An unterminated literal means the rest of the text cannot be
        # told from code. Extracting from it is how a value becomes a
        # join operand, so nothing is extracted.
        return []
    known = dict(aliases) if aliases is not None else table_aliases(sql)
    pattern = re.compile(
        rf"{_QUALIFIED}\s*=\s*(?P<c>{_IDENT})\s*\.\s*(?P<d>{_IDENT})",
        re.I)
    seen: set = set()
    out: list = []
    for match in pattern.finditer(text):
        left_alias = _unquote(match.group("a"))
        left_col = _unquote(match.group("b"))
        right_alias = _unquote(match.group("c"))
        right_col = _unquote(match.group("d"))
        left = known.get(left_alias)
        right = known.get(right_alias)
        if left is None or right is None:
            continue                      # an alias we could not resolve
        if left == right:
            continue                      # a self-join says nothing new
        pair = (left, left_col, right, right_col)
        mirror = (right, right_col, left, left_col)
        if pair in seen or mirror in seen:
            continue
        seen.add(pair)
        out.append({
            "left_schema": left[0], "left_object": left[1],
            "left_column": left_col,
            "right_schema": right[0], "right_object": right[1],
            "right_column": right_col,
        })
    return out


def column_vocabulary(sql: str, aliases: Mapping | None = None) -> list:
    """{object, column, means} for every plain column given a better name.

    The Rosetta stone: `A.C1 AS INVOICE_NUMBER` says C1 means an invoice
    number, in the words of somebody who knew. Only a BARE qualified
    column may carry a meaning -- an alias over SUM(), a CASE, or a
    concatenation describes a computation, not the column, and calling
    it a name for C1 would be false.
    """
    text, complete = scrub(sql)
    if not complete:
        return []
    known = dict(aliases) if aliases is not None else table_aliases(sql)
    head = re.split(r"\bFROM\b", text, maxsplit=1, flags=re.I)
    if len(head) < 2:
        return []
    select = re.split(r"\bSELECT\b", head[0], maxsplit=1, flags=re.I)
    # A set quantifier binds to the SELECT, not to the first item. Left in
    # place it makes `DISTINCT A.C1 AS INVOICE_NUMBER` fail to match and
    # the FIRST column of every DISTINCT view is silently unlearned.
    body = _SET_QUANTIFIER.sub("", select[-1], count=1)
    # A column with no table prefix is ambiguous -- unless exactly one
    # object is in scope, in which case there is nothing for it to be
    # ambiguous WITH. That case is not an edge case: a view over a single
    # table, renaming its columns, is the commonest shape there is, and
    # `SELECT SETCNTRLVALUE AS BUSINESS_UNIT FROM PS_SET_CNTRL_REC` is
    # exactly the sentence this harvest exists to read.
    distinct_sources = set(known.values())
    lone_source = (next(iter(distinct_sources))
                   if len(distinct_sources) == 1 else None)
    out: list = []
    seen: set = set()
    for item in _split_select_items(body):
        match = re.fullmatch(
            rf"\s*{_QUALIFIED}\s+(?:AS\s+)?(?P<alias>{_IDENT})\s*",
            item, re.I)
        if match is not None:
            table_alias = _unquote(match.group("a"))
            column = _unquote(match.group("b"))
            target = known.get(table_alias)
        elif lone_source is not None:
            bare = re.fullmatch(
                rf"\s*(?P<column>{_IDENT})\s+(?:AS\s+)?"
                rf"(?P<alias>{_IDENT})\s*", item, re.I)
            if bare is None:
                continue                  # an expression, not a column
            column = _unquote(bare.group("column"))
            if column in _NOT_AN_ALIAS or column in _NOT_A_COLUMN:
                continue                  # a keyword or a constant
            match = bare
            target = lone_source
        else:
            continue                      # an expression, or ambiguous
        raw_alias = match.group("alias")
        if raw_alias.strip().startswith('"'):
            # A bare identifier can never contain a space, an apostrophe
            # or mixed case -- only a QUOTED one can, which is exactly
            # the shape a party name takes: `AS "Acme Manufacturing
            # rebate"`. _unquote uppercases everything, which would
            # destroy that signal before it could be checked, so this
            # refuses on the raw text rather than sanitizing what
            # survives. A column named with a quoted phrase is rare
            # enough in real view SQL that refusing it costs little;
            # keeping it costs a customer name in a search index.
            continue
        means = _unquote(raw_alias)
        if target is None or not means or means == column:
            continue
        if means in _NOT_AN_ALIAS or means in _EMPTY_ALIASES:
            continue
        key = (target, column, means)
        if key in seen:
            continue
        seen.add(key)
        out.append({"schema": target[0], "object": target[1],
                    "column": column, "means": means})
    return out


def _split_select_items(body: str) -> Iterable[str]:
    """Top-level commas only: a comma inside FUNC(a, b) is not a boundary."""
    depth = 0
    current: list = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == "," and depth == 0:
            yield "".join(current)
            current = []
            continue
        current.append(char)
    if current:
        yield "".join(current)


def readable_words(name: str) -> str:
    """INVOICE_NUMBER -> "invoice number", for search and for a person."""
    text = re.sub(r"[^A-Za-z0-9]+", " ", str(name or "")).strip().lower()
    return " ".join(text.split())
