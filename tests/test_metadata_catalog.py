"""The offline metadata catalog: structure from every configured source.

These tests deliberately use company-prefixed physical names, a catalog-only
warehouse, stale PeopleTools declarations, and transaction rows containing
recognisable sentinels.  The catalog is useful only if it can explain how a
logical PeopleTools record maps to the physical database while remaining a
STRUCTURE artifact -- never a quiet extract of customer or financial data.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config, DbCfg  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.metadata import (  # noqa: E402
    MetadataBuildLimits,
    MetadataCatalog,
    build_catalog,
)


ROW_SENTINEL = "SENTINEL JANE DOE"
AMOUNT_SENTINEL = "9876543.21"
SECRET_SENTINEL = "metadata-test-password-do-not-copy"


def _primary_database(path: Path) -> None:
    """A small, non-delivered PeopleSoft catalog plus real physical objects."""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE PSRECDEFN (
              RECNAME TEXT, RECDESCR TEXT, RECTYPE INTEGER,
              SQLTABLENAME TEXT
            );
            CREATE TABLE PSRECFIELD (
              RECNAME TEXT, FIELDNAME TEXT, FIELDNUM INTEGER, USEEDIT INTEGER
            );
            CREATE TABLE PSDBFIELD (
              FIELDNAME TEXT, FIELDTYPE INTEGER, LENGTH INTEGER,
              DECIMALPOS INTEGER, FORMAT TEXT
            );
            CREATE TABLE PSDBFLDLABL (
              FIELDNAME TEXT, LABEL_ID TEXT, SHORTNAME TEXT, LONGNAME TEXT
            );
            CREATE TABLE PSXLATITEM (
              FIELDNAME TEXT, FIELDVALUE TEXT, XLATSHORTNAME TEXT,
              XLATLONGNAME TEXT, EFFDT TEXT, EFF_STATUS TEXT, XLATSEQNO INTEGER
            );
            CREATE TABLE PSQRYDEFN (OPRID TEXT, QRYNAME TEXT);
            CREATE TABLE PSQRYRECORD (
              OPRID TEXT, QRYNAME TEXT, RECNAME TEXT
            );

            CREATE TABLE CORP_AR_QUEUE (
              BUSINESS_UNIT TEXT NOT NULL,
              INTERFACE_ID TEXT NOT NULL,
              X_APPR_STAT TEXT,
              CREATED_DTTM TEXT,
              CUSTOMER_NAME TEXT,
              AMOUNT REAL
            );
            CREATE UNIQUE INDEX CORP_AR_QUEUE_U1
              ON CORP_AR_QUEUE (BUSINESS_UNIT, INTERFACE_ID);

            CREATE TABLE ACME_LEGACY_QUEUE (
              BUSINESS_UNIT TEXT, INTERFACE_ID TEXT, LOAD_STATUS TEXT
            );
            CREATE INDEX ACME_LEGACY_QUEUE_I1
              ON ACME_LEGACY_QUEUE (INTERFACE_ID, BUSINESS_UNIT);

            CREATE TABLE A_AMBIG_QUEUE (INTERFACE_ID TEXT);
            CREATE TABLE B_AMBIG_QUEUE (INTERFACE_ID TEXT);
            CREATE TABLE CORP_ARXQ (INTERFACE_ID TEXT);
            CREATE VIEW WRONG_KIND_OBJ AS
              SELECT INTERFACE_ID FROM CORP_AR_QUEUE;

            CREATE TABLE PRIVATE_AR_DATA (
              CUSTOMER_NAME TEXT, OPEN_AMOUNT REAL
            );

            CREATE TABLE CORP_CUSTOMER_KEY (
              BUSINESS_UNIT TEXT NOT NULL,
              CUSTOMER_ID TEXT NOT NULL,
              PRIMARY KEY (BUSINESS_UNIT, CUSTOMER_ID)
            );
            CREATE TABLE CORP_AR_DETAIL (
              BUSINESS_UNIT TEXT NOT NULL,
              CUSTOMER_ID TEXT NOT NULL,
              LINE_NBR INTEGER NOT NULL,
              DETAIL_STATUS TEXT,
              PRIMARY KEY (BUSINESS_UNIT, CUSTOMER_ID, LINE_NBR),
              UNIQUE (BUSINESS_UNIT, CUSTOMER_ID),
              FOREIGN KEY (BUSINESS_UNIT, CUSTOMER_ID)
                REFERENCES CORP_CUSTOMER_KEY (BUSINESS_UNIT, CUSTOMER_ID)
            );
            CREATE TABLE CORP_UNRESOLVED_REF (
              EXTERNAL_ID TEXT,
              FOREIGN KEY (EXTERNAL_ID)
                REFERENCES NOT_DEPLOYED_OBJECT (EXTERNAL_ID)
            );
            CREATE VIEW CORP_AR_DETAIL_V AS
              SELECT BUSINESS_UNIT, CUSTOMER_ID, LINE_NBR
              FROM CORP_AR_DETAIL;
            """
        )
        con.executemany(
            "INSERT INTO PSRECDEFN VALUES (?,?,?,?)",
            [
                ("Z_AR_QUEUE", "Phoenix receivables approval queue", 0,
                 "CORP_AR_QUEUE"),
                ("LEGACY_QUEUE", "Legacy interface load queue", 0, ""),
                ("AMBIG_QUEUE", "Ambiguous interface queue", 0, ""),
                ("OLD_QUEUE", "Retired interface declaration", 0,
                 "MISSING_OLD_QUEUE"),
                ("AR_Q", "Underscore wildcard trap", 0, ""),
                ("DERIVED_WRK", "Derived work record", 2,
                 "CORP_AR_QUEUE"),
                ("WRONG_KIND_REC", "Table record pointing at a view", 0,
                 "WRONG_KIND_OBJ"),
            ],
        )
        con.executemany(
            "INSERT INTO PSRECFIELD VALUES (?,?,?,?)",
            [
                ("Z_AR_QUEUE", "BUSINESS_UNIT", 1, 1),
                ("Z_AR_QUEUE", "INTERFACE_ID", 2, 1),
                ("Z_AR_QUEUE", "X_APPR_STAT", 3, 0),
                ("Z_AR_QUEUE", "CREATED_DTTM", 4, 0),
                ("LEGACY_QUEUE", "INTERFACE_ID", 1, 1),
                ("LEGACY_QUEUE", "LOAD_STATUS", 2, 0),
                ("AMBIG_QUEUE", "INTERFACE_ID", 1, 1),
                ("OLD_QUEUE", "INTERFACE_ID", 1, 1),
                ("OLD_QUEUE", "X_SECRET_STAT", 2, 0),
                ("AR_Q", "INTERFACE_ID", 1, 1),
                ("DERIVED_WRK", "INTERFACE_ID", 1, 1),
                ("WRONG_KIND_REC", "INTERFACE_ID", 1, 1),
            ],
        )
        con.executemany(
            "INSERT INTO PSDBFIELD VALUES (?,?,?,?,?)",
            [
                ("BUSINESS_UNIT", 0, 5, 0, ""),
                ("INTERFACE_ID", 0, 20, 0, ""),
                ("X_APPR_STAT", 0, 1, 0, ""),
                ("CREATED_DTTM", 5, 26, 0, ""),
                ("LOAD_STATUS", 0, 1, 0, ""),
                ("X_SECRET_STAT", 0, 1, 0, ""),
            ],
        )
        con.executemany(
            "INSERT INTO PSDBFLDLABL VALUES (?,?,?,?)",
            [
                ("X_APPR_STAT", "", "Approval", "Approval Status"),
                ("LOAD_STATUS", "", "Load Sts", "Interface Load Status"),
                ("X_SECRET_STAT", "", "Bespoke", "Bespoke Approval Status"),
            ],
        )
        con.executemany(
            "INSERT INTO PSXLATITEM VALUES (?,?,?,?,?,?,?)",
            [
                ("X_APPR_STAT", "A", "Approved", "Approved for posting",
                 "2020-01-01", "A", 1),
                ("X_APPR_STAT", "E", "Error", "Approval error",
                 "2020-01-01", "A", 2),
            ],
        )
        con.executemany(
            "INSERT INTO PSQRYDEFN VALUES (?,?)",
            [
                ("", "CUSTOM_APPROVAL_QRY"),
                ("MRAO", "PRIVATE_SECRET_QRY"),
            ],
        )
        con.executemany(
            "INSERT INTO PSQRYRECORD VALUES (?,?,?)",
            [
                ("", "CUSTOM_APPROVAL_QRY", "Z_AR_QUEUE"),
                ("MRAO", "PRIVATE_SECRET_QRY", "Z_AR_QUEUE"),
            ],
        )
        con.execute(
            "INSERT INTO PRIVATE_AR_DATA VALUES (?, ?)",
            (ROW_SENTINEL, float(AMOUNT_SENTINEL)),
        )
        con.commit()
    finally:
        con.close()


