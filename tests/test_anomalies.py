"""Focused contracts for metadata-led transaction/process anomaly detection."""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.anomalies import AnomalyDetector, AnomalyError  # noqa: E402
from pstb.config import Config, load_config  # noqa: E402
from pstb.db import Database  # noqa: E402


class EndToEndCustomTableTests(unittest.TestCase):
    """No delivered prefix and no fake DB: execute the generated SQL."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        path = root / "anomaly.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE ACME_TXN_HDR (
                BUSINESS_UNIT TEXT, TXN_ID TEXT, CREATED_DTTM TEXT);
            CREATE TABLE FIN_TXN_DIST (
                BUSINESS_UNIT TEXT, TXN_ID TEXT, DISTRIB_ID TEXT,
                CREATED_DTTM TEXT);
            CREATE TABLE ACME_PROCESS_RUN (
                BUSINESS_UNIT TEXT, PROCESS_NAME TEXT, START_DTTM TEXT,
                END_DTTM TEXT, RUN_STATUS TEXT);
            CREATE INDEX ACME_HDR_DT ON ACME_TXN_HDR
                (BUSINESS_UNIT, CREATED_DTTM);
            CREATE INDEX FIN_DIST_DT ON FIN_TXN_DIST
                (BUSINESS_UNIT, CREATED_DTTM);
            CREATE INDEX ACME_RUN_DT ON ACME_PROCESS_RUN
                (BUSINESS_UNIT, START_DTTM);
        """)
        asof = dt.date(2026, 7, 1)
        start = asof - dt.timedelta(days=90)
        for offset in range(90):
            day = start + dt.timedelta(days=offset)
            # Dense weekday operations; weekends being absent exercises the
            # same-weekday seasonal baseline rather than inflating anomalies.
            if day.weekday() >= 5:
                continue
            stamp = day.isoformat() + " 09:00:00"
            con.executemany(
                "INSERT INTO ACME_TXN_HDR VALUES ('US001',?,?)",
                [(f"H{offset}-{i}", stamp) for i in range(20)])
            con.executemany(
                "INSERT INTO FIN_TXN_DIST VALUES ('US001',?,?,?)",
                [(f"H{offset}-{i // 2}", f"D{i}", stamp) for i in range(40)])
            con.executemany(
                "INSERT INTO ACME_PROCESS_RUN VALUES ('US001','INVOICE_LOAD',?,?,?)",
                [(stamp, day.isoformat() + " 09:01:00", "SUCCESS") for _ in range(5)])
        today = asof.isoformat() + " 09:00:00"
        con.executemany(
            "INSERT INTO ACME_TXN_HDR VALUES ('US001',?,?)",
            [(f"TODAY-{i}", today) for i in range(25)])
        # Deliberately no FIN_TXN_DIST rows today: counterpart mismatch.
        con.executemany(
            "INSERT INTO ACME_PROCESS_RUN VALUES ('US001','INVOICE_LOAD',?,?,?)",
            [(today, asof.isoformat() + " 09:10:00", "FAILED") for _ in range(5)])
        con.commit()
        con.close()

        cfg = Config.sample(root)
        cfg.db.sqlite_path = path.name
        cfg.anomalies.infer_tables = False
        cfg.anomalies.infer_processes = False
        cfg.anomalies.material_count = 5
        cfg.anomalies.table_rules = [
            {"name": "custom headers", "table": "ACME_TXN_HDR",
             "date_column": "CREATED_DTTM", "scope_column": "BUSINESS_UNIT"},
            {"name": "custom distributions", "table": "FIN_TXN_DIST",
             "date_column": "CREATED_DTTM", "scope_column": "BUSINESS_UNIT"},
        ]
        cfg.anomalies.relationship_rules = [{
            "name": "accepted headers create distributions",
            "left_table": "ACME_TXN_HDR", "right_table": "FIN_TXN_DIST",
            "direction": "left_requires_right", "minimum_trigger_count": 5,
            "explanation": "the accepted interface creates accounting distributions",
        }]
        cfg.anomalies.process_rules = [{
            "name": "invoice loader", "table": "ACME_PROCESS_RUN",
            "date_column": "START_DTTM", "start_column": "START_DTTM",
            "end_column": "END_DTTM", "process_name_column": "PROCESS_NAME",
            "status_column": "RUN_STATUS", "success_values": ["SUCCESS"],
            "scope_column": "BUSINESS_UNIT",
        }]
        cfg.anomalies.min_duration_increase_seconds = 30
        self.db = Database(cfg)
        self.detector = AnomalyDetector(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_counterpart_volume_trend_and_process_degradation(self) -> None:
        statements = []
        original = self.db.query

        def spy(sql, params=None, max_rows=None):
            statements.append(" ".join(str(sql).split()))
            return original(sql, params, max_rows)

        self.db.query = spy
        try:
            out = self.detector.detect(
                "2026-07-01", history_months=3, business_unit="US001",
                include_inferred=False)
        finally:
            self.db.query = original

        kinds = {alert["kind"] for alert in out["alerts"]}
        self.assertIn("related_table_volume_mismatch", kinds)
        self.assertIn("daily_volume_deviation", kinds)
        self.assertIn("process_duration_degradation", kinds)
        self.assertIn("process_success_rate_degradation", kinds)
        mismatch = next(a for a in out["alerts"]
                        if a["kind"] == "related_table_volume_mismatch")
        self.assertEqual(mismatch["observed"]["ACME_TXN_HDR"], 25)
        self.assertEqual(mismatch["observed"]["FIN_TXN_DIST"], 0)
        self.assertIn(mismatch["confidence"], ("high", "medium"))
        self.assertEqual(out["discovery"]["configured_tables"],
                         ["ACME_TXN_HDR", "FIN_TXN_DIST"])
        self.assertIn("no table-name prefix is assumed",
                      out["discovery"]["physical_name_basis"])
        self.assertTrue(statements)
        for statement in statements:
            self.assertTrue(statement.upper().startswith(("SELECT", "PRAGMA")),
                            statement)
            self.assertNotRegex(statement.upper(), r"\b(INSERT|UPDATE|DELETE|MERGE|DROP)\b")

    def test_only_three_or_six_months_are_accepted(self) -> None:
        six = self.detector.detect("2026-07-01", history_months=6,
                                   business_unit="US001",
                                   include_inferred=False)
        self.assertEqual(six["history_start"], "2026-01-01")
        self.assertEqual(six["history_months"], 6)
        with self.assertRaisesRegex(AnomalyError, "must be 3 or 6"):
            self.detector.detect("2026-07-01", history_months=12,
                                 include_inferred=False)


class InferenceAndStatisticsTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.anomalies.min_relation_days = 8
        cfg.anomalies.min_relation_confidence = .55
        self.detector = AnomalyDetector(object(), cfg)

    def test_physical_mapping_supports_any_unique_company_prefix(self) -> None:
        objects = {"ACME_ORDER_HDR", "FIN_ORDER_LINE", "DELIVERED_ORDER_AUDIT"}
        table, basis = self.detector._resolve_physical("ORDER_HDR", "", objects)
        self.assertEqual(table, "ACME_ORDER_HDR")
        self.assertIn("unique catalog suffix", basis)
        table, basis = self.detector._resolve_physical(
            "ORDER_LINE", "FIN_ORDER_LINE", objects)
        self.assertEqual(table, "FIN_ORDER_LINE")
        self.assertEqual(basis, "PSRECDEFN.SQLTABLENAME")

    def test_relation_inference_combines_metadata_and_observed_behavior(self) -> None:
        start, asof = dt.date(2026, 4, 1), dt.date(2026, 7, 1)
        days = [start + dt.timedelta(days=i) for i in range((asof - start).days)]
        left = {day: 20 for day in days if day.weekday() < 5}
        right = {day: 40 for day in days if day.weekday() < 5}
        specs = {name: {"table": name} for name in
                 ("ACME_ORDER_HDR", "FIN_ORDER_DIST")}
        columns = {
            "ACME_ORDER_HDR": {"TXN_ID", "CREATED_DTTM", "BUSINESS_UNIT"},
            "FIN_ORDER_DIST": {"TXN_ID", "DISTRIB_ID", "CREATED_DTTM",
                               "BUSINESS_UNIT"},
        }
        groups = {"query:ORDER_AUDIT": {"ACME_ORDER_HDR", "FIN_ORDER_DIST"}}
        rules = self.detector._inferred_relations(
            specs, {"ACME_ORDER_HDR": left, "FIN_ORDER_DIST": right},
            columns, groups, start, asof)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["direction"], "mutual")
        self.assertGreaterEqual(rules[0]["confidence"], .8)
        self.assertTrue(any("shared identifying columns" in e
                            for e in rules[0]["evidence"]))
        self.assertTrue(any("co-used" in e for e in rules[0]["evidence"]))
        self.assertTrue(any("correlation" in e for e in rules[0]["evidence"]))

    def test_sparse_history_is_not_misreported_as_clean_or_abnormal(self) -> None:
        start, asof = dt.date(2026, 4, 1), dt.date(2026, 7, 1)
        counts = {dt.date(2026, 4, 30): 100, dt.date(2026, 5, 31): 120,
                  dt.date(2026, 6, 30): 110}
        baseline = self.detector._baseline(counts, start, asof)
        self.assertEqual(baseline["status"], "sparse_history")
        spec = {"table": "ACME_MONTH_END", "confidence": 1.0}
        self.assertIsNone(self.detector._volume_alert(spec, 0, baseline, asof))
        self.assertIn("Missing dates are treated as zero", baseline["note"])

    def test_weekday_seasonality_ignores_normal_weekends(self) -> None:
        start, asof = dt.date(2026, 1, 1), dt.date(2026, 7, 1)
        counts = {}
        for i in range((asof - start).days):
            day = start + dt.timedelta(days=i)
            if day.weekday() < 5:
                counts[day] = 20
        baseline = self.detector._baseline(counts, start, asof)
        self.assertEqual(baseline["status"], "ready")
        self.assertEqual(baseline["method"], "same-weekday seasonal baseline")
        self.assertEqual(baseline["median"], 20)


