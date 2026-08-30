"""Six ways the governance surface misdescribed its own rows.

All found by the phase-7 design review and verified against the running
code before being fixed. The common shape: information that existed --
an origin, a decider, a record's human label, a join count -- and a
projection that silently dropped it, or wording whose EFFECT diverged
from the operator's stated choice.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from pstb.source_knowledge import (SourceKnowledge, SourceKnowledgeError,
                                   selection_effect)

FINGERPRINT = "sha256:" + "0" * 64


def _store(root):
    return SourceKnowledge(Path(root) / "sk.db", source="default",
                           source_fingerprint=FINGERPRINT)


def _propose(sk, meaning, *, selection="prefer", origin="gui",
             name="TU_X7"):
    return sk.propose(object_id=f"tbl:MAIN.{name}", schema="MAIN",
                      object_name=name, object_kind="table",
                      meaning=meaning, selection=selection, origin=origin)


class VetoFlipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-veto-")
        self.sk = _store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_prefer_wording_that_reads_as_a_veto_is_refused(self):
        """Readers derive the effect from WORDING on every read. A prefer
        meaning saying 'Do not use for reporting totals...' would become
        a durable veto, enforced pre-database at every query site, that
        the operator never chose. Write time is the only place the two
        can be forced to agree."""
        for wording in (
            "Do not use for reporting totals; the header is authoritative",
            "Vendor staging rows; do not use this table for balances",
            "Obsolete record kept for the auditors",
            "Deprecated table retained for the conversion team",
        ):
            with self.subTest(wording=wording), \
                    self.assertRaises(SourceKnowledgeError) as caught:
                _propose(self.sk, wording, selection="prefer")
            self.assertIn("reads as a veto", str(caught.exception))
            self.assertIn("selection=exclude", str(caught.exception))

    def test_an_omitted_selection_lets_the_wording_decide(self):
        """The third state, and the one the first fix broke: a
        conversation-taught lesson names no selection at all, and its
        'do not use X' wording IS the choice -- five existing tests
        document that contract. Only the explicit contradiction is
        refused; silence defers to the words."""
        out = self.sk.propose(
            object_id="tbl:MAIN.TU_OLD2", schema="MAIN",
            object_name="TU_OLD2", object_kind="table",
            meaning="Do not use this table; obsolete staging copy")
        self.assertEqual(out["selection_effect"], "exclude")

    def test_a_deliberate_exclusion_still_works(self):
        out = _propose(self.sk, "Backup copy from the ledger conversion",
                       selection="exclude", name="TU_OLD")
        self.assertEqual(out["selection_effect"], "exclude")
        self.assertEqual(selection_effect(out["meaning"]), "exclude")

    def test_ordinary_prefer_wording_is_untouched(self):
        """The refusal must not fire on correct answers -- a guard that
        blocks reasonable meanings gets worked around, not respected."""
        for wording in (
            "Vendor invoice staging rows for the nightly load",
            "Customer master used by billing and collections",
            "Live journal detail; one row per journal line",
        ):
            with self.subTest(wording=wording):
                out = _propose(self.sk, wording, name=f"T{hash(wording) % 97}")
                self.assertEqual(out["selection_effect"], "prefer")


class ProvenanceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-prov-")
        self.sk = _store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_origin_and_decider_reach_the_public_row(self):
        """Both were captured at write time and selected by nothing, so
        the queue labeled every proposal 'proposed in conversation' and
        every decision anonymous -- including the GUI form's own."""
        out = _propose(self.sk, "Vendor invoice staging rows", origin="gui")
        self.assertEqual(out.get("origin"), "gui")
        decided = self.sk.decide(
            out["id"], approve=True, decided_by="njm",
            current_object={
                "source_database": out["source_database"],
                "source_fingerprint": FINGERPRINT,
                "object_id": out["object_id"],
                "schema": out["schema"], "object": out["object"],
                "aliases_safe": True,
            })
        self.assertEqual(decided.get("decided_by"), "njm")
        self.assertEqual(decided.get("origin"), "gui")

    def test_a_mangled_cosmetic_label_degrades_and_never_raises(self):
        """_public runs on EVERY row at connection open; one raise
        disables the whole overlay, after which the record-veto resolver
        fails closed and refuses every query on the source. A display
        label must never have that power."""
        out = _propose(self.sk, "Vendor invoice staging rows")
        con = sqlite3.connect(self.sk.path)
        con.execute("UPDATE proposals SET origin=? WHERE id=?",
                    ("bad\x00label\x07", out["id"]))
        con.commit()
        con.close()
        rows = self.sk.list_proposals("")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("\x00", rows[0].get("origin", ""))