def _warehouse_database(path: Path) -> None:
    """A configured non-PeopleSoft source; native catalog evidence only."""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE DW_AR_FACT (
              WAREHOUSE_BU TEXT, INVOICE_ID TEXT, APPROVAL_STATE TEXT
            );
            CREATE INDEX DW_AR_FACT_I1
              ON DW_AR_FACT (WAREHOUSE_BU, INVOICE_ID);

            -- Same physical name as the primary on purpose. Source identity
            -- must prevent these two unrelated objects from being merged.
            CREATE TABLE CORP_AR_QUEUE (
              TENANT_ID TEXT, WAREHOUSE_EVENT_ID TEXT
            );
            CREATE INDEX WH_CORP_AR_QUEUE_I1
              ON CORP_AR_QUEUE (TENANT_ID, WAREHOUSE_EVENT_ID);
            """
        )
        con.commit()
    finally:
        con.close()


def _drop_optional_peopletools_columns(path: Path) -> None:
    """Leave the identity columns while removing optional label/type shape."""
    con = sqlite3.connect(path)
    try:
        con.execute("ALTER TABLE PSDBFIELD DROP COLUMN FORMAT")
        con.execute("ALTER TABLE PSDBFLDLABL DROP COLUMN LONGNAME")
        con.execute("ALTER TABLE PSXLATITEM DROP COLUMN XLATLONGNAME")
        con.commit()
    finally:
        con.close()


def _artifact_text(path: Path) -> str:
    """All persisted text, without relying only on raw SQLite byte layout."""
    con = sqlite3.connect(path)
    try:
        parts: list[str] = []
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            # Names come from the artifact's own catalog, not user input.
            try:
                rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
            except sqlite3.DatabaseError:
                continue
            for row in rows:
                parts.extend(str(value) for value in row if value is not None)
        return "\n".join(parts)
    finally:
        con.close()


def _set_meta(path: Path, key: str, value: str) -> None:
    con = sqlite3.connect(path)
    try:
        changed = con.execute(
            "UPDATE meta SET value = ? WHERE key = ?", (value, key))
        if not changed.rowcount:
            con.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                        (key, value))
        con.commit()
    finally:
        con.close()


class _FixtureCase(unittest.TestCase):
    """Shared source databases; each test gets its own catalog artifact."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-metadata-")
        self.root = Path(self.temp.name)
        self.primary_path = self.root / "primary.db"
        self.warehouse_path = self.root / "warehouse.db"
        self.catalog_path = self.root / "metadata_catalog.db"
        _primary_database(self.primary_path)
        _warehouse_database(self.warehouse_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(self) -> Config:
        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(self.primary_path)
        # Irrelevant to SQLite, intentionally present to prove configuration
        # credentials do not become searchable metadata.
        cfg.db.oracle_password = SECRET_SENTINEL
        cfg.sources = {
            "warehouse": DbCfg(
                backend="sqlite", sqlite_path=str(self.warehouse_path))
        }
        return cfg

    def _build(self, *, limits: MetadataBuildLimits | None = None,
               primary_cls=Database) -> tuple[MetadataCatalog, dict]:
        cfg = self._config()
        limits = limits or MetadataBuildLimits.from_config(
            cfg.metadata_catalog)
        primary = primary_cls(cfg)
        wh_cfg = Config(root=self.root, db=cfg.sources["warehouse"],
                        tools=cfg.tools)
        warehouse = Database(wh_cfg)
        try:
            info = build_catalog(
                self.catalog_path,
                [("default", primary), ("warehouse", warehouse)],
                limits=limits,
                peopletools_source="default",
            )
        finally:
            primary.close()
            warehouse.close()
        return MetadataCatalog(self.catalog_path), info

    def _source_databases(self, primary_cls=Database):
        cfg = self._config()
        primary = primary_cls(cfg)
        wh_cfg = Config(root=self.root, db=cfg.sources["warehouse"],
                        tools=cfg.tools)
        warehouse = Database(wh_cfg)
        return cfg, primary, warehouse

    @staticmethod
    def _matches(out: dict, physical: str = "", source: str = "") -> list:
        matches = list(out.get("matches") or [])
        if physical:
            matches = [m for m in matches
                       if str(
                           m.get("physical_object")
                           or (m.get("attributes") or {}).get("physical_object")
                           or (m.get("attributes") or {}).get("physical_name")
                           or (m.get("name") if m.get("kind") in
                               ("table", "view") else "")
                       ).upper()
                       == physical.upper()]
        if source:
            matches = [m for m in matches
                       if str(m.get("source") or "").lower()
                       == source.lower()]
        return matches


class PhysicalResolutionTests(_FixtureCase):
    def test_explicit_sqltablename_maps_without_guessing_a_prefix(self) -> None:
        catalog, _ = self._build()
        logical = self._matches(catalog.search("Z_AR_QUEUE"),
                                "CORP_AR_QUEUE", "default")
        physical = self._matches(catalog.search("CORP_AR_QUEUE"),
                                 "CORP_AR_QUEUE", "default")
        self.assertTrue(logical)
        self.assertTrue(physical)
        logical_hit = logical[0]
        physical_hit = physical[0]
        self.assertEqual(logical_hit["object_id"], physical_hit["object_id"])
        self.assertIn("Z_AR_QUEUE", logical_hit["logical_records"])
        self.assertEqual(logical_hit["confidence"]["tier"], "confirmed")
        self.assertIn("SQLTABLENAME",
                      logical_hit["confidence"]["basis"].upper())
        self.assertEqual(logical_hit["physical_object"], "CORP_AR_QUEUE")
        self.assertTrue(any(
            e.get("authority") == "declared"
            for e in logical_hit.get("evidence") or []))
        self.assertNotEqual(logical_hit["physical_object"],
                            "PS_Z_AR_QUEUE")
        self.assertEqual(physical_hit["confidence"],
                         physical_hit["object_confidence"])
        self.assertEqual(physical_hit["confidence"]["tier"], "confirmed")
        self.assertIn("sqlite_master",
                      physical_hit["confidence"]["basis"])

    def test_unique_suffix_is_inferred_and_explains_lower_confidence(self):
        catalog, _ = self._build()
        hits = self._matches(catalog.search("LEGACY_QUEUE"),
                             "ACME_LEGACY_QUEUE", "default")
        self.assertTrue(hits)
        hit = hits[0]
        self.assertIn("LEGACY_QUEUE", hit["logical_records"])
        self.assertEqual(hit["physical_object"], "ACME_LEGACY_QUEUE")
        self.assertNotEqual(hit["confidence"]["tier"], "confirmed")
        basis = str(hit["confidence"].get("basis") or "").lower()
        self.assertIn("suffix", basis)
        self.assertTrue(any(
            e.get("authority") == "inferred"
            for e in hit.get("evidence") or []))

    def test_ambiguous_and_stale_mappings_remain_unresolved(self) -> None:
        catalog, _ = self._build()
        for record in ("AMBIG_QUEUE", "OLD_QUEUE"):
            out = catalog.search(record, limit=50)
            blob = str(out).upper()
            self.assertNotIn(f"PS_{record}", blob)
            hit = next(m for m in out["matches"]
                       if record in (m.get("logical_records") or []))
            self.assertIsNone(hit["physical_object"])
            self.assertIn(hit["confidence"]["tier"],
                          {"candidate", "inconclusive"})

    def test_sql_like_wildcards_in_logical_name_cannot_create_a_mapping(self):
        catalog, _ = self._build()
        out = catalog.search("AR_Q", source="default", limit=50)
        hit = next(m for m in out["matches"]
                   if "AR_Q" in (m.get("logical_records") or []))
        self.assertIsNone(hit["physical_object"])
        self.assertNotIn("CORP_ARXQ", str(hit))
        self.assertIn("unresolved", hit["confidence"]["basis"].lower())

    def test_nonphysical_record_type_is_not_presented_as_queryable_table(self):
        catalog, _ = self._build()
        out = catalog.search("DERIVED_WRK", source="default", limit=50)
        hit = next(m for m in out["matches"]
                   if "DERIVED_WRK" in (m.get("logical_records") or []))
        self.assertIsNone(hit["physical_object"])
        self.assertFalse(hit.get("queryable", False))
        self.assertIn("derived", str(hit).lower())

    def test_peopletools_table_record_cannot_confirm_a_native_view(self):
        catalog, _ = self._build()
        out = catalog.search("WRONG_KIND_REC", source="default", limit=50)
        hit = next(m for m in out["matches"]
                   if "WRONG_KIND_REC" in (m.get("logical_records") or []))
        self.assertIsNone(hit["physical_object"])
        self.assertIn("expects a table", str(hit).lower())
        self.assertIn("view", str(hit).lower())


class PeopleToolsMetadataTests(_FixtureCase):
    def test_field_label_and_translate_text_are_searchable(self) -> None:
        catalog, _ = self._build()
        for phrase in ("approval status", "approved for posting",
                       "approval error"):
            hits = self._matches(
                catalog.search(phrase, source="default", limit=50),
                "CORP_AR_QUEUE", "default")
            self.assertTrue(hits, f"{phrase!r} did not find CORP_AR_QUEUE")
            hit = hits[0]
            self.assertTrue(hit.get("match_reasons"), hit)
            self.assertTrue(hit.get("term_coverage"), hit)
            self.assertTrue(any(
                e.get("collector") == "peopletools"
                for e in hit.get("evidence") or []), hit)

    def test_ordered_index_columns_are_preserved_in_object_context(self):
        catalog, _ = self._build()
        out = catalog.context("CORP_AR_QUEUE", source="default", limit=40)
        self.assertTrue(out["available"])
        self.assertTrue(out["found"])
        index = next(i for i in out["indexes"]
                     if i["name"] == "CORP_AR_QUEUE_U1")
        self.assertTrue(index["unique"])
        self.assertEqual(index["columns"],
                         ["BUSINESS_UNIT", "INTERFACE_ID"])

    def test_field_alias_context_resolves_to_its_unique_physical_object(self):
        catalog, _ = self._build()
        for identifier in ("X_APPR_STAT",
                           "CORP_AR_QUEUE.X_APPR_STAT"):
            with self.subTest(identifier=identifier):
                out = catalog.context(identifier, source="default")
                self.assertTrue(out["found"], out)
                self.assertFalse(out.get("ambiguous", False), out)
                self.assertEqual(out["physical_object"], "CORP_AR_QUEUE")
                self.assertEqual(out["object"]["kind"], "table")
                self.assertIn("Z_AR_QUEUE", out["logical_records"])
                self.assertIn(
                    "X_APPR_STAT",
                    [column["name"] for column in out["columns"]],
                )

    def test_shared_field_alias_is_explicitly_ambiguous_and_bounded(self):
        catalog, _ = self._build()
        out = catalog.context(
            "INTERFACE_ID", source="default", limit=1)
        self.assertFalse(out["found"], out)
        self.assertTrue(out["ambiguous"], out)
        self.assertLessEqual(len(out["candidates"]), 1)
        self.assertTrue(out["truncated"], out)
        self.assertTrue(all(
            candidate.get("physical_object")
            and candidate.get("kind") in {"table", "view"}
            for candidate in out["candidates"]
        ), out)

    def test_custom_field_label_keeps_an_unresolved_record_discoverable(self):
        catalog, _ = self._build()
        result = catalog.search(
            "bespoke approval status", source="default", limit=50)
        record = next(
            match for match in result["matches"]
            if "OLD_QUEUE" in (match.get("logical_records") or [])
        )
        self.assertIsNone(record["physical_object"])
        self.assertIn(record["confidence"]["tier"],
                      {"candidate", "inconclusive"})
        self.assertTrue(any(
            evidence.get("collector") == "peopletools"
            for evidence in record.get("evidence") or []
        ), record)

        field = catalog.context("X_SECRET_STAT", source="default")
        self.assertTrue(field["found"], field)
        self.assertIsNone(field["physical_object"])
        self.assertIn("OLD_QUEUE", field["logical_records"])
        self.assertNotIn("X_SECRET_STAT", field["logical_records"])
        self.assertEqual(field["object"]["kind"], "record")

    def test_public_query_relationship_is_portable_and_private_name_is_absent(self):
        catalog, _ = self._build()
        public = self._matches(
            catalog.search("CUSTOM_APPROVAL_QRY", source="default"),
            "CORP_AR_QUEUE", "default",
        )
        self.assertTrue(public)
        self.assertIn("used by query", str(public[0]["match_reasons"]).lower())
        self.assertFalse(catalog.search(
            "PRIVATE_SECRET_QRY", source="default")["matches"])
        self.assertNotIn("PRIVATE_SECRET_QRY",
                         _artifact_text(self.catalog_path))

    def test_missing_optional_peopletools_shape_degrades_only_that_layer(self):
        _drop_optional_peopletools_columns(self.primary_path)
        catalog, _ = self._build()
        # SHORTNAME remains available when LONGNAME is absent, and the native
        # object/column layers must not disappear with optional definitions.
        hits = self._matches(catalog.search(
            "approval", source="default", limit=50),
            "CORP_AR_QUEUE", "default")
        self.assertTrue(hits)
        detail = catalog.context("CORP_AR_QUEUE", source="default")
        self.assertTrue(detail["found"])
        self.assertIn("X_APPR_STAT",
                      [c["name"] for c in detail.get("columns") or []])
        source = next(s for s in catalog.describe()["sources"]
                      if s["name"] == "default")
        self.assertNotEqual(source["status"], "failed")

    def test_effective_dated_translate_value_does_not_keep_the_oldest(self):
        con = sqlite3.connect(self.primary_path)
        try:
            con.execute(
                "INSERT INTO PSXLATITEM VALUES (?,?,?,?,?,?,?)",
                ("X_APPR_STAT", "A", "Current", "Currently approved",
                 "2025-01-01", "A", 2),
            )
            con.commit()
        finally:
            con.close()

        catalog, _ = self._build()
        detail = catalog.context("CORP_AR_QUEUE", source="default")
        field = next(c for c in detail["columns"]
                     if c["name"] == "X_APPR_STAT")
        values = field.get("translate_values") or []
        current = next(v for v in values if v["value"] == "A"
                       and v.get("current"))
        self.assertEqual(current["label"], "Currently approved")
        self.assertEqual(current["effective_date"], "2025-01-01")


class NativeLineageTests(_FixtureCase):
    def test_lineage_limits_are_configurable_and_validated(self):
        cfg = self._config()
        cfg.metadata_catalog.max_constraints = 321
        cfg.metadata_catalog.max_constraint_columns = 987
        cfg.metadata_catalog.max_dependencies = 654
        limits = MetadataBuildLimits.from_config(cfg.metadata_catalog)
        self.assertEqual(limits.max_constraints, 321)
        self.assertEqual(limits.max_constraint_columns, 987)
        self.assertEqual(limits.max_dependencies, 654)
        with self.assertRaises(Exception):
            MetadataBuildLimits(max_constraints=0).validate()
        with self.assertRaises(Exception):
            MetadataBuildLimits(max_dependencies=2_000_001).validate()
        with self.assertRaises(Exception):
            MetadataBuildLimits(max_constraint_columns=5_000_001).validate()

    def test_sqlite_primary_unique_and_foreign_keys_are_namespaced(self):
        catalog, _ = self._build()
        detail = catalog.context("CORP_AR_DETAIL", source="default", limit=50)
        self.assertTrue(detail["found"], detail)
        self.assertEqual(detail["schema"], "MAIN")
        by_type = {item["type"]: item
                   for item in detail.get("constraints") or []}
        self.assertEqual(
            by_type["primary_key"]["columns"],
            ["BUSINESS_UNIT", "CUSTOMER_ID", "LINE_NBR"],
        )
        self.assertEqual(
            by_type["unique"]["columns"],
            ["BUSINESS_UNIT", "CUSTOMER_ID"],
        )
        foreign = by_type["foreign_key"]
        self.assertEqual(
            [(pair["column"], pair["referenced_column"])
             for pair in foreign["column_pairs"]],
            [("BUSINESS_UNIT", "BUSINESS_UNIT"),
             ("CUSTOMER_ID", "CUSTOMER_ID")],
        )
        self.assertEqual(foreign["reference"]["source"], "default")
        self.assertEqual(foreign["reference"]["schema"], "MAIN")
        self.assertEqual(
            foreign["reference"]["object"], "CORP_CUSTOMER_KEY")
        self.assertEqual(
            foreign["reference"]["resolution_status"], "resolved")

    def test_unresolved_foreign_key_target_remains_explicit(self):
        catalog, _ = self._build()
        detail = catalog.context(
            "CORP_UNRESOLVED_REF", source="default", limit=50)
        foreign = next(item for item in detail["constraints"]
                       if item["type"] == "foreign_key")
        self.assertEqual(
            foreign["reference"]["object"], "NOT_DEPLOYED_OBJECT")
        self.assertEqual(
            foreign["reference"]["resolution_status"], "unresolved")
        self.assertEqual(foreign["reference"]["kind"], "external_object")
        found = catalog.search(
            "NOT_DEPLOYED_OBJECT", source="default", limit=20)
        self.assertTrue(any(
            hit.get("physical_object") == "CORP_UNRESOLVED_REF"
            for hit in found.get("matches") or []
        ), found)

    def test_sqlite_view_dependency_gap_is_machine_readable_and_not_partial(self):
        catalog, _ = self._build()
        described = catalog.describe()
        note = next(
            item for item in described["notes"]
            if item["source"] == "default"
            and item["layer"] == "view_dependencies"
        )
        self.assertEqual(note["status"], "unavailable")
        self.assertFalse(note["ok"])
        self.assertFalse(note["partial"])
        self.assertIn("full view sql", note["note"].lower())
        # Unsupported dependency introspection must not erase the valid
        # object/column/constraint coverage or label it partial.
        self.assertFalse(described["snapshot"]["partial"], described)
        self.assertNotIn("SELECT BUSINESS_UNIT", _artifact_text(
            self.catalog_path).upper())

    def test_constraint_limit_is_disclosed_as_partial(self):
        cfg = self._config()
        limits = MetadataBuildLimits.from_config(
            cfg.metadata_catalog, max_constraints=1)
        catalog, _ = self._build(limits=limits)
        described = catalog.describe()
        hit = next(item for item in described["limit_hits"]
                   if item["layer"] == "constraints"
                   and item["source"] == "default")
        self.assertEqual(hit["limit"], 1)
        self.assertEqual(hit["rows_kept"], 1)
        self.assertTrue(described["snapshot"]["partial"])

    def test_constraint_column_limit_is_applied_before_edge_allocation(self):
        cfg = self._config()
        limits = MetadataBuildLimits.from_config(
            cfg.metadata_catalog, max_constraint_columns=2)
        catalog, _ = self._build(limits=limits)
        described = catalog.describe()
        hit = next(item for item in described["limit_hits"]
                   if item["layer"] == "constraint_columns"
                   and item["source"] == "default")
        self.assertEqual(hit["rows_kept"], 2)
        detail = catalog.context(
            "CORP_AR_DETAIL", source="default", limit=50)
        primary = next(item for item in detail["constraints"]
                       if item["type"] == "primary_key")
        self.assertEqual(len(primary["columns"]), 2)
        self.assertFalse(primary["rowset_complete"])
        self.assertTrue(detail["snapshot"]["partial"])


class NativeDependencyCollectorTests(unittest.TestCase):
    def _state(self, max_dependencies: int = 10):
        from pstb.metadata import _DDL, _Writer

        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(_DDL)
        state = _Writer(
            con, MetadataBuildLimits(max_dependencies=max_dependencies))
        return con, state

    def test_resolved_view_dependency_targets_observed_object(self):
        import json
        from pstb.metadata import _collect_view_dependencies

        con, state = self._state()
        try:
            view_id = state.node(
                source="erp", schema="FIN", kind="view", name="AR_OPEN_V",
                collector="db_catalog", evidence="ALL_OBJECTS",
                authority="observed", confidence="confirmed")
            table_id = state.node(
                source="erp", schema="FIN", kind="table", name="AR_OPEN",
                collector="db_catalog", evidence="ALL_OBJECTS",
                authority="observed", confidence="confirmed")
            objects = {
                ("FIN", "AR_OPEN_V"): {
                    "id": view_id, "schema": "FIN", "name": "AR_OPEN_V",
                    "kind": "view"},
                ("FIN", "AR_OPEN"): {
                    "id": table_id, "schema": "FIN", "name": "AR_OPEN",
                    "kind": "table"},
            }
            row = {
                "schema_name": "FIN", "view_name": "AR_OPEN_V",
                "referenced_schema": "FIN", "referenced_object": "AR_OPEN",
                "referenced_type": "TABLE", "referenced_link": "",
            }
            db = SimpleNamespace(
                dialect="oracle",
                cfg=SimpleNamespace(db=SimpleNamespace(schema="FIN")))
            with mock.patch(
                    "pstb.metadata._view_dependency_pages",
                    return_value=iter([([row], False)])):
                count = _collect_view_dependencies(
                    state, "erp", db, objects, object_overflow=False)
            self.assertEqual(count, 1)
            edge = con.execute(
                "SELECT * FROM edges WHERE kind='view_depends_on'"
            ).fetchone()
            self.assertEqual((edge["src"], edge["dst"]),
                             (view_id, table_id))
            self.assertEqual(edge["confidence"], "confirmed")
            self.assertEqual(
                json.loads(edge["attrs"])["resolution_status"],
                "resolved")
        finally:
            con.close()

    def test_dependency_cap_precedes_unresolved_stub_allocation(self):
        import json
        from pstb.metadata import _collect_view_dependencies

        con, state = self._state(max_dependencies=1)
        try:
            view_id = state.node(
                source="erp", schema="FIN", kind="view", name="AR_OPEN_V",
                collector="db_catalog", evidence="ALL_OBJECTS",
                authority="observed", confidence="confirmed")
            objects = {("FIN", "AR_OPEN_V"): {
                "id": view_id, "schema": "FIN", "name": "AR_OPEN_V",
                "kind": "view"}}
            rows = [{
                "schema_name": "FIN", "view_name": "AR_OPEN_V",
                "referenced_schema": "EXT", "referenced_object": name,
                "referenced_type": "TABLE", "referenced_link": "REMOTE",
            } for name in ("FIRST_EXTERNAL", "SECOND_EXTERNAL")]
            db = SimpleNamespace(
                dialect="oracle",
                cfg=SimpleNamespace(db=SimpleNamespace(schema="FIN")))
            with mock.patch(
                    "pstb.metadata._view_dependency_pages",
                    return_value=iter([(rows, False)])):
                count = _collect_view_dependencies(
                    state, "erp", db, objects, object_overflow=False)
            self.assertEqual(count, 1)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind='external_object'"
            ).fetchone()[0], 1)
            edge = con.execute(
                "SELECT attrs FROM edges WHERE kind='view_depends_on'"
            ).fetchone()
            self.assertEqual(
                json.loads(edge["attrs"])["resolution_status"], "unresolved")
            self.assertEqual(state.limit_hits[0]["layer"],
                             "view_dependencies")
        finally:
            con.close()


