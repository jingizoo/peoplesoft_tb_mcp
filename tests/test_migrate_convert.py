"""Delivered-table data conversion: column mapping, risks, pre-flight probes.

The fixture is a delivered table that reshapes the way real ones do between
releases: a column renamed, a column widened, one shortened, decimals cut, a
new 9.2 column with no source, one 9.1 column dropped, and — the dangerous
one — a key removed so 9.1 rows collide on the 9.2 key.

Pre-flight must count these on the real data, not merely predict them: a
migration is decided by how many rows are actually at risk.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config, DbCfg  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.migrate.convert import build_mapping, insert_select  # noqa: E402
from pstb.migrate.pipeline import MigratePipeline  # noqa: E402
from pstb.migrate.spec import BLOCKER, WARNING, MigrateError  # noqa: E402
from pstb.sources import SourceRegistry  # noqa: E402

_TOOLS_DDL = (
    "CREATE TABLE PSRECDEFN (RECNAME TEXT, RECTYPE INTEGER, SQLTABLENAME TEXT,"
    " RELLANGRECNAME TEXT, AUDITRECNAME TEXT, LASTUPDOPRID TEXT, RECDESCR TEXT)",
    "CREATE TABLE PSRECFIELD (RECNAME TEXT, FIELDNAME TEXT, FIELDNUM INTEGER,"
    " USEEDIT INTEGER, EDITTABLE TEXT)",
    "CREATE TABLE PSDBFIELD (FIELDNAME TEXT, FIELDTYPE INTEGER,"
    " LENGTH INTEGER, DECIMALPOS INTEGER)",
    "CREATE TABLE PSSQLTEXTDEFN (SQLID TEXT, SEQNUM INTEGER, SQLTEXT TEXT)",
)


def _seed(path: Path, dbfields: dict, records: list, tables: dict) -> None:
    c = sqlite3.connect(path)
    for ddl in _TOOLS_DDL:
        c.execute(ddl)
    c.executemany("INSERT INTO PSDBFIELD VALUES (?,?,?,?)",
                  [(n, t, l, d) for n, (t, l, d) in dbfields.items()])
    for recname, rectype, oprid, fields in records:
        c.execute("INSERT INTO PSRECDEFN VALUES (?,?,?,?,?,?,?)",
                  (recname, rectype, "", "", "", oprid, f"{recname} descr"))
        c.executemany("INSERT INTO PSRECFIELD VALUES (?,?,?,?,?)",
                      [(recname, f, i + 1, use, "")
                       for i, (f, use) in enumerate(fields)])
    for ddl, rows in tables.values():
        c.execute(ddl)
        if rows:
            n = len(rows[0])
            c.executemany(
                f"INSERT INTO {ddl.split()[2]} VALUES ({','.join('?' * n)})",
                rows)
    c.commit()
    c.close()


# 9.1: JRNL_LN keyed by (BUSINESS_UNIT, JOURNAL_ID, JOURNAL_LINE), with a
# 10-char DEPTID, 4-decimal amount, an OLD_REF column and no PROCESS_INSTANCE.
_SRC_DBFIELDS = {
    "BUSINESS_UNIT": (0, 5, 0), "JOURNAL_ID": (0, 10, 0),
    "JOURNAL_LINE": (2, 9, 0), "DEPTID": (0, 10, 0),
    "MONETARY_AMOUNT": (2, 28, 4), "OLD_REF": (0, 30, 0),
    "LINE_DESCR": (0, 30, 0),
}
# 9.2: JOURNAL_LINE is no longer a key (rows collide), DEPTID shortened to 5,
# amount cut to 2 decimals, OLD_REF renamed to REFERENCE_ID, and a new
# required PROCESS_INSTANCE column appears.
_TGT_DBFIELDS = {
    "BUSINESS_UNIT": (0, 5, 0), "JOURNAL_ID": (0, 10, 0),
    "JOURNAL_LINE": (2, 9, 0), "DEPTID": (0, 5, 0),
    "MONETARY_AMOUNT": (2, 28, 2), "REFERENCE_ID": (0, 30, 0),
    "LINE_DESCR": (0, 30, 0), "PROCESS_INSTANCE": (2, 10, 0),
}


def seed_src(path: Path) -> None:
    _seed(path, _SRC_DBFIELDS, [
        ("JRNL_LN", 0, "PPLSOFT", [
            ("BUSINESS_UNIT", 1), ("JOURNAL_ID", 1), ("JOURNAL_LINE", 1),
            ("DEPTID", 0), ("MONETARY_AMOUNT", 0), ("OLD_REF", 0),
            ("LINE_DESCR", 0)]),
    ], {"JRNL_LN": (
        "CREATE TABLE PS_JRNL_LN (BUSINESS_UNIT TEXT, JOURNAL_ID TEXT,"
        " JOURNAL_LINE INTEGER, DEPTID TEXT, MONETARY_AMOUNT REAL,"
        " OLD_REF TEXT, LINE_DESCR TEXT)",
        [
            # Two rows differing only by JOURNAL_LINE -> collide in 9.2.
            ("US001", "J1", 1, "DEPT001", 100.1234, "R1", "line one"),
            ("US001", "J1", 2, "SALES", 200.50, "R2", "line two"),
            # DEPT001 is 7 chars -> truncates into the 5-char 9.2 DEPTID.
            ("US002", "J2", 1, "DEPT001", 50.0, "R3", "line three"),
            ("US002", "J3", 1, "OPS", 25.25, "R4", "line four"),
        ])})


def seed_tgt(path: Path) -> None:
    _seed(path, _TGT_DBFIELDS, [
        ("JRNL_LN", 0, "PPLSOFT", [
            ("BUSINESS_UNIT", 1), ("JOURNAL_ID", 1), ("JOURNAL_LINE", 0),
            ("DEPTID", 0), ("MONETARY_AMOUNT", 0), ("REFERENCE_ID", 0),
            ("LINE_DESCR", 0), ("PROCESS_INSTANCE", 0)]),
    ], {"JRNL_LN": (
        "CREATE TABLE PS_JRNL_LN (BUSINESS_UNIT TEXT, JOURNAL_ID TEXT,"
        " JOURNAL_LINE INTEGER, DEPTID TEXT, MONETARY_AMOUNT REAL,"
        " REFERENCE_ID TEXT, LINE_DESCR TEXT, PROCESS_INSTANCE INTEGER)",
        [])})


class DeliveredConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.src_path, self.tgt_path = tmp / "s91.db", tmp / "t92.db"
        seed_src(self.src_path)
        seed_tgt(self.tgt_path)
        cfg = Config(root=tmp)
        cfg.db = DbCfg(backend="sqlite", sqlite_path=str(self.tgt_path))
        cfg.sources = {"s91": DbCfg(backend="sqlite",
                                    sqlite_path=str(self.src_path))}
        cfg.migrate.source = "s91"
        cfg.migrate.custom_prefixes = ["Z_"]
        cfg.migrate.delivered_data = "convert"
        cfg.migrate.state_path = str(tmp / "state.db")
        cfg.migrate.out_dir = str(tmp / "out")
        cfg.migrate.mapping_overrides = str(tmp / "maps.json")
        self.tmp, self.cfg = tmp, cfg
        self.primary = Database(cfg)
        self.pipe = MigratePipeline(cfg, SourceRegistry(cfg, self.primary))

    def tearDown(self) -> None:
        self.primary.close()
        self._tmp.cleanup()

    def _plan(self):
        return self.pipe.plan(seed_records=["JRNL_LN"])

    # ---- classification --------------------------------------------------
    def test_delivered_record_is_converted_only_when_opted_in(self) -> None:
        plan = self._plan()
        item = plan["items"][0]
        self.assertEqual(item["classification"], "delivered_convert")
        self.assertEqual(item["data_plan"], "mapped_sql")
        self.assertTrue(any("bypass" in n.lower() or "Oracle" in n
                            for n in item["notes"]),
                        "the conversion path must state what it bypasses")

        self.cfg.migrate.delivered_data = "skip"
        pipe = MigratePipeline(self.cfg, SourceRegistry(self.cfg, self.primary))
        item = pipe.plan(seed_records=["JRNL_LN"])["items"][0]
        self.assertEqual(item["classification"], "delivered_ok")
        self.assertEqual(item["data_plan"], "none")

    def test_invalid_delivered_data_policy_is_refused(self) -> None:
        self.cfg.migrate.delivered_data = "yes please"
        with self.assertRaises(MigrateError):
            MigratePipeline(self.cfg, SourceRegistry(self.cfg, self.primary))

    # ---- mapping ---------------------------------------------------------
    def test_mapping_resolves_every_target_column(self) -> None:
        self._plan()
        m = self.pipe.mapping("JRNL_LN")["mappings"]["JRNL_LN"]
        kinds = {c["target_column"]: c["kind"] for c in m["columns"]}
        self.assertEqual(kinds["BUSINESS_UNIT"], "direct")
        self.assertEqual(kinds["DEPTID"], "direct")
        # New in 9.2 with no source -> PeopleSoft's own numeric default.
        self.assertEqual(kinds["PROCESS_INSTANCE"], "defaulted")
        expr = {c["target_column"]: c["source_expr"] for c in m["columns"]}
        self.assertEqual(expr["PROCESS_INSTANCE"], "0")
        # REFERENCE_ID has no same-named source and no override yet.
        self.assertEqual(kinds["REFERENCE_ID"], "defaulted")
        self.assertEqual(m["dropped_source"], ["OLD_REF"])
        self.assertIn("REFERENCE_ID", m["rename_suggestions"])
        self.assertEqual(
            m["rename_suggestions"]["REFERENCE_ID"][0]["source_column"],
            "OLD_REF")

    def test_shape_risks_are_reported_with_severity(self) -> None:
        self._plan()
        m = self.pipe.mapping("JRNL_LN")["mappings"]["JRNL_LN"]
        risks = {c["target_column"]: [r["message"] for r in c["risks"]]
                 for c in m["columns"]}
        self.assertTrue(any("truncate" in r for r in risks["DEPTID"]))
        self.assertTrue(any("round" in r for r in risks["MONETARY_AMOUNT"]))
        record_risks = " ".join(r["message"] for r in m["record_risks"])
        self.assertIn("Key set changed", record_risks)
        self.assertIn("JOURNAL_LINE", record_risks)
        self.assertTrue(m["blocked"], "a lost key column must block")

    def test_overrides_apply_renames_expressions_and_filters(self) -> None:
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {
                "where": "BUSINESS_UNIT = 'US001'",
                "columns": {
                    "REFERENCE_ID": {"from": "OLD_REF"},
                    "PROCESS_INSTANCE": {"expr": "0"},
                },
            }
        }))
        m = self.pipe.mapping("JRNL_LN")["mappings"]["JRNL_LN"]
        kinds = {c["target_column"]: c["kind"] for c in m["columns"]}
        self.assertEqual(kinds["REFERENCE_ID"], "renamed")
        self.assertEqual(kinds["PROCESS_INSTANCE"], "expression")
        self.assertEqual(m["where"], "BUSINESS_UNIT = 'US001'")
        # Once mapped, OLD_REF is no longer an unexplained dropped column.
        self.assertEqual(m["dropped_source"], [])

    def test_override_pointing_at_a_missing_column_blocks(self) -> None:
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {"columns": {"REFERENCE_ID": {"from": "NOPE"}}}}))
        m = self.pipe.mapping("JRNL_LN")["mappings"]["JRNL_LN"]
        msgs = [r["message"] for c in m["columns"] for r in c["risks"]]
        self.assertTrue(any("does not exist on the 9.1 table" in x
                            for x in msgs))

    def test_a_key_column_with_no_source_is_a_blocker(self) -> None:
        src = self.pipe.source.record("JRNL_LN")
        tgt = self.pipe.target.record("JRNL_LN")
        for f in tgt.fields:          # make the new 9.2-only column a key
            if f.fieldname == "PROCESS_INSTANCE":
                f.useedit = 1
        m = build_mapping("JRNL_LN", src, tgt, {})
        pi = next(c for c in m.columns if c.target_column == "PROCESS_INSTANCE")
        self.assertTrue(any(sev == BLOCKER for sev, _, _ in pi.risks))
        self.assertTrue(any("KEY column" in msg for _, _, msg in pi.risks))
        # No probe can count this away — it must survive pre-flight.
        self.assertIn("unsourced_key",
                      [code for _, code, _ in m.unmeasured_blockers()])

    # ---- pre-flight ------------------------------------------------------
    def test_preflight_counts_real_rows_at_risk(self) -> None:
        self._plan()
        res = self.pipe.preflight()["results"][0]
        self.assertEqual(res["rows_examined"], 4)
        probes = {p["probe"]: p for p in res["probes"]}
        # DEPT001 (7 chars) appears twice and exceeds the 5-char 9.2 column.
        self.assertEqual(probes["truncation"]["rows_at_risk"], 2)
        self.assertEqual(probes["truncation"]["column"], "DEPTID")
        # 100.1234 has more than 2 decimals; the others do not.
        self.assertEqual(probes["rounding"]["rows_at_risk"], 1)
        # J1 lines 1 and 2 collapse onto one 9.2 key.
        self.assertEqual(probes["key_collision"]["rows_at_risk"], 1)
        self.assertEqual(probes["key_collision"]["severity"], BLOCKER)
        self.assertFalse(res["ok"])
        self.assertTrue(res["blocking"])
        self.assertEqual(self.pipe.state.get("JRNL_LN")["status"], "blocked")

    def test_preflight_row_filter_narrows_what_is_measured(self) -> None:
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {"where": "BUSINESS_UNIT = 'US002'"}}))
        res = self.pipe.preflight()["results"][0]
        self.assertEqual(res["rows_examined"], 2)
        probes = {p["probe"]: p for p in res["probes"]}
        self.assertEqual(probes["truncation"]["rows_at_risk"], 1)
        # US002's rows have distinct journal IDs, so no collision remains.
        self.assertEqual(probes["key_collision"]["rows_at_risk"], 0)

    def test_a_clean_mapping_passes_preflight(self) -> None:
        # The mapping still carries a blocker-level key-set change, but that
        # risk is MEASURABLE: on this filtered subset nothing actually
        # collides, so the data clears it. A structural warning that no
        # amount of clean data can clear would make pre-flight pointless.
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {
                "where": "BUSINESS_UNIT = 'US002' AND DEPTID = 'OPS'",
                "columns": {"REFERENCE_ID": {"from": "OLD_REF"}}}}))
        m = self.pipe.mapping("JRNL_LN")["mappings"]["JRNL_LN"]
        self.assertTrue(m["blocked"], "the key-set change is still a blocker")
        self.assertEqual(m["unmeasured_blockers"], [])
        res = self.pipe.preflight()["results"][0]
        self.assertTrue(res["ok"], res)
        self.assertFalse(res.get("blocking"))

    def test_an_unmeasurable_blocker_survives_a_clean_probe(self) -> None:
        # A type-family change cannot be counted away: even with zero rows
        # selected, pre-flight must keep reporting it.
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {"where": "BUSINESS_UNIT = 'NOPE'",
                        "columns": {"REFERENCE_ID": {"from": "JOURNAL_LINE"}}}}))
        res = self.pipe.preflight()["results"][0]
        self.assertEqual(res["rows_examined"], 0)
        self.assertTrue(res["blocking"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["unmeasured_blockers"][0]["code"], "type_family")
        self.assertEqual(self.pipe.state.get("JRNL_LN")["status"], "blocked")

    # ---- emit ------------------------------------------------------------
    def test_emit_writes_a_runnable_insert_select(self) -> None:
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {"where": "BUSINESS_UNIT = 'US001'",
                        "columns": {"REFERENCE_ID": {"from": "OLD_REF"}}}}))
        result = self.pipe.emit()
        sql = (Path(result["out_dir"]) / "convert" / "JRNL_LN.sql").read_text()
        self.assertIn("INSERT INTO PS_JRNL_LN (", sql)
        self.assertIn("OLD_REF", sql)          # renamed source column
        self.assertIn("REFERENCE_ID", sql)     # target column
        self.assertIn("FROM PS_JRNL_LN@SOURCE91", sql)
        self.assertIn("WHERE BUSINESS_UNIT = 'US001'", sql)
        # Every risk is restated in the file, tagged with its code and
        # whether pre-flight can settle it against real data.
        self.assertIn("[blocker:key_set_change (measurable)]", sql)
        self.assertIn("[warning:truncation (measurable)]", sql)
        self.assertIn("[warning:unsourced_column]", sql)
        self.assertEqual(result["convert_records"], 1)
        blocked = result["blocked_mappings"][0]
        self.assertEqual(blocked["recname"], "JRNL_LN")
        self.assertTrue(blocked["blockers"][0]["measurable_by_preflight"])
        resolved = json.loads(
            (Path(result["out_dir"]) / "resolved_mappings.json").read_text())
        self.assertIn("JRNL_LN", resolved["mappings"])

    def test_staging_mode_emits_landing_ddl(self) -> None:
        self.cfg.migrate.convert_via = "staging"
        self._plan()
        result = self.pipe.emit()
        out = Path(result["out_dir"])
        sql = (out / "convert" / "JRNL_LN.sql").read_text()
        self.assertIn("FROM STG_PS_JRNL_LN", sql)
        ddl = (out / "05_staging_ddl.sql").read_text()
        self.assertIn("CREATE TABLE STG_PS_JRNL_LN", ddl)
        self.assertIn("OLD_REF", ddl)          # staging carries the 9.1 shape
        self.assertNotIn("PROCESS_INSTANCE", ddl)

    def test_mapping_template_seeds_the_overrides_file(self) -> None:
        self._plan()
        res = self.pipe.mapping_template()
        written = json.loads(Path(res["written"]).read_text())
        self.assertIn("JRNL_LN", written)
        self.assertIn("REFERENCE_ID", written["JRNL_LN"]["columns"])
        self.assertIn("OLD_REF",
                      written["JRNL_LN"]["columns"]["REFERENCE_ID"]["_comment"])
        # A second run must not silently discard operator edits.
        with self.assertRaises(MigrateError):
            self.pipe.mapping_template()

    # ---- reconcile -------------------------------------------------------
    def test_reconcile_follows_renames_and_the_row_filter(self) -> None:
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {"where": "BUSINESS_UNIT = 'US002'",
                        "columns": {"REFERENCE_ID": {"from": "OLD_REF"}}}}))
        # Load exactly the filtered rows, reshaped, into 9.2.
        c = sqlite3.connect(self.tgt_path)
        c.executemany("INSERT INTO PS_JRNL_LN VALUES (?,?,?,?,?,?,?,?)", [
            ("US002", "J2", 1, "DEPT0", 50.0, "R3", "line three", 0),
            ("US002", "J3", 1, "OPS", 25.25, "R4", "line four", 0),
        ])
        c.commit()
        c.close()
        res = self.pipe.reconcile()["results"][0]
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["source_rows"], 2)
        self.assertEqual(res["source_filter"], "BUSINESS_UNIT = 'US002'")
        # PROCESS_INSTANCE has no 9.1 source, so it is reported as
        # unverifiable rather than silently counted as matching.
        self.assertIn("PROCESS_INSTANCE", res.get("unverifiable_columns", []))

    def test_reconcile_reports_a_renamed_column_mismatch(self) -> None:
        self._plan()
        Path(self.cfg.migrate.mapping_overrides).write_text(json.dumps({
            "JRNL_LN": {"where": "BUSINESS_UNIT = 'US002'",
                        "columns": {"REFERENCE_ID": {"from": "OLD_REF"}}}}))
        c = sqlite3.connect(self.tgt_path)
        c.executemany("INSERT INTO PS_JRNL_LN VALUES (?,?,?,?,?,?,?,?)", [
            ("US002", "J2", 1, "DEPT0", 50.0, "R3", "line three", 0),
            ("US002", "J3", 1, "OPS", 999.0, "R4", "line four", 0),
        ])
        c.commit()
        c.close()
        res = self.pipe.reconcile()["results"][0]
        self.assertFalse(res["ok"])
        self.assertTrue(any("MONETARY_AMOUNT" in m for m in res["mismatches"]))

    # ---- discovery -------------------------------------------------------
    def test_delivered_discovery_finds_tables_by_pattern(self) -> None:
        out = self.pipe.discover(delivered_like="JRNL%")
        self.assertEqual([r["recname"] for r in out["records"]], ["JRNL_LN"])
        self.assertEqual(out["mode"], "delivered")


class InsertSelectShapeTests(unittest.TestCase):
    """The generated SQL must list target columns and source expressions in
    the same order — a misaligned INSERT-SELECT loads silently wrong data."""

    def test_column_and_value_lists_align(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s, t = Path(tmp) / "s.db", Path(tmp) / "t.db"
            seed_src(s)
            seed_tgt(t)
            cfg = Config(root=Path(tmp))
            cfg.db = DbCfg(backend="sqlite", sqlite_path=str(t))
            cfg.sources = {"s91": DbCfg(backend="sqlite", sqlite_path=str(s))}
            cfg.migrate.source = "s91"
            cfg.migrate.delivered_data = "convert"
            cfg.migrate.state_path = str(Path(tmp) / "st.db")
            db = Database(cfg)
            try:
                pipe = MigratePipeline(cfg, SourceRegistry(cfg, db))
                m = build_mapping("JRNL_LN", pipe.source.record("JRNL_LN"),
                                  pipe.target.record("JRNL_LN"), {})
                sql = insert_select(m)
                body = sql.split("INSERT INTO", 1)[1]
                cols = body.split("(", 1)[1].split(")", 1)[0]
                vals = body.split(") SELECT", 1)[1].split("\nFROM", 1)[0]
                self.assertEqual(len([c for c in cols.split(",") if c.strip()]),
                                 len([v for v in vals.split(",") if v.strip()]))
                self.assertEqual(len(m.columns),
                                 len([c for c in cols.split(",") if c.strip()]))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
