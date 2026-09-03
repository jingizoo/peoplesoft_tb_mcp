"""The BigQuery silo: on Oracle the unlosable thing is the read-only
grant; here it is the customer's money.

Slice 1 promises, held by these tests: the byte cap rides the client's
DEFAULT job config so no call-site can forget it; the dry-run cost gate
refuses over-cap queries BEFORE the job exists and is fed real binds
(a gate that stands down for parameterized SQL guards nothing); no
dead-session retry ever re-bills a query; ADC failures are translated
with the remedy and NEVER latched (a metadata-server blip must not
brick a healthy revision); validation is case-insensitive while
execution stays true-case; the catalog build spends one billed job per
metadata surface, never one per page; liveness never claims
verified-empty (the streaming buffer is invisible to __TABLES__); and
the miner is off with the artifact saying exactly why.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pstb.config import Config, DbCfg, normalize_db_schemas
from pstb.db import (Database, DbError, _to_bq_named, _to_pyformat)

try:
    from google.cloud import bigquery as real_bigquery
    HAVE_BQ = True
except ImportError:                              # pragma: no cover
    HAVE_BQ = False


def _bq_cfg(**over):
    cfg = Config.sample(Path(tempfile.mkdtemp()))
    cfg.db.backend = "bigquery"
    cfg.db.bigquery_project = over.pop("project", "acme-fin")
    cfg.db.schema = over.pop("schema", "sales_mart")
    cfg.db.schemas = [cfg.db.schema]
    for key, value in over.items():
        setattr(cfg.db, key, value)
    return cfg


class TranslatorTests(unittest.TestCase):
    SQL = ("SELECT a FROM t WHERE x LIKE '%INV%' AND y = :y "
           "AND z = ':not_a_bind' AND w = :w")

    def test_pyformat_escapes_percent_and_respects_literals(self):
        q, used = _to_pyformat(self.SQL, {"y": 1, "w": 2, "extra": 3})
        self.assertIn("LIKE '%%INV%%'", q)
        self.assertIn("%(y)s", q)
        self.assertIn("%(w)s", q)
        self.assertIn("':not_a_bind'", q)
        self.assertEqual(used, {"y": 1, "w": 2})

    def test_named_translation_matches_the_same_spans(self):
        q, used = _to_bq_named(self.SQL, {"y": 1, "w": 2})
        self.assertIn("@y", q)
        self.assertIn("@w", q)
        self.assertIn("':not_a_bind'", q)
        self.assertIn("LIKE '%INV%'", q)      # no escaping on this path

    def test_translation_changes_only_binds_and_percents(self):
        q, _ = _to_pyformat(self.SQL, {"y": 1, "w": 2})
        restored = q.replace("%%", "%")
        restored = re.sub(r"%\((\w+)\)s", r":\1", restored)
        self.assertEqual(restored, self.SQL)

    @unittest.skipUnless(HAVE_BQ, "bigquery extra not installed")
    def test_typed_parameters_bool_before_int(self):
        import datetime
        import decimal

        from pstb.db import _bq_query_parameters
        typed = {p.name: p.type_ for p in _bq_query_parameters({
            "flag": True, "n": 2, "x": 1.5,
            "amt": decimal.Decimal("9.99"),
            "day": datetime.date(2026, 1, 1), "s": "text"})}
        self.assertEqual(typed["flag"], "BOOL")
        self.assertEqual(typed["n"], "INT64")
        self.assertEqual(typed["amt"], "NUMERIC")
        self.assertEqual(typed["day"], "DATE")
        self.assertEqual(typed["s"], "STRING")


class _FakeJob:
    def __init__(self):
        self.total_bytes_billed = 12345
        self.cache_hit = False


class _FakeCursor:
    def __init__(self, rows, record, description=None):
        self._rows = list(rows)
        self._record = record
        self.description = description or [("a",), ("b",)]
        self.query_job = _FakeJob()

    def execute(self, sql, params=None, **kwargs):
        if "job_config" in kwargs:
            raise AssertionError(
                "a per-execute job_config could shadow the default "
                "config's byte cap; nothing may ever pass one")
        self._record.append((sql, params))
        if isinstance(self._record, _Scripted) and self._record.error:
            error, self._record.error = self._record.error, None
            raise error

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def close(self):
        pass

    def commit(self):                            # pragma: no cover
        raise AssertionError("the silo never commits")


class _Scripted(list):
    def __init__(self):
        super().__init__()
        self.error = None


class _FakeConnection:
    def __init__(self, rows, record, description=None):
        self._rows = rows
        self._record = record
        self._description = description

    def cursor(self):
        return _FakeCursor(self._rows, self._record, self._description)

    def close(self):
        pass


class _FakeDbapi:
    def __init__(self, rows, record, description=None):
        self._rows = rows
        self._record = record
        self._description = description
        self.paramstyle = "pyformat"

    def connect(self, client):
        return _FakeConnection(self._rows, self._record,
                               self._description)


class _FakeClientModule:
    """Stands in for google.cloud.bigquery at the CLIENT layer."""

    def __init__(self, record):
        self._record = record

    class QueryJobConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.job_timeout_ms = None

        def __getattr__(self, name):
            try:
                return self.__dict__["kwargs"][name]
            except KeyError as exc:
                raise AttributeError(name) from exc

    def Client(self, project=None, location=None,
               default_query_job_config=None):
        self._record.append({
            "project": project, "location": location,
            "job_config": default_query_job_config})
        if default_query_job_config is None or not default_query_job_config.kwargs.get(
                "maximum_bytes_billed"):
            raise AssertionError(
                "a client without the byte cap is an unbounded wallet")
        return SimpleNamespace(close=lambda: None)


def _fake_bigquery_database(cfg, rows=(), description=None):
    """A bigquery Database wired to fakes at the import seam."""
    db = Database(cfg)
    executed = _Scripted()
    constructions = []
    fake_bq = _FakeClientModule(constructions)
    fake_dbapi = _FakeDbapi(list(rows), executed, description)
    fake_cloud = SimpleNamespace(bigquery=fake_bq)
    fake_bq.dbapi = fake_dbapi
    modules = {
        "google": SimpleNamespace(cloud=fake_cloud),
        "google.cloud": fake_cloud,
        "google.cloud.bigquery": fake_bq,
        "google.cloud.bigquery.dbapi": fake_dbapi,
    }
    return db, executed, constructions, patch.dict(sys.modules, modules)


class QueryPathTests(unittest.TestCase):
    def test_the_cap_rides_every_client_construction(self):
        cfg = _bq_cfg(bigquery_max_bytes_billed=555_555_555)
        db, executed, constructions, ctx = _fake_bigquery_database(
            cfg, rows=[(1, 2)])
        with ctx:
            rows, truncated = db.query("SELECT a, b FROM t WHERE x=:x",
                                       {"x": 9})
        self.assertEqual(rows, [{"a": 1, "b": 2}])
        self.assertEqual(len(constructions), 1)
        job_cfg = constructions[0]["job_config"]
        self.assertEqual(job_cfg.kwargs["maximum_bytes_billed"],
                         555_555_555)
        self.assertEqual(job_cfg.kwargs["default_dataset"],
                         "acme-fin.sales_mart")
        self.assertGreater(job_cfg.job_timeout_ms, 0)

    def test_execution_goes_through_pyformat(self):
        cfg = _bq_cfg()
        db, executed, _, ctx = _fake_bigquery_database(cfg, rows=[])
        with ctx:
            db.query("SELECT a, b FROM t WHERE x = :x AND y LIKE '%Z%'",
                     {"x": 1})
        sql, params = executed[0]
        self.assertIn("%(x)s", sql)
        self.assertIn("'%%Z%%'", sql)
        self.assertEqual(params, {"x": 1})

    def test_actual_spend_is_captured_per_thread(self):
        cfg = _bq_cfg()
        db, _, _, ctx = _fake_bigquery_database(cfg, rows=[(1, 2)])
        with ctx:
            db.query("SELECT a, b FROM t", {})
            self.assertEqual(db.last_query_stats()["bytes_billed"], 12345)
            other: dict = {}

            def worker():
                other["stats"] = db.last_query_stats()

            t = threading.Thread(target=worker)
            t.start()
            t.join()
        self.assertEqual(other["stats"], {})

    def test_a_dead_connection_marker_is_never_retried(self):
        """Re-running a query on this backend RE-BILLS it."""
        cfg = _bq_cfg()
        db, executed, _, ctx = _fake_bigquery_database(cfg, rows=[])
        executed.error = RuntimeError(
            "Remote end closed connection without response")
        with ctx:
            with self.assertRaises(DbError):
                db.query("SELECT a FROM t", {})
        self.assertEqual(len(executed), 1)

    def test_the_table_cache_costs_one_job(self):
        cfg = _bq_cfg()
        db, executed, _, ctx = _fake_bigquery_database(
            cfg, rows=[("Orders_2024",), ("Vendors",)],
            description=[("table_name",)])
        with ctx:
            first = db.table_names()
            second = db.table_names()
        self.assertEqual(first, frozenset({"ORDERS_2024", "VENDORS"}))
        self.assertIs(first, second)
        self.assertEqual(len(executed), 1)
        self.assertIn("INFORMATION_SCHEMA.TABLES", executed[0][0])


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(_bq_cfg())

    def _remedy(self, message):
        return str(self.db._translate(RuntimeError(message)))

    def test_the_byte_cap_refusal_reads_as_designed_behavior(self):
        text = self._remedy(
            "403 Query exceeded limit for bytes billed: 1073741824")
        self.assertIn("bigquery_max_bytes_billed", text)
        self.assertIn("LIMIT does not reduce bytes billed", text)

    def test_not_found_names_the_case_rule(self):
        text = self._remedy("404 Not found: Table acme:ds.orders")
        self.assertIn("case-sensitive", text)

    def test_permission_failures_name_the_two_roles(self):
        text = self._remedy("403 Access Denied: BigQuery BigQuery: "
                            "Permission denied on dataset")
        self.assertIn("roles/bigquery.jobUser", text)
        self.assertIn("roles/bigquery.dataViewer", text)

    def test_adc_failures_carry_the_full_remedy_every_time(self):
        text = self._remedy(
            "Could not automatically determine credentials")
        self.assertIn("gcloud auth application-default login", text)
        self.assertIn("Cloud Run", text)

    def test_a_timeout_gets_the_bigquery_remedy_not_ps_ledger(self):
        text = self._remedy("Operation did not complete: timeout")
        self.assertNotIn("PS_LEDGER", text)
        self.assertIn("partition", text)

    def test_credential_failures_are_never_latched(self):
        """The argued inverse of the Oracle lockout: ADC has no
        FAILED_LOGIN_ATTEMPTS counter, and a latch would let one
        metadata-server blip brick a healthy revision until restart."""
        cfg = _bq_cfg()
        db, executed, _, ctx = _fake_bigquery_database(cfg, rows=[])
        with ctx:
            executed.error = RuntimeError(
                "Could not automatically determine credentials")
            with self.assertRaises(DbError):
                db.query("SELECT a FROM t", {})
            self.assertEqual(db._credentials_refused, "")
            executed.error = RuntimeError(
                "Could not automatically determine credentials")
            with self.assertRaises(DbError):
                db.query("SELECT a FROM t", {})
        self.assertEqual(len(executed), 2)      # it tried again


class CostGateTests(unittest.TestCase):
    def _gate(self, plan, warn=268_435_456, binds=None):
        from pstb.engine import TBEngine as Engine, EngineError
        cfg = _bq_cfg(bigquery_warn_bytes=warn)
        fake_self = SimpleNamespace(
            db=SimpleNamespace(dialect="bigquery", cfg=cfg,
                               explain_plan=lambda sql, p: plan))
        return Engine._cost_gate, fake_self, EngineError

    def test_over_cap_is_refused_before_the_job_exists(self):
        gate, fake_self, EngineError = self._gate({
            "available": True, "estimated_bytes": 5_000_000_000,
            "cap_bytes": 1_073_741_824})
        with self.assertRaises(EngineError) as caught:
            gate(fake_self, "SELECT 1", "SELECT 1", {"x": 1})
        self.assertIn("bigquery_max_bytes_billed", str(caught.exception))

    def test_above_warn_is_disclosed_below_warn_is_silent(self):
        gate, fake_self, _ = self._gate({
            "available": True, "estimated_bytes": 300_000_000,
            "cap_bytes": 1_073_741_824, "estimated_cost_usd": 0.002})
        note = gate(fake_self, "SELECT 1", "SELECT 1", {})
        self.assertEqual(note["plan"]["estimated_bytes"], 300_000_000)
        gate, fake_self, _ = self._gate({
            "available": True, "estimated_bytes": 5_000_000,
            "cap_bytes": 1_073_741_824})
        self.assertEqual(gate(fake_self, "SELECT 1", "SELECT 1", {}), {})

    def test_unavailable_estimates_degrade_with_the_reason(self):
        gate, fake_self, _ = self._gate({"available": False,
                                         "reason": "dry run refused"})
        note = gate(fake_self, "SELECT 1", "SELECT 1", {})
        self.assertFalse(note["plan"]["available"])
        self.assertIn("without a byte check", note["plan"]["note"])

    def test_the_gate_receives_real_binds(self):
        """A dry run of parameterized SQL needs values: the guards
        doctrine PREFERS parameterized SQL, so a bindless gate stands
        down for exactly the query class it must price."""
        seen = {}

        def plan(sql, params):
            seen["params"] = params
            return {"available": True, "estimated_bytes": 1,
                    "cap_bytes": 10}

        from pstb.engine import TBEngine as Engine
        cfg = _bq_cfg()
        fake_self = SimpleNamespace(
            db=SimpleNamespace(dialect="bigquery", cfg=cfg,
                               explain_plan=plan))
        Engine._cost_gate(fake_self, "S", "S", {"bu": "US001"})
        self.assertEqual(seen["params"], {"bu": "US001"})

    def test_partitioned_execution_is_refused(self):
        from pstb.engine import TBEngine as Engine, EngineError
        fake_self = SimpleNamespace(
            db=SimpleNamespace(dialect="bigquery"))
        with self.assertRaises(EngineError) as caught:
            Engine._run_partitioned(fake_self, "S", "S",
                                    {"column": "BU"}, {}, 100)
        self.assertIn("multiply", str(caught.exception))


class ConfigTests(unittest.TestCase):
    def test_the_primary_db_refuses_bigquery(self):
        from pstb.config import _validate_bigquery_source
        with self.assertRaises(RuntimeError):
            _validate_bigquery_source(
                {"backend": "bigquery", "bigquery_project": "p",
                 "schema": "d"}, section="db")

    def test_a_source_needs_project_and_one_dataset(self):
        from pstb.config import _validate_bigquery_source
        with self.assertRaises(RuntimeError):
            _validate_bigquery_source(
                {"backend": "bigquery", "schema": "d"},
                section="sources.w")
        with self.assertRaises(RuntimeError):
            _validate_bigquery_source(
                {"backend": "bigquery", "bigquery_project": "p",
                 "schema": ["a", "b"]}, section="sources.w")

    def test_budget_floors_ceilings_and_bool_rejection(self):
        from pstb.config import _validate_bigquery_source
        base = {"backend": "bigquery", "bigquery_project": "p",
                "schema": "d"}
        for bad in ({"bigquery_max_bytes_billed": 1024},
                    {"bigquery_max_bytes_billed": True},
                    {"bigquery_max_bytes_billed": 2**41},
                    {"bigquery_warn_bytes": 2**31,
                     "bigquery_max_bytes_billed": 2**30}):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    _validate_bigquery_source({**base, **bad},
                                              section="sources.w")
        _validate_bigquery_source(
            {**base, "bigquery_max_bytes_billed": 10 * 1024 * 1024},
            section="sources.w")

    def test_dataset_case_survives_repeated_normalization(self):
        cfg = DbCfg(backend="bigquery", schema="Sales_Mart")
        normalize_db_schemas(cfg, section="sources.w")
        normalize_db_schemas(cfg, section="sources.w")
        self.assertEqual(cfg.schema, "Sales_Mart")
        oracle = DbCfg(backend="oracle", schema="sysadm")
        normalize_db_schemas(oracle, section="db")
        self.assertEqual(oracle.schema, "SYSADM")

    def test_validation_space_still_uppercases(self):
        cfg = _bq_cfg(schema="Sales_Mart")
        db = Database(cfg)
        self.assertEqual(db.default_schema, "SALES_MART")
        self.assertEqual(db.bigquery_dataset, "Sales_Mart")
        self.assertEqual(db.prefix, "")


class _CollectorDb:
    """A scripted bigquery-shaped Database for the catalog build."""

    dialect = "bigquery"

    def __init__(self, tables=None, columns=None, storage=None,
                 views=None):
        self.cfg = _bq_cfg()
        self.executed = []
        self._tables = tables if tables is not None else [
            {"schema_name": "sales_mart", "object_name": "Orders_2024",
             "object_type": "TABLE"},
            {"schema_name": "sales_mart", "object_name": "Vendors",
             "object_type": "TABLE"},
            {"schema_name": "sales_mart", "object_name": "Order_Lines_V",
             "object_type": "VIEW"},
        ]
        self._columns = columns if columns is not None else [
            {"schema_name": "sales_mart", "object_name": "Orders_2024",
             "column_name": "ORDER_ID", "data_type": "STRING",
             "data_length": None, "nullable": "N"},
            {"schema_name": "sales_mart", "object_name": "Orders_2024",
             "column_name": "VENDOR_ID", "data_type": "STRING",
             "data_length": None, "nullable": "Y"},
            {"schema_name": "sales_mart", "object_name": "Vendors",
             "column_name": "VENDOR_ID", "data_type": "STRING",
             "data_length": None, "nullable": "N"},
            {"schema_name": "sales_mart", "object_name": "Vendors",
             "column_name": "VENDOR_NAME", "data_type": "STRING",
             "data_length": None, "nullable": "Y"},
            {"schema_name": "sales_mart", "object_name": "Order_Lines_V",
             "column_name": "ORDER_ID", "data_type": "STRING",
             "data_length": None, "nullable": "Y"},
        ]
        self._storage = storage if storage is not None else [
            {"table_id": "Orders_2024", "row_count": 120,
             "modified_at": "2026-08-30T00:00:00", "type": 1},
            {"table_id": "Vendors", "row_count": 0,
             "modified_at": "2026-01-01T00:00:00", "type": 1},
            {"table_id": "Order_Lines_V", "row_count": None,
             "modified_at": None, "type": 2},
        ]
        self._views = views if views is not None else [
            {"schema_name": "sales_mart", "view_name": "Order_Lines_V",
             "text": "SELECT O.ORDER_ID AS ORDER_NUMBER FROM "
                     "`acme-fin.sales_mart.Orders_2024` O JOIN "
                     "`acme-fin.sales_mart.Vendors` V "
                     "ON O.VENDOR_ID = V.VENDOR_ID"},
        ]

    @property
    def default_schema(self):
        return "SALES_MART"

    @property
    def allowed_schemas(self):
        return ("SALES_MART",)

    @property
    def bigquery_dataset(self):
        return "sales_mart"

    def query(self, sql, params=None, max_rows=None):
        self.executed.append(sql)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return list(self._tables), False
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return list(self._columns), False
        if "__TABLES__" in sql:
            return list(self._storage), False
        if "INFORMATION_SCHEMA.VIEWS" in sql:
            return list(self._views), False
        raise AssertionError(f"unexpected billed query: {sql[:80]}")


class CatalogBuildTests(unittest.TestCase):
    def _build(self, db=None):
        from pstb.metadata import build_catalog
        root = Path(tempfile.mkdtemp())
        db = db or _CollectorDb()
        build_catalog(root / "c.db", [("warehouse", db)])
        import sqlite3
        con = sqlite3.connect(root / "c.db")
        con.row_factory = sqlite3.Row
        return con, db

    def test_one_billed_job_per_metadata_surface(self):
        con, db = self._build()
        surfaces = {}
        for sql in db.executed:
            for marker in ("INFORMATION_SCHEMA.TABLES",
                           "INFORMATION_SCHEMA.COLUMNS", "__TABLES__",
                           "INFORMATION_SCHEMA.VIEWS"):
                if marker in sql:
                    surfaces[marker] = surfaces.get(marker, 0) + 1
        con.close()
        self.assertEqual(max(surfaces.values()), 1, surfaces)
        self.assertEqual(len(surfaces), 4)

    def test_artifact_names_fold_uppercase_and_the_remedy_covers_it(self):
        """The catalog is a case-blind discovery index (the native
        writer folds with _u); TRUE case comes from list_tables, which
        reads INFORMATION_SCHEMA directly, and the not-found remedy
        says to copy from there. A disclosed slice-1 trade, not an
        accident."""
        con, _ = self._build()
        names = {r[0] for r in con.execute(
            "SELECT name FROM nodes WHERE kind IN ('table','view')")}
        con.close()
        self.assertIn("ORDERS_2024", names)
        remedy = str(Database(_bq_cfg())._translate(
            RuntimeError("404 Not found: Table x")))
        self.assertIn("list_tables", remedy)

    def test_liveness_never_claims_verified_empty(self):
        """__TABLES__ cannot see the streaming buffer, so the artifact
        must never say verified_empty_current -- by name, unreachable."""
        from pstb.metadata import MetadataCatalog
        con, db = self._build()
        liveness = {r["name"]: r["liveness"] for r in con.execute(
            "SELECT n.name, p.liveness FROM object_profiles p "
            "JOIN nodes n ON n.id = p.node_id")}
        path = con.execute("PRAGMA database_list").fetchone()[2]
        con.close()
        self.assertEqual(liveness["ORDERS_2024"], "populated")
        self.assertEqual(liveness["VENDORS"], "empty")
        catalog = MetadataCatalog(path, source="warehouse")
        for name in ("ORDERS_2024", "VENDORS"):
            evidence = catalog.object_evidence(name, source="warehouse")
            self.assertTrue(evidence.get("found"), name)
            self.assertNotEqual(evidence.get("caveat_branch"),
                                "verified_empty_current",
                                f"{name} over-claims")
            self.assertFalse(evidence.get("empty_verified"), name)

    def test_schema_coverage_does_not_false_alarm(self):
        """A healthy harvest must not carry 'returned no TABLE/VIEW
        metadata': the coverage check compares in VALIDATION space
        (uppercase), and a true-case dataset name slipping into it
        turns a working source into a standing false alarm -- the
        caveat-that-fires-on-a-correct-answer this repo's doctrine
        ranks worse than a miss."""
        con, _ = self._build()
        notes = [r[0] for r in con.execute("SELECT note FROM notes")]
        con.close()
        for note in notes:
            self.assertNotIn("returned no TABLE/VIEW metadata", note)

    def test_the_miner_note_and_tier0_note_land(self):
        con, _ = self._build()
        notes = [r[0] for r in con.execute("SELECT note FROM notes")]
        con.close()
        joined = " ".join(notes)
        self.assertIn("value-overlap join mining is disabled on "
                      "BigQuery", joined)
        self.assertIn("no enforced keys", joined)

    def test_view_vocabulary_survives_the_normalizer(self):
        """Real-shaped view text -- backticked, project-qualified --
        must still teach the alias and the tier-1 join."""
        con, _ = self._build()
        aliases = [r[0] for r in con.execute(
            "SELECT alias_upper FROM aliases")]
        edges = [r[0] for r in con.execute(
            "SELECT kind FROM edges WHERE kind='view_declared_join'")]
        con.close()
        self.assertIn("ORDER_NUMBER", " ".join(aliases))
        self.assertTrue(edges, "the declared join was lost")

    def test_an_empty_dataset_refuses_to_clobber_the_artifact(self):
        """Zero harvested objects reads as broken grants, not an empty
        world: the existing fail-closed doctrine refuses the build and
        keeps whatever artifact already stood."""
        from pstb.metadata import MetadataError, build_catalog
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(MetadataError) as caught:
            build_catalog(root / "c.db", [("warehouse", _CollectorDb(
                tables=[], columns=[], storage=[], views=[]))])
        self.assertIn("No metadata could be harvested",
                      str(caught.exception))


class EnginePathTests(unittest.TestCase):
    def test_table_exists_reads_the_cache_not_the_meter(self):
        from pstb.engine import TBEngine as Engine
        calls = []
        fake_self = SimpleNamespace(
            db=SimpleNamespace(
                dialect="bigquery",
                table_names=lambda: calls.append(1) or frozenset(
                    {"ORDERS_2024"})),
            _table_target=lambda name: ("", name.upper()))
        exists = Engine._table_exists(fake_self, "orders_2024")
        self.assertTrue(exists)
        self.assertEqual(len(calls), 1)

    def test_profile_record_is_refused_with_the_reason(self):
        from pstb.profiles import RecordProfiler
        profiler = RecordProfiler.__new__(RecordProfiler)
        profiler.db = SimpleNamespace(dialect="bigquery")
        with self.assertRaises(DbError) as caught:
            RecordProfiler.profile(profiler, "TU_X")
        self.assertIn("bills the whole table", str(caught.exception))


class PackagingTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_the_extra_and_ci_are_wired(self):
        pyproject = (self.ROOT / "pyproject.toml").read_text()
        self.assertIn('bigquery = ["google-cloud-bigquery', pyproject)
        workflow = next(
            (self.ROOT / ".github" / "workflows").glob("*.yml")
        ).read_text()
        self.assertIn(".[gui,iap,bigquery]", workflow)

    @unittest.skipUnless(HAVE_BQ, "bigquery extra not installed")
    def test_the_storage_shortcut_is_not_installed(self):
        import importlib.util
        self.assertIsNone(
            importlib.util.find_spec("google.cloud.bigquery_storage"),
            "bigquery-storage switches the shim to a second, "
            "differently-billed fetch path this release has not tested")

    def test_an_absent_package_names_the_extra(self):
        code = (
            "import sys, builtins\n"
            "real = builtins.__import__\n"
            "def block(name, *a, **k):\n"
            "    if name.startswith('google'):\n"
            "        raise ImportError(name)\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = block\n"
            "from pstb.config import Config\n"
            "from pstb.db import Database, DbError\n"
            "import pathlib, tempfile\n"
            "cfg = Config.sample(pathlib.Path(tempfile.mkdtemp()))\n"
            "cfg.db.backend = 'bigquery'\n"
            "cfg.db.bigquery_project = 'p'\n"
            "cfg.db.schema = 'd'\n"
            "db = Database(cfg)\n"
            "try:\n"
            "    db.query('SELECT 1', {})\n"
            "    print('FAIL: queried without the package')\n"
            "except DbError as e:\n"
            "    assert '.[bigquery]' in str(e), e\n"
            "    print('names the extra')\n")
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], capture_output=True,
            text=True, cwd=str(self.ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("names the extra", result.stdout)


@unittest.skipUnless(HAVE_BQ, "bigquery extra not installed")
class ShimContractTests(unittest.TestCase):
    """Drift-catchers against the REAL shim, no network needed."""

    def test_the_shim_speaks_pyformat(self):
        from google.cloud.bigquery import dbapi
        self.assertEqual(dbapi.paramstyle, "pyformat")

    def test_job_config_accepts_the_fields_we_set(self):
        cfg = real_bigquery.QueryJobConfig(
            maximum_bytes_billed=1, use_query_cache=True,
            default_dataset="p.d", labels={"app": "pstb"})
        cfg.job_timeout_ms = 1000
        self.assertEqual(cfg.maximum_bytes_billed, 1)

    def test_our_pyformat_output_survives_the_shims_formatter(self):
        from google.cloud.bigquery.dbapi import cursor as shim_cursor
        corpus = [
            ("SELECT a FROM t WHERE x LIKE '%INV%' AND y = :y",
             {"y": 1}),
            ("SELECT ':fake' AS c FROM t WHERE r = :r", {"r": "v"}),
            ("SELECT 100 % 7 AS m FROM t WHERE k = :k", {"k": 2}),
        ]
        for sql, params in corpus:
            with self.subTest(sql=sql):
                q, used = _to_pyformat(sql, params)
                formatted = shim_cursor._format_operation(q, used)
                self.assertNotIn("%(", formatted)


if __name__ == "__main__":
    unittest.main()