class CrossSourceTests(_FixtureCase):
    def test_source_and_kind_filters_run_before_the_scan_cap(self) -> None:
        con = sqlite3.connect(self.primary_path)
        try:
            for n in range(80):
                con.execute(f"CREATE TABLE FOO_NOISE_{n:03d} (ID TEXT)")
            con.commit()
        finally:
            con.close()
        con = sqlite3.connect(self.warehouse_path)
        try:
            con.execute("CREATE TABLE FOO_TARGET (ID TEXT)")
            con.commit()
        finally:
            con.close()

        catalog, _ = self._build()
        out = catalog.search(
            "FOO", source="warehouse", kinds="table", limit=1)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["matches"][0]["source"], "warehouse")
        self.assertEqual(out["matches"][0]["physical_object"], "FOO_TARGET")

    def test_configured_catalog_only_source_is_namespaced_and_searchable(self):
        catalog, _ = self._build()
        out = catalog.search("DW_AR_FACT", source="warehouse")
        hits = self._matches(out, "DW_AR_FACT", "warehouse")
        self.assertTrue(hits)
        self.assertEqual({m["source"] for m in out["matches"]},
                         {"warehouse"})
        described = catalog.describe()
        self.assertEqual({s["name"] for s in described["sources"]},
                         {"default", "warehouse"})
        warehouse = next(s for s in described["sources"]
                         if s["name"] == "warehouse")
        self.assertEqual(warehouse["peopletools_status"], "not_applicable")
        self.assertIn("metadata", described["coverage_note"].lower())

    def test_same_physical_name_in_two_sources_is_never_merged(self) -> None:
        catalog, _ = self._build()
        out = catalog.search("CORP_AR_QUEUE", limit=50)
        hits = [m for m in self._matches(out, "CORP_AR_QUEUE")
                if m.get("physical_object") == "CORP_AR_QUEUE"]
        self.assertEqual({m["source"] for m in hits},
                         {"default", "warehouse"})
        self.assertEqual(len({m["object_id"] for m in hits}), 2)
        default = next(m for m in hits if m["source"] == "default")
        warehouse = next(m for m in hits if m["source"] == "warehouse")
        self.assertIn("Z_AR_QUEUE", default["logical_records"])
        self.assertNotIn("Z_AR_QUEUE", warehouse["logical_records"])

    def test_unknown_source_refuses_instead_of_falling_back_to_default(self):
        catalog, _ = self._build()
        with self.assertRaises(Exception) as ctx:
            catalog.search("CORP_AR_QUEUE", source="not-configured")
        self.assertIn("source", str(ctx.exception).lower())

    def test_same_name_in_two_schemas_needs_a_qualified_identifier(self):
        class TwoSchemaCatalog:
            dialect = "sqlserver"
            prefix = ""
            cfg = SimpleNamespace(db=SimpleNamespace(schema=""))

            def columns(self, _table):
                return set()

            def query(self, sql, params=None, max_rows=None):
                if ("FROM sys.objects O" in sql
                        and "JOIN sys.columns" not in sql
                        and "JOIN sys.indexes" not in sql):
                    after = (str((params or {}).get("s") or ""),
                             str((params or {}).get("n") or ""))
                    rows = [
                        {"schema_name": "FIN", "object_name": "DUP_OBJECT",
                         "object_type": "TABLE"},
                        {"schema_name": "OPS", "object_name": "DUP_OBJECT",
                         "object_type": "TABLE"},
                    ]
                    rows = [r for r in rows
                            if (r["schema_name"], r["object_name"]) > after]
                    cap = int(max_rows)
                    return rows[:cap], len(rows) > cap
                if "JOIN sys.columns" in sql or "JOIN sys.indexes" in sql:
                    return [], False
                raise AssertionError(sql)

        path = self.root / "two-schema.db"
        build_catalog(path, [("shared", TwoSchemaCatalog())],
                      peopletools_source="none")
        catalog = MetadataCatalog(path)
        broad = catalog.context("DUP_OBJECT", source="shared")
        self.assertFalse(broad["found"])
        self.assertTrue(broad["ambiguous"])
        self.assertEqual({c["schema"] for c in broad["candidates"]},
                         {"FIN", "OPS"})
        exact = catalog.context("FIN.DUP_OBJECT", source="shared")
        self.assertTrue(exact["found"])
        self.assertEqual(exact["object"]["schema"], "FIN")


