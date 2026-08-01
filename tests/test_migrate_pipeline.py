"""End-to-end regression for the 9.1 -> 9.2 record-porting pipeline.

Two SQLite databases stand in for the instances: a "9.1" with a small custom
subsystem (header table with a subrecord, an audit record, a custom prompt
table, a view, and one record that drifted) and a "9.2" that already has some
of it. The pipeline must discover, close over dependencies, classify, emit
apply artifacts, and verify builds/loads — all through the same guarded
Database layer the rest of the product uses.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config, DbCfg  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.migrate.pipeline import MigratePipeline  # noqa: E402
from pstb.migrate.spec import MigrateError  # noqa: E402
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

# FIELDNAME -> (FIELDTYPE, LENGTH, DECIMALPOS); shared by both instances.
_DBFIELDS = {
    "BUSINESS_UNIT": (0, 5, 0),
    "INVOICE_ID": (0, 22, 0),
    "ADDRESS1": (0, 55, 0),
    "CITY": (0, 30, 0),
    "Z_TYPE": (0, 4, 0),
    "STATUS_CD": (0, 4, 0),
    "AMOUNT": (2, 15, 3),
    "DESCR": (0, 30, 0),
    "AUDIT_OPRID": (0, 30, 0),
    "AUDIT_STAMP": (6, 26, 0),
    "Z_ID": (0, 10, 0),
    "OLD_FLAG": (0, 1, 0),
    "NEW_ATTR": (0, 10, 0),
}


def _seed_common(c: sqlite3.Connection) -> None:
    for ddl in _TOOLS_DDL:
        c.execute(ddl)
    c.executemany("INSERT INTO PSDBFIELD VALUES (?,?,?,?)",
                  [(n, t, l, d) for n, (t, l, d) in _DBFIELDS.items()])


def _rec(c, recname, rectype, oprid, audit="", fields=()):
    c.execute("INSERT INTO PSRECDEFN VALUES (?,?,?,?,?,?,?)",
              (recname, rectype, "", "", audit, oprid, f"{recname} descr"))
    c.executemany("INSERT INTO PSRECFIELD VALUES (?,?,?,?,?)",
                  [(recname, f, i + 1, use, edit)
                   for i, (f, use, edit) in enumerate(fields)])


def seed_source(path: Path) -> None:
    """The 9.1 instance: the custom subsystem plus the delivered records it
    leans on (one of which no longer exists in 9.2)."""
    c = sqlite3.connect(path)
    _seed_common(c)
    _rec(c, "Z_INVOICE_HDR", 0, "NJM", audit="Z_INVOICE_AUD", fields=(
        ("BUSINESS_UNIT", 1, "BUS_UNIT_TBL_GL"),
        ("INVOICE_ID", 1, ""),
        ("Z_ADDR_SBR", 0, ""),          # subrecord reference
        ("Z_TYPE", 0, "Z_TYPE_TBL"),    # custom prompt
        ("STATUS_CD", 0, "OLD_DLV_VW"),  # delivered prompt, dropped in 9.2
        ("AMOUNT", 0, ""),
    ))
    _rec(c, "Z_ADDR_SBR", 3, "NJM", fields=(
        ("ADDRESS1", 0, ""), ("CITY", 0, "")))
    _rec(c, "Z_INVOICE_AUD", 0, "NJM", fields=(
        ("AUDIT_OPRID", 0, ""), ("AUDIT_STAMP", 0, ""), ("INVOICE_ID", 0, "")))
    _rec(c, "Z_TYPE_TBL", 0, "NJM", fields=(
        ("Z_TYPE", 1, ""), ("DESCR", 0, "")))
    _rec(c, "Z_INV_VW", 1, "NJM", fields=(
        ("INVOICE_ID", 1, ""), ("AMOUNT", 0, "")))
    c.execute("INSERT INTO PSSQLTEXTDEFN VALUES (?,?,?)",
              ("Z_INV_VW", 0,
               "SELECT H.INVOICE_ID, H.AMOUNT FROM PS_Z_INVOICE_HDR H "
               "JOIN PS_BUS_UNIT_TBL_GL B ON B.BUSINESS_UNIT = H.BUSINESS_UNIT"))
    _rec(c, "Z_DRIFT_TBL", 0, "NJM", fields=(
        ("Z_ID", 1, ""), ("AMOUNT", 0, ""), ("OLD_FLAG", 0, "")))
    # Delivered records: one healthy, one an admin re-saved (oprid noise),
    # one that 9.2 no longer ships.
    _rec(c, "BUS_UNIT_TBL_GL", 0, "ADMIN", fields=(
        ("BUSINESS_UNIT", 1, ""), ("DESCR", 0, "")))
    _rec(c, "OLD_DLV_VW", 1, "PPLSOFT", fields=(("STATUS_CD", 1, ""),))

    c.execute("CREATE TABLE PS_Z_INVOICE_HDR (BUSINESS_UNIT TEXT, INVOICE_ID"
              " TEXT, ADDRESS1 TEXT, CITY TEXT, Z_TYPE TEXT, STATUS_CD TEXT,"
              " AMOUNT REAL)")
    c.executemany("INSERT INTO PS_Z_INVOICE_HDR VALUES (?,?,?,?,?,?,?)", [
        ("US001", "INV-1", "1 Main St", "Reno", "STD", "A", 100.5),
        ("US001", "INV-2", "2 Main St", "Reno", "STD", "A", 200.25),
        ("US002", "INV-3", "3 Main St", "Elko", "EXP", "C", 50.0),
    ])
    c.execute("CREATE TABLE PS_Z_INVOICE_AUD (AUDIT_OPRID TEXT, AUDIT_STAMP"
              " TEXT, INVOICE_ID TEXT)")
    c.execute("CREATE TABLE PS_Z_TYPE_TBL (Z_TYPE TEXT, DESCR TEXT)")
    c.executemany("INSERT INTO PS_Z_TYPE_TBL VALUES (?,?)",
                  [("STD", "Standard"), ("EXP", "Expedited")])
    c.execute("CREATE TABLE PS_Z_DRIFT_TBL (Z_ID TEXT, AMOUNT REAL,"
              " OLD_FLAG TEXT)")
    c.executemany("INSERT INTO PS_Z_DRIFT_TBL VALUES (?,?,?)",
                  [("A", 10.0, "Y"), ("B", 5.5, "N")])
    c.commit()
    c.close()


def seed_target(path: Path) -> None:
    """The 9.2 instance: Z_TYPE_TBL already ported identically, Z_DRIFT_TBL
    ported but reshaped, delivered BUS_UNIT_TBL_GL present, OLD_DLV_VW gone."""
    c = sqlite3.connect(path)
    _seed_common(c)
    _rec(c, "Z_TYPE_TBL", 0, "NJM", fields=(
        ("Z_TYPE", 1, ""), ("DESCR", 0, "")))
    _rec(c, "Z_DRIFT_TBL", 0, "NJM", fields=(
        ("Z_ID", 1, ""), ("AMOUNT", 0, ""), ("NEW_ATTR", 0, "")))
    _rec(c, "BUS_UNIT_TBL_GL", 0, "PPLSOFT", fields=(
        ("BUSINESS_UNIT", 1, ""), ("DESCR", 0, "")))
    c.execute("CREATE TABLE PS_Z_TYPE_TBL (Z_TYPE TEXT, DESCR TEXT)")
    c.executemany("INSERT INTO PS_Z_TYPE_TBL VALUES (?,?)",
                  [("STD", "Standard"), ("EXP", "Expedited")])
    c.execute("CREATE TABLE PS_Z_DRIFT_TBL (Z_ID TEXT, AMOUNT REAL,"
              " NEW_ATTR TEXT)")
    c.execute("INSERT INTO PS_Z_DRIFT_TBL VALUES ('A', 10.0, ' ')")
    c.commit()
    c.close()


class MigratePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.src_path = tmp / "fscm91.db"
        self.tgt_path = tmp / "fscm92.db"
        seed_source(self.src_path)
        seed_target(self.tgt_path)
        cfg = Config(root=tmp)
        cfg.db = DbCfg(backend="sqlite", sqlite_path=str(self.tgt_path))
        cfg.sources = {"src91": DbCfg(backend="sqlite",
                                      sqlite_path=str(self.src_path))}
        cfg.migrate.source = "src91"
        cfg.migrate.target = ""          # primary db IS the 9.2 instance
        cfg.migrate.custom_prefixes = ["Z_"]
        cfg.migrate.state_path = str(tmp / "state.db")
        cfg.migrate.out_dir = str(tmp / "out")
        self.cfg = cfg
        self.primary = Database(cfg)
        self.pipe = MigratePipeline(cfg, SourceRegistry(cfg, self.primary))

    def tearDown(self) -> None:
        self.primary.close()
        self._tmp.cleanup()

    def _classifications(self) -> dict:
        plan = self.pipe.plan()
        return {i["recname"]: i["classification"] for i in plan["items"]}, plan

    # ---- discovery -------------------------------------------------------
    def test_discovery_signals(self) -> None:
        by_prefix = self.pipe.discover(mode="prefix")
        names = {r["recname"] for r in by_prefix["records"]}
        self.assertEqual(names, {"Z_INVOICE_HDR", "Z_ADDR_SBR",
                                 "Z_INVOICE_AUD", "Z_TYPE_TBL", "Z_INV_VW",
                                 "Z_DRIFT_TBL"})
        # oprid mode also surfaces the delivered record an admin re-saved —
        # the documented noise that makes discovery a review list.
        by_oprid = self.pipe.discover(mode="oprid")
        oprid_names = {r["recname"] for r in by_oprid["records"]}
        self.assertIn("BUS_UNIT_TBL_GL", oprid_names)
        flagged = next(r for r in by_oprid["records"]
                       if r["recname"] == "BUS_UNIT_TBL_GL")
        self.assertFalse(flagged["matched_prefix"])

    # ---- plan ------------------------------------------------------------
    def test_plan_classifies_every_record(self) -> None:
        got, plan = self._classifications()
        self.assertEqual(got, {
            "Z_INVOICE_HDR": "build_and_load",
            "Z_ADDR_SBR": "build_definition",
            "Z_INVOICE_AUD": "build_and_load",
            "Z_TYPE_TBL": "load_only",
            "Z_INV_VW": "build_definition",
            "Z_DRIFT_TBL": "drift_review",
            "BUS_UNIT_TBL_GL": "delivered_ok",
            "OLD_DLV_VW": "delivered_missing",
        })
        items = {i["recname"]: i for i in plan["items"]}
        self.assertEqual(items["Z_INVOICE_HDR"]["row_count"], 3)
        diff = items["Z_DRIFT_TBL"]["shape_diff"]
        self.assertEqual(diff["source_only"], ["OLD_FLAG"])
        self.assertEqual(diff["target_only"], ["NEW_ATTR"])
        # A record seeded by the oprid signal but not matching the prefixes
        # classifies as delivered — with a note saying how to correct that
        # when it is really a badly named custom record.
        self.assertTrue(any("custom_prefixes" in n
                            for n in items["BUS_UNIT_TBL_GL"]["notes"]))

    def test_plan_provenance_explains_membership(self) -> None:
        _, plan = self._classifications()
        via = {i["recname"]: set(i["via"]) for i in plan["items"]}
        self.assertIn("subrec:Z_INVOICE_HDR", via["Z_ADDR_SBR"])
        self.assertIn("audit:Z_INVOICE_HDR", via["Z_INVOICE_AUD"])
        self.assertIn("prompt:Z_INVOICE_HDR.Z_TYPE", via["Z_TYPE_TBL"])
        self.assertIn("prompt:Z_INVOICE_HDR.STATUS_CD", via["OLD_DLV_VW"])
        self.assertIn("view:Z_INV_VW", via["BUS_UNIT_TBL_GL"])

    def test_subrecord_fields_expand_into_the_shape(self) -> None:
        shown = self.pipe.show_record("Z_INVOICE_HDR")
        by_name = {f["name"]: f for f in shown["source_91"]["fields"]}
        self.assertIn("ADDRESS1", by_name)
        self.assertEqual(by_name["ADDRESS1"]["from_subrecord"], "Z_ADDR_SBR")
        self.assertIsNone(shown["target_92"])
        self.assertIn("Z_ADDR_SBR", shown["source_91"]["subrecords"])

    def test_explicit_seeds_stay_scoped(self) -> None:
        plan = self.pipe.plan(seed_records=["Z_TYPE_TBL"])
        names = {i["recname"] for i in plan["items"]}
        self.assertEqual(names, {"Z_TYPE_TBL"})

    # ---- emit ------------------------------------------------------------
    def test_emit_writes_the_apply_artifacts(self) -> None:
        self.pipe.plan()
        result = self.pipe.emit()
        out = Path(result["out_dir"])
        project = (out / "01_project_records.txt").read_text()
        for rec in ("Z_INVOICE_HDR", "Z_ADDR_SBR", "Z_INVOICE_AUD",
                    "Z_INV_VW", "Z_DRIFT_TBL"):
            self.assertIn(rec, project)
        for rec in ("Z_TYPE_TBL", "BUS_UNIT_TBL_GL", "OLD_DLV_VW"):
            self.assertNotIn(rec, project,
                             f"{rec} must not be in the App Designer project")
        exp = (out / "02_export_records.dms").read_text()
        imp = (out / "03_import_records.dms").read_text()
        for rec in ("Z_INVOICE_HDR", "Z_INVOICE_AUD", "Z_TYPE_TBL"):
            self.assertIn(f"EXPORT {rec};", exp)
            self.assertIn(f"REPLACE_DATA {rec};", imp)
        # Drifted and delivered records never ride the straight copy.
        self.assertNotIn("Z_DRIFT_TBL", exp)
        self.assertNotIn("BUS_UNIT_TBL_GL", exp)
        drift = (out / "drift" / "Z_DRIFT_TBL.sql").read_text()
        self.assertIn("NEW_ATTR", drift)
        self.assertIn("OLD_FLAG", drift)
        self.assertEqual([b["recname"] for b in result["blockers"]],
                         ["OLD_DLV_VW"])
        self.assertTrue((out / "plan.json").exists())
        self.assertTrue((out / "README.txt").exists())

    # ---- verify + reconcile ---------------------------------------------
    def test_build_verification_and_reconcile_cycle(self) -> None:
        self.pipe.plan()
        before = self.pipe.verify_build()
        by_rec = {r["recname"]: r for r in before["results"]}
        self.assertFalse(by_rec["Z_INVOICE_HDR"]["ok"])
        self.assertTrue(by_rec["Z_ADDR_SBR"]["ok"])  # definition-only

        recon = self.pipe.reconcile()
        r = {x["recname"]: x for x in recon["results"]}
        self.assertTrue(r["Z_TYPE_TBL"]["ok"])          # already loaded
        self.assertFalse(r["Z_INVOICE_HDR"]["ok"])      # not built yet
        self.assertFalse(r["Z_DRIFT_TBL"]["ok"])        # 2 rows vs 1
        self.assertTrue(any("row count" in m
                            for m in r["Z_DRIFT_TBL"]["mismatches"]))

        # Operator "runs App Designer Build + Data Mover import" on 9.2.
        c = sqlite3.connect(self.tgt_path)
        c.execute("CREATE TABLE PS_Z_INVOICE_HDR (BUSINESS_UNIT TEXT,"
                  " INVOICE_ID TEXT, ADDRESS1 TEXT, CITY TEXT, Z_TYPE TEXT,"
                  " STATUS_CD TEXT, AMOUNT REAL)")
        c.executemany("INSERT INTO PS_Z_INVOICE_HDR VALUES (?,?,?,?,?,?,?)", [
            ("US001", "INV-1", "1 Main St", "Reno", "STD", "A", 100.5),
            ("US001", "INV-2", "2 Main St", "Reno", "STD", "A", 200.25),
            ("US002", "INV-3", "3 Main St", "Elko", "EXP", "C", 50.0),
        ])
        c.execute("CREATE TABLE PS_Z_INVOICE_AUD (AUDIT_OPRID TEXT,"
                  " AUDIT_STAMP TEXT, INVOICE_ID TEXT)")
        c.commit()
        c.close()

        after = self.pipe.verify_build()
        by_rec = {r_["recname"]: r_ for r_ in after["results"]}
        self.assertTrue(by_rec["Z_INVOICE_HDR"]["ok"],
                        by_rec["Z_INVOICE_HDR"])
        recon2 = self.pipe.reconcile()
        r2 = {x["recname"]: x for x in recon2["results"]}
        self.assertTrue(r2["Z_INVOICE_HDR"]["ok"], r2["Z_INVOICE_HDR"])
        self.assertTrue(r2["Z_INVOICE_AUD"]["ok"])
        # Clean records advance in the state db; the drifted one stays put.
        self.assertEqual(self.pipe.state.get("Z_INVOICE_HDR")["status"],
                         "reconciled")
        self.assertNotEqual(self.pipe.state.get("Z_DRIFT_TBL")["status"],
                            "reconciled")

    def test_sum_mismatch_is_caught_even_when_counts_match(self) -> None:
        self.pipe.plan(seed_records=["Z_DRIFT_TBL"])
        c = sqlite3.connect(self.tgt_path)
        c.execute("DELETE FROM PS_Z_DRIFT_TBL")
        c.executemany("INSERT INTO PS_Z_DRIFT_TBL VALUES (?,?,?)",
                      [("A", 10.0, " "), ("B", 999.0, " ")])  # wrong amount
        c.commit()
        c.close()
        recon = self.pipe.reconcile(recnames=["Z_DRIFT_TBL"])
        r = recon["results"][0]
        self.assertFalse(r["ok"])
        self.assertTrue(any("AMOUNT" in m for m in r["mismatches"]))

    # ---- state -----------------------------------------------------------
    def test_state_tracks_manual_steps_and_survives_replan(self) -> None:
        self.pipe.plan()
        self.pipe.mark("Z_INVOICE_HDR", "definitions_exported",
                       "project MIG91TO92 copied")
        self.pipe.plan()  # replan must not lose recorded progress
        self.assertEqual(self.pipe.state.get("Z_INVOICE_HDR")["status"],
                         "definitions_exported")
        with self.assertRaises(MigrateError):
            self.pipe.mark("Z_INVOICE_HDR", "not_a_status")
        summary = self.pipe.status()
        self.assertEqual(summary["records"], 8)
        self.assertIn("build_and_load", summary["by_classification"])

    def test_unconfigured_source_fails_with_guidance(self) -> None:
        cfg = Config(root=Path(self._tmp.name))
        cfg.db = DbCfg(backend="sqlite", sqlite_path=str(self.tgt_path))
        db = Database(cfg)
        try:
            with self.assertRaises(MigrateError) as ctx:
                MigratePipeline(cfg, SourceRegistry(cfg, db))
            self.assertIn("migrate.source", str(ctx.exception))
        finally:
            db.close()


class MigrateConfigTests(unittest.TestCase):
    def test_yaml_block_round_trips(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        from pstb.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(
                "migrate:\n"
                "  source: fscm91\n"
                "  custom_prefixes: [Z_, ZZ_, W3_]\n"
                "  discovery: prefix\n"
                "  max_records: 500\n"
            )
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.migrate.source, "fscm91")
            self.assertEqual(cfg.migrate.custom_prefixes, ["Z_", "ZZ_", "W3_"])
            self.assertEqual(cfg.migrate.discovery, "prefix")
            self.assertEqual(cfg.migrate.max_records, 500)
            self.assertEqual(cfg.migrate.target, "")


if __name__ == "__main__":
    unittest.main()
