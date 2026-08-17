"""Versioned metadata intelligence across PeopleSoft and configured databases.

This is a derived, read-only INDEX of structure.  It deliberately contains no
transaction rows, balances, customer/vendor values, credentials, or full view
SQL.  A slow offline build reads source catalogs; question-time search opens
one local SQLite artifact read-only.

The catalog keeps three claims separate:

* observed -- a physical object, column, index, constraint, or dependency
  visible in the database catalog;
* declared -- a PeopleTools definition such as PSRECDEFN.SQLTABLENAME;
* inferred -- a unique suffix mapping when no explicit physical name exists.

That distinction is the confidence model.  No LLM assigns a percentage, and no
``PS_`` or company prefix is manufactured.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


SCHEMA_VERSION = 2
DEFAULT_FILENAME = "metadata_catalog.db"
MAX_RESULT_CAP = 100

HARD_MAX_OBJECTS = 1_000_000
HARD_MAX_FIELDS = 5_000_000
HARD_MAX_INDEXES = 2_000_000
HARD_MAX_CONSTRAINTS = 2_000_000
HARD_MAX_CONSTRAINT_COLUMNS = 5_000_000
HARD_MAX_DEPENDENCIES = 2_000_000
HARD_MAX_PEOPLETOOLS_ROWS = 5_000_000
HARD_MAX_PAGE_SIZE = 25_000

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_$#]{1,}")
_STOP = frozenset({
    "A", "AN", "THE", "THIS", "THAT", "OUR", "MY", "YOUR", "OF",
    "FOR", "FROM", "WITH", "IN", "ON", "AT", "TO", "AND", "OR",
    "WHAT", "WHICH", "WHERE", "SHOW", "FIND", "GET", "TABLE", "RECORD",
    "RECORDS", "COLUMN", "COLUMNS", "FIELD", "FIELDS", "CONFIGURED",
})


class MetadataError(RuntimeError):
    """A catalog build/read that cannot honestly continue."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _u(value) -> str:
    return str(value or "").strip().upper()


def _s(value) -> str:
    return str(value or "").strip()


def _stable_id(kind: str, source: str, schema: str, name: str,
               parent: str = "") -> str:
    raw = json.dumps([source, schema, kind, parent, name], separators=(",", ":"),
                     ensure_ascii=True)
    return f"{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def catalog_path(cfg) -> Path:
    return Path(getattr(cfg, "root", ".") or ".") / DEFAULT_FILENAME


@dataclass(frozen=True)
class MetadataBuildLimits:
    max_objects: int = 100_000
    max_fields: int = 500_000
    max_indexes: int = 250_000
    max_constraints: int = 250_000
    max_constraint_columns: int = 1_000_000
    max_dependencies: int = 250_000
    max_peopletools_rows: int = 500_000
    query_page_size: int = 5_000
    stale_after_hours: int = 168

    @classmethod
    def from_config(cls, cfg=None, **overrides):
        source = cfg or object()
        values = {
            name: overrides.get(name, getattr(source, name, fld.default))
            for name, fld in cls.__dataclass_fields__.items()
        }
        try:
            out = cls(**{name: int(value) for name, value in values.items()})
        except (TypeError, ValueError) as exc:
            raise MetadataError(
                f"metadata_catalog limits must be whole numbers: {exc}") from exc
        out.validate()
        return out

    def validate(self) -> None:
        for name, value, ceiling in (
            ("max_objects", self.max_objects, HARD_MAX_OBJECTS),
            ("max_fields", self.max_fields, HARD_MAX_FIELDS),
            ("max_indexes", self.max_indexes, HARD_MAX_INDEXES),
            ("max_constraints", self.max_constraints, HARD_MAX_CONSTRAINTS),
            ("max_constraint_columns", self.max_constraint_columns,
             HARD_MAX_CONSTRAINT_COLUMNS),
            ("max_dependencies", self.max_dependencies, HARD_MAX_DEPENDENCIES),
            ("max_peopletools_rows", self.max_peopletools_rows,
             HARD_MAX_PEOPLETOOLS_ROWS),
            ("query_page_size", self.query_page_size, HARD_MAX_PAGE_SIZE),
        ):
            if value < 1 or value > ceiling:
                raise MetadataError(
                    f"metadata_catalog.{name} must be between 1 and "
                    f"{ceiling:,}; received {value:,}")
        if self.stale_after_hours < 1:
            raise MetadataError(
                "metadata_catalog.stale_after_hours must be at least 1")