class NativeCatalogDialectTests(unittest.TestCase):
    class _Recorder:
        def __init__(self, dialect: str, schema: str, responses: list):
            self.dialect = dialect
            self.cfg = SimpleNamespace(db=SimpleNamespace(schema=schema))
            self.responses = list(responses)
            self.calls: list[dict] = []

        def query(self, sql, params=None, max_rows=None):
            self.calls.append({
                "sql": " ".join(sql.split()),
                "params": dict(params or {}),
                "max_rows": max_rows,
            })
            if not self.responses:
                raise AssertionError(f"unexpected catalog query: {sql}")
            return self.responses.pop(0)

    def test_oracle_first_pages_omit_empty_string_cursor_binds(self):
        """Oracle treats '' as NULL, so an empty keyset cursor returns no rows."""
        from pstb.metadata import (_column_pages, _constraint_pages,
                                   _index_pages, _object_page,
                                   _view_dependency_pages)

        objects = self._Recorder("oracle", "p2go", [([], False)])
        _object_page(objects, None, 25)
        self.assertEqual(objects.calls[0]["params"], {"owner": "P2GO"})
        self.assertNotIn(":n", objects.calls[0]["sql"])

        columns = self._Recorder("oracle", "p2go", [([], False)])
        list(_column_pages(columns, page_size=25))
        self.assertEqual(columns.calls[0]["params"], {"owner": "P2GO"})
        self.assertNotIn(":t", columns.calls[0]["sql"])
        self.assertNotIn(":p", columns.calls[0]["sql"])

        indexes = self._Recorder("oracle", "p2go", [([], False)])
        list(_index_pages(indexes, page_size=25))
        self.assertEqual(indexes.calls[0]["params"], {"owner": "P2GO"})
        self.assertNotIn(":t", indexes.calls[0]["sql"])
        self.assertNotIn(":i", indexes.calls[0]["sql"])
        self.assertNotIn(":p", indexes.calls[0]["sql"])

        constraints = self._Recorder("oracle", "p2go", [([], False)])
        list(_constraint_pages(constraints, page_size=25))
        self.assertEqual(
            constraints.calls[0]["params"], {"owner": "P2GO"})
        self.assertNotIn(":t", constraints.calls[0]["sql"])
        self.assertNotIn(":c", constraints.calls[0]["sql"])
        self.assertNotIn(":p", constraints.calls[0]["sql"])

        dependencies = self._Recorder("oracle", "p2go", [([], False)])
        list(_view_dependency_pages(dependencies, page_size=25))
        self.assertEqual(
            dependencies.calls[0]["params"], {"owner": "P2GO"})
        for bind in (":v", ":rs", ":ro", ":rl"):
            self.assertNotIn(bind, dependencies.calls[0]["sql"])

    def test_configured_oracle_uses_owner_scoped_all_catalogs_and_cursors(self):
        from pstb.metadata import (_column_pages, _constraint_pages,
                                   _index_pages, _object_page,
                                   _view_dependency_pages)

        objects = self._Recorder("oracle", "sysadm", [([], False)])
        _object_page(objects, ("SYSADM", "FIRST_TABLE"), 25)
        call = objects.calls[0]
        self.assertIn("FROM ALL_OBJECTS", call["sql"])
        self.assertEqual(call["params"],
                         {"owner": "SYSADM", "n": "FIRST_TABLE"})

        column_row = {
            "schema_name": "SYSADM", "object_name": "CORP_AR_QUEUE",
            "column_name": "INTERFACE_ID", "ordinal_position": 2,
        }
        columns = self._Recorder(
            "oracle", "sysadm", [([column_row], True), ([], False)])
        pages = list(_column_pages(columns, page_size=1))
        self.assertEqual(pages[0][0], [column_row])
        self.assertIn("FROM ALL_TAB_COLUMNS", columns.calls[0]["sql"])
        self.assertEqual(
            columns.calls[1]["params"],
            {"t": "CORP_AR_QUEUE", "p": 2, "owner": "SYSADM"},
        )

        index_rows = [
            {"schema_name": "SYSADM", "object_name": "CORP_AR_QUEUE",
             "index_name": "CORP_AR_QUEUE_U1",
             "column_name": "BUSINESS_UNIT", "ordinal_position": 1},
            {"schema_name": "SYSADM", "object_name": "CORP_AR_QUEUE",
             "index_name": "CORP_AR_QUEUE_U1",
             "column_name": "INTERFACE_ID", "ordinal_position": 2},
        ]
        indexes = self._Recorder(
            "oracle", "sysadm", [(index_rows, True), ([], False)])
        pages = list(_index_pages(indexes, page_size=2))
        self.assertEqual(
            [row["column_name"] for row in pages[0][0]],
            ["BUSINESS_UNIT", "INTERFACE_ID"],
        )
        self.assertIn("FROM ALL_IND_COLUMNS", indexes.calls[0]["sql"])
        self.assertEqual(
            indexes.calls[1]["params"],
            {"t": "CORP_AR_QUEUE", "i": "CORP_AR_QUEUE_U1", "p": 2,
             "owner": "SYSADM"},
        )

        constraint_row = {
            "schema_name": "SYSADM", "object_name": "CORP_AR_QUEUE",
            "constraint_name": "CORP_AR_QUEUE_PK", "constraint_type": "P",
            "column_name": "BUSINESS_UNIT", "ordinal_position": 1,
        }
        constraints = self._Recorder(
            "oracle", "sysadm", [([constraint_row], True), ([], False)])
        list(_constraint_pages(constraints, page_size=1))
        self.assertIn("FROM ALL_CONSTRAINTS", constraints.calls[0]["sql"])
        self.assertIn("JOIN ALL_CONS_COLUMNS", constraints.calls[0]["sql"])
        self.assertNotIn("SEARCH_CONDITION", constraints.calls[0]["sql"])
        self.assertEqual(
            constraints.calls[1]["params"],
            {"t": "CORP_AR_QUEUE", "c": "CORP_AR_QUEUE_PK", "p": 1,
             "owner": "SYSADM"},
        )

        dependency_row = {
            "schema_name": "SYSADM", "view_name": "CORP_AR_QUEUE_V",
            "referenced_schema": "SYSADM",
            "referenced_object": "CORP_AR_QUEUE",
            "referenced_type": "TABLE", "referenced_link": "",
        }
        dependencies = self._Recorder(
            "oracle", "sysadm", [([dependency_row], True), ([], False)])
        list(_view_dependency_pages(dependencies, page_size=1))
        self.assertIn("FROM ALL_DEPENDENCIES", dependencies.calls[0]["sql"])
        self.assertNotIn("ALL_VIEWS", dependencies.calls[0]["sql"])
        self.assertNotIn("TEXT", dependencies.calls[0]["sql"])
        self.assertIn(
            "NVL(REFERENCED_OWNER,CHR(0))", dependencies.calls[1]["sql"])
        self.assertIn("NVL(:rs,CHR(0))", dependencies.calls[1]["sql"])
        self.assertIn(
            "NVL(REFERENCED_LINK_NAME,CHR(0))",
            dependencies.calls[1]["sql"],
        )
        self.assertIn("NVL(:rl,CHR(0))", dependencies.calls[1]["sql"])
        self.assertNotIn("NVL(REFERENCED_OWNER,'')",
                         dependencies.calls[1]["sql"])
        self.assertEqual(
            dependencies.calls[1]["params"],
            {"v": "CORP_AR_QUEUE_V", "rs": "SYSADM",
             "ro": "CORP_AR_QUEUE", "rl": "", "owner": "SYSADM"},
        )

    def test_unconfigured_oracle_stays_in_current_user_catalogs(self):
        from pstb.metadata import (_column_pages, _constraint_pages,
                                   _index_pages, _object_page,
                                   _view_dependency_pages)

        objects = self._Recorder("oracle", "", [([], False)])
        _object_page(objects, None, 25)
        self.assertIn("FROM USER_OBJECTS", objects.calls[0]["sql"])
        self.assertNotIn("ALL_OBJECTS", objects.calls[0]["sql"])
        self.assertEqual(objects.calls[0]["params"], {})
        self.assertNotIn(":n", objects.calls[0]["sql"])

        columns = self._Recorder("oracle", "", [([], False)])
        list(_column_pages(columns, page_size=10))
        self.assertIn("FROM USER_TAB_COLUMNS", columns.calls[0]["sql"])
        self.assertNotIn("ALL_TAB_COLUMNS", columns.calls[0]["sql"])
        self.assertEqual(columns.calls[0]["params"], {})
        self.assertNotIn(":t", columns.calls[0]["sql"])

        indexes = self._Recorder("oracle", "", [([], False)])
        list(_index_pages(indexes, page_size=10))
        self.assertIn("FROM USER_IND_COLUMNS", indexes.calls[0]["sql"])
        self.assertIn("JOIN USER_INDEXES", indexes.calls[0]["sql"])
        self.assertNotIn("ALL_IND_COLUMNS", indexes.calls[0]["sql"])
        self.assertEqual(indexes.calls[0]["params"], {})
        self.assertNotIn(":t", indexes.calls[0]["sql"])

        constraints = self._Recorder("oracle", "", [([], False)])
        list(_constraint_pages(constraints, page_size=10))
        self.assertIn("FROM USER_CONSTRAINTS", constraints.calls[0]["sql"])
        self.assertIn("JOIN USER_CONS_COLUMNS", constraints.calls[0]["sql"])
        self.assertNotIn("ALL_CONSTRAINTS", constraints.calls[0]["sql"])
        self.assertEqual(constraints.calls[0]["params"], {})
        self.assertNotIn(":t", constraints.calls[0]["sql"])

        dependencies = self._Recorder("oracle", "", [([], False)])
        list(_view_dependency_pages(dependencies, page_size=10))
        self.assertIn("FROM USER_DEPENDENCIES", dependencies.calls[0]["sql"])
        self.assertNotIn("ALL_DEPENDENCIES", dependencies.calls[0]["sql"])
        self.assertEqual(dependencies.calls[0]["params"], {})
        self.assertNotIn(":v", dependencies.calls[0]["sql"])

    def test_sqlserver_pages_advance_schema_object_and_index_cursors(self):
        from pstb.metadata import (_column_pages, _constraint_pages,
                                   _index_pages, _object_page,
                                   _view_dependency_pages)

        objects = self._Recorder("sqlserver", "", [([], False)])
        _object_page(objects, ("FIN", "AR_QUEUE"), 25)
        call = objects.calls[0]
        self.assertIn("FROM sys.objects O JOIN sys.schemas S", call["sql"])
        self.assertIn("UPPER(S.name) > :s", call["sql"])
        self.assertEqual(call["params"], {"s": "FIN", "n": "AR_QUEUE"})

        column_row = {
            "schema_name": "FIN", "object_name": "AR_QUEUE",
            "column_name": "INTERFACE_ID", "ordinal_position": 2,
        }
        columns = self._Recorder(
            "sqlserver", "", [([column_row], True), ([], False)])
        list(_column_pages(columns, page_size=1))
        self.assertIn("JOIN sys.columns C", columns.calls[0]["sql"])
        self.assertEqual(
            columns.calls[1]["params"],
            {"s": "FIN", "t": "AR_QUEUE", "p": 2},
        )

        index_rows = [
            {"schema_name": "FIN", "object_name": "AR_QUEUE",
             "index_name": "AR_QUEUE_U1", "column_name": "BUSINESS_UNIT",
             "ordinal_position": 1},
            {"schema_name": "FIN", "object_name": "AR_QUEUE",
             "index_name": "AR_QUEUE_U1", "column_name": "INTERFACE_ID",
             "ordinal_position": 2},
        ]
        indexes = self._Recorder(
            "sqlserver", "", [(index_rows, True), ([], False)])
        pages = list(_index_pages(indexes, page_size=2))
        self.assertEqual(
            [row["column_name"] for row in pages[0][0]],
            ["BUSINESS_UNIT", "INTERFACE_ID"],
        )
        self.assertIn("IC.key_ordinal>0", indexes.calls[0]["sql"])
        self.assertEqual(
            indexes.calls[1]["params"],
            {"s": "FIN", "t": "AR_QUEUE", "i": "AR_QUEUE_U1", "p": 2},
        )

        constraint_row = {
            "schema_name": "FIN", "object_name": "AR_QUEUE",
            "constraint_name": "AR_QUEUE_FK1", "constraint_type": "F",
            "column_name": "CUSTOMER_ID", "ordinal_position": 1,
            "referenced_schema": "FIN", "referenced_object": "CUSTOMER",
            "referenced_column": "CUSTOMER_ID",
        }
        constraints = self._Recorder(
            "sqlserver", "", [([constraint_row], True), ([], False)])
        list(_constraint_pages(constraints, page_size=1))
        sql = constraints.calls[0]["sql"]
        self.assertIn("FROM sys.key_constraints", sql)
        self.assertIn("FROM sys.foreign_keys", sql)
        self.assertIn("JOIN sys.foreign_key_columns", sql)
        self.assertNotIn("definition", sql.lower())
        key_constraint_arm = sql.split("UNION ALL", 1)[0]
        self.assertNotIn("KC.is_disabled", key_constraint_arm)
        self.assertNotIn("KC.is_not_trusted", key_constraint_arm)
        self.assertIn("KI.is_disabled", key_constraint_arm)
        self.assertEqual(
            constraints.calls[1]["params"],
            {"s": "FIN", "t": "AR_QUEUE", "c": "AR_QUEUE_FK1", "p": 1},
        )

        dependency_row = {
            "schema_name": "FIN", "view_name": "AR_QUEUE_V",
            "referenced_schema": "FIN", "referenced_object": "AR_QUEUE",
            "referenced_type": "TABLE", "referenced_database": "",
            "referenced_server": "",
        }
        dependencies = self._Recorder(
            "sqlserver", "", [([dependency_row], True), ([], False)])
        list(_view_dependency_pages(dependencies, page_size=1))
        dep_sql = dependencies.calls[0]["sql"]
        self.assertIn("sys.sql_expression_dependencies", dep_sql)
        self.assertNotIn("sys.sql_modules", dep_sql)
        self.assertNotIn("OBJECT_DEFINITION", dep_sql)
        self.assertEqual(
            dependencies.calls[1]["params"],
            {"s": "FIN", "v": "AR_QUEUE_V", "rs": "FIN",
             "ro": "AR_QUEUE", "rd": "", "rsv": ""},
        )

        scoped = self._Recorder("sqlserver", "fin", [([], False)])
        list(_constraint_pages(scoped, page_size=10))
        self.assertIn("UPPER(Q.schema_name)=:owner", scoped.calls[0]["sql"])
        self.assertEqual(scoped.calls[0]["params"]["owner"], "FIN")