class ConfigurationTests(unittest.TestCase):
    def test_deployment_yaml_loads_rules_and_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
anomalies:
  candidate_limit: 7
  material_pct: 0.75
  table_rules:
    - table: ACME_TXN_HDR
      date_column: CREATED_DTTM
  relationship_rules:
    - left_table: ACME_TXN_HDR
      right_table: FIN_TXN_DIST
""")
            cfg = load_config(str(path))
        self.assertEqual(cfg.anomalies.candidate_limit, 7)
        self.assertEqual(cfg.anomalies.material_pct, .75)
        self.assertEqual(cfg.anomalies.table_rules[0]["table"], "ACME_TXN_HDR")
        self.assertEqual(cfg.anomalies.relationship_rules[0]["right_table"],
                         "FIN_TXN_DIST")

    def test_malformed_rule_is_a_reported_configuration_error(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.anomalies.table_rules = ["ACME_TXN_HDR"]
        detector = AnomalyDetector(object(), cfg)
        specs, errors = detector._configured_tables({})
        self.assertEqual(specs, {})
        self.assertIn("must be a YAML mapping", errors[0]["error"])

    def test_sqlserver_index_metadata_supports_safe_inference(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.db.backend = "sqlserver"
        cfg.db.schema = "dbo"
        db = Database(cfg)
        seen = {}

        def query(sql, params=None, max_rows=None):
            seen.update({"sql": sql, "params": params, "max_rows": max_rows})
            return ([
                {"name": "IX_EVENT", "col": "BUSINESS_UNIT", "pos": 1,
                 "uniq": 0},
                {"name": "IX_EVENT", "col": "CREATED_DTTM", "pos": 2,
                 "uniq": 0},
            ], False)

        db.query = query
        indexes = db.indexes("ACME_TXN_HDR")
        self.assertEqual(indexes[0]["columns"],
                         ["BUSINESS_UNIT", "CREATED_DTTM"])
        self.assertEqual(seen["params"], {"t": "ACME_TXN_HDR", "o": "dbo"})
        self.assertIn("IC.key_ordinal", seen["sql"])


if __name__ == "__main__":
    unittest.main()
