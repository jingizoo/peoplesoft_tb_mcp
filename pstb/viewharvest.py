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

# Identifiers Oracle and friends actually allow, unquoted or quoted.
_IDENT = r'(?:"[^"]{1,128}"|[A-Za-z][A-Za-z0-9_$#]{0,127})'
_QUALIFIED = rf"(?P<a>{_IDENT})\s*\.\s*(?P<b>{_IDENT})"

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STRING = re.compile(r"'(?:''|[^'])*'")
_PLACEHOLDER = " ~L~ "

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


def strip_noise(sql: str) -> str:
    """Comments and string literals out, before anything is matched.

    Order matters and so does the placeholder: a literal replaced by
    empty text could glue two identifiers into one that never existed,
    and a literal left in could be matched as a join operand.
    """
    text = str(sql or "")
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _LINE_COMMENT.sub(" ", text)
    text = _STRING.sub(_PLACEHOLDER, text)
    return text


def table_aliases(sql: str) -> dict:
    """{alias -> (schema, object)} for every plainly named FROM/JOIN source.

    A subquery, a table function, or anything else that is not a bare
    (optionally schema-qualified) name yields nothing: its columns cannot
    be attributed to a catalog object, so a join through it would name
    the wrong table.
    """
    text = strip_noise(sql)
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
    text = strip_noise(sql)
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
    text = strip_noise(sql)
    known = dict(aliases) if aliases is not None else table_aliases(sql)
    head = re.split(r"\bFROM\b", text, maxsplit=1, flags=re.I)
    if len(head) < 2:
        return []
    select = re.split(r"\bSELECT\b", head[0], maxsplit=1, flags=re.I)
    body = select[-1]
    out: list = []
    seen: set = set()
    for item in _split_select_items(body):
        match = re.fullmatch(
            rf"\s*{_QUALIFIED}\s+(?:AS\s+)?(?P<alias>{_IDENT})\s*",
            item, re.I)
        if match is None:
            continue                      # an expression, not a column
        table_alias = _unquote(match.group("a"))
        column = _unquote(match.group("b"))
        means = _unquote(match.group("alias"))
        target = known.get(table_alias)
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