class ArtifactStateTests(_FixtureCase):
    def test_missing_artifact_is_actionable_not_a_traceback(self) -> None:
        missing = MetadataCatalog(self.root / "does-not-exist.db")
        for out in (missing.describe(), missing.search("anything"),
                    missing.context("anything")):
            self.assertFalse(out["available"])
            self.assertIn("build_metadata_catalog", out["how_to_build"])

    def test_old_snapshot_is_disclosed_everywhere_but_remains_readable(self):
        self._build()
        _set_meta(self.catalog_path, "built_at", "2000-01-01T00:00:00")
        _set_meta(self.catalog_path, "stale_after_hours", "1")
        catalog = MetadataCatalog(self.catalog_path)
        outputs = [catalog.describe(), catalog.search("CORP_AR_QUEUE"),
                   catalog.context("CORP_AR_QUEUE", source="default")]
        for out in outputs:
            self.assertTrue(out["available"])
            snapshot = out["snapshot"]
            self.assertTrue(snapshot.get("stale"), out)
            self.assertIn("older", snapshot["note"].lower())

    def test_limit_hit_is_partial_in_describe_search_and_context(self) -> None:
        cfg = self._config()
        limits = MetadataBuildLimits.from_config(
            cfg.metadata_catalog, max_objects=5)
        catalog, _ = self._build(limits=limits)
        described = catalog.describe()
        outputs = [described, catalog.search("queue"),
                   catalog.context("A_AMBIG_QUEUE", source="default")]
        for out in outputs:
            snapshot = out["snapshot"]
            self.assertTrue(snapshot["partial"], out)
            self.assertIn("partial", snapshot["note"].lower())
        self.assertTrue(described["limit_hits"])
        self.assertIn("limit", str(described["limit_hits"]).lower())

    def test_exact_object_limit_is_complete_but_one_more_is_partial(self):
        source_path = self.root / "exact-source.db"
        con = sqlite3.connect(source_path)
        try:
            for name in ("ONE", "TWO", "THREE"):
                con.execute(f"CREATE TABLE {name} (ID TEXT)")
            con.commit()
        finally:
            con.close()
        cfg = Config.sample(self.root)
        cfg.db.sqlite_path = str(source_path)
        limits = MetadataBuildLimits(max_objects=3)

        db = Database(cfg)
        exact_path = self.root / "exact.db"
        try:
            build_catalog(exact_path, [("default", db)], limits=limits,
                          peopletools_source="none")
        finally:
            db.close()
        self.assertFalse(MetadataCatalog(exact_path).describe()
                         ["snapshot"]["partial"])

        con = sqlite3.connect(source_path)
        try:
            con.execute("CREATE TABLE FOUR (ID TEXT)")
            con.commit()
        finally:
            con.close()
        db = Database(cfg)
        overflow_path = self.root / "overflow.db"
        try:
            build_catalog(overflow_path, [("default", db)], limits=limits,
                          peopletools_source="none")
        finally:
            db.close()
        described = MetadataCatalog(overflow_path).describe()
        self.assertTrue(described["snapshot"]["partial"])
        self.assertTrue(described["limit_hits"])

    def test_peopletools_row_cap_is_disclosed_as_partial(self) -> None:
        cfg = self._config()
        limits = MetadataBuildLimits.from_config(
            cfg.metadata_catalog, max_peopletools_rows=3)
        catalog, _ = self._build(limits=limits)
        described = catalog.describe()
        self.assertTrue(described["snapshot"]["partial"])
        self.assertTrue(any(
            hit.get("layer") == "PSRECDEFN"
            for hit in described["limit_hits"]), described["limit_hits"])

    def test_failed_atomic_replace_preserves_previous_catalog(self) -> None:
        catalog, _ = self._build()
        before = catalog.search("CORP_AR_QUEUE")
        _, primary, warehouse = self._source_databases()
        with mock.patch("pstb.metadata.os.replace",
                        side_effect=OSError("simulated replace failure")):
            try:
                with self.assertRaises(OSError):
                    build_catalog(
                        self.catalog_path,
                        [("default", primary), ("warehouse", warehouse)],
                        peopletools_source="default",
                    )
            finally:
                primary.close()
                warehouse.close()
        after = MetadataCatalog(self.catalog_path).search("CORP_AR_QUEUE")
        self.assertEqual([m["object_id"] for m in before["matches"]],
                         [m["object_id"] for m in after["matches"]])
        self.assertFalse(Path(str(self.catalog_path) + ".building").exists())