_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
  name TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  schema_name TEXT,
  status TEXT NOT NULL,
  peopletools_status TEXT,
  objects INTEGER NOT NULL DEFAULT 0,
  fields INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE nodes (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  schema_name TEXT,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  label TEXT,
  description TEXT,
  collector TEXT NOT NULL,
  evidence TEXT NOT NULL,
  authority TEXT NOT NULL,
  confidence TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  attrs TEXT NOT NULL
);
CREATE INDEX nodes_source_kind ON nodes(source, kind);
CREATE INDEX nodes_source_name ON nodes(source, name);
CREATE INDEX nodes_source_kind_name ON nodes(source, kind, name);
CREATE TABLE edges (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL,
  confidence TEXT NOT NULL,
  evidence TEXT NOT NULL,
  collector TEXT NOT NULL,
  authority TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  attrs TEXT NOT NULL,
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX edges_src ON edges(src);
CREATE INDEX edges_dst ON edges(dst);
CREATE TABLE aliases (
  source TEXT NOT NULL,
  alias_upper TEXT NOT NULL,
  node_id TEXT NOT NULL,
  facet TEXT NOT NULL,
  PRIMARY KEY (source, alias_upper, node_id, facet)
);
CREATE INDEX aliases_lookup ON aliases(alias_upper, source);
CREATE TABLE search_terms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT NOT NULL,
  facet TEXT NOT NULL,
  text TEXT NOT NULL,
  UNIQUE(node_id, facet, text)
);
CREATE INDEX search_node ON search_terms(node_id);
CREATE TABLE notes (
  source TEXT NOT NULL,
  layer TEXT NOT NULL,
  note TEXT NOT NULL,
  ok INTEGER NOT NULL,
  partial INTEGER NOT NULL,
  status TEXT NOT NULL
);
"""


class _Writer:
    def __init__(self, con: sqlite3.Connection, limits: MetadataBuildLimits):
        self.con = con
        self.limits = limits
        self.collected_at = _stamp()
        self.partial = False
        self.degraded: set[str] = set()
        self.limit_hits: list[dict] = []

    def source(self, name: str, db) -> None:
        db_cfg = getattr(getattr(db, "cfg", None), "db", None)
        schema = _s(getattr(db_cfg, "schema", ""))
        self.con.execute(
            "INSERT OR REPLACE INTO sources "
            "(name,backend,schema_name,status,peopletools_status,objects,fields) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, _s(getattr(db, "dialect", "unknown")), schema,
             "building", "not_checked", 0, 0))

    def finish_source(self, name: str, status: str, objects: int, fields: int,
                      peopletools_status: str = "not_applicable") -> None:
        self.con.execute(
            "UPDATE sources SET status=?,peopletools_status=?,objects=?,fields=? "
            "WHERE name=?", (status, peopletools_status, objects, fields, name))

    def note(self, source: str, layer: str, note: str, *, ok: bool = True,
             partial: bool = False, status: str = "") -> None:
        layer_status = _s(status).lower() or (
            "partial" if partial else "available" if ok else "unavailable")
        if layer_status not in {"available", "unavailable", "partial"}:
            raise MetadataError(f"invalid metadata layer status: {layer_status}")
        self.con.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)",
                         (source, layer, note, int(ok), int(partial),
                          layer_status))
        # An unsupported optional layer is honestly unavailable without
        # making the layers that were harvested incomplete. Read failures and
        # configured caps are partial and do degrade the source snapshot.
        if partial:
            self.degraded.add(source)
        if partial:
            self.partial = True

    def limit(self, source: str, layer: str, cap: int, kept: int) -> None:
        hit = {"source": source, "layer": layer, "limit": int(cap),
               "rows_kept": int(kept)}
        self.limit_hits.append(hit)
        self.note(
            source, layer,
            f"{layer} reached the configured {cap:,}-row limit; {kept:,} "
            "rows were retained and this snapshot is PARTIAL.",
            ok=False, partial=True)

    def node(self, *, source: str, schema: str, kind: str, name: str,
             label: str = "", description: str = "", collector: str,
             evidence: str, authority: str, confidence: str,
             parent: str = "", attrs: Optional[dict] = None) -> str:
        nid = _stable_id(kind, source, schema, name, parent)
        payload = json.dumps(attrs or {}, sort_keys=True, default=str)
        self.con.execute(
            "INSERT INTO nodes "
            "(id,source,schema_name,kind,name,label,description,collector,"
            "evidence,authority,confidence,collected_at,attrs) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "label=CASE WHEN nodes.label='' THEN excluded.label ELSE nodes.label END,"
            "description=CASE WHEN nodes.description='' THEN excluded.description "
            "ELSE nodes.description END",
            (nid, source, schema, kind, name, label, description, collector,
             evidence, authority, confidence, self.collected_at, payload))
        self.term(nid, "name", name)
        if label:
            self.term(nid, "label", label)
        if description:
            self.term(nid, "description", description)
        return nid

    def edge(self, src: str, dst: str, kind: str, *, confidence: str,
             evidence: str, collector: str, authority: str,
             attrs: Optional[dict] = None) -> None:
        if not src or not dst or src == dst:
            return
        self.con.execute(
            "INSERT INTO edges "
            "(src,dst,kind,confidence,evidence,collector,authority,collected_at,attrs) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(src,dst,kind) DO NOTHING",
            (src, dst, kind, confidence, evidence, collector, authority,
             self.collected_at, json.dumps(attrs or {}, sort_keys=True,
                                            default=str)))

    def alias(self, source: str, alias: str, node_id: str, facet: str) -> None:
        value = _u(alias)
        if value:
            self.con.execute(
                "INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)",
                (source, value, node_id, facet))

    def term(self, node_id: str, facet: str, text: str) -> None:
        value = _s(text)
        if value:
            self.con.execute(
                "INSERT OR IGNORE INTO search_terms (node_id,facet,text) "
                "VALUES (?,?,?)", (node_id, facet, value))

    def object_matches(self, source: str, physical: str,
                       schema: str = "") -> list[sqlite3.Row]:
        params: list = [source, _u(physical)]
        sql = ("SELECT * FROM nodes WHERE source=? AND kind IN ('table','view') "
               "AND name=?")
        if schema:
            sql += " AND schema_name=?"
            params.append(_u(schema))
        return self.con.execute(sql, params).fetchall()

    def suffix_matches(self, source: str, logical: str) -> list[sqlite3.Row]:
        # Native harvest pre-indexes every underscore-delimited suffix.  This
        # avoids an O(records × objects) leading-wildcard scan on delivered
        # PS_<RECNAME> installations and treats '_' literally.
        return self.con.execute(
            "SELECT N.* FROM aliases A JOIN nodes N ON N.id=A.node_id "
            "WHERE A.source=? AND A.alias_upper=? "
            "AND A.facet='physical suffix' "
            "AND N.kind IN ('table','view') ORDER BY N.schema_name,N.name "
            "LIMIT 3", (source, _u(logical))).fetchall()


def _object_page(db, after: tuple | None, cap: int) -> tuple[list[dict], bool]:
    dialect = db.dialect
    configured = _u(getattr(db.cfg.db, "schema", ""))
    if dialect == "sqlite":
        name = after[1] if after else ""
        return db.query(
            "SELECT 'main' AS schema_name, name AS object_name, "
            "CASE type WHEN 'table' THEN 'TABLE' ELSE 'VIEW' END AS object_type "
            "FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' AND UPPER(name) > :n "
            "ORDER BY UPPER(name)", {"n": name}, max_rows=cap)
    if dialect == "oracle":
        name = after[1] if after else ""
        if configured:
            return db.query(
                "SELECT OWNER AS schema_name, OBJECT_NAME AS object_name, "
                "OBJECT_TYPE AS object_type FROM ALL_OBJECTS "
                "WHERE OWNER=:owner AND OBJECT_TYPE IN ('TABLE','VIEW') "
                "AND OBJECT_NAME > :n ORDER BY OBJECT_NAME",
                {"owner": configured, "n": name}, max_rows=cap)
        return db.query(
            "SELECT USER AS schema_name, OBJECT_NAME AS object_name, "
            "OBJECT_TYPE AS object_type FROM USER_OBJECTS "
            "WHERE OBJECT_TYPE IN ('TABLE','VIEW') AND OBJECT_NAME > :n "
            "ORDER BY OBJECT_NAME", {"n": name}, max_rows=cap)
    if dialect == "sqlserver":
        schema, name = after or ("", "")
        params = {"s": schema, "n": name}
        where = (
            "O.type IN ('U','V') AND (UPPER(S.name) > :s OR "
            "(UPPER(S.name)=:s AND UPPER(O.name)>:n))")
        if configured:
            where = "UPPER(S.name)=:owner AND UPPER(O.name)>:n"
            params = {"owner": configured, "n": name}
        return db.query(
            "SELECT S.name AS schema_name,O.name AS object_name,"
            "CASE O.type WHEN 'U' THEN 'TABLE' ELSE 'VIEW' END AS object_type "
            "FROM sys.objects O JOIN sys.schemas S ON S.schema_id=O.schema_id "
            f"WHERE {where} ORDER BY UPPER(S.name),UPPER(O.name)",
            params, max_rows=cap)
    raise MetadataError(f"unsupported metadata backend: {dialect}")


def _column_pages(db, page_size: int) -> Iterable[tuple[list[dict], bool]]:
    dialect = db.dialect
    configured = _u(getattr(db.cfg.db, "schema", ""))
    after: tuple | None = None
    while True:
        if dialect == "oracle":
            table, pos = after or ("", 0)
            params = {"t": table, "p": pos}
            if configured:
                sql = (
                    "SELECT OWNER AS schema_name,TABLE_NAME AS object_name,"
                    "COLUMN_NAME AS column_name,COLUMN_ID AS ordinal_position,"
                    "DATA_TYPE AS data_type,DATA_LENGTH AS data_length,"
                    "NULLABLE AS nullable FROM ALL_TAB_COLUMNS WHERE OWNER=:owner "
                    "AND (TABLE_NAME>:t OR (TABLE_NAME=:t AND COLUMN_ID>:p)) "
                    "ORDER BY TABLE_NAME,COLUMN_ID")
                params["owner"] = configured
            else:
                sql = (
                    "SELECT USER AS schema_name,TABLE_NAME AS object_name,"
                    "COLUMN_NAME AS column_name,COLUMN_ID AS ordinal_position,"
                    "DATA_TYPE AS data_type,DATA_LENGTH AS data_length,"
                    "NULLABLE AS nullable FROM USER_TAB_COLUMNS WHERE "
                    "TABLE_NAME>:t OR (TABLE_NAME=:t AND COLUMN_ID>:p) "
                    "ORDER BY TABLE_NAME,COLUMN_ID")
        elif dialect == "sqlserver":
            schema, table, pos = after or ("", "", 0)
            params = {"s": schema, "t": table, "p": pos}
            keyset = (
                "(UPPER(S.name)>:s OR (UPPER(S.name)=:s AND "
                "(UPPER(O.name)>:t OR (UPPER(O.name)=:t AND C.column_id>:p))))")
            if configured:
                keyset = ("UPPER(S.name)=:owner AND (UPPER(O.name)>:t OR "
                          "(UPPER(O.name)=:t AND C.column_id>:p))")
                params["owner"] = configured
            sql = (
                "SELECT S.name AS schema_name,O.name AS object_name,"
                "C.name AS column_name,C.column_id AS ordinal_position,"
                "T.name AS data_type,C.max_length AS data_length,"
                "CASE C.is_nullable WHEN 1 THEN 'Y' ELSE 'N' END AS nullable "
                "FROM sys.objects O JOIN sys.schemas S ON S.schema_id=O.schema_id "
                "JOIN sys.columns C ON C.object_id=O.object_id "
                "JOIN sys.types T ON T.user_type_id=C.user_type_id "
                f"WHERE O.type IN ('U','V') AND {keyset} "
                "ORDER BY UPPER(S.name),UPPER(O.name),C.column_id")
        else:
            return
        page, truncated = db.query(sql, params, max_rows=page_size)
        if not page:
            return
        yield page, truncated
        if not truncated:
            return
        last = page[-1]
        if dialect == "oracle":
            nxt = (_u(last.get("object_name")),
                   int(last.get("ordinal_position") or 0))
        else:
            nxt = (_u(last.get("schema_name")), _u(last.get("object_name")),
                   int(last.get("ordinal_position") or 0))
        if nxt == after or any(v is None for v in nxt):
            raise MetadataError("native column pagination did not advance")
        after = nxt


def _index_pages(db, page_size: int) -> Iterable[tuple[list[dict], bool]]:
    dialect = db.dialect
    configured = _u(getattr(db.cfg.db, "schema", ""))
    if dialect == "sqlite":
        return
    after: tuple | None = None
    while True:
        if dialect == "oracle":
            table, index, pos = after or ("", "", 0)
            params = {"t": table, "i": index, "p": pos}
            keyset = (
                "(C.TABLE_NAME>:t OR (C.TABLE_NAME=:t AND "
                "(C.INDEX_NAME>:i OR (C.INDEX_NAME=:i AND "
                "C.COLUMN_POSITION>:p))))")
            if configured:
                params["owner"] = configured
                sql = (
                    "SELECT C.TABLE_OWNER AS schema_name,C.TABLE_NAME AS object_name,"
                    "C.INDEX_NAME AS index_name,C.COLUMN_NAME AS column_name,"
                    "C.COLUMN_POSITION AS ordinal_position,"
                    "I.UNIQUENESS AS uniqueness,0 AS filtered "
                    "FROM ALL_IND_COLUMNS C "
                    "JOIN ALL_INDEXES I ON I.OWNER=C.INDEX_OWNER "
                    "AND I.INDEX_NAME=C.INDEX_NAME WHERE "
                    f"C.TABLE_OWNER=:owner AND {keyset} ORDER BY C.TABLE_NAME,"
                    "C.INDEX_NAME,C.COLUMN_POSITION")
            else:
                # USER_* is both faster and semantically correct here.  An
                # unqualified primary connection means the current schema,
                # not every owner for which the account happens to hold a
                # catalog grant.
                sql = (
                    "SELECT USER AS schema_name,C.TABLE_NAME AS object_name,"
                    "C.INDEX_NAME AS index_name,C.COLUMN_NAME AS column_name,"
                    "C.COLUMN_POSITION AS ordinal_position,"
                    "I.UNIQUENESS AS uniqueness,0 AS filtered "
                    "FROM USER_IND_COLUMNS C "
                    "JOIN USER_INDEXES I ON I.INDEX_NAME=C.INDEX_NAME "
                    f"WHERE {keyset} ORDER BY C.TABLE_NAME,C.INDEX_NAME,"
                    "C.COLUMN_POSITION")
        else:
            schema, table, index, pos = after or ("", "", "", 0)
            params = {"s": schema, "t": table, "i": index, "p": pos}
            keyset = (
                "(UPPER(S.name)>:s OR (UPPER(S.name)=:s AND "
                "(UPPER(O.name)>:t OR (UPPER(O.name)=:t AND "
                "(UPPER(I.name)>:i OR (UPPER(I.name)=:i AND "
                "IC.key_ordinal>:p))))))")
            if configured:
                keyset = (
                    "UPPER(S.name)=:owner AND (UPPER(O.name)>:t OR "
                    "(UPPER(O.name)=:t AND (UPPER(I.name)>:i OR "
                    "(UPPER(I.name)=:i AND IC.key_ordinal>:p))))")
                params["owner"] = configured
            sql = (
                "SELECT S.name AS schema_name,O.name AS object_name,"
                "I.name AS index_name,C.name AS column_name,"
                "IC.key_ordinal AS ordinal_position,I.is_unique AS uniqueness,"
                "I.has_filter AS filtered "
                "FROM sys.objects O JOIN sys.schemas S ON S.schema_id=O.schema_id "
                "JOIN sys.indexes I ON I.object_id=O.object_id "
                "JOIN sys.index_columns IC ON IC.object_id=I.object_id "
                "AND IC.index_id=I.index_id JOIN sys.columns C ON "
                "C.object_id=IC.object_id AND C.column_id=IC.column_id "
                f"WHERE O.type IN ('U','V') AND I.index_id>0 AND "
                f"I.is_hypothetical=0 AND IC.key_ordinal>0 AND {keyset} "
                "ORDER BY UPPER(S.name),UPPER(O.name),UPPER(I.name),"
                "IC.key_ordinal")
        page, truncated = db.query(sql, params, max_rows=page_size)
        if not page:
            return
        yield page, truncated
        if not truncated:
            return
        last = page[-1]
        if dialect == "oracle":
            nxt = (_u(last.get("object_name")), _u(last.get("index_name")),
                   int(last.get("ordinal_position") or 0))
        else:
            nxt = (_u(last.get("schema_name")), _u(last.get("object_name")),
                   _u(last.get("index_name")),
                   int(last.get("ordinal_position") or 0))
        if nxt == after:
            raise MetadataError("native index pagination did not advance")
        after = nxt


def _constraint_pages(db, page_size: int) -> Iterable[tuple[list[dict], bool]]:
    """Yield ordered key/foreign-key columns without reading application rows."""
    dialect = db.dialect
    configured = _u(getattr(db.cfg.db, "schema", ""))
    if dialect == "sqlite":
        return
    after: tuple | None = None
    while True:
        if dialect == "oracle":
            table, constraint, pos = after or ("", "", 0)
            params = {"t": table, "c": constraint, "p": pos}
            keyset = (
                "(C.TABLE_NAME>:t OR (C.TABLE_NAME=:t AND "
                "(C.CONSTRAINT_NAME>:c OR (C.CONSTRAINT_NAME=:c AND "
                "CC.POSITION>:p))))")
            if configured:
                params["owner"] = configured
                sql = (
                    "SELECT C.OWNER AS schema_name,C.TABLE_NAME AS object_name,"
                    "C.CONSTRAINT_NAME AS constraint_name,"
                    "C.CONSTRAINT_TYPE AS constraint_type,"
                    "CC.COLUMN_NAME AS column_name,CC.POSITION AS ordinal_position,"
                    "C.R_OWNER AS referenced_schema,RC.TABLE_NAME AS referenced_object,"
                    "RCC.COLUMN_NAME AS referenced_column,"
                    "C.R_CONSTRAINT_NAME AS referenced_constraint,"
                    "C.DELETE_RULE AS delete_rule,C.STATUS AS constraint_status,"
                    "C.VALIDATED AS validated "
                    "FROM ALL_CONSTRAINTS C JOIN ALL_CONS_COLUMNS CC ON "
                    "CC.OWNER=C.OWNER AND CC.CONSTRAINT_NAME=C.CONSTRAINT_NAME "
                    "LEFT JOIN ALL_CONSTRAINTS RC ON RC.OWNER=C.R_OWNER AND "
                    "RC.CONSTRAINT_NAME=C.R_CONSTRAINT_NAME "
                    "LEFT JOIN ALL_CONS_COLUMNS RCC ON RCC.OWNER=RC.OWNER AND "
                    "RCC.CONSTRAINT_NAME=RC.CONSTRAINT_NAME AND "
                    "RCC.POSITION=CC.POSITION WHERE C.OWNER=:owner AND "
                    f"C.CONSTRAINT_TYPE IN ('P','U','R') AND {keyset} "
                    "ORDER BY C.TABLE_NAME,C.CONSTRAINT_NAME,CC.POSITION")
            else:
                # USER_* keeps an unqualified connection in its current
                # schema. A cross-schema FK whose target is not visible still
                # retains R_OWNER/R_CONSTRAINT_NAME as an unresolved reference.
                sql = (
                    "SELECT USER AS schema_name,C.TABLE_NAME AS object_name,"
                    "C.CONSTRAINT_NAME AS constraint_name,"
                    "C.CONSTRAINT_TYPE AS constraint_type,"
                    "CC.COLUMN_NAME AS column_name,CC.POSITION AS ordinal_position,"
                    "C.R_OWNER AS referenced_schema,RC.TABLE_NAME AS referenced_object,"
                    "RCC.COLUMN_NAME AS referenced_column,"
                    "C.R_CONSTRAINT_NAME AS referenced_constraint,"
                    "C.DELETE_RULE AS delete_rule,C.STATUS AS constraint_status,"
                    "C.VALIDATED AS validated "
                    "FROM USER_CONSTRAINTS C JOIN USER_CONS_COLUMNS CC ON "
                    "CC.CONSTRAINT_NAME=C.CONSTRAINT_NAME "
                    "LEFT JOIN USER_CONSTRAINTS RC ON "
                    "RC.CONSTRAINT_NAME=C.R_CONSTRAINT_NAME "
                    "LEFT JOIN USER_CONS_COLUMNS RCC ON "
                    "RCC.CONSTRAINT_NAME=RC.CONSTRAINT_NAME AND "
                    "RCC.POSITION=CC.POSITION WHERE "
                    f"C.CONSTRAINT_TYPE IN ('P','U','R') AND {keyset} "
                    "ORDER BY C.TABLE_NAME,C.CONSTRAINT_NAME,CC.POSITION")
        elif dialect == "sqlserver":
            schema, table, constraint, pos = after or ("", "", "", 0)
            params = {"s": schema, "t": table, "c": constraint, "p": pos}
            keyset = (
                "(UPPER(Q.schema_name)>:s OR (UPPER(Q.schema_name)=:s AND "
                "(UPPER(Q.object_name)>:t OR (UPPER(Q.object_name)=:t AND "
                "(UPPER(Q.constraint_name)>:c OR "
                "(UPPER(Q.constraint_name)=:c AND Q.ordinal_position>:p))))))")
            if configured:
                keyset = (
                    "UPPER(Q.schema_name)=:owner AND "
                    "(UPPER(Q.object_name)>:t OR (UPPER(Q.object_name)=:t AND "
                    "(UPPER(Q.constraint_name)>:c OR "
                    "(UPPER(Q.constraint_name)=:c AND Q.ordinal_position>:p))))")
                params = {"owner": configured, "t": table,
                          "c": constraint, "p": pos}
            sql = (
                "SELECT Q.schema_name,Q.object_name,Q.constraint_name,"
                "Q.constraint_type,Q.column_name,Q.ordinal_position,"
                "Q.referenced_schema,Q.referenced_object,Q.referenced_column,"
                "Q.referenced_constraint,Q.delete_rule,Q.constraint_status,"
                "Q.validated FROM ("
                "SELECT S.name AS schema_name,O.name AS object_name,"
                "KC.name AS constraint_name,KC.type AS constraint_type,"
                "C.name AS column_name,IC.key_ordinal AS ordinal_position,"
                "NULL AS referenced_schema,NULL AS referenced_object,"
                "NULL AS referenced_column,NULL AS referenced_constraint,"
                "NULL AS delete_rule,CASE KI.is_disabled WHEN 1 THEN "
                "'DISABLED' ELSE 'ENABLED' END AS constraint_status,"
                "'NOT APPLICABLE' AS validated FROM sys.key_constraints KC "
                "JOIN sys.objects O ON O.object_id=KC.parent_object_id "
                "JOIN sys.schemas S ON S.schema_id=O.schema_id "
                "JOIN sys.indexes KI ON KI.object_id=O.object_id AND "
                "KI.index_id=KC.unique_index_id "
                "JOIN sys.index_columns IC ON IC.object_id=O.object_id AND "
                "IC.index_id=KC.unique_index_id AND IC.key_ordinal>0 "
                "JOIN sys.columns C ON C.object_id=IC.object_id AND "
                "C.column_id=IC.column_id WHERE KC.type IN ('PK','UQ') "
                "UNION ALL "
                "SELECT S.name AS schema_name,O.name AS object_name,"
                "FK.name AS constraint_name,'F' AS constraint_type,"
                "PC.name AS column_name,FC.constraint_column_id AS ordinal_position,"
                "RS.name AS referenced_schema,RO.name AS referenced_object,"
                "RC.name AS referenced_column,NULL AS referenced_constraint,"
                "FK.delete_referential_action_desc AS delete_rule,"
                "CASE FK.is_disabled WHEN 1 THEN 'DISABLED' ELSE 'ENABLED' END,"
                "CASE FK.is_not_trusted WHEN 1 THEN 'NOT TRUSTED' ELSE "
                "'TRUSTED' END FROM sys.foreign_keys FK "
                "JOIN sys.foreign_key_columns FC ON "
                "FC.constraint_object_id=FK.object_id "
                "JOIN sys.objects O ON O.object_id=FK.parent_object_id "
                "JOIN sys.schemas S ON S.schema_id=O.schema_id "
                "JOIN sys.columns PC ON PC.object_id=O.object_id AND "
                "PC.column_id=FC.parent_column_id "
                "LEFT JOIN sys.objects RO ON RO.object_id=FK.referenced_object_id "
                "LEFT JOIN sys.schemas RS ON RS.schema_id=RO.schema_id "
                "LEFT JOIN sys.columns RC ON RC.object_id=RO.object_id AND "
                "RC.column_id=FC.referenced_column_id) Q "
                f"WHERE {keyset} ORDER BY UPPER(Q.schema_name),"
                "UPPER(Q.object_name),UPPER(Q.constraint_name),"
                "Q.ordinal_position")
        else:
            return
        page, truncated = db.query(sql, params, max_rows=page_size)
        if not page:
            return
        yield page, truncated
        if not truncated:
            return
        last = page[-1]
        if dialect == "oracle":
            nxt = (_u(last.get("object_name")),
                   _u(last.get("constraint_name")),
                   int(last.get("ordinal_position") or 0))
        else:
            nxt = (_u(last.get("schema_name")),
                   _u(last.get("object_name")),
                   _u(last.get("constraint_name")),
                   int(last.get("ordinal_position") or 0))
        if nxt == after:
            raise MetadataError("native constraint pagination did not advance")
        after = nxt


def _view_dependency_pages(
        db, page_size: int) -> Iterable[tuple[list[dict], bool]]:
    """Yield catalog-native view dependencies; never fetch view definitions."""
    dialect = db.dialect
    configured = _u(getattr(db.cfg.db, "schema", ""))
    if dialect == "sqlite":
        return
    after: tuple | None = None
    while True:
        if dialect == "oracle":
            view, ref_schema, ref_object, ref_link = after or (
                "", "", "", "")
            params = {"v": view, "rs": ref_schema, "ro": ref_object,
                      "rl": ref_link}
            keyset = (
                "(NAME>:v OR (NAME=:v AND (NVL(REFERENCED_OWNER,'')>:rs OR "
                "(NVL(REFERENCED_OWNER,'')=:rs AND "
                "(REFERENCED_NAME>:ro OR (REFERENCED_NAME=:ro AND "
                "NVL(REFERENCED_LINK_NAME,'')>:rl))))))")
            if configured:
                params["owner"] = configured
                sql = (
                    "SELECT OWNER AS schema_name,NAME AS view_name,"
                    "REFERENCED_OWNER AS referenced_schema,"
                    "REFERENCED_NAME AS referenced_object,"
                    "REFERENCED_TYPE AS referenced_type,"
                    "REFERENCED_LINK_NAME AS referenced_link FROM "
                    "ALL_DEPENDENCIES WHERE OWNER=:owner AND TYPE='VIEW' AND "
                    "REFERENCED_TYPE IN ('TABLE','VIEW','MATERIALIZED VIEW') AND "
                    f"{keyset} ORDER BY NAME,NVL(REFERENCED_OWNER,''),"
                    "REFERENCED_NAME,NVL(REFERENCED_LINK_NAME,'')")
            else:
                sql = (
                    "SELECT USER AS schema_name,NAME AS view_name,"
                    "REFERENCED_OWNER AS referenced_schema,"
                    "REFERENCED_NAME AS referenced_object,"
                    "REFERENCED_TYPE AS referenced_type,"
                    "REFERENCED_LINK_NAME AS referenced_link FROM "
                    "USER_DEPENDENCIES WHERE TYPE='VIEW' AND REFERENCED_TYPE IN "
                    f"('TABLE','VIEW','MATERIALIZED VIEW') AND {keyset} "
                    "ORDER BY NAME,NVL(REFERENCED_OWNER,''),REFERENCED_NAME,"
                    "NVL(REFERENCED_LINK_NAME,'')")
        elif dialect == "sqlserver":
            view_schema, view, ref_schema, ref_object, ref_database, ref_server = (
                after or ("", "", "", "", "", ""))
            params = {"s": view_schema, "v": view, "rs": ref_schema,
                      "ro": ref_object, "rd": ref_database,
                      "rsv": ref_server}
            keyset = (
                "(UPPER(Q.schema_name)>:s OR (UPPER(Q.schema_name)=:s AND "
                "(UPPER(Q.view_name)>:v OR (UPPER(Q.view_name)=:v AND "
                "(UPPER(Q.referenced_schema)>:rs OR "
                "(UPPER(Q.referenced_schema)=:rs AND "
                "(UPPER(Q.referenced_object)>:ro OR "
                "(UPPER(Q.referenced_object)=:ro AND "
                "(UPPER(Q.referenced_database)>:rd OR "
                "(UPPER(Q.referenced_database)=:rd AND "
                "UPPER(Q.referenced_server)>:rsv))))))))))")
            if configured:
                keyset = (
                    "UPPER(Q.schema_name)=:owner AND "
                    "(UPPER(Q.view_name)>:v OR (UPPER(Q.view_name)=:v AND "
                    "(UPPER(Q.referenced_schema)>:rs OR "
                    "(UPPER(Q.referenced_schema)=:rs AND "
                    "(UPPER(Q.referenced_object)>:ro OR "
                    "(UPPER(Q.referenced_object)=:ro AND "
                    "(UPPER(Q.referenced_database)>:rd OR "
                    "(UPPER(Q.referenced_database)=:rd AND "
                    "UPPER(Q.referenced_server)>:rsv))))))))")
                params = {"owner": configured, "v": view,
                          "rs": ref_schema, "ro": ref_object,
                          "rd": ref_database, "rsv": ref_server}
            sql = (
                "SELECT Q.schema_name,Q.view_name,Q.referenced_schema,"
                "Q.referenced_object,Q.referenced_type,Q.referenced_database,"
                "Q.referenced_server "
                "FROM (SELECT DISTINCT S.name AS schema_name,V.name AS view_name,"
                "COALESCE(RS.name,D.referenced_schema_name,'') AS "
                "referenced_schema,COALESCE(RO.name,D.referenced_entity_name,'') "
                "AS referenced_object,CASE RO.type WHEN 'U' THEN 'TABLE' "
                "WHEN 'V' THEN 'VIEW' ELSE 'UNRESOLVED' END AS referenced_type,"
                "COALESCE(D.referenced_database_name,'') AS referenced_database,"
                "COALESCE(D.referenced_server_name,'') AS referenced_server "
                "FROM sys.views V JOIN sys.schemas S ON S.schema_id=V.schema_id "
                "JOIN sys.sql_expression_dependencies D ON "
                "D.referencing_id=V.object_id LEFT JOIN sys.objects RO ON "
                "RO.object_id=D.referenced_id LEFT JOIN sys.schemas RS ON "
                "RS.schema_id=RO.schema_id WHERE "
                "COALESCE(RO.name,D.referenced_entity_name,'')<>'') Q WHERE "
                f"{keyset} ORDER BY UPPER(Q.schema_name),UPPER(Q.view_name),"
                "UPPER(Q.referenced_schema),UPPER(Q.referenced_object),"
                "UPPER(Q.referenced_database),UPPER(Q.referenced_server)")
        else:
            return
        page, truncated = db.query(sql, params, max_rows=page_size)
        if not page:
            return
        yield page, truncated
        if not truncated:
            return
        last = page[-1]
        if dialect == "oracle":
            nxt = (_u(last.get("view_name")),
                   _u(last.get("referenced_schema")),
                   _u(last.get("referenced_object")),
                   _u(last.get("referenced_link")))
        else:
            nxt = (_u(last.get("schema_name")), _u(last.get("view_name")),
                   _u(last.get("referenced_schema")),
                   _u(last.get("referenced_object")),
                   _u(last.get("referenced_database")),
                   _u(last.get("referenced_server")))
        if nxt == after:
            raise MetadataError(
                "native view-dependency pagination did not advance")
        after = nxt


def _external_object_node(
        state: _Writer, source: str, schema: str, name: str, *,
        evidence: str, parent: str, attrs: dict) -> str:
    """Persist an observed reference name without claiming the object exists."""
    ref_schema = _u(schema) or "UNKNOWN"
    ref_name = _u(name) or "UNKNOWN"
    payload = {**attrs, "resolution_status": "unresolved",
               "structural_reference_only": True}
    nid = state.node(
        source=source, schema=ref_schema, kind="external_object", name=ref_name,
        parent=parent, collector="db_catalog", evidence=evidence,
        authority="observed", confidence="inconclusive", attrs=payload)
    state.alias(source, f"{ref_schema}.{ref_name}", nid,
                "unresolved structural reference")
    return nid


def _collect_constraints(
        state: _Writer, source: str, db, object_keys: dict,
        *, object_overflow: bool) -> int:
    """Collect bounded PK/UQ/FK definitions and their ordered columns."""
    limits = state.limits
    count = 0
    overflow = False
    column_memberships = 0
    column_overflow = False
    incomplete_rows = False

    def add_constraint(schema: str, obj: str, name: str, type_code: str,
                       rows: list[dict], *, generated_name: bool = False,
                       rowset_complete: bool = True) -> bool:
        nonlocal count, overflow, column_memberships, column_overflow
        if count >= limits.max_constraints:
            overflow = True
            return False
        owner = object_keys.get((_u(schema) or "MAIN", _u(obj)))
        if owner is None:
            return True
        code = _u(type_code)
        ctype = {
            "P": "primary_key", "PK": "primary_key",
            "U": "unique", "UQ": "unique",
            "R": "foreign_key", "F": "foreign_key",
        }.get(code)
        if not ctype:
            return True
        membership_remaining = (
            limits.max_constraint_columns - column_memberships)
        if rows and membership_remaining <= 0:
            column_overflow = True
            return False
        if len(rows) > membership_remaining:
            rows = rows[:membership_remaining]
            rowset_complete = False
            column_overflow = True
        columns = [_u(row.get("column_name")) for row in rows
                   if _s(row.get("column_name"))]
        referenced_schema = next(
            (_u(row.get("referenced_schema")) for row in rows
             if _s(row.get("referenced_schema"))), "")
        referenced_object = next(
            (_u(row.get("referenced_object")) for row in rows
             if _s(row.get("referenced_object"))), "")
        referenced_constraint = next(
            (_u(row.get("referenced_constraint")) for row in rows
             if _s(row.get("referenced_constraint"))), "")
        pairs = [{
            "ordinal": int(row.get("ordinal_position") or pos),
            "column": _u(row.get("column_name")),
            "referenced_column": _u(row.get("referenced_column")) or None,
        } for pos, row in enumerate(rows, 1)]
        constraint_name = _u(name) or f"{ctype.upper()}"
        evidence = {
            "sqlite": "SQLite constraint PRAGMA",
            "oracle": ("ALL_CONSTRAINTS/ALL_CONS_COLUMNS"
                       if _u(getattr(db.cfg.db, "schema", "")) else
                       "USER_CONSTRAINTS/USER_CONS_COLUMNS"),
            "sqlserver": "sys.key_constraints/sys.foreign_keys",
        }.get(db.dialect, "database constraint catalog")
        ref_status = "not_applicable"
        target = None
        if ctype == "foreign_key":
            target_schema = referenced_schema or owner["schema"]
            target = object_keys.get((target_schema, referenced_object))
            ref_status = "resolved" if target is not None else "unresolved"
        attrs = {
            "object": owner["name"],
            "constraint_type": ctype,
            "columns": columns,
            "column_pairs": pairs,
            "generated_name": bool(generated_name),
            "rowset_complete": bool(rowset_complete),
            "referenced_schema": referenced_schema or None,
            "referenced_object": referenced_object or None,
            "referenced_constraint": referenced_constraint or None,
            "reference_status": ref_status,
            "delete_rule": _s(rows[0].get("delete_rule")) or None,
            "update_rule": _s(rows[0].get("update_rule")) or None,
            "match_rule": _s(rows[0].get("match_type")) or None,
            "status": _s(rows[0].get("constraint_status")) or None,
            "validated": _s(rows[0].get("validated")) or None,
        }
        cid = state.node(
            source=source, schema=owner["schema"], kind="constraint",
            name=constraint_name, parent=owner["id"],
            collector="db_catalog", evidence=evidence,
            authority="observed", confidence="confirmed", attrs=attrs)
        state.edge(
            owner["id"], cid, "object_has_constraint",
            confidence="confirmed", evidence=evidence,
            collector="db_catalog", authority="observed",
            attrs={"constraint_type": ctype, "columns": columns})
        state.alias(source, constraint_name, cid, "constraint")
        state.alias(source, f"{owner['schema']}.{owner['name']}."
                    f"{constraint_name}", cid, "qualified constraint")
        state.term(cid, "constraint columns", " ".join(columns))
        for pos, column in enumerate(columns, 1):
            column_id = _stable_id(
                "column", source, owner["schema"], column, owner["id"])
            if state.con.execute(
                    "SELECT 1 FROM nodes WHERE id=?", (column_id,)).fetchone():
                state.edge(
                    cid, column_id, "constraint_has_column",
                    confidence="confirmed", evidence=evidence,
                    collector="db_catalog", authority="observed",
                    attrs={"ordinal": pos})
        if ctype == "foreign_key":
            if target is None and (referenced_object or referenced_constraint):
                target = {"id": _external_object_node(
                    state, source, referenced_schema or owner["schema"],
                    referenced_object or "UNKNOWN", evidence=(
                        "unresolved foreign-key target from constraint catalog"),
                    parent=(f"fk:{cid}:{referenced_constraint}"),
                    attrs={
                        "referenced_constraint": referenced_constraint or None,
                        "referencing_schema": owner["schema"],
                        "referencing_object": owner["name"],
                    })}
            if target is not None:
                state.edge(
                    cid, target["id"], "foreign_key_references_object",
                    confidence=("confirmed" if ref_status == "resolved"
                                else "inconclusive"),
                    evidence=evidence, collector="db_catalog",
                    authority="observed",
                    attrs={"resolution_status": ref_status,
                           "column_pairs": pairs})
                state.term(cid, "referenced object",
                           " ".join(filter(None, [referenced_schema,
                                                  referenced_object])))
                if ref_status == "resolved":
                    for pair in pairs:
                        ref_col = _u(pair.get("referenced_column"))
                        if not ref_col:
                            continue
                        ref_col_id = _stable_id(
                            "column", source, target["schema"], ref_col,
                            target["id"])
                        if state.con.execute(
                                "SELECT 1 FROM nodes WHERE id=?",
                                (ref_col_id,)).fetchone():
                            state.edge(
                                cid, ref_col_id,
                                "foreign_key_references_column",
                                confidence="confirmed", evidence=evidence,
                                collector="db_catalog", authority="observed",
                                attrs={"local_column": pair["column"],
                                       "ordinal": pair["ordinal"]})
        count += 1
        column_memberships += len(rows)
        return not column_overflow

    try:
        if object_overflow and db.dialect != "sqlite":
            state.note(
                source, "constraints",
                "Native constraint harvest was skipped after the object "
                "catalog hit its limit; key/foreign-key coverage is unknown.",
                ok=False, partial=True)
            return 0
        if db.dialect == "sqlite":
            stop = False
            for owner in object_keys.values():
                if owner["kind"] != "table" or stop:
                    continue
                pk_rows, pk_truncated = db.query(
                    "SELECT cid,name AS column_name,pk AS ordinal_position FROM "
                    "pragma_table_info(:table_name) WHERE pk>0 ORDER BY pk",
                    {"table_name": owner["name"]},
                    max_rows=limits.query_page_size)
                incomplete_rows = incomplete_rows or pk_truncated
                if pk_rows and not add_constraint(
                        owner["schema"], owner["name"], "PRIMARY KEY", "P",
                        [{**row, "constraint_status": "ENABLED"}
                         for row in pk_rows], generated_name=True,
                        rowset_complete=not pk_truncated):
                    stop = True
                    break
                uniques, unique_truncated = db.query(
                    "SELECT name,origin,partial FROM "
                    "pragma_index_list(:table_name) WHERE origin='u' "
                    "ORDER BY seq", {"table_name": owner["name"]},
                    max_rows=limits.query_page_size)
                incomplete_rows = incomplete_rows or unique_truncated
                for unique in uniques:
                    unique_name = _u(unique.get("name"))
                    cols, col_truncated = db.query(
                        "SELECT seqno,cid,name AS column_name FROM "
                        "pragma_index_xinfo(:index_name) WHERE \"key\"=1 "
                        "ORDER BY seqno", {"index_name": unique_name},
                        max_rows=limits.query_page_size)
                    incomplete_rows = incomplete_rows or col_truncated
                    if not add_constraint(
                            owner["schema"], owner["name"], unique_name, "U",
                            [{**row, "ordinal_position":
                              int(row.get("seqno") or 0) + 1,
                              "constraint_status": "ENABLED"} for row in cols],
                            generated_name=unique_name.startswith(
                                "SQLITE_AUTOINDEX_"),
                            rowset_complete=not col_truncated):
                        stop = True
                        break
                if stop:
                    break
                foreign, foreign_truncated = db.query(
                    "SELECT id,seq,\"table\" AS referenced_object,"
                    "\"from\" AS column_name,\"to\" AS referenced_column,"
                    "on_update AS update_rule,on_delete AS delete_rule,"
                    "\"match\" AS match_type FROM "
                    "pragma_foreign_key_list(:table_name) ORDER BY id,seq",
                    {"table_name": owner["name"]},
                    max_rows=limits.query_page_size)
                incomplete_rows = incomplete_rows or foreign_truncated
                by_id: dict[int, list[dict]] = {}
                for row in foreign:
                    by_id.setdefault(int(row.get("id") or 0), []).append({
                        **row,
                        "ordinal_position": int(row.get("seq") or 0) + 1,
                        "referenced_schema": owner["schema"],
                        "constraint_status": "ENABLED",
                    })
                for fk_id, rows in sorted(by_id.items()):
                    if not add_constraint(
                            owner["schema"], owner["name"],
                            f"FOREIGN KEY {fk_id}", "R", rows,
                            generated_name=True,
                            rowset_complete=not foreign_truncated):
                        stop = True
                        break
                if stop:
                    break
        else:
            current = None
            rows: list[dict] = []
            stop = False
            for page, _truncated in _constraint_pages(
                    db, limits.query_page_size):
                for row in page:
                    key = (_u(row.get("schema_name")),
                           _u(row.get("object_name")),
                           _u(row.get("constraint_name")),
                           _u(row.get("constraint_type")))
                    if current is not None and key != current:
                        if not add_constraint(*current, rows):
                            stop = True
                            break
                        rows = []
                    current = key
                    rows.append(row)
                if stop:
                    break
            if current is not None and not stop:
                add_constraint(*current, rows)
    except Exception as exc:
        state.note(
            source, "constraints",
            f"primary/unique/foreign-key constraints could not be read "
            f"({type(exc).__name__}); constraint coverage is unknown, not none",
            ok=False, partial=True)
        return count
    if incomplete_rows:
        state.note(
            source, "constraints",
            "At least one SQLite constraint definition exceeded the per-query "
            "page size; retained constraint columns are partial.",
            ok=False, partial=True)
    if overflow:
        state.limit(source, "constraints", limits.max_constraints, count)
    if column_overflow:
        state.limit(
            source, "constraint_columns", limits.max_constraint_columns,
            column_memberships)
    if not overflow and not column_overflow and not incomplete_rows:
        state.note(
            source, "constraints",
            f"Collected {count:,} primary, unique, and foreign-key "
            "definitions from the native constraint catalog.",
            status="available")
    return count


def _collect_view_dependencies(
        state: _Writer, source: str, db, object_keys: dict,
        *, object_overflow: bool) -> int:
    """Collect bounded view-to-object edges without retaining definition SQL."""
    limits = state.limits
    if db.dialect == "sqlite":
        state.note(
            source, "view_dependencies",
            "Unavailable: SQLite exposes no structured view-dependency "
            "catalog. Full view SQL is deliberately not parsed or stored.",
            ok=False, partial=False, status="unavailable")
        return 0
    if object_overflow:
        state.note(
            source, "view_dependencies",
            "View dependency harvest was skipped after the object catalog hit "
            "its limit; dependency coverage is unknown.",
            ok=False, partial=True)
        return 0
    count = 0
    overflow = False
    seen: set[tuple[str, str, str, str, str, str]] = set()
    try:
        stop = False
        for page, _truncated in _view_dependency_pages(
                db, limits.query_page_size):
            for row in page:
                view_schema = _u(row.get("schema_name")) or "MAIN"
                view_name = _u(row.get("view_name"))
                view = object_keys.get((view_schema, view_name))
                if view is None or view["kind"] != "view":
                    continue
                # Native catalogs provide the resolved owner when they can.
                # A blank owner on an unresolved/dynamic reference is unknown;
                # do not guess that it shares the view's schema.
                ref_schema = _u(row.get("referenced_schema")) or "UNKNOWN"
                ref_name = _u(row.get("referenced_object"))
                if not ref_name:
                    continue
                external_db = _u(row.get("referenced_database"))
                external_server = _u(row.get("referenced_server"))
                db_link = _u(row.get("referenced_link"))
                raw_pair = (view["id"], external_server, external_db, db_link,
                            ref_schema, ref_name)
                if raw_pair in seen:
                    continue
                if count >= limits.max_dependencies:
                    overflow = True
                    stop = True
                    break
                target = (None if external_server or external_db or db_link else
                          object_keys.get((ref_schema, ref_name)))
                resolution = "resolved" if target is not None else "unresolved"
                evidence = {
                    "oracle": ("ALL_DEPENDENCIES" if
                               _u(getattr(db.cfg.db, "schema", "")) else
                               "USER_DEPENDENCIES"),
                    "sqlserver": "sys.sql_expression_dependencies",
                }.get(db.dialect, "database dependency catalog")
                if target is None:
                    target = {"id": _external_object_node(
                        state, source, ref_schema, ref_name,
                        evidence="unresolved view target from dependency catalog",
                        parent=(f"view:{view['id']}:{external_server}:"
                                f"{external_db}:{db_link}"),
                        attrs={
                            "referenced_server": external_server or None,
                            "referenced_database": external_db or None,
                            "referenced_link": db_link or None,
                            "referenced_type": _u(row.get("referenced_type"))
                            or None,
                            "referencing_schema": view_schema,
                            "referencing_object": view_name,
                        })}
                seen.add(raw_pair)
                state.edge(
                    view["id"], target["id"], "view_depends_on",
                    confidence=("confirmed" if resolution == "resolved"
                                else "inconclusive"),
                    evidence=evidence, collector="db_catalog",
                    authority="observed", attrs={
                        "resolution_status": resolution,
                        "referenced_schema": ref_schema,
                        "referenced_object": ref_name,
                        "referenced_type": _u(row.get("referenced_type"))
                        or None,
                        "referenced_server": external_server or None,
                        "referenced_database": external_db or None,
                        "referenced_link": db_link or None,
                    })
                state.term(view["id"], "view dependency",
                           " ".join(filter(None, [ref_schema, ref_name])))
                count += 1
            if stop:
                break
    except Exception as exc:
        state.note(
            source, "view_dependencies",
            f"view dependencies could not be read ({type(exc).__name__}); "
            "dependency coverage is unknown, not none",
            ok=False, partial=True)
        return count
    if overflow:
        state.limit(
            source, "view_dependencies", limits.max_dependencies, count)
    else:
        state.note(
            source, "view_dependencies",
            f"Collected {count:,} view dependency edges from the native "
            "dependency catalog without reading or storing definition SQL.",
            status="available")
    return count


def _collect_native(state: _Writer, source: str, db) -> tuple[int, int]:
    limits = state.limits
    objects: list[dict] = []
    after = None
    object_overflow = False
    while len(objects) < limits.max_objects:
        size = min(limits.query_page_size, limits.max_objects - len(objects))
        page, truncated = _object_page(db, after, size)
        if not page:
            break
        for row in page:
            schema = _u(row.get("schema_name")) or _u(db.cfg.db.schema) or "MAIN"
            name = _u(row.get("object_name"))
            if not name:
                continue
            kind = "view" if "VIEW" in _u(row.get("object_type")) else "table"
            if db.dialect == "oracle":
                object_evidence = ("ALL_OBJECTS" if _u(db.cfg.db.schema)
                                   else "USER_OBJECTS")
            else:
                object_evidence = {
                    "sqlite": "sqlite_master",
                    "sqlserver": "sys.objects",
                }.get(db.dialect, "database catalog")
            nid = state.node(
                source=source, schema=schema, kind=kind, name=name,
                collector="db_catalog", evidence=object_evidence,
                authority="observed", confidence="confirmed",
                attrs={"physical_name": name, "object_type": kind})
            state.alias(source, name, nid, "physical object")
            state.alias(source, f"{schema}.{name}", nid, "qualified object")
            parts = name.split("_")
            for start in range(1, len(parts)):
                state.alias(source, "_".join(parts[start:]), nid,
                            "physical suffix")
            objects.append({"schema": schema, "name": name, "kind": kind,
                            "id": nid})
        if not truncated:
            break
        last = page[-1]
        nxt = (_u(last.get("schema_name")), _u(last.get("object_name")))
        if nxt == after:
            state.note(source, "objects", "object pagination did not advance",
                       ok=False, partial=True)
            break
        after = nxt
        if len(objects) >= limits.max_objects:
            object_overflow = True
            break
    if object_overflow:
        state.limit(source, "objects", limits.max_objects, len(objects))

    object_keys = {(o["schema"], o["name"]): o for o in objects}
    fields = 0
    field_overflow = False

    def add_column(schema: str, obj: str, row: dict) -> None:
        nonlocal fields
        key = (_u(schema) or "MAIN", _u(obj))
        owner = object_keys.get(key)
        if owner is None or fields >= limits.max_fields:
            return
        name = _u(row.get("column_name") or row.get("name"))
        if not name:
            return
        attrs = {
            "object": owner["name"],
            "ordinal": int(row.get("ordinal_position") or row.get("cid") or 0),
            "data_type": _s(row.get("data_type") or row.get("type")),
            "data_length": row.get("data_length"),
            "nullable": _s(row.get("nullable") or row.get("notnull")
                           or row.get("is_notnull")),
        }
        fid = state.node(
            source=source, schema=owner["schema"], kind="column", name=name,
            parent=owner["id"], collector="db_catalog",
            evidence="physical column catalog", authority="observed",
            confidence="confirmed", attrs=attrs)
        state.edge(owner["id"], fid, "object_has_column",
                   confidence="confirmed", evidence="physical column catalog",
                   collector="db_catalog", authority="observed",
                   attrs={"ordinal": attrs["ordinal"]})
        state.alias(source, f"{owner['name']}.{name}", fid, "physical column")
        state.term(fid, "data type", attrs["data_type"])
        fields += 1

    if db.dialect == "sqlite":
        for obj in objects:
            remaining = limits.max_fields - fields
            if remaining <= 0:
                probe, _ = db.query(
                    "SELECT cid,name,type,\"notnull\" AS is_notnull FROM "
                    "pragma_table_xinfo(:table_name) WHERE hidden=0 "
                    "ORDER BY cid", {"table_name": obj["name"]}, max_rows=1)
                field_overflow = field_overflow or bool(probe)
                continue
            rows, truncated = db.query(
                "SELECT cid,name,type,\"notnull\" AS is_notnull FROM "
                "pragma_table_xinfo(:table_name) WHERE hidden=0 ORDER BY cid",
                {"table_name": obj["name"]},
                max_rows=min(10_000, remaining))
            field_overflow = field_overflow or truncated
            for row in rows:
                row = {**row, "ordinal_position": int(row.get("cid") or 0) + 1,
                       "nullable": "N" if row.get("is_notnull") else "Y"}
                add_column(obj["schema"], obj["name"], row)
    elif object_overflow:
        state.note(
            source, "columns",
            "Native column harvest was skipped after the object catalog hit "
            "its limit; this prevents an unbounded full-catalog scan.",
            ok=False, partial=True)
    else:
        for page, _truncated in _column_pages(db, limits.query_page_size):
            for pos, row in enumerate(page):
                add_column(row.get("schema_name"), row.get("object_name"), row)
                if fields >= limits.max_fields:
                    field_overflow = (pos < len(page) - 1 or _truncated)
                    break
            if fields >= limits.max_fields:
                break
    if field_overflow:
        state.limit(source, "fields", limits.max_fields, fields)

    index_count = 0
    index_overflow = False

    def add_index(schema: str, obj: str, name: str, columns: list[str],
                  unique: bool, *, expression_based: bool = False,
                  filtered: bool = False) -> bool:
        nonlocal index_count, index_overflow
        if index_count >= limits.max_indexes:
            index_overflow = True
            return False
        owner = object_keys.get((_u(schema) or "MAIN", _u(obj)))
        if owner is None or not name:
            return True
        iid = state.node(
            source=source, schema=owner["schema"], kind="index", name=_u(name),
            parent=owner["id"], collector="db_catalog",
            evidence="ordered database index catalog", authority="observed",
            confidence="confirmed",
            attrs={"object": owner["name"], "columns": columns,
                   "unique": bool(unique),
                   "expression_based": bool(expression_based),
                   "filtered": bool(filtered)})
        state.edge(owner["id"], iid, "object_has_index",
                   confidence="confirmed", evidence="database index catalog",
                   collector="db_catalog", authority="observed",
                   attrs={"columns": columns, "unique": bool(unique),
                          "expression_based": bool(expression_based),
                          "filtered": bool(filtered)})
        state.term(iid, "index columns", " ".join(columns))
        for pos, col in enumerate(columns, 1):
            cid = _stable_id("column", source, owner["schema"], _u(col),
                             owner["id"])
            if state.con.execute(
                    "SELECT 1 FROM nodes WHERE id=?", (cid,)).fetchone():
                state.edge(iid, cid, "index_has_column",
                           confidence="confirmed",
                           evidence="ordered index column",
                           collector="db_catalog", authority="observed",
                           attrs={"ordinal": pos})
        index_count += 1
        return True

    try:
        if object_overflow and db.dialect != "sqlite":
            state.note(
                source, "indexes",
                "Native index harvest was skipped after the object catalog "
                "hit its limit; index coverage is unknown for this source.",
                ok=False, partial=True)
        elif db.dialect == "sqlite":
            for obj in objects:
                if index_count >= limits.max_indexes:
                    probe, _ = db.query(
                        "SELECT name FROM pragma_index_list(:table_name) "
                        "ORDER BY seq", {"table_name": obj["name"]},
                        max_rows=1)
                    index_overflow = index_overflow or bool(probe)
                    continue
                idxs, indexes_truncated = db.query(
                    "SELECT name, \"unique\" AS uniqueness,partial FROM "
                    "pragma_index_list(:table_name) ORDER BY seq",
                    {"table_name": obj["name"]},
                    max_rows=min(10_000,
                                 limits.max_indexes - index_count))
                index_overflow = index_overflow or indexes_truncated
                for idx in idxs:
                    idx_name = _s(idx.get("name"))
                    if not idx_name:
                        continue
                    cols, _ = db.query(
                        "SELECT seqno,cid,name,\"key\" AS is_key FROM "
                        "pragma_index_xinfo(:index_name) WHERE \"key\"=1 "
                        "ORDER BY seqno", {"index_name": idx_name},
                        max_rows=10_000)
                    ordered = [_u(c.get("name")) for c in cols
                               if _s(c.get("name"))]
                    expression_based = any(
                        not _s(c.get("name")) or int(c.get("cid") or 0) < 0
                        for c in cols)
                    add_index(obj["schema"], obj["name"], idx_name, ordered,
                              bool(idx.get("uniqueness")),
                              expression_based=expression_based,
                              filtered=bool(idx.get("partial")))
        else:
            current = None
            columns: list[str] = []
            unique = False
            filtered = False
            expression_based = False
            for page, _ in _index_pages(db, limits.query_page_size):
                for row in page:
                    key = (_u(row.get("schema_name")),
                           _u(row.get("object_name")),
                           _u(row.get("index_name")))
                    if current is not None and key != current:
                        if not add_index(*current, columns, unique,
                                         expression_based=expression_based,
                                         filtered=filtered):
                            break
                        columns = []
                        expression_based = False
                    current = key
                    column_name = _u(row.get("column_name"))
                    if column_name:
                        columns.append(column_name)
                    expression_based = (expression_based or not column_name
                                        or column_name.startswith("SYS_NC"))
                    filtered = bool(row.get("filtered"))
                    raw_unique = row.get("uniqueness")
                    if isinstance(raw_unique, str):
                        unique = _u(raw_unique) in {"UNIQUE", "TRUE", "Y", "1"}
                    else:
                        unique = bool(raw_unique)
                if index_count >= limits.max_indexes:
                    index_overflow = True
                    break
            if current is not None:
                add_index(*current, columns, unique,
                          expression_based=expression_based,
                          filtered=filtered)
    except Exception as exc:  # optional layer
        state.note(source, "indexes",
                   f"ordered indexes could not be read ({type(exc).__name__}); "
                   "index coverage is unknown, not none", ok=False,
                   partial=True)
    if index_overflow:
        state.limit(source, "indexes", limits.max_indexes, index_count)
    _collect_constraints(
        state, source, db, object_keys, object_overflow=object_overflow)
    _collect_view_dependencies(
        state, source, db, object_keys, object_overflow=object_overflow)
    return len(objects), fields


def _pt_columns(db, table: str) -> set[str]:
    try:
        return {_u(c) for c in db.columns(table)}
    except Exception:
        return set()


def _pt_rows(db, table: str, columns: list[str], keys: list[str], *,
             page_size: int, cap: int, distinct: bool = False,
             where: str = "", on_limit=None) -> Iterable[dict]:
    prefix = getattr(db, "prefix", "")
    after = None
    kept = 0
    overflow = False
    while kept < cap:
        clauses = [f"({where})"] if where else []
        params: dict = {}
        if after is not None:
            alternatives = []
            for i, key in enumerate(keys):
                equal = [f"{keys[j]}=:mk{j}" for j in range(i)]
                alternatives.append("(" + " AND ".join(
                    equal + [f"{key}>:mk{i}"]) + ")")
            clauses.append("(" + " OR ".join(alternatives) + ")")
            params = {f"mk{i}": value for i, value in enumerate(after)}
        sql = "SELECT " + ("DISTINCT " if distinct else "") + ",".join(columns)
        sql += f" FROM {prefix}{table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY " + ",".join(keys)
        size = min(page_size, cap - kept)
        page, truncated = db.query(sql, params, max_rows=size)
        if not page:
            return
        for row in page:
            yield row
        kept += len(page)
        if not truncated:
            return
        overflow = kept >= cap
        nxt = tuple(page[-1].get(key.lower()) for key in keys)
        if any(value is None for value in nxt) or nxt == after:
            raise MetadataError(f"{table} pagination did not advance")
        after = nxt
    if overflow and on_limit is not None:
        on_limit(kept)


def _pt_public_query_rows(db, *, page_size: int, cap: int,
                          on_limit=None) -> Iterable[dict]:
    """Public PSQuery/record memberships only.

    Private query names and owners are user-specific metadata.  They must not
    enter a shared offline artifact, so this collector requires the OPRID key
    on both definitions and record memberships and joins only blank-owner
    definitions.  If a release/site shape cannot prove public visibility, the
    caller skips the layer rather than broadening it.
    """
    prefix = getattr(db, "prefix", "")
    after = None
    kept = 0
    overflow = False
    while kept < cap:
        params = {"q": after[0], "r": after[1]} if after else {}
        keyset = (
            "AND (UPPER(R.QRYNAME)>:q OR (UPPER(R.QRYNAME)=:q "
            "AND UPPER(R.RECNAME)>:r)) " if after else "")
        sql = (
            "SELECT DISTINCT R.QRYNAME,R.RECNAME FROM "
            f"{prefix}PSQRYRECORD R JOIN {prefix}PSQRYDEFN D ON "
            "D.QRYNAME=R.QRYNAME WHERE "
            "(TRIM(D.OPRID) IS NULL OR TRIM(D.OPRID)='') AND "
            "(TRIM(R.OPRID) IS NULL OR TRIM(R.OPRID)='') "
            + keyset +
            "ORDER BY UPPER(R.QRYNAME),UPPER(R.RECNAME)")
        size = min(page_size, cap - kept)
        page, truncated = db.query(sql, params, max_rows=size)
        if not page:
            return
        yield from page
        kept += len(page)
        if not truncated:
            return
        overflow = kept >= cap
        nxt = (_u(page[-1].get("qryname")), _u(page[-1].get("recname")))
        if not all(nxt) or nxt == after:
            raise MetadataError("public PSQuery pagination did not advance")
        after = nxt
    if overflow and on_limit is not None:
        on_limit(kept)


_RECTYPE = {0: "table", 1: "view", 2: "derived", 3: "subrecord",
            5: "dynamic view", 6: "query view", 7: "temp table"}


def _collect_peopletools(state: _Writer, source: str, db) -> str:
    limits = state.limits
    rec_cols = _pt_columns(db, "PSRECDEFN")
    if "RECNAME" not in rec_cols:
        state.note(source, "PSRECDEFN",
                   "PSRECDEFN is not readable; native catalog metadata is "
                   "available but PeopleTools logical identity is not.",
                   ok=False, partial=True)
        return "unavailable"

    select = ["RECNAME"] + [c for c in ("RECDESCR", "RECTYPE", "SQLTABLENAME")
                              if c in rec_cols]
    records: dict[str, dict] = {}
    object_layer_partial = any(
        hit.get("source") == source and hit.get("layer") == "objects"
        for hit in state.limit_hits)
    try:
        for row in _pt_rows(db, "PSRECDEFN", select, ["RECNAME"],
                            page_size=limits.query_page_size,
                            cap=limits.max_peopletools_rows,
                            on_limit=lambda kept: state.limit(
                                source, "PSRECDEFN",
                                limits.max_peopletools_rows, kept)):
            rec = _u(row.get("recname"))
            if not rec:
                continue
            declared = _u(row.get("sqltablename"))
            physical = ""
            physical_id = ""
            basis = "unresolved"
            confidence = "inconclusive"
            mapping_authority = "declared"
            matches: list[sqlite3.Row] = []
            try:
                record_type_code = int(row.get("rectype") or 0)
            except (TypeError, ValueError):
                record_type_code = -1
            expected_kind = "view" if record_type_code == 1 else "table"
            declared_schema = ""
            declared_name = declared
            if "." in declared:
                declared_schema, _, declared_name = declared.rpartition(".")
            if record_type_code not in {0, 1, 7}:
                basis = (
                    f"PeopleTools record type {_RECTYPE.get(record_type_code, record_type_code)} "
                    "is not a SQL-backed physical object; mapping is not applicable")
                confidence = "inconclusive"
            elif declared_name:
                matches = state.object_matches(source, declared_name,
                                               declared_schema)
                if len(matches) == 1:
                    if matches[0]["kind"] != expected_kind:
                        basis = (
                            "PSRECDEFN record type expects a " + expected_kind +
                            " but SQLTABLENAME resolves to a " +
                            str(matches[0]["kind"]) + "; mapping not accepted")
                        confidence = "candidate"
                    else:
                        physical, physical_id = (matches[0]["name"],
                                                 matches[0]["id"])
                        basis = "PSRECDEFN.SQLTABLENAME corroborated by live catalog"
                        confidence = "confirmed"
                elif len(matches) > 1:
                    basis = (
                        "PSRECDEFN.SQLTABLENAME is unqualified and resolves to "
                        "multiple visible schemas; qualify the physical owner")
                    confidence = "inconclusive"
                else:
                    basis = ("PSRECDEFN.SQLTABLENAME declared " + declared +
                             " but that object is not visible in the catalog")
                    confidence = "candidate"
            else:
                matches = state.object_matches(source, rec)
                if len(matches) == 1:
                    if matches[0]["kind"] != expected_kind:
                        basis = ("exact name has the wrong native object kind; "
                                 f"expected {expected_kind}, observed "
                                 f"{matches[0]['kind']}")
                        confidence = "candidate"
                    else:
                        physical, physical_id = (matches[0]["name"],
                                                 matches[0]["id"])
                        basis = "exact logical/physical catalog identity"
                        confidence = "confirmed"
                        mapping_authority = "observed"
                elif len(matches) > 1:
                    basis = "exact name exists in multiple schemas; owner is ambiguous"
                    confidence = "inconclusive"
                else:
                    matches = ([] if object_layer_partial
                               else state.suffix_matches(source, rec))
                    if object_layer_partial:
                        basis = (
                            "object catalog is partial, so suffix uniqueness "
                            "cannot be inferred")
                        confidence = "inconclusive"
                    elif len(matches) == 1:
                        if matches[0]["kind"] != expected_kind:
                            basis = ("unique suffix has the wrong native object "
                                     f"kind; expected {expected_kind}, observed "
                                     f"{matches[0]['kind']}")
                            confidence = "candidate"
                        else:
                            physical, physical_id = (matches[0]["name"],
                                                     matches[0]["id"])
                            basis = "unique live-catalog suffix match; no prefix assumed"
                            confidence = "corroborated"
                            mapping_authority = "inferred"
                    elif len(matches) > 1:
                        basis = "ambiguous live-catalog suffix; no prefix was guessed"
            attrs = {
                "logical_record": rec,
                "physical_object": physical or None,
                "declared_physical": declared or None,
                "mapping_basis": basis,
                "mapping_confidence": confidence,
                "mapping_authority": mapping_authority,
                "record_type": _RECTYPE.get(record_type_code,
                                                _s(row.get("rectype"))),
            }
            rid = state.node(
                source=source, schema="PEOPLETOOLS", kind="record", name=rec,
                label=_s(row.get("recdescr")), collector="peopletools",
                evidence="PSRECDEFN", authority="declared",
                confidence="confirmed", attrs=attrs)
            state.alias(source, rec, rid, "logical record")
            state.term(rid, "mapping basis", basis)
            if physical_id:
                state.alias(source, physical, rid, "physical object")
                state.term(rid, "physical object", physical)
                state.edge(rid, physical_id, "record_physicalizes_to",
                           confidence=confidence, evidence=basis,
                           collector="peopletools", authority=mapping_authority)
            records[rec] = {"id": rid, "physical_id": physical_id,
                            "physical": physical}
    except Exception as exc:
        state.note(source, "PSRECDEFN",
                   f"PeopleTools record definitions are partial "
                   f"({type(exc).__name__}: {str(exc)[:120]})",
                   ok=False, partial=True)
        if not records:
            return "unavailable"

    field_nodes: dict[str, str] = {}
    dbfield_cols = _pt_columns(db, "PSDBFIELD")
    if "FIELDNAME" in dbfield_cols:
        cols = ["FIELDNAME"] + [c for c in
                ("FIELDTYPE", "LENGTH", "DECIMALPOS", "FORMAT")
                if c in dbfield_cols]
        try:
            for row in _pt_rows(db, "PSDBFIELD", cols, ["FIELDNAME"],
                                page_size=limits.query_page_size,
                                cap=limits.max_peopletools_rows,
                                on_limit=lambda kept: state.limit(
                                    source, "PSDBFIELD",
                                    limits.max_peopletools_rows, kept)):
                name = _u(row.get("fieldname"))
                if not name:
                    continue
                fid = state.node(
                    source=source, schema="PEOPLETOOLS", kind="field", name=name,
                    collector="peopletools", evidence="PSDBFIELD",
                    authority="declared", confidence="confirmed",
                    attrs={k: row.get(k.lower()) for k in cols if k != "FIELDNAME"})
                state.alias(source, name, fid, "PeopleTools field")
                field_nodes[name] = fid
        except Exception as exc:
            state.note(source, "PSDBFIELD",
                       f"field definitions are partial ({type(exc).__name__})",
                       ok=False, partial=True)
    else:
        state.note(source, "PSDBFIELD",
                   "PSDBFIELD is not readable; record membership remains but "
                   "global field types/formats are unavailable.",
                   ok=False, status="unavailable")

    rf_cols = _pt_columns(db, "PSRECFIELD")
    if {"RECNAME", "FIELDNAME"} <= rf_cols:
        cols = ["RECNAME", "FIELDNAME"] + [c for c in ("FIELDNUM", "USEEDIT")
                                                  if c in rf_cols]
        keys = ["RECNAME", "FIELDNUM"] if "FIELDNUM" in rf_cols \
            else ["RECNAME", "FIELDNAME"]
        try:
            for row in _pt_rows(db, "PSRECFIELD", cols, keys,
                                page_size=limits.query_page_size,
                                cap=limits.max_peopletools_rows,
                                on_limit=lambda kept: state.limit(
                                    source, "PSRECFIELD",
                                    limits.max_peopletools_rows, kept)):
                rec, field = _u(row.get("recname")), _u(row.get("fieldname"))
                record = records.get(rec)
                if not record or not field:
                    continue
                fid = field_nodes.get(field)
                if not fid:
                    fid = state.node(
                        source=source, schema="PEOPLETOOLS", kind="field",
                        name=field, collector="peopletools",
                        evidence="PSRECFIELD.FIELDNAME", authority="declared",
                        confidence="confirmed")
                    field_nodes[field] = fid
                state.edge(record["id"], fid, "record_defines_field",
                           confidence="confirmed", evidence="PSRECFIELD",
                           collector="peopletools", authority="declared",
                           attrs={"ordinal": row.get("fieldnum"),
                                  "useedit_raw": row.get("useedit")})
                state.term(record["id"], "field name", field)
                if record["physical_id"]:
                    cid = _stable_id("column", source,
                                     state.con.execute(
                                         "SELECT schema_name FROM nodes WHERE id=?",
                                         (record["physical_id"],)).fetchone()[0],
                                     field, record["physical_id"])
                    exists = state.con.execute(
                        "SELECT 1 FROM nodes WHERE id=?", (cid,)).fetchone()
                    if exists:
                        state.edge(fid, cid, "field_maps_to_column",
                                   confidence="confirmed",
                                   evidence="matching PSRECFIELD/native column",
                                   collector="metadata_catalog",
                                   authority="observed")
        except Exception as exc:
            state.note(source, "PSRECFIELD",
                       f"record fields are partial ({type(exc).__name__})",
                       ok=False, partial=True)
    else:
        state.note(source, "PSRECFIELD",
                   "PSRECFIELD is not readable; logical records have no field "
                   "membership.", ok=False, partial=True)

    label_cols = _pt_columns(db, "PSDBFLDLABL")
    if {"FIELDNAME", "LABEL_ID"} <= label_cols:
        cols = ["FIELDNAME", "LABEL_ID"] + [c for c in
                ("SHORTNAME", "LONGNAME") if c in label_cols]
        try:
            for row in _pt_rows(db, "PSDBFLDLABL", cols,
                                ["FIELDNAME", "LABEL_ID"],
                                page_size=limits.query_page_size,
                                cap=limits.max_peopletools_rows,
                                on_limit=lambda kept: state.limit(
                                    source, "PSDBFLDLABL",
                                    limits.max_peopletools_rows, kept)):
                field = _u(row.get("fieldname"))
                fid = field_nodes.get(field)
                if not fid:
                    continue
                for key in ("label_id", "shortname", "longname"):
                    if row.get(key):
                        state.term(fid, "field label", _s(row[key]))
                state.con.execute(
                    "UPDATE nodes SET label=CASE WHEN label='' THEN ? ELSE label END "
                    "WHERE id=?", (_s(row.get("longname") or
                                           row.get("shortname") or
                                           row.get("label_id")), fid))
        except Exception as exc:
            state.note(source, "PSDBFLDLABL",
                       f"field labels are partial ({type(exc).__name__})",
                       ok=False, partial=True)
    else:
        state.note(
            source, "PSDBFLDLABL",
            "Field-label metadata is unavailable for this source/PeopleTools "
            "shape; absence of a natural-language label is inconclusive.",
            ok=False, status="unavailable")

    xlat_cols = _pt_columns(db, "PSXLATITEM")
    if {"FIELDNAME", "FIELDVALUE"} <= xlat_cols:
        cols = ["FIELDNAME", "FIELDVALUE"] + [c for c in
                ("XLATSHORTNAME", "XLATLONGNAME", "EFFDT", "EFF_STATUS",
                 "XLATSEQNO") if c in xlat_cols]
        keys = ["FIELDNAME", "FIELDVALUE"] + [c for c in
                ("EFFDT", "XLATSEQNO") if c in xlat_cols]
        try:
            for row in _pt_rows(db, "PSXLATITEM", cols, keys,
                                page_size=limits.query_page_size,
                                cap=limits.max_peopletools_rows,
                                on_limit=lambda kept: state.limit(
                                    source, "PSXLATITEM",
                                    limits.max_peopletools_rows, kept)):
                field, value = _u(row.get("fieldname")), _s(row.get("fieldvalue"))
                fid = field_nodes.get(field)
                if not fid or not value:
                    continue
                label = _s(row.get("xlatlongname") or row.get("xlatshortname"))
                version = ":".join(_s(row.get(key)) for key in
                                   ("effdt", "xlatseqno"))
                cid = state.node(
                    source=source, schema="PEOPLETOOLS", kind="code_value",
                    name=f"{field}={value}", label=label,
                    parent=f"{fid}:{version}", collector="peopletools",
                    evidence="PSXLATITEM",
                    authority="declared", confidence="confirmed",
                    attrs={k: row.get(k) for k in
                           ("effdt", "eff_status", "xlatseqno") if row.get(k)})
                state.edge(fid, cid, "field_has_code_value",
                           confidence="confirmed", evidence="PSXLATITEM",
                           collector="peopletools", authority="declared")
                state.term(fid, "translate value", f"{value} {label}")
        except Exception as exc:
            state.note(source, "PSXLATITEM",
                       f"translate values are partial ({type(exc).__name__})",
                       ok=False, partial=True)
    else:
        state.note(
            source, "PSXLATITEM",
            "Translate-value metadata is unavailable; code meanings are not "
            "represented by this snapshot.", ok=False, status="unavailable")

    # Page and PSQuery co-use are direct, high-value relationship evidence.
    pnl_cols = _pt_columns(db, "PSPNLFIELD")
    if {"PNLNAME", "RECNAME"} <= pnl_cols:
        try:
            for row in _pt_rows(db, "PSPNLFIELD", ["PNLNAME", "RECNAME"],
                                ["PNLNAME", "RECNAME"], distinct=True,
                                page_size=limits.query_page_size,
                                cap=limits.max_peopletools_rows,
                                on_limit=lambda kept: state.limit(
                                    source, "PSPNLFIELD",
                                    limits.max_peopletools_rows, kept)):
                page, rec = _u(row.get("pnlname")), _u(row.get("recname"))
                if not page or rec not in records:
                    continue
                pid = state.node(
                    source=source, schema="PEOPLETOOLS", kind="page", name=page,
                    collector="peopletools", evidence="PSPNLFIELD.PNLNAME",
                    authority="declared", confidence="confirmed")
                state.edge(pid, records[rec]["id"], "page_uses_record",
                           confidence="confirmed", evidence="PSPNLFIELD.RECNAME",
                           collector="peopletools", authority="declared")
                state.term(records[rec]["id"], "used by page", page)
        except Exception as exc:
            state.note(source, "PSPNLFIELD",
                       f"page/record use is partial ({type(exc).__name__})",
                       ok=False, partial=True)
    else:
        state.note(
            source, "PSPNLFIELD",
            "Page/record usage metadata is unavailable; record discovery "
            "still uses definitions, fields and the native catalog.",
            ok=False, status="unavailable")

    qry_cols = _pt_columns(db, "PSQRYRECORD")
    qdef_cols = _pt_columns(db, "PSQRYDEFN")
    if ({"QRYNAME", "RECNAME", "OPRID"} <= qry_cols
            and {"QRYNAME", "OPRID"} <= qdef_cols):
        try:
            for row in _pt_public_query_rows(
                    db, page_size=limits.query_page_size,
                    cap=limits.max_peopletools_rows,
                    on_limit=lambda kept: state.limit(
                        source, "PSQRYRECORD",
                        limits.max_peopletools_rows, kept)):
                query, rec = _u(row.get("qryname")), _u(row.get("recname"))
                if not query or rec not in records:
                    continue
                qid = state.node(
                    source=source, schema="PEOPLETOOLS", kind="query", name=query,
                    collector="peopletools", evidence="PSQRYRECORD.QRYNAME",
                    authority="declared", confidence="confirmed")
                state.edge(qid, records[rec]["id"], "query_uses_record",
                           confidence="confirmed", evidence="PSQRYRECORD.RECNAME",
                           collector="peopletools", authority="declared")
                state.term(records[rec]["id"], "used by query", query)
        except Exception as exc:
            state.note(source, "PSQRYRECORD",
                       f"query/record use is partial ({type(exc).__name__})",
                       ok=False, partial=True)
    elif qry_cols or qdef_cols:
        state.note(
            source, "PSQRYRECORD",
            "Saved-query relationships were skipped because this PeopleTools "
            "shape cannot prove public visibility; private query names were "
            "not copied into the shared catalog.",
            ok=False, status="unavailable")
    else:
        state.note(
            source, "PSQRYRECORD",
            "Public saved-query/record usage metadata is unavailable; private "
            "queries are never copied as a fallback.",
            ok=False, status="unavailable")
    return "available" if source not in state.degraded else "partial"


def _try_fts(con: sqlite3.Connection) -> bool:
    try:
        con.execute(
            "CREATE VIRTUAL TABLE search_fts USING fts5("
            "node_id UNINDEXED, facet UNINDEXED, text)")
        con.execute(
            "INSERT INTO search_fts (node_id,facet,text) "
            "SELECT node_id,facet,text FROM search_terms")
        return True
    except sqlite3.OperationalError:
        return False


def build_catalog(path, sources: Iterable[tuple[str, object]], *,
                  limits: MetadataBuildLimits | None = None,
                  peopletools_source: str = "default") -> dict:
    """Build a metadata artifact beside the target, then atomically publish it."""
    limits = limits or MetadataBuildLimits()
    limits.validate()
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    con: sqlite3.Connection | None = None
    source_list = (list(sources.items()) if hasattr(sources, "items")
                   else list(sources))
    try:
        con = sqlite3.connect(str(tmp))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA temp_store=FILE")
        con.executescript(_DDL)
        state = _Writer(con, limits)
        for source, db in source_list:
            state.source(source, db)
            objects = fields = 0
            pt_status = "not_applicable"
            status = "complete"
            try:
                objects, fields = _collect_native(state, source, db)
                if source == peopletools_source:
                    pt_status = _collect_peopletools(state, source, db)
                if source in state.degraded:
                    status = "partial"
            except Exception as exc:
                status = "failed"
                state.note(source, "source",
                           f"source metadata failed ({type(exc).__name__}: "
                           f"{str(exc)[:180]})", ok=False, partial=True)
            state.finish_source(source, status, objects, fields, pt_status)
            con.commit()

        node_count = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        if not node_count:
            raise MetadataError(
                "No metadata could be harvested. The existing catalog was not "
                "replaced; check read-only catalog grants and source settings.")
        edge_count = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        fts = _try_fts(con)
        built_at = _stamp()
        fingerprint = hashlib.sha256(json.dumps(
            [built_at, node_count, edge_count,
             [name for name, _db in source_list]],
            separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()[:20]
        info = {
            "schema_version": str(SCHEMA_VERSION),
            "snapshot_id": fingerprint,
            "built_at": built_at,
            "nodes": str(node_count),
            "edges": str(edge_count),
            "sources": ",".join(name for name, _db in source_list),
            "degraded": ",".join(sorted(state.degraded)),
            "partial": "yes" if state.partial else "no",
            "limit_hits": json.dumps(state.limit_hits, sort_keys=True),
            "fts": "yes" if fts else "no",
            "stale_after_hours": str(limits.stale_after_hours),
        }
        con.executemany("INSERT INTO meta VALUES (?,?)", info.items())
        con.commit()
        con.close()
        con = None
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        return {**info, "path": str(target)}
    except Exception:
        if con is not None:
            con.close()
        if tmp.exists():
            tmp.unlink()
        raise


def _terms(text: str) -> list[str]:
    out = []
    for match in _WORD.finditer(text or ""):
        value = match.group(0).upper()
        if len(value) < 2 or value in _STOP:
            continue
        variants = [value]
        if value.endswith("IES") and len(value) > 5:
            variants.append(value[:-3] + "Y")
        elif (value.endswith("S") and len(value) > 4
              and not value.endswith(("SS", "US", "IS"))):
            variants.append(value[:-1])
        if value.endswith("ING") and len(value) > 7:
            variants.append(value[:-3])
        for variant in variants:
            if variant not in out:
                out.append(variant)
            if len(out) >= 10:
                return out
    return out


def _json(value: str) -> dict:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


class MetadataCatalog:
    """Read-only question-time interface to ``metadata_catalog.db``."""

    def __init__(self, path, stale_after_hours: int = 168):
        self.path = Path(path)
        self.stale_after_hours = max(int(stale_after_hours or 168), 1)

    def available(self) -> bool:
        return self.path.exists()

    def _open(self) -> sqlite3.Connection:
        if not self.available():
            raise MetadataError(
                f"No metadata catalog at {self.path.name}. Build it with: "
                "python scripts/build_metadata_catalog.py")
        try:
            # as_uri() percent-encodes '?'/'#'/spaces in deployment paths;
            # hand-building a file: URI makes those characters query syntax.
            con = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            version = con.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if not version or str(version[0]) != str(SCHEMA_VERSION):
                found = str(version[0]) if version else "missing"
                con.close()
                raise MetadataError(
                    f"Metadata catalog schema is {found}; this build expects "
                    f"{SCHEMA_VERSION}. Rebuild it with: "
                    "python scripts/build_metadata_catalog.py")
            return con
        except MetadataError:
            raise
        except sqlite3.DatabaseError as exc:
            raise MetadataError(
                "The metadata catalog is unreadable or incomplete. The source "
                "databases were not queried; rebuild it with: "
                "python scripts/build_metadata_catalog.py") from exc

    @staticmethod
    def _meta(con) -> dict:
        return {row["key"]: row["value"] for row in
                con.execute("SELECT key,value FROM meta")}

    def _snapshot(self, con) -> dict:
        meta = self._meta(con)
        built = meta.get("built_at", "")
        stale = False
        age_hours = None
        try:
            when = datetime.fromisoformat(built.replace("Z", "+00:00"))
            age_hours = max((_utcnow() - when).total_seconds() / 3600.0, 0.0)
            stale = age_hours > self.stale_after_hours
        except (TypeError, ValueError):
            stale = True
        degraded = [s for s in (meta.get("degraded") or "").split(",") if s]
        sources = [str(row[0]) for row in con.execute(
            "SELECT name FROM sources ORDER BY name")]
        try:
            limit_hits = json.loads(meta.get("limit_hits") or "[]")
        except (TypeError, ValueError):
            limit_hits = []
        return {
            "id": meta.get("snapshot_id", ""),
            "schema_version": int(meta.get("schema_version") or 0),
            "built_at": built,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
            "stale": stale,
            "stale_after_hours": self.stale_after_hours,
            "partial": meta.get("partial") == "yes",
            "status": ("partial" if meta.get("partial") == "yes" or degraded
                       else "complete"),
            "sources": sources,
            "sources_degraded": degraded,
            "limit_hits": limit_hits,
            "note": (
                "This is a derived metadata snapshot, not live financial "
                "evidence. Rebuild it after customizations or schema changes."
                + (" It is older than the configured freshness target."
                   if stale else "")
                + (" One or more sources/layers are partial; inspect catalog "
                   "status before treating absence as evidence."
                   if meta.get("partial") == "yes" or degraded else "")),
        }

    def describe(self) -> dict:
        if not self.available():
            return {"available": False,
                    "detail": f"No metadata catalog at {self.path.name}.",
                    "how_to_build": "python scripts/build_metadata_catalog.py"}
        try:
            con = self._open()
        except MetadataError as exc:
            return {"available": False, "detail": str(exc),
                    "how_to_build": "python scripts/build_metadata_catalog.py"}
        try:
            meta = self._meta(con)
            sources = [dict(row) for row in con.execute(
                "SELECT * FROM sources ORDER BY name")]
            kinds = [dict(row) for row in con.execute(
                "SELECT kind,COUNT(*) AS count FROM nodes GROUP BY kind "
                "ORDER BY count DESC,kind")]
            edges = [dict(row) for row in con.execute(
                "SELECT kind,COUNT(*) AS count FROM edges GROUP BY kind "
                "ORDER BY count DESC,kind")]
            note_count = int(con.execute(
                "SELECT COUNT(*) FROM notes").fetchone()[0])
            notes = [dict(row) for row in con.execute(
                "SELECT source,layer,note,ok,partial,status FROM notes "
                "ORDER BY source,layer LIMIT 100")]
            try:
                hits = json.loads(meta.get("limit_hits") or "[]")
            except (TypeError, ValueError):
                hits = []
            return {
                "available": True,
                "path": self.path.name,
                "schema_version": meta.get("schema_version", ""),
                "snapshot": self._snapshot(con),
                "sources": sources,
                "node_kinds": kinds,
                "edge_kinds": edges,
                "limit_hits": hits,
                "notes": notes,
                "note_count": note_count,
                "notes_truncated": note_count > len(notes),
                "search": ("full text" if meta.get("fts") == "yes" else
                           "substring fallback (FTS5 unavailable)"),
                "coverage_note": (
                    "Names, definitions and relationships only. No transaction "
                    "rows, balances, customer/vendor values, credentials or "
                    "full view SQL are stored. Constraint and dependency names "
                    "come only from native catalogs; unresolved targets stay "
                    "explicit. A metadata match can select a "
                    "source; it cannot substantiate a financial conclusion."),
            }
        finally:
            con.close()

    @staticmethod
    def _node(row) -> dict:
        attrs = _json(row["attrs"])
        out = {
            "id": row["id"],
            "source": row["source"], "schema": row["schema_name"],
            "kind": row["kind"], "name": row["name"],
            "label": row["label"] or None,
            "description": row["description"] or None,
            "confidence": {
                "tier": row["confidence"],
                "basis": attrs.get("mapping_basis") or row["evidence"],
            },
            "provenance": {
                "collector": row["collector"], "evidence": row["evidence"],
                "authority": row["authority"],
                "collected_at": row["collected_at"],
            },
        }
        if attrs:
            out["attributes"] = attrs
        return out

    def _related(self, con, node_id: str, limit: int = 8) -> tuple[list, bool]:
        rows = con.execute(
            "SELECT E.* FROM edges E WHERE E.src=? OR E.dst=? ORDER BY "
            "CASE E.confidence WHEN 'confirmed' THEN 0 WHEN 'corroborated' "
            "THEN 1 ELSE 2 END,E.kind,E.src,E.dst LIMIT ?",
            (node_id, node_id, limit + 1)).fetchall()
        out = []
        for row in rows[:limit]:
            other = row["dst"] if row["src"] == node_id else row["src"]
            node_row = con.execute(
                "SELECT * FROM nodes WHERE id=?", (other,)).fetchone()
            # A capped field harvest may leave an optional index edge without
            # its column node.  Never manufacture a relationship target.
            if node_row is None:
                continue
            node = self._node(node_row)
            out.append({
                "relationship": row["kind"],
                "direction": "outbound" if row["src"] == node_id else "inbound",
                "confidence": row["confidence"],
                "evidence": row["evidence"],
                "provenance": {"collector": row["collector"],
                               "authority": row["authority"]},
                "node": node,
            })
        return out, len(rows) > limit

    def _validate_source(self, con, source: str) -> str:
        name = _s(source)
        if not name:
            return ""
        if not con.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
            available = [row[0] for row in con.execute(
                "SELECT name FROM sources ORDER BY name")]
            raise MetadataError(
                f"Unknown metadata source {name!r}. Available sources: "
                f"{', '.join(available)}")
        return name

    @staticmethod
    def _evidence(row, *, confidence: str | None = None) -> dict:
        return {
            "collector": row["collector"],
            "evidence": row["evidence"],
            "authority": row["authority"],
            "confidence": confidence or row["confidence"],
            "collected_at": row["collected_at"],
        }

    @staticmethod
    def _confidence_rank(value: str) -> int:
        return {"confirmed": 0, "corroborated": 1, "candidate": 2,
                "inconclusive": 3}.get(str(value or "").lower(), 4)

    def _mapping_rows(self, con, object_id: str,
                      limit: int = 21) -> list[tuple]:
        out = []
        edges = con.execute(
            "SELECT * FROM edges WHERE dst=? AND "
            "kind='record_physicalizes_to' ORDER BY "
            "CASE confidence WHEN 'confirmed' THEN 0 WHEN 'corroborated' "
            "THEN 1 ELSE 2 END,src LIMIT ?", (object_id, limit)).fetchall()
        for edge in edges:
            record = con.execute(
                "SELECT * FROM nodes WHERE id=? AND kind='record'",
                (edge["src"],)).fetchone()
            if record is not None:
                out.append((edge, record))
        return out

    def _object_summary(self, con, row, *, reasons: list[str] | None = None,
                        matched_terms: list[str] | None = None,
                        relevance: int = 0, extra_evidence: list | None = None,
                        unresolved_record=None) -> dict:
        """One presentation object, with logical names and proof attached."""
        if unresolved_record is not None:
            attrs = _json(unresolved_record["attrs"])
            mapping_tier = attrs.get("mapping_confidence") or "inconclusive"
            mapping_basis = attrs.get("mapping_basis") or "physical mapping unresolved"
            mapping_authority = attrs.get("mapping_authority") or "declared"
            return {
                "object_id": unresolved_record["id"],
                "source": unresolved_record["source"],
                "schema": None,
                "kind": "record",
                "name": unresolved_record["name"],
                "logical_records": [unresolved_record["name"]],
                "physical_object": None,
                "label": unresolved_record["label"] or None,
                "confidence": {
                    "tier": mapping_tier,
                    "basis": mapping_basis,
                },
                "confidence_basis": mapping_basis,
                "match_reasons": sorted(set(reasons or [])),
                "matched_terms": sorted(set(matched_terms or [])),
                "term_coverage": 0.0,
                "relevance": relevance,
                "evidence": [{**self._evidence(unresolved_record),
                              "authority": mapping_authority,
                              "confidence": mapping_tier}],
                "attributes": attrs,
            }

        mappings = self._mapping_rows(con, row["id"])
        logical = [record["name"] for _edge, record in mappings[:20]]
        attrs = _json(row["attrs"])
        confidence = row["confidence"]
        basis = row["evidence"]
        evidence = [self._evidence(row)]
        label = row["label"] or None

        # Confidence must describe the path that produced this match.  An
        # object can have several logical records mapped to it at different
        # confidence levels; blindly taking the best mapping would make a
        # weak custom-record hit look confirmed because an unrelated record
        # also uses the same object.  Direct table/column hits are native
        # catalog observations.  Record/field/page/query hits use only a
        # mapping reached from the matched metadata node.
        matched_rows = list(extra_evidence or [])
        # Token search can match LEGACY_QUEUE inside ACME_LEGACY_QUEUE.  That
        # is useful retrieval, but it is not an exact physical-name proof.
        # Only a term equal to the full object name gets native-object
        # confidence; suffix/name-fragment hits retain their mapping tier.
        direct_object_match = row["name"] in set(matched_terms or [])
        related_record_ids: set[str] = set()
        for item in matched_rows[:20]:
            kind = item["kind"]
            if kind == "record":
                related_record_ids.add(item["id"])
            elif kind == "field":
                related_record_ids.update(r[0] for r in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='record_defines_field' LIMIT 51", (item["id"],)))
            elif kind == "code_value":
                for field in con.execute(
                        "SELECT src FROM edges WHERE dst=? AND "
                        "kind='field_has_code_value' LIMIT 21", (item["id"],)):
                    related_record_ids.update(r[0] for r in con.execute(
                        "SELECT src FROM edges WHERE dst=? AND "
                        "kind='record_defines_field' LIMIT 51", (field[0],)))
            elif kind in ("page", "query"):
                relationship = ("page_uses_record" if kind == "page"
                                else "query_uses_record")
                related_record_ids.update(r[0] for r in con.execute(
                    "SELECT dst FROM edges WHERE src=? AND kind=? LIMIT 51",
                    (item["id"], relationship)))
        selected_mapping = next(
            ((edge, record) for edge, record in mappings
             if record["id"] in related_record_ids), None)
        if not direct_object_match and selected_mapping is not None:
            edge, record = selected_mapping
            confidence = edge["confidence"]
            basis = edge["evidence"]
            label = record["label"] or label
            evidence.append(self._evidence(edge))
        for item in extra_evidence or []:
            ev = self._evidence(item)
            if ev not in evidence:
                evidence.append(ev)
        return {
            "object_id": row["id"],
            "source": row["source"],
            "schema": row["schema_name"],
            "kind": row["kind"],
            "name": row["name"],
            "logical_records": logical,
            "logical_records_truncated": len(mappings) > 20,
            "mappings": [{
                "logical_record": record["name"],
                "confidence": edge["confidence"],
                "confidence_basis": edge["evidence"],
            } for edge, record in mappings[:20]],
            "physical_object": row["name"],
            "label": label,
            "object_confidence": {
                "tier": row["confidence"], "basis": row["evidence"]},
            "confidence": {"tier": confidence, "basis": basis},
            "confidence_basis": basis,
            "match_reasons": sorted(set(reasons or [])),
            "matched_terms": sorted(set(matched_terms or [])),
            "term_coverage": 0.0,
            "relevance": relevance,
            "evidence": evidence,
            "attributes": attrs,
        }

    def _objects_for_node(self, con, node_id: str, max_hops: int = 3) -> list:
        """Resolve a matched field/label/code/page to bounded physical objects."""
        start = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if start is None:
            return []
        if start["kind"] in ("table", "view"):
            return [start]
        found: dict[str, sqlite3.Row] = {}

        def add_object(oid: str) -> None:
            row = con.execute(
                "SELECT * FROM nodes WHERE id=? AND kind IN ('table','view')",
                (oid,)).fetchone()
            if row is not None:
                found[row["id"]] = row

        def from_record(rid: str) -> None:
            for edge in con.execute(
                    "SELECT dst FROM edges WHERE src=? AND "
                    "kind='record_physicalizes_to' LIMIT 21", (rid,)):
                add_object(edge["dst"])

        def from_field(fid: str) -> None:
            for edge in con.execute(
                    "SELECT dst FROM edges WHERE src=? AND "
                    "kind='field_maps_to_column' LIMIT 51", (fid,)):
                from_column(edge["dst"])
            for edge in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='record_defines_field' LIMIT 101", (fid,)):
                from_record(edge["src"])

        def from_column(cid: str) -> None:
            for edge in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='object_has_column' LIMIT 21", (cid,)):
                add_object(edge["src"])

        def from_constraint(cid: str) -> None:
            for edge in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='object_has_constraint' LIMIT 21", (cid,)):
                add_object(edge["src"])

        kind = start["kind"]
        if kind == "record":
            from_record(node_id)
        elif kind == "field":
            from_field(node_id)
        elif kind == "column":
            from_column(node_id)
        elif kind == "index":
            for edge in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='object_has_index' LIMIT 21", (node_id,)):
                add_object(edge["src"])
        elif kind == "constraint":
            from_constraint(node_id)
        elif kind == "external_object":
            for edge in con.execute(
                    "SELECT src,kind FROM edges WHERE dst=? AND kind IN "
                    "('view_depends_on','foreign_key_references_object') "
                    "LIMIT 101", (node_id,)):
                if edge["kind"] == "view_depends_on":
                    add_object(edge["src"])
                else:
                    from_constraint(edge["src"])
        elif kind == "code_value":
            for edge in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='field_has_code_value' LIMIT 21", (node_id,)):
                from_field(edge["src"])
        elif kind in ("page", "query"):
            relationship = ("page_uses_record" if kind == "page"
                            else "query_uses_record")
            for edge in con.execute(
                    "SELECT dst FROM edges WHERE src=? AND kind=? LIMIT 101",
                    (node_id, relationship)):
                from_record(edge["dst"])
        return list(found.values())

    def _unresolved_records_for_node(self, con, node_id: str) -> list:
        """Logical records reached by a hit that have no proven table."""
        start = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if start is None:
            return []
        record_ids: set[str] = set()
        if start["kind"] == "record":
            record_ids.add(start["id"])
        elif start["kind"] == "field":
            record_ids.update(row[0] for row in con.execute(
                "SELECT src FROM edges WHERE dst=? AND "
                "kind='record_defines_field' LIMIT 101", (node_id,)))
        elif start["kind"] == "code_value":
            for row in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='field_has_code_value' LIMIT 21", (node_id,)):
                record_ids.update(r[0] for r in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='record_defines_field' LIMIT 101", (row[0],)))
        elif start["kind"] == "column":
            for row in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='field_maps_to_column' LIMIT 21", (node_id,)):
                record_ids.update(r[0] for r in con.execute(
                    "SELECT src FROM edges WHERE dst=? AND "
                    "kind='record_defines_field' LIMIT 101", (row[0],)))
        elif start["kind"] in ("page", "query"):
            relationship = ("page_uses_record" if start["kind"] == "page"
                            else "query_uses_record")
            record_ids.update(row[0] for row in con.execute(
                "SELECT dst FROM edges WHERE src=? AND kind=? LIMIT 101",
                (node_id, relationship)))
        out = []
        for rid in sorted(record_ids):
            mapped = con.execute(
                "SELECT 1 FROM edges WHERE src=? AND "
                "kind='record_physicalizes_to' LIMIT 1", (rid,)).fetchone()
            if mapped:
                continue
            row = con.execute(
                "SELECT * FROM nodes WHERE id=? AND kind='record'", (rid,)
            ).fetchone()
            if row is not None:
                out.append(row)
        return out

    @staticmethod
    def _unavailable(path: Path) -> dict:
        return {
            "available": False,
            "detail": f"No readable metadata catalog at {path.name}.",
            "how_to_build": "python scripts/build_metadata_catalog.py",
        }

    def _translate_values(self, con, field_id: str, limit: int) -> tuple[list, bool]:
        rows = con.execute(
            "SELECT N.* FROM edges E JOIN nodes N ON N.id=E.dst "
            "WHERE E.src=? AND E.kind='field_has_code_value' "
            "ORDER BY N.name,N.id LIMIT ?", (field_id, limit + 1)).fetchall()
        codes = []
        for code in rows[:limit]:
            attrs = _json(code["attrs"])
            codes.append({
                "_id": code["id"],
                "value": code["name"].split("=", 1)[-1],
                "label": code["label"] or None,
                "effective_date": attrs.get("effdt"),
                "status": attrs.get("eff_status"),
                "sequence": attrs.get("xlatseqno"),
            })
        catalog_as_of = (self._meta(con).get("built_at") or "")[:10]
        by_value: dict[str, list] = {}
        for code in codes:
            by_value.setdefault(code["value"], []).append(code)
        current_ids = set()
        for versions in by_value.values():
            active = [
                value for value in versions
                if _u(value.get("status")) in ("", "A")
                and (not _s(value.get("effective_date")) or not catalog_as_of
                     or _s(value.get("effective_date"))[:10] <= catalog_as_of)]
            if active:
                current = max(
                    active, key=lambda value: (
                        _s(value.get("effective_date")),
                        int(value.get("sequence") or 0), value["_id"]))
                current_ids.add(current["_id"])
        for code in codes:
            code["current"] = code["_id"] in current_ids
            code.pop("_id", None)
        return codes, len(rows) > limit

    def search(self, query: str, source: str = "", kinds: str = "",
               limit: int = 20) -> dict:
        text = _s(query)
        if not text:
            raise MetadataError("search_metadata needs something to search for")
        if len(text) > 500:
            raise MetadataError("search_metadata query is limited to 500 characters")
        try:
            cap = min(max(int(limit or 20), 1), MAX_RESULT_CAP)
        except (TypeError, ValueError) as exc:
            raise MetadataError("search_metadata limit must be a whole number") from exc
        if not self.available():
            return self._unavailable(self.path)
        terms = _terms(text)
        if not terms:
            return {"available": self.available(), "query": text, "matches": [],
                    "detail": "No meaningful metadata terms were supplied."}
        try:
            con = self._open()
        except MetadataError:
            return self._unavailable(self.path)
        try:
            src = self._validate_source(con, source)
            wanted = {_u(k) for k in re.split(r"[\s,]+", kinds or "") if k}
            if wanted:
                available_kinds = {str(row[0]).upper() for row in con.execute(
                    "SELECT DISTINCT kind FROM nodes")}
                unknown = sorted(wanted - available_kinds)
                if unknown:
                    raise MetadataError(
                        "Unknown metadata kind(s): " + ", ".join(unknown) +
                        ". Available kinds: " +
                        ", ".join(sorted(available_kinds)))
            has_fts = bool(con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone())
            hits: list[sqlite3.Row] = []
            scan_cap = max(cap * 80, 200)
            node_filters = []
            filter_params: list = []
            if src:
                node_filters.append("N.source=?")
                filter_params.append(src)
            if wanted:
                marks = ",".join("?" for _ in wanted)
                node_filters.append(f"UPPER(N.kind) IN ({marks})")
                filter_params.extend(sorted(wanted))
            filter_sql = (" AND " + " AND ".join(node_filters)
                          if node_filters else "")
            if has_fts:
                expression = " OR ".join(f'"{t}"*' for t in terms)
                hits = con.execute(
                    "SELECT F.node_id,F.facet,F.text,bm25(search_fts) AS rank "
                    "FROM search_fts F JOIN nodes N ON N.id=F.node_id "
                    "WHERE search_fts MATCH ?" + filter_sql +
                    " ORDER BY rank LIMIT ?",
                    [expression, *filter_params, scan_cap]).fetchall()
            else:
                seen = set()
                for term in terms:
                    for row in con.execute(
                            "SELECT T.node_id,T.facet,T.text,0 AS rank "
                            "FROM search_terms T JOIN nodes N ON N.id=T.node_id "
                            "WHERE UPPER(T.text) LIKE ?" + filter_sql +
                            " LIMIT ?", [f"%{term}%", *filter_params, scan_cap]):
                        key = (row["node_id"], row["facet"], row["text"])
                        if key not in seen:
                            hits.append(row)
                            seen.add(key)
            by_node: dict[str, dict] = {}
            for hit in hits:
                entry = by_node.setdefault(hit["node_id"],
                                           {"facets": set(), "texts": []})
                entry["facets"].add(hit["facet"])
                entry["texts"].append(_u(hit["text"]))
            if not by_node:
                return {"available": True, "query": text, "terms": terms,
                        "matches": [], "count": 0, "truncated": False,
                        "snapshot": self._snapshot(con),
                        "detail": "No indexed metadata matched those terms."}
            grouped: dict[str, dict] = {}
            phrase = _u(text).replace("_", " ")
            facet_weight = {"name": 50, "physical object": 45,
                            "logical record": 45, "field label": 35,
                            "translate value": 30, "description": 25,
                            "field name": 25, "used by page": 15,
                            "used by query": 15, "constraint columns": 30,
                            "referenced object": 30,
                            "view dependency": 30}
            for nid, found in by_node.items():
                row = con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
                if row is None:
                    continue
                found = by_node[nid]
                objects = self._objects_for_node(con, nid)
                unresolved_records = self._unresolved_records_for_node(con, nid)
                targets = [*objects, *unresolved_records]
                for target in targets:
                    unresolved = target if target["kind"] == "record" else None
                    key = target["id"]
                    group = grouped.setdefault(key, {
                        "row": target, "unresolved": unresolved,
                        "facets": set(), "texts": [], "evidence": [],
                        "matched_metadata": {}})
                    group["facets"].update(found["facets"])
                    group["texts"].extend(found["texts"])
                    group["evidence"].append(row)
                    group["matched_metadata"][row["id"]] = {
                        "kind": row["kind"], "name": row["name"],
                        "label": row["label"] or None,
                        "facets": sorted(found["facets"]),
                        "matched_terms": [t for t in terms if any(
                            t in value for value in found["texts"])],
                        "provenance": self._evidence(row),
                    }
            ranked = []
            for group in grouped.values():
                matched_terms = [t for t in terms if any(
                    t in value for value in group["texts"])]
                coverage = len(matched_terms) / len(terms)
                score = int(coverage * 100) + sum(
                    facet_weight.get(facet, 10) for facet in group["facets"])
                summary = self._object_summary(
                    con, group["row"],
                    reasons=[
                        f"{facet}: {item['name']}"
                        for item in group["matched_metadata"].values()
                        for facet in item["facets"]],
                    matched_terms=matched_terms, relevance=score,
                    extra_evidence=group["evidence"],
                    unresolved_record=group["unresolved"])
                summary["matched_metadata"] = list(
                    group["matched_metadata"].values())[:10]
                names = [summary.get("physical_object") or "",
                         *summary.get("logical_records", [])]
                if any(phrase == _u(name).replace("_", " ") or
                       _u(text) == _u(name) for name in names):
                    summary["relevance"] += 200
                summary["term_coverage"] = round(coverage, 3)
                ranked.append(summary)
            ranked.sort(key=lambda item: (
                -item["relevance"],
                item["source"], item.get("schema") or "", item["name"]))
            snapshot = self._snapshot(con)
            return {
                "available": True, "query": text, "terms": terms,
                "source_filter": src or None,
                "kind_filter": sorted(wanted),
                "matches": ranked[:cap], "count": len(ranked[:cap]),
                "truncated": len(ranked) > cap or len(hits) >= scan_cap,
                "snapshot": snapshot,
                "coverage_note": (
                    "Search covers the offline database catalog and available "
                    "PeopleTools definitions for the listed sources. It stores "
                    "structure only and is not financial evidence."
                    + (" One or more build limits were reached; absence is "
                       "inconclusive." if snapshot["limit_hits"] else "")),
                "note": (
                    "Relevance combines explainable term coverage and metadata "
                    "facets. Confidence describes the source relationship, not "
                    "the financial correctness of a future query. Use "
                    "get_metadata_context before querying an unfamiliar object; "
                    "a catalog result is not financial evidence."),
            }
        finally:
            con.close()

    def context(self, identifier: str, source: str = "", limit: int = 40) -> dict:
        asked = _u(identifier)
        if not asked:
            raise MetadataError("get_metadata_context needs an identifier")
        if len(str(identifier)) > 500:
            raise MetadataError(
                "get_metadata_context identifier is limited to 500 characters")
        try:
            cap = min(max(int(limit or 40), 1), MAX_RESULT_CAP)
        except (TypeError, ValueError) as exc:
            raise MetadataError(
                "get_metadata_context limit must be a whole number") from exc
        if not self.available():
            return self._unavailable(self.path)
        try:
            con = self._open()
        except MetadataError:
            return self._unavailable(self.path)
        try:
            src = self._validate_source(con, source)
            params: list = [asked]
            sql = ("SELECT N.*,A.facet AS alias_facet FROM aliases A JOIN nodes N "
                   "ON N.id=A.node_id WHERE A.alias_upper=?")
            if src:
                sql += " AND A.source=?"
                params.append(src)
            sql += (" ORDER BY CASE N.kind WHEN 'table' THEN 0 WHEN 'view' "
                    "THEN 1 WHEN 'record' THEN 2 ELSE 3 END,N.source,"
                    "N.schema_name,N.name LIMIT ?")
            params.append(cap + 1)
            candidates = con.execute(sql, params).fetchall()
            if not candidates:
                params = [asked]
                sql = "SELECT *, 'name' AS alias_facet FROM nodes WHERE UPPER(name)=?"
                if src:
                    sql += " AND source=?"
                    params.append(src)
                sql += " ORDER BY source,schema_name,kind,name LIMIT ?"
                params.append(cap + 1)
                candidates = con.execute(sql, params).fetchall()
            if not candidates:
                return {"available": True, "identifier": identifier,
                        "found": False, "snapshot": self._snapshot(con),
                        "detail": "No exact logical name, physical name or alias "
                                  "matched. Use search_metadata first."}
            chosen = candidates[0]
            matched_alias = candidates[0]["alias_facet"]
            source_names = {row["source"] for row in candidates}
            physical_candidates = {row["id"]: row for row in candidates
                                   if row["kind"] in ("table", "view")}
            if not src and len(source_names) > 1:
                return {
                    "available": True, "identifier": identifier,
                    "found": False, "ambiguous": True,
                    "candidates": [self._node(row) for row in candidates[:cap]],
                    "truncated": len(candidates) > cap,
                    "snapshot": self._snapshot(con),
                    "detail": "That identifier exists in more than one source. "
                              "Pass source= explicitly; same-named objects are "
                              "never merged across databases.",
                }
            if not physical_candidates:
                for candidate in candidates[:MAX_RESULT_CAP + 1]:
                    for resolved in self._objects_for_node(
                            con, candidate["id"]):
                        physical_candidates[resolved["id"]] = resolved
            unresolved_records: dict[str, sqlite3.Row] = {}
            for candidate in candidates[:MAX_RESULT_CAP + 1]:
                for record in self._unresolved_records_for_node(
                        con, candidate["id"]):
                    unresolved_records[record["id"]] = record
            if not physical_candidates and len(unresolved_records) == 1:
                chosen = next(iter(unresolved_records.values()))
            elif not physical_candidates and len(unresolved_records) > 1:
                choices = list(unresolved_records.values())
                return {
                    "available": True, "identifier": identifier,
                    "found": False, "ambiguous": True,
                    "candidates": [self._object_summary(
                        con, row, unresolved_record=row)
                        for row in choices[:cap]],
                    "truncated": len(choices) > cap,
                    "snapshot": self._snapshot(con),
                    "detail": "That field belongs to more than one unresolved "
                              "logical record. Pass the record name explicitly.",
                }
            # Two schemas in one source may legitimately contain the same
            # unqualified name. A shared PeopleTools field can likewise map
            # to several physical objects. Never choose one by sort order.
            if len(physical_candidates) > 1:
                choices = list(physical_candidates.values())
                return {
                    "available": True, "identifier": identifier,
                    "found": False, "ambiguous": True,
                    "candidates": [self._object_summary(con, row) for row in
                                   choices[:cap]],
                    "truncated": len(choices) > cap,
                    "snapshot": self._snapshot(con),
                    "detail": (
                        "That identifier maps to more than one physical object. "
                        "Pass source/schema/object (or a qualified column) "
                        "explicitly."),
                }

            object_row = (next(iter(physical_candidates.values()))
                          if physical_candidates else None)
            if object_row is None and chosen["kind"] == "record":
                edge = con.execute(
                    "SELECT dst FROM edges WHERE src=? AND "
                    "kind='record_physicalizes_to' ORDER BY dst LIMIT 1",
                    (chosen["id"],)).fetchone()
                if edge:
                    object_row = con.execute(
                        "SELECT * FROM nodes WHERE id=?", (edge["dst"],)).fetchone()

            columns = []
            indexes = []
            constraints = []
            dependencies = []
            mappings = []
            relationships = []
            context_truncated = False
            if object_row is not None:
                for edge, record in self._mapping_rows(
                        con, object_row["id"], cap + 1):
                    mappings.append({
                        "logical_record": record["name"],
                        "physical_object": object_row["name"],
                        "confidence": edge["confidence"],
                        "confidence_basis": edge["evidence"],
                        "evidence": [self._evidence(edge)],
                    })
                for edge in con.execute(
                        "SELECT * FROM edges WHERE src=? AND "
                        "kind='object_has_index' ORDER BY dst LIMIT ?",
                        (object_row["id"], cap + 1)):
                    node = con.execute(
                        "SELECT * FROM nodes WHERE id=?", (edge["dst"],)).fetchone()
                    if node is None:
                        continue
                    attrs = _json(node["attrs"])
                    indexes.append({
                        "name": node["name"], "unique": bool(attrs.get("unique")),
                        "columns": list(attrs.get("columns") or []),
                        "expression_based": bool(attrs.get("expression_based")),
                        "filtered": bool(attrs.get("filtered")),
                        "coverage_note": (
                            "Expression terms are present; only named key "
                            "columns are listed."
                            if attrs.get("expression_based") else
                            "This is a partial/filtered index; uniqueness is "
                            "not a whole-table key."
                            if attrs.get("filtered") else None),
                        "evidence": [self._evidence(node)],
                    })
                for edge in con.execute(
                        "SELECT * FROM edges WHERE src=? AND "
                        "kind='object_has_constraint' ORDER BY dst LIMIT ?",
                        (object_row["id"], cap + 1)):
                    node = con.execute(
                        "SELECT * FROM nodes WHERE id=?", (edge["dst"],)
                    ).fetchone()
                    if node is None:
                        continue
                    attrs = _json(node["attrs"])
                    target_edge = con.execute(
                        "SELECT * FROM edges WHERE src=? AND "
                        "kind='foreign_key_references_object' LIMIT 1",
                        (node["id"],),
                    ).fetchone()
                    reference = None
                    if target_edge is not None:
                        target = con.execute(
                            "SELECT * FROM nodes WHERE id=?",
                            (target_edge["dst"],),
                        ).fetchone()
                        if target is not None:
                            target_attrs = _json(target["attrs"])
                            reference = {
                                "source": target["source"],
                                "schema": target["schema_name"],
                                "object": (None if target["name"] == "UNKNOWN"
                                           else target["name"]),
                                "kind": target["kind"],
                                "resolution_status": _json(
                                    target_edge["attrs"]).get(
                                        "resolution_status") or
                                    target_attrs.get("resolution_status") or
                                    "unknown",
                                "referenced_constraint": attrs.get(
                                    "referenced_constraint"),
                            }
                    constraints.append({
                        "name": node["name"],
                        "type": attrs.get("constraint_type"),
                        "columns": list(attrs.get("columns") or []),
                        "column_pairs": list(attrs.get("column_pairs") or []),
                        "reference": reference,
                        "generated_name": bool(attrs.get("generated_name")),
                        "rowset_complete": attrs.get("rowset_complete", True),
                        "status": attrs.get("status"),
                        "validated": attrs.get("validated"),
                        "delete_rule": attrs.get("delete_rule"),
                        "update_rule": attrs.get("update_rule"),
                        "evidence": [self._evidence(node)],
                    })
                dependency_rows = con.execute(
                    "SELECT E.src,E.dst,E.confidence,E.evidence,E.collector,"
                    "E.authority,E.collected_at,E.attrs,N.source,"
                    "N.schema_name,N.name,N.kind AS node_kind FROM edges E "
                    "JOIN nodes N ON "
                    "N.id=CASE WHEN E.src=? THEN E.dst ELSE E.src END "
                    "WHERE E.kind='view_depends_on' AND (E.src=? OR E.dst=?) "
                    "ORDER BY E.src,E.dst LIMIT ?",
                    (object_row["id"], object_row["id"], object_row["id"],
                     cap + 1),
                ).fetchall()
                for dep in dependency_rows:
                    dep_attrs = _json(dep["attrs"])
                    dependencies.append({
                        "direction": ("outbound" if dep["src"] ==
                                      object_row["id"] else "inbound"),
                        "relationship": "view_depends_on",
                        "source": dep["source"],
                        "schema": dep["schema_name"],
                        "object": dep["name"],
                        "kind": dep["node_kind"],
                        "resolution_status": dep_attrs.get(
                            "resolution_status") or "unknown",
                        "confidence": dep["confidence"],
                        "evidence": [{
                            "collector": dep["collector"],
                            "evidence": dep["evidence"],
                            "authority": dep["authority"],
                            "confidence": dep["confidence"],
                            "collected_at": dep["collected_at"],
                        }],
                    })
                col_rows = []
                for edge in con.execute(
                        "SELECT * FROM edges WHERE src=? AND "
                        "kind='object_has_column' LIMIT ?",
                        (object_row["id"], cap + 1)):
                    node = con.execute(
                        "SELECT * FROM nodes WHERE id=?", (edge["dst"],)).fetchone()
                    if node is not None:
                        col_rows.append((int(_json(edge["attrs"]).get("ordinal") or 0),
                                         node))
                for _ordinal, node in sorted(col_rows,
                                             key=lambda pair: (pair[0], pair[1]["name"])):
                    attrs = _json(node["attrs"])
                    item = {
                        "name": node["name"],
                        "ordinal": attrs.get("ordinal"),
                        "data_type": attrs.get("data_type") or None,
                        "data_length": attrs.get("data_length"),
                        "nullable": attrs.get("nullable") or None,
                        "evidence": [self._evidence(node)],
                    }
                    field_edge = con.execute(
                        "SELECT * FROM edges WHERE dst=? AND "
                        "kind='field_maps_to_column' ORDER BY src LIMIT 1",
                        (node["id"],)).fetchone()
                    if field_edge:
                        field = con.execute(
                            "SELECT * FROM nodes WHERE id=?", (field_edge["src"],)
                        ).fetchone()
                        if field is not None:
                            item["field_label"] = field["label"] or None
                            codes, codes_truncated = self._translate_values(
                                con, field["id"], cap)
                            context_truncated = (context_truncated or
                                                 codes_truncated)
                            if codes:
                                item["translate_values"] = codes
                    columns.append(item)
            elif chosen["kind"] == "record":
                record_attrs = _json(chosen["attrs"])
                mappings.append({
                    "logical_record": chosen["name"],
                    "physical_object": None,
                    "confidence": record_attrs.get("mapping_confidence",
                                                   "inconclusive"),
                    "confidence_basis": record_attrs.get(
                        "mapping_basis", "physical mapping unresolved"),
                    "evidence": [self._evidence(chosen)],
                })
                field_rows = []
                for edge in con.execute(
                        "SELECT * FROM edges WHERE src=? AND "
                        "kind='record_defines_field' LIMIT ?",
                        (chosen["id"], cap + 1)):
                    field = con.execute(
                        "SELECT * FROM nodes WHERE id=?", (edge["dst"],)
                    ).fetchone()
                    if field is not None:
                        ordinal = int(_json(edge["attrs"]).get("ordinal") or 0)
                        field_rows.append((ordinal, field))
                context_truncated = len(field_rows) > cap
                for ordinal, field in sorted(
                        field_rows, key=lambda pair: (pair[0], pair[1]["name"]))[:cap]:
                    attrs = _json(field["attrs"])
                    codes, codes_truncated = self._translate_values(
                        con, field["id"], cap)
                    context_truncated = context_truncated or codes_truncated
                    item = {
                        "name": field["name"], "ordinal": ordinal or None,
                        "physical_column": None,
                        "data_type": ("PeopleTools type code " +
                                      str(attrs.get("FIELDTYPE"))
                                      if attrs.get("FIELDTYPE") is not None
                                      else None),
                        "data_length": attrs.get("LENGTH"),
                        "nullable": None,
                        "field_label": field["label"] or None,
                        "evidence": [self._evidence(field)],
                    }
                    if codes:
                        item["translate_values"] = codes
                    columns.append(item)

            total = (len(mappings) + len(constraints) + len(dependencies)
                     + len(indexes) + len(columns))
            remaining = cap
            mappings_out = mappings[:remaining]
            remaining -= len(mappings_out)
            constraints_out = constraints[:remaining]
            remaining -= len(constraints_out)
            dependencies_out = dependencies[:remaining]
            remaining -= len(dependencies_out)
            indexes_out = indexes[:remaining]
            remaining -= len(indexes_out)
            columns_out = columns[:remaining]
            relationships.extend(
                [{"relationship": "record_physicalizes_to", **item}
                 for item in mappings_out])
            relationships.extend(
                [{"relationship": "object_has_constraint", **item}
                 for item in constraints_out])
            relationships.extend(dependencies_out)
            subject = (self._object_summary(con, object_row)
                       if object_row is not None else
                       self._object_summary(con, chosen,
                                            unresolved_record=chosen))
            snapshot = self._snapshot(con)
            notes = [row[0] for row in con.execute(
                "SELECT note FROM notes WHERE source=? ORDER BY layer LIMIT 21",
                (chosen["source"],))]
            return {
                "available": True, "identifier": identifier, "found": True,
                "matched_alias": matched_alias,
                "subject": subject,
                "object": subject,
                "object_id": subject["object_id"],
                "source": subject["source"],
                "schema": subject.get("schema"),
                "physical_object": subject.get("physical_object"),
                "logical_records": subject.get("logical_records", []),
                "columns": columns_out,
                "indexes": indexes_out,
                "constraints": constraints_out,
                "dependencies": dependencies_out,
                "mappings": mappings_out,
                "relationships": relationships,
                "notes": notes,
                "truncated": total > cap or context_truncated,
                "snapshot": snapshot,
                "coverage_note": (
                    "Object shape, keys and lineage come from the offline "
                    "metadata catalog. They contain no transaction rows or "
                    "full view definitions and cannot "
                    "substantiate a financial conclusion."
                    + (" Build limits were reached; missing metadata is "
                       "inconclusive." if snapshot["limit_hits"] else "")),
                "how_to_use": (
                    "Treat confirmed/observed mappings as structural facts; "
                    "corroborated suffix mappings as evidence; candidate or "
                    "inconclusive mappings require review. Apply caller scope, "
                    "date/status/currency semantics and live financial tools "
                    "separately."),
            }
        finally:
            con.close()
