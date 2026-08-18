"""Physical isolation for per-database metadata/relationship knowledge.

The SQLite artifact is both the semantic catalog (nodes/search terms) and the
relationship graph (edges).  A source selection must choose one whole file;
an omitted filter inside a query must never reopen another database.
"""
from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pstb.config import Config, DbCfg
from pstb.db import Database
from pstb.metadata import (
    MetadataCatalog,
    MetadataBuildLimits,
    MetadataError,
    build_status_path,
    build_catalog,
    read_build_status,
    source_catalog_path,
    source_fingerprint,
    write_build_status,
)


class SourceKnowledgeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.finance_path = self.root / "finance.db"
        self.p2go_path = self.root / "p2go.db"
        with sqlite3.connect(self.finance_path) as con:
            con.execute(
                "CREATE TABLE FINANCE_FACT (ID INTEGER PRIMARY KEY, AMT REAL)"
            )
        with sqlite3.connect(self.p2go_path) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute(
                "CREATE TABLE P2GO_PARENT (ID INTEGER PRIMARY KEY, NAME TEXT)"
            )
            con.execute(
                "CREATE TABLE P2GO_FACT (ID INTEGER PRIMARY KEY, "
                "PARENT_ID INTEGER NOT NULL, VALUE REAL, "
                "FOREIGN KEY(PARENT_ID) REFERENCES P2GO_PARENT(ID))"
            )
            # Same-named columns are deliberately not treated as a graph edge.
            con.execute(
                "CREATE TABLE P2GO_UNRELATED_A (ID INTEGER, CODE TEXT)"
            )
            con.execute(
                "CREATE TABLE P2GO_UNRELATED_B (ID INTEGER, CODE TEXT)"
            )
            con.execute(
                "CREATE VIEW P2GO_PARENT_VIEW AS "
                "SELECT ID, NAME FROM P2GO_PARENT"
            )

        self.cfg = Config.sample(self.root)
        self.cfg.db = DbCfg(
            backend="sqlite", sqlite_path=str(self.finance_path))
        self.cfg.sources = {
            "p2go": DbCfg(
                backend="sqlite", sqlite_path=str(self.p2go_path))
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _database(self, source: str) -> Database:
        db_cfg = self.cfg.db if source == "default" else self.cfg.sources[source]
        bound = Config(
            root=self.cfg.root,
            defaults=self.cfg.defaults,
            db=db_cfg,
            llm=self.cfg.llm,
            wiki=self.cfg.wiki,
            tools=self.cfg.tools,
        )
        return Database(bound)

    def _build(self, source: str) -> Path:
        path = source_catalog_path(self.cfg, source)
        database = self._database(source)
        try:
            build_catalog(
                path, [(source, database)], peopletools_source="none")
        finally:
            database.close()
        return path

    def test_single_source_default_keeps_the_legacy_path(self) -> None:
        cfg = Config.sample(self.root)
        self.assertEqual(
            source_catalog_path(cfg, "default"),
            self.root / "metadata_catalog.db",
        )

    def test_multi_source_paths_are_distinct_hashed_and_confined(self) -> None:
        finance = source_catalog_path(self.cfg, "default")
        p2go = source_catalog_path(self.cfg, "p2go")
        hostile = source_catalog_path(self.cfg, "../../P2Go / Finance")
        self.assertNotEqual(finance, p2go)
        self.assertEqual(finance.parent, self.root / "metadata_catalogs")
        self.assertEqual(p2go.parent, finance.parent)
        self.assertEqual(hostile.parent, finance.parent)
        self.assertNotIn("..", hostile.name)
        self.assertNotIn("/", hostile.name)
        self.assertRegex(p2go.name, r"^p2go-[0-9a-f]{12}\.db$")

    def test_latest_build_status_is_private_bounded_and_source_bound(self):
        artifact = self._build("p2go")
        write_build_status(artifact, {
            "source_database": "p2go",
            "build_run_id": "a" * 32,
            "attempted_at": "2026-08-18T12:00:00Z",
            "published": False,
            "status": "failed",
            "previous_snapshot_id": "b" * 20,
            "failure_category": "metadata_unavailable",
            "schema_coverage": {
                "default": "P2GO",
                "configured": ["P2GO", "TUSINVC"],
                "object_counts": {"P2GO": 0, "TUSINVC": 0},
                "missing": ["P2GO", "TUSINVC"],
                "complete": False,
            },
            # Persistence reselects fields and cannot retain diagnostics.
            "error": "password=secret; SELECT * FROM PRIVATE_TABLE",
        })
        status_file = build_status_path(artifact)
        self.assertEqual(status_file.stat().st_mode & 0o777, 0o600)
        text = status_file.read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("PRIVATE_TABLE", text)
        status = read_build_status(artifact, "p2go")
        self.assertFalse(status["published"])
        self.assertEqual(status["previous_snapshot_id"], "b" * 20)
        self.assertEqual(status["schema_coverage"]["missing"],
                         ["P2GO", "TUSINVC"])
        self.assertEqual(read_build_status(artifact, "finance"), {})
        described = MetadataCatalog(artifact, source="p2go").describe()
        self.assertEqual(described["latest_build"], status)
        self.assertEqual(described["snapshot"]["latest_build"], status)

    def test_status_writer_does_not_follow_predictable_temp_symlink(self):
        artifact = self._build("p2go")
        target = build_status_path(artifact)
        victim = self.root / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        old_temp = target.with_name(target.name + ".building")
        old_temp.symlink_to(victim)

        write_build_status(artifact, {
            "source_database": "p2go",
            "build_run_id": "c" * 32,
            "attempted_at": "2026-08-18T12:00:00Z",
            "published": False,
            "status": "building",
        })

        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertEqual(read_build_status(artifact, "p2go")["status"],
                         "building")

    def test_success_status_must_match_the_served_snapshot(self):
        artifact = self._build("p2go")
        write_build_status(artifact, {
            "source_database": "p2go",
            "build_run_id": "d" * 32,
            "attempted_at": "2026-08-18T12:00:00Z",
            "published": True,
            "status": "complete",
            "snapshot_id": "e" * 20,
        })

        latest = MetadataCatalog(
            artifact, source="p2go").describe()["latest_build"]
        self.assertFalse(latest["published"])
        self.assertEqual(latest["status"], "failed")
        self.assertFalse(latest["snapshot_matches"])
        self.assertEqual(latest["failure_category"], "snapshot_mismatch")

    def test_one_file_contains_semantic_nodes_and_relationship_edges(self) -> None:
        path = self._build("p2go")
        with sqlite3.connect(path) as con:
            sources = [row[0] for row in con.execute(
                "SELECT name FROM sources")]
            nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            fk_edges = con.execute(
                "SELECT COUNT(*) FROM edges "
                "WHERE kind='foreign_key_references_object'"
            ).fetchone()[0]
        self.assertEqual(sources, ["p2go"])
        self.assertGreater(nodes, 0)
        self.assertGreater(edges, 0)
        self.assertGreater(fk_edges, 0)

    def test_bound_reader_search_context_and_describe_stay_in_source(self) -> None:
        path = self._build("p2go")
        catalog = MetadataCatalog(path, source="p2go")
        described = catalog.describe()
        self.assertTrue(described["available"])
        self.assertEqual(described["source_database"], "p2go")
        self.assertEqual(
            [row["name"] for row in described["sources"]], ["p2go"])

        search = catalog.search("P2GO FACT")
        self.assertTrue(search["available"])
        self.assertEqual(search["source_database"], "p2go")
        self.assertEqual(search["source_filter"], "p2go")
        self.assertTrue(search["matches"])
        self.assertEqual(
            {row["source"] for row in search["matches"]}, {"p2go"})

        context = catalog.context("P2GO_FACT")
        self.assertTrue(context["found"])
        self.assertEqual(context["source_database"], "p2go")
        self.assertEqual(context["source"], "p2go")
        with self.assertRaises(MetadataError):
            catalog.search("FINANCE", source="default")
        with self.assertRaises(MetadataError):
            catalog.context("FINANCE_FACT", source="default")

    def test_source_mismatch_file_fails_closed_before_search(self) -> None:
        finance_artifact = self._build("default")
        wrong = MetadataCatalog(finance_artifact, source="p2go")
        described = wrong.describe()
        self.assertFalse(described["available"])
        self.assertEqual(described["source_database"], "p2go")
        self.assertIn("source mismatch", described["detail"])
        searched = wrong.search("FINANCE FACT")
        self.assertFalse(searched["available"])
        self.assertEqual(searched["source_database"], "p2go")
        self.assertIn("source mismatch", searched["detail"])
        stopwords = wrong.search("the")
        self.assertFalse(stopwords["available"])
        self.assertIn("source mismatch", stopwords["detail"])
        context = wrong.context("FINANCE_FACT")
        self.assertFalse(context["available"])
        self.assertEqual(context["source_database"], "p2go")
        self.assertIn("source mismatch", context["detail"])

    def test_unverifiable_endpoint_binding_disables_only_that_catalog(self) -> None:
        catalog = MetadataCatalog(
            self.root / "not-opened.db",
            source="p2go",
            binding_error=(
                "P2Go endpoint cannot be bound until its tenant-tested "
                "locator is configured."),
        )

        described = catalog.describe()
        self.assertFalse(described["available"])
        self.assertEqual(described["source_database"], "p2go")
        self.assertIn("endpoint cannot be bound", described["detail"])
        with self.assertRaisesRegex(MetadataError, "endpoint cannot be bound"):
            catalog._open()

    def test_relationship_path_uses_literal_foreign_key_columns(self) -> None:
        catalog = MetadataCatalog(self._build("p2go"), source="p2go")
        result = catalog.relationship_path(
            "P2GO_FACT", "P2GO_PARENT", source="p2go")

        self.assertTrue(result["available"])
        self.assertTrue(result["found"])
        self.assertEqual(result["source_database"], "p2go")
        self.assertEqual(
            result["snapshot"]["source_fingerprint"],
            source_fingerprint(self.cfg, "p2go"),
        )
        self.assertEqual(result["hop_count"], 1)
        hop = result["hops"][0]
        self.assertEqual(hop["relationship"], "foreign_key")
        self.assertEqual(hop["direction"], "references")
        self.assertEqual(hop["column_pairs"], [{
            "left_column": "PARENT_ID",
            "right_column": "ID",
            "ordinal": 1,
        }])
        self.assertTrue(result["queryable_join"])
        self.assertIn("T0.PARENT_ID = T1.ID", result["sql"])

    def test_reverse_relationship_path_reorients_join_columns(self) -> None:
        catalog = MetadataCatalog(self._build("p2go"), source="p2go")
        result = catalog.relationship_path(
            "P2GO_PARENT", "P2GO_FACT", source="p2go")

        self.assertTrue(result["found"])
        hop = result["hops"][0]
        self.assertEqual(hop["direction"], "referenced_by")
        self.assertEqual(hop["column_pairs"], [{
            "left_column": "ID",
            "right_column": "PARENT_ID",
            "ordinal": 1,
        }])
        self.assertIn("T0.ID = T1.PARENT_ID", result["sql"])

    def test_view_dependency_is_traversable_but_claims_no_join_columns(self) -> None:
        path = self._build("p2go")
        # SQLite intentionally has no structured dependency collector. Insert
        # one native-shaped edge to exercise the runtime traversal; Oracle and
        # SQL Server collector coverage lives in test_metadata_catalog.
        with sqlite3.connect(path) as con:
            view_id = con.execute(
                "SELECT id FROM nodes WHERE source='p2go' "
                "AND kind='view' AND name='P2GO_PARENT_VIEW'"
            ).fetchone()[0]
            table_id = con.execute(
                "SELECT id FROM nodes WHERE source='p2go' "
                "AND kind='table' AND name='P2GO_PARENT'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
                (view_id, table_id, "view_depends_on", "confirmed",
                 "native dependency catalog", "db_catalog", "observed",
                 "2026-01-01T00:00:00Z", '{"resolution_status":"resolved"}'),
            )

        catalog = MetadataCatalog(path, source="p2go")
        forward = catalog.relationship_path(
            "P2GO_PARENT_VIEW", "P2GO_PARENT", source="p2go")
        reverse = catalog.relationship_path(
            "P2GO_PARENT", "P2GO_PARENT_VIEW", source="p2go")
        self.assertEqual(forward["hops"][0]["relationship"],
                         "view_dependency")
        self.assertEqual(forward["hops"][0]["direction"], "depends_on")
        self.assertEqual(reverse["hops"][0]["direction"], "used_by_view")
        for result in (forward, reverse):
            self.assertTrue(result["found"])
            self.assertEqual(result["source_database"], "p2go")
            self.assertFalse(result["queryable_join"])
            self.assertEqual(result["sql"], "")
            self.assertEqual(result["hops"][0]["column_pairs"], [])

    def test_same_column_names_are_not_promoted_to_relationships(self) -> None:
        catalog = MetadataCatalog(self._build("p2go"), source="p2go")
        result = catalog.relationship_path(
            "P2GO_UNRELATED_A", "P2GO_UNRELATED_B", source="p2go")

        self.assertTrue(result["available"])
        self.assertFalse(result["found"])
        self.assertEqual(result["source_database"], "p2go")
        self.assertIn("Matching column names", result["detail"])

    def test_truncated_composite_fk_never_emits_partial_join_sql(self) -> None:
        source_path = self.root / "composite.db"
        with sqlite3.connect(source_path) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("CREATE TABLE COMPOSITE_PARENT (A INTEGER, B INTEGER)")
            con.execute(
                "CREATE TABLE COMPOSITE_CHILD (A INTEGER, B INTEGER, "
                "FOREIGN KEY(A,B) REFERENCES COMPOSITE_PARENT(A,B))")
        bound = Config.sample(self.root)
        bound.db = DbCfg(backend="sqlite", sqlite_path=str(source_path))
        database = Database(bound)
        artifact = self.root / "truncated-composite.db"
        try:
            build_catalog(
                artifact, [("composite", database)],
                peopletools_source="none",
                limits=MetadataBuildLimits(max_constraint_columns=1))
        finally:
            database.close()

        result = MetadataCatalog(
            artifact, source="composite").relationship_path(
                "COMPOSITE_CHILD", "COMPOSITE_PARENT", source="composite")
        self.assertTrue(result["found"])
        self.assertFalse(result["hops"][0]["column_pairs_complete"])
        self.assertEqual(result["hops"][0]["confidence"], "inconclusive")
        self.assertFalse(result["queryable_join"])
        self.assertEqual(result["sql"], "")

    def test_source_mismatch_relationship_fails_closed(self) -> None:
        wrong = MetadataCatalog(self._build("default"), source="p2go")
        result = wrong.relationship_path(
            "FINANCE_FACT", "FINANCE_FACT", source="p2go")

        self.assertFalse(result["available"])
        self.assertIn("source mismatch", result["detail"])

    def test_repointed_source_cannot_reuse_a_stale_artifact(self) -> None:
        path = self._build("p2go")
        original = source_fingerprint(self.cfg, "p2go")
        catalog = MetadataCatalog(
            path, source="p2go", expected_fingerprint=original)
        self.assertTrue(catalog.describe()["available"])

        replacement = self.root / "replacement-p2go.db"
        with sqlite3.connect(replacement) as con:
            con.execute("CREATE TABLE P2GO_PARENT (ID INTEGER PRIMARY KEY)")
        self.cfg.sources["p2go"].sqlite_path = str(replacement)
        changed = source_fingerprint(self.cfg, "p2go")
        self.assertNotEqual(changed, original)

        stale = MetadataCatalog(
            path, source="p2go", expected_fingerprint=changed).describe()
        self.assertFalse(stale["available"])
        self.assertIn("fingerprint mismatch", stale["detail"])

    def test_fingerprint_excludes_credentials_but_tracks_locator_schema(self) -> None:
        cfg = Config.sample(self.root)
        cfg.sources = {
            "warehouse": DbCfg(
                backend="sqlserver", schema="dbo",
                mssql_conn_str=(
                    "Server=warehouse.example;Database=Ledger;UID=reader;"
                    "PWD=top-secret;Encrypt=yes"))
        }
        first = source_fingerprint(cfg, "warehouse")
        cfg.sources["warehouse"].mssql_conn_str = (
            "Server=warehouse.example;Database=Ledger;UID=someone-else;"
            "PWD=another-secret;Encrypt=no")
        self.assertEqual(source_fingerprint(cfg, "warehouse"), first)
        cfg.sources["warehouse"].schema = "reporting"
        self.assertNotEqual(source_fingerprint(cfg, "warehouse"), first)

    def test_fingerprint_tracks_full_schema_allowlist_not_its_order(self):
        cfg = Config.sample(self.root)
        cfg.sources = {"p2go": DbCfg(
            backend="oracle", schema="P2GO",
            schemas=["P2GO", "TUSINVC"],
            oracle_dsn="db.example:1521/service",
            oracle_user="APPSADM", oracle_password="secret")}
        first = source_fingerprint(cfg, "p2go")
        scalar_cfg = Config.sample(self.root)
        scalar_cfg.sources = {"p2go": DbCfg(
            backend="oracle", schema="P2GO",
            oracle_dsn="db.example:1521/service",
            oracle_user="APPSADM", oracle_password="another-secret")}
        self.assertNotEqual(first, source_fingerprint(scalar_cfg, "p2go"))
        cfg.sources["p2go"].schemas = ["TUSINVC", "P2GO"]
        self.assertEqual(source_fingerprint(cfg, "p2go"), first)
        cfg.sources["p2go"].schemas.append("ANOTHER")
        self.assertNotEqual(source_fingerprint(cfg, "p2go"), first)

    def test_blank_oracle_schema_binds_login_and_tns_resolution(self) -> None:
        p2go_network = self.root / "network" / "p2go"
        finance_network = self.root / "network" / "finance"
        p2go_network.mkdir(parents=True)
        finance_network.mkdir(parents=True)
        p2go_tns = p2go_network / "tnsnames.ora"
        p2go_tns.write_text(
            "P2GO_SERVICE=(DESCRIPTION=(ADDRESS=(HOST=p2go.example))"
            "(CONNECT_DATA=(SERVICE_NAME=p2go)))")
        (finance_network / "tnsnames.ora").write_text(
            "P2GO_SERVICE=(DESCRIPTION=(ADDRESS=(HOST=finance.example))"
            "(CONNECT_DATA=(SERVICE_NAME=finance)))")
        cfg = Config.sample(self.root)
        cfg.sources = {"p2go": DbCfg(
            backend="oracle", schema="", oracle_dsn="P2GO_SERVICE",
            oracle_user="P2GO_READ", oracle_password="secret-one",
            oracle_config_dir="network/p2go")}
        first = source_fingerprint(cfg, "p2go")
        cfg.sources["p2go"].oracle_password = "rotated-secret"
        self.assertEqual(source_fingerprint(cfg, "p2go"), first)
        cfg.sources["p2go"].oracle_user = "FINANCE_READ"
        self.assertNotEqual(source_fingerprint(cfg, "p2go"), first)
        cfg.sources["p2go"].oracle_user = "P2GO_READ"
        p2go_tns.write_text(
            "P2GO_SERVICE=(DESCRIPTION=(ADDRESS=(HOST=repointed.example))"
            "(CONNECT_DATA=(SERVICE_NAME=p2go)))")
        self.assertNotEqual(source_fingerprint(cfg, "p2go"), first)
        p2go_tns.write_text(
            "P2GO_SERVICE=(DESCRIPTION=(ADDRESS=(HOST=p2go.example))"
            "(CONNECT_DATA=(SERVICE_NAME=p2go)))")
        cfg.sources["p2go"].oracle_config_dir = "network/finance"
        self.assertNotEqual(source_fingerprint(cfg, "p2go"), first)

    def test_blank_sqlserver_schema_binds_login_and_dsn(self) -> None:
        cfg = Config.sample(self.root)
        cfg.sources = {"p2go": DbCfg(
            backend="sqlserver", schema="",
            mssql_conn_str=(
                "DSN=P2GO;Server=p2go.example;Database=P2Go;"
                "UID=p2reader;PWD=secret-one"))}
        first = source_fingerprint(cfg, "p2go")
        cfg.sources["p2go"].mssql_conn_str = (
            "DSN=P2GO;Server=p2go.example;Database=P2Go;"
            "UID=p2reader;PWD=rotated-secret")
        self.assertEqual(source_fingerprint(cfg, "p2go"), first)
        cfg.sources["p2go"].mssql_conn_str = (
            "DSN=P2GO;Server=p2go.example;Database=P2Go;"
            "UID=finance_reader;PWD=rotated-secret")
        self.assertNotEqual(source_fingerprint(cfg, "p2go"), first)
        cfg.sources["p2go"].mssql_conn_str = (
            "DSN=FINANCE;Server=finance.example;Database=Finance;"
            "UID=p2reader;PWD=rotated-secret")
        self.assertNotEqual(source_fingerprint(cfg, "p2go"), first)

    def test_sqlserver_dsn_only_artifact_binding_fails_closed(self) -> None:
        cfg = Config.sample(self.root)
        cfg.sources = {"p2go": DbCfg(
            backend="sqlserver", schema="dbo",
            mssql_conn_str="DSN=P2GO;UID=p2reader;PWD=secret")}
        with self.assertRaises(MetadataError) as caught:
            source_fingerprint(cfg, "p2go")
        self.assertIn("explicit Server", str(caught.exception))

    def test_sqlserver_indirect_or_default_database_binding_fails_closed(
            self) -> None:
        cfg = Config.sample(self.root)
        cfg.sources = {"p2go": DbCfg(
            backend="sqlserver", schema="dbo",
            mssql_conn_str="FILEDSN=/tmp/p2go.dsn;UID=p2reader")}
        for connection in (
            "FILEDSN=/tmp/p2go.dsn;UID=p2reader",
            "FILEDSN=/tmp/finance.dsn;UID=p2reader",
            "Server=dbhost;UID=p2reader",
            "Server=(localdb)\\MSSQLLocalDB;"
            "AttachDbFilename=/tmp/p2go.mdf;UID=p2reader",
        ):
            cfg.sources["p2go"].mssql_conn_str = connection
            with self.subTest(connection=connection), self.assertRaises(
                    MetadataError) as caught:
                source_fingerprint(cfg, "p2go")
            self.assertIn("Database/Initial Catalog", str(caught.exception))

    def test_rebuilding_p2go_does_not_replace_finance(self) -> None:
        finance_artifact = self._build("default")
        p2go_artifact = self._build("p2go")
        finance_before = finance_artifact.read_bytes()
        p2go_before = p2go_artifact.read_bytes()

        with sqlite3.connect(self.p2go_path) as con:
            con.execute("CREATE TABLE P2GO_NEW_RELATION (ID INTEGER)")
        self._build("p2go")

        self.assertEqual(finance_artifact.read_bytes(), finance_before)
        self.assertNotEqual(p2go_artifact.read_bytes(), p2go_before)

    def test_cli_narrow_refresh_preserves_the_other_source_artifact(self) -> None:
        from scripts import build_metadata_catalog as builder

        config_path = self.root / "config.yaml"
        config_path.write_text(
            "db:\n  backend: sqlite\n"
            f"  sqlite_path: {self.finance_path}\n"
            "sources:\n  p2go:\n    backend: sqlite\n"
            f"    sqlite_path: {self.p2go_path}\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main([
                "--config", str(config_path), "--quiet"]), 0)
        loaded_cfg = __import__("pstb.config", fromlist=["load_config"]
                            ).load_config(str(config_path))
        finance_artifact = source_catalog_path(loaded_cfg, "default")
        p2go_artifact = source_catalog_path(loaded_cfg, "p2go")
        finance_before = finance_artifact.read_bytes()
        finance_status_before = build_status_path(finance_artifact).read_bytes()
        first_p2go_status = read_build_status(p2go_artifact, "p2go")
        self.assertTrue(first_p2go_status["published"])
        self.assertEqual(first_p2go_status["status"], "complete")

        with sqlite3.connect(self.p2go_path) as con:
            con.execute("CREATE TABLE P2GO_SECOND_REFRESH (ID INTEGER)")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main([
                "--config", str(config_path), "--source", "p2go",
                "--peopletools-source", "none", "--quiet"]), 0)
        self.assertEqual(finance_artifact.read_bytes(), finance_before)
        self.assertEqual(build_status_path(finance_artifact).read_bytes(),
                         finance_status_before)
        refreshed = read_build_status(p2go_artifact, "p2go")
        self.assertTrue(refreshed["published"])
        self.assertNotEqual(refreshed["build_run_id"],
                            first_p2go_status["build_run_id"])
        self.assertEqual(refreshed["previous_snapshot_id"],
                         first_p2go_status["snapshot_id"])

    def test_failed_refresh_is_observable_while_prior_snapshot_survives(self):
        from scripts import build_metadata_catalog as builder

        config_path = self.root / "config.yaml"
        config_path.write_text(
            "db:\n  backend: sqlite\n"
            f"  sqlite_path: {self.finance_path}\n"
            "sources:\n  p2go:\n    backend: sqlite\n"
            f"    sqlite_path: {self.p2go_path}\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main([
                "--config", str(config_path), "--source", "p2go",
                "--peopletools-source", "none", "--quiet"]), 0)
        loaded_cfg = __import__("pstb.config", fromlist=["load_config"]
                            ).load_config(str(config_path))
        artifact = source_catalog_path(loaded_cfg, "p2go")
        before = artifact.read_bytes()
        prior = read_build_status(artifact, "p2go")

        empty = self.root / "empty-p2go.db"
        sqlite3.connect(empty).close()
        config_path.write_text(
            "db:\n  backend: sqlite\n"
            f"  sqlite_path: {self.finance_path}\n"
            "sources:\n  p2go:\n    backend: sqlite\n"
            f"    sqlite_path: {empty}\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(builder.main([
                "--config", str(config_path), "--source", "p2go",
                "--peopletools-source", "none", "--quiet"]), 1)
        self.assertEqual(artifact.read_bytes(), before)
        failed = read_build_status(artifact, "p2go")
        self.assertFalse(failed["published"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["previous_snapshot_id"],
                         prior["snapshot_id"])
        self.assertEqual(failed["failure_category"],
                         "metadata_unavailable")
        self.assertIn("prior p2go artifact", output.getvalue())
        self.assertEqual(
            MetadataCatalog(artifact, source="p2go").describe()[
                "latest_build"],
            failed,
        )

    def test_status_write_failure_cannot_leave_prior_success_looking_current(self):
        from scripts import build_metadata_catalog as builder

        config_path = self.root / "config.yaml"
        config_path.write_text(
            "db:\n  backend: sqlite\n"
            f"  sqlite_path: {self.finance_path}\n"
            "sources:\n  p2go:\n    backend: sqlite\n"
            f"    sqlite_path: {self.p2go_path}\n",
            encoding="utf-8",
        )
        real_write = builder.write_build_status
        calls = 0

        def fail_completion(path, record):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(path, record)
            raise OSError("simulated status filesystem failure")

        output = io.StringIO()
        with patch.object(builder, "write_build_status",
                          side_effect=fail_completion), \
                contextlib.redirect_stdout(output):
            self.assertEqual(builder.main([
                "--config", str(config_path), "--source", "p2go",
                "--peopletools-source", "none", "--quiet"]), 1)

        loaded_cfg = __import__(
            "pstb.config", fromlist=["load_config"]).load_config(
                str(config_path))
        artifact = source_catalog_path(loaded_cfg, "p2go")
        self.assertTrue(artifact.exists(), "the usable artifact was published")
        latest = read_build_status(artifact, "p2go")
        self.assertEqual(latest["status"], "building")
        self.assertFalse(latest["published"])
        self.assertIn("STATUS INCOMPLETE", output.getvalue())


class SourceMetadataToolResultTests(unittest.TestCase):
    def test_every_metadata_tool_names_the_exact_canonical_source(self) -> None:
        from pstb import server

        class FakeCatalog:
            def describe(self):
                return {"available": True}

            def search(self, **_kwargs):
                return {"available": True, "matches": []}

            def context(self, **_kwargs):
                return {"available": True, "found": True}

        with patch.object(
                server, "_metadata_for_source",
                return_value=("p2go", FakeCatalog())):
            described = server.describe_metadata_catalog(source="p2go")
            searched = server.search_metadata("orders", source="p2go")
            context = server.get_metadata_context(
                "P2GO_ORDER", source="p2go")
        for result in (described, searched, context):
            self.assertEqual(result["source_database"], "p2go")

    def test_secondary_join_path_uses_the_bound_metadata_graph(self) -> None:
        from pstb import server

        class FakeCatalog:
            def relationship_path(self, **kwargs):
                return {
                    "available": True,
                    "found": True,
                    "received": kwargs,
                }

        with patch.object(
                server, "_metadata_for_source",
                return_value=("p2go", FakeCatalog())):
            result = server.join_path(
                "P2GO_FACT", "P2GO_PARENT", source="p2go")

        self.assertTrue(result["found"])
        self.assertEqual(result["source_database"], "p2go")
        self.assertEqual(result["received"]["source"], "p2go")


if __name__ == "__main__":
    unittest.main()