class SafetyAndBoundTests(_FixtureCase):
    def test_sqlite_reserved_names_and_expression_indexes_degrade_safely(self):
        con = sqlite3.connect(self.primary_path)
        try:
            con.execute('CREATE TABLE "ORDER" (ID TEXT, CODE TEXT)')
            con.execute(
                'CREATE INDEX ORDER_EXPR_I1 ON "ORDER" (UPPER(CODE))')
            con.commit()
        finally:
            con.close()

        catalog, _ = self._build()
        detail = catalog.context("ORDER", source="default")
        self.assertTrue(detail["found"])
        self.assertEqual([c["name"] for c in detail["columns"]],
                         ["ID", "CODE"])
        index = next(i for i in detail["indexes"]
                     if i["name"] == "ORDER_EXPR_I1")
        self.assertNotIn("", index.get("columns") or [])
        self.assertTrue(index.get("expression_based")
                        or index.get("coverage_note"), index)

    def test_source_queries_are_read_only_and_artifact_has_no_row_values(self):
        seen: list[str] = []

        class AuditedDatabase(Database):
            def query(self, sql, params=None, max_rows=None):
                seen.append(sql)
                return super().query(sql, params, max_rows)

        catalog, _ = self._build(primary_cls=AuditedDatabase)
        self.assertTrue(catalog.search("PRIVATE_AR_DATA")["matches"])
        for sql in seen:
            head = sql.lstrip().split(None, 1)[0].upper()
            self.assertIn(head, {"SELECT", "WITH", "PRAGMA"}, sql)
        text = _artifact_text(self.catalog_path)
        self.assertNotIn(ROW_SENTINEL, text)
        self.assertNotIn(AMOUNT_SENTINEL, text)
        self.assertNotIn(SECRET_SENTINEL, text)

    def test_search_text_is_bound_and_cannot_damage_the_artifact(self) -> None:
        catalog, _ = self._build()
        attack = "CORP_AR_QUEUE'; DROP TABLE objects; --"
        out = catalog.search(attack)
        self.assertTrue(out["available"])
        self.assertTrue(catalog.describe()["available"])
        self.assertTrue(catalog.search("CORP_AR_QUEUE")["matches"])

    def test_runtime_queries_need_only_the_offline_artifact(self) -> None:
        catalog, _ = self._build()
        os.unlink(self.primary_path)
        os.unlink(self.warehouse_path)
        self.assertTrue(catalog.search("approval status")["matches"])
        self.assertTrue(catalog.context(
            "CORP_AR_QUEUE", source="default")["found"])

    def test_context_limit_is_bounded_and_disclosed(self) -> None:
        catalog, _ = self._build()
        out = catalog.context("CORP_AR_QUEUE", source="default", limit=1)
        related = ((out.get("columns") or []) + (out.get("indexes") or [])
                   + (out.get("constraints") or [])
                   + (out.get("dependencies") or [])
                   + (out.get("mappings") or []))
        self.assertLessEqual(len(related), 1)
        self.assertTrue(out["truncated"])

        enormous = catalog.context(
            "CORP_AR_QUEUE", source="default", limit=999_999)
        self.assertLessEqual(
            len((enormous.get("columns") or [])
                + (enormous.get("indexes") or [])
                + (enormous.get("constraints") or [])
                + (enormous.get("dependencies") or [])
                + (enormous.get("mappings") or [])),
            100,
        )