class ContextLabelTests(unittest.TestCase):
    def test_context_shows_the_records_label_with_its_provenance(self):
        """21 of 63 sample objects have a human-written description that
        only a lucky SEARCH phrase could surface; context() -- the tool a
        person uses to study one object -- always said label: null."""
        from pstb.metadata import MetadataCatalog
        catalog = MetadataCatalog(
            Path(__file__).resolve().parent.parent / "metadata_catalog.db")
        subject = catalog.context("PS_JRNL_LN", source="default")["subject"]
        self.assertEqual(subject["label"], "Journal Line")
        provenance = subject["label_source"]
        self.assertEqual(provenance["logical_record"], "JRNL_LN")
        self.assertIn(provenance["confidence"],
                      ("confirmed", "corroborated", "candidate",
                       "inconclusive"))
        self.assertTrue(provenance["basis"])

    def test_the_label_does_not_upgrade_match_confidence(self):
        """Labeling the object must not strengthen the evidence for
        having CHOSEN it -- the old comment's warning stands."""
        from pstb.metadata import MetadataCatalog
        catalog = MetadataCatalog(
            Path(__file__).resolve().parent.parent / "metadata_catalog.db")
        subject = catalog.context("PS_JRNL_LN", source="default")["subject"]
        self.assertNotEqual(subject["confidence"]["basis"],
                            subject["label_source"]["basis"])


class ViewNoteTests(unittest.TestCase):
    def test_the_note_counts_the_joins_that_were_actually_written(self):
        """#179 deferred edge emission but left the note above it, so
        `joins` was always 0 at the note -- every build reported
        '0 join(s)' and a schema whose views declare joins but teach no
        vocabulary got no note at all. The census reads this line."""
        import pstb.metadata as metadata
        from pstb.config import Config
        from pstb.db import Database
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "p.db"
            con = sqlite3.connect(db_path)
            con.executescript("""
                CREATE TABLE TU_A (C1 TEXT, C2 TEXT);
                CREATE TABLE TU_B (C1 TEXT, C7 TEXT);
                CREATE VIEW JOIN_ONLY AS
                  SELECT A.C2 AS X FROM TU_A A JOIN TU_B B ON A.C1 = B.C1;
            """)
            for i in range(30):
                con.execute("INSERT INTO TU_A VALUES (?,?)",
                            (f"K{i}", f"v{i}"))
                con.execute("INSERT INTO TU_B VALUES (?,?)",
                            (f"K{i}", f"n{i}"))
            con.commit()
            con.close()
            cfg = Config.sample(root)
            cfg.db.sqlite_path = str(db_path)
            cfg.sources = {}
            db = Database(cfg)
            try:
                metadata.build_catalog(root / "c.db", [("default", db)],
                                       peopletools_source="default")
            finally:
                db.close()
            catalog = sqlite3.connect(root / "c.db")
            notes = [row[0] for row in catalog.execute(
                "SELECT note FROM notes WHERE layer='view_vocabulary'")]
            catalog.close()
        joined = " ".join(notes)
        self.assertIn("1 join(s)", joined)
        self.assertNotIn("0 join(s)", joined)


class SourceFilterTests(unittest.TestCase):
    def setUp(self):
        import pstb.gui.app as gui
        self.gui = gui
        self.client = TestClient(gui.app, client=("127.0.0.1", 5555),
                                 base_url="http://localhost")

    def test_the_operator_path_honors_the_source_parameter(self):
        """A drawer titled 'Metadata meanings - P2Go' listed Finance
        proposals, site-memory facts and every source's decision history,
        because ?source= was read only on the unauthenticated-remote
        branch."""
        registry = SimpleNamespace(
            names=lambda: ["default", "p2go"],
            resolve_name=lambda s="": (s or "default"))
        stores = {
            "default": [{"id": "f1", "meaning": "finance meaning",
                         "schema": "SYSADM", "object": "PS_X",
                         "kind": "table", "status": "pending",
                         "proposed_at": "t", "aliases": []}],
            "p2go": [{"id": "p1", "meaning": "p2go meaning",
                      "schema": "P2GO", "object": "ORDERS",
                      "kind": "table", "status": "pending",
                      "proposed_at": "t", "aliases": []}],
        }
        fake = lambda name: SimpleNamespace(
            list_proposals=lambda status="": stores[name])
        with patch.object(self.gui.engine, "registry", registry), \
                patch.object(self.gui, "_approval_source_names",
                             lambda: ["default", "p2go"]), \
                patch.object(self.gui, "_source_knowledge_store", fake):
            everything = self.client.get("/api/approvals").json()
            only_p2go = self.client.get(
                "/api/approvals?source=p2go").json()
        all_ids = {i["id"] for i in everything["items"]}
        self.assertIn("f1", all_ids)
        self.assertIn("p1", all_ids)
        p2go_rows = [i for i in only_p2go["items"]
                     if i["queue"] == "source_knowledge"]
        self.assertEqual({i["id"] for i in p2go_rows}, {"p1"})
        self.assertEqual([i for i in only_p2go["items"]
                          if i["queue"] == "memory"], [])

    def test_an_unknown_source_is_refused_by_name(self):
        registry = SimpleNamespace(
            names=lambda: ["default", "p2go"],
            resolve_name=lambda s="": (s or "default"))
        with patch.object(self.gui.engine, "registry", registry):
            r = self.client.get("/api/approvals?source=nosuch")
        self.assertEqual(r.status_code, 404)
        self.assertIn("p2go", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