class SourceProvenanceTests(unittest.TestCase):
    """Which database answered must be in the payload, not inferred.

    On a two-database deployment a table list from a reporting mart and one
    from PeopleSoft were byte-indistinguishable: only search_records named a
    source, and it meant something else by the word. So the reader, the card
    and the model all had to guess, and the guess is always "PeopleSoft".
    """

    def test_every_ad_hoc_tool_names_the_database_that_answered(self) -> None:
        from pstb import server as srv
        for label, out in (
                ("run_sql", srv.run_sql(sql="SELECT 1 AS x")),
                ("list_tables", srv.list_tables(pattern="%LEDGER%")),
                ("describe_table", srv.describe_table(table_name="PS_LEDGER")),
                ("search_records", srv.search_records(query="ledger")),
                ("join_path", srv.join_path(from_record="PS_ITEM",
                                            to_record="PS_CUSTOMER"))):
            with self.subTest(label):
                self.assertEqual(out.get("source_database"), "default")

    def test_it_does_not_collide_with_search_records_own_source_key(self):
        # search_records uses "source" for where the record INFO came from
        # (psrecdefn / database catalog). One key holding two meanings is how
        # a mart result ends up badged with a PeopleTools word.
        from pstb import server as srv
        out = srv.search_records(query="ledger")
        self.assertEqual(out.get("source"), "psrecdefn")
        self.assertEqual(out.get("source_database"), "default")

    def test_an_alias_reports_the_resolved_name_not_what_was_typed(self):
        from pstb import server as srv
        for alias in ("", "default", "peoplesoft", "PS", "main"):
            with self.subTest(alias):
                out = srv.list_tables(pattern="%LEDGER%", source=alias)
                self.assertEqual(out.get("source_database"), "default")

    def test_an_unknown_source_is_a_clean_error_not_a_raise(self) -> None:
        # for_source used to be evaluated OUTSIDE _safe, so a typo reached
        # the model as a crash-shaped "TOOL ERROR:" instead of the remedied
        # message it was written to be.
        from pstb import server as srv
        out = srv.list_tables(pattern="%", source="nope")
        self.assertIn("Unknown source", out.get("error", ""))
        self.assertIn("Configured sources", out["error"])

    def test_the_badge_is_muted_for_the_primary_and_loud_otherwise(self):
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        self.assertIn("function sourceBadge(", html)
        self.assertIn("source: the finance database", html)
        self.assertIn("curated financial tools do not answer from it", html)
        # The page chrome names no company or vendor; a smoke check enforces
        # it and the first draft of this badge broke it.
        self.assertNotIn("PeopleSoft", html.split("function sourceBadge(")[1]
                         .split("function renderToolResult(")[0])
        self.assertIn("function renderToolBody(", html,
                      "the wrapper must delegate, not replace, the 40 "
                      "existing renderers")

class SourceScopeWarningTests(unittest.TestCase):
    """`--source` rebuilds the artifact; it does not patch it.

    A narrower --source is a legitimate diagnostic, but the result is a
    SMALLER catalog rather than an updated one, and it is written atomically
    so it looks complete. Discovered the hard way: `--source p2go
    --peopletools-source none` succeeded and left a catalog with no
    PeopleSoft in it, after which search_metadata reported delivered records
    as not existing.
    """

    def _run(self, *args):
        import contextlib
        import io
        from scripts import build_metadata_catalog as builder
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = builder.main(list(args))
        return code, out.getvalue()

    def _config(self) -> str:
        import sqlite3
        import tempfile
        d = Path(tempfile.mkdtemp())
        # A real table, not an empty file: a source with nothing in it
        # harvests nothing and the builder correctly refuses to write,
        # which would mask the warning this test is about.
        con = sqlite3.connect(d / "extra.db")
        con.execute("CREATE TABLE EXTRA_FACT (ID INTEGER, AMT REAL)")
        con.commit()
        con.close()
        (d / "config.yaml").write_text(
            "db:\n  backend: sqlite\n"
            f"  sqlite_path: {ROOT / 'sample_data' / 'ps_sample.db'}\n"
            "sources:\n  extra:\n    backend: sqlite\n"
            f"    sqlite_path: {d / 'extra.db'}\n")
        return str(d / "config.yaml")

    def test_excluding_the_peopletools_source_names_the_safe_fix(self):
        code, text = self._run("--config", self._config(), "--source", "extra")
        self.assertEqual(code, 2)
        self.assertIn("REPLACES the whole artifact", text)
        self.assertIn("--source default,extra", text,
                      "the message must name the command that keeps the "
                      "catalog whole, not only the one that shrinks it")

    def test_a_narrower_source_warns_which_sources_it_drops(self):
        code, text = self._run("--config", self._config(), "--source", "extra",
                               "--peopletools-source", "none", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("will NOT contain default", text)

    def test_the_normal_full_build_says_nothing_extra(self):
        code, text = self._run("--config", self._config(), "--quiet")
        self.assertEqual(code, 0)
        self.assertNotIn("NOTE:", text)
