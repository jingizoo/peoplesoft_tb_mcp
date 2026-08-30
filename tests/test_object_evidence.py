"""The evidence surface: what a person needs to write a meaning, and
nothing a schema or a silo forbids them from seeing.

Phase 7's design review measured, rather than assumed, that the
vocabulary harvest yields nothing on most objects -- so the deliverable
here is not a drafter. It is a reader (object_evidence) and a sorter
(meaning_worklist) that never write anything, gated the same way the
failed-question worklist already is. Every test holds one promise: no
volumetric leaves the catalog, no rendered sentence carries a digit, no
packet crosses a silo, and a refusal bucket is exactly as informative as
the evidence that produced it.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from pstb import meaning_worklist, viewharvest
from pstb.config import Config
from pstb.db import Database
from pstb.metadata import MetadataCatalog, build_catalog
from pstb.meaning_worklist import (build_worklist, classify_object,
                                   founding_signals)
from pstb.source_knowledge import SourceKnowledge

FINGERPRINT = "sha256:" + "0" * 64
ROW_SENTINEL = "SENTINEL JANE DOE"
AMOUNT_SENTINEL = "9876543.21"


def _catalog(root, script, *, inserts=()):
    dbp = Path(root) / "p.db"
    con = sqlite3.connect(dbp)
    con.executescript(script)
    for statement, params in inserts:
        con.execute(statement, params)
    con.commit()
    con.close()
    cfg = Config.sample(root)
    cfg.db.sqlite_path = str(dbp)
    cfg.sources = {}
    db = Database(cfg)
    try:
        build_catalog(Path(root) / "c.db", [("default", db)],
                      peopletools_source="default")
    finally:
        db.close()
    return MetadataCatalog(Path(root) / "c.db")


def _store(root):
    return SourceKnowledge(Path(root) / "sk.db", source="default",
                           source_fingerprint=FINGERPRINT)


class EvidencePacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-evidence-")
        self.root = Path(self.temp.name)
        self.catalog = _catalog(self.root, """
            CREATE TABLE TU_X7 (
                C1 TEXT, C2 TEXT, C4 NUMERIC, C9 TEXT, VENDOR_CD TEXT);
            CREATE TABLE TU_Q2 (C1 TEXT, C7 TEXT, VENDOR_CD TEXT);
            CREATE VIEW OPEN_INVOICES AS
              SELECT A.C1 AS INVOICE_NUMBER,
                     A.C1 AS "Acme Manufacturing rebate"
                FROM TU_X7 A JOIN TU_Q2 B ON A.C2 = B.C1
               WHERE A.C9 = 'OPEN';
        """)
        con = sqlite3.connect(Path(self.root) / "p.db")
        for i in range(60):
            # VENDOR_CD is a SEPARATE, undeclared key-shaped pair: the
            # view above declares C2=C1, and a declared join silences
            # the miner for that exact pair -- so a mined-join fixture
            # needs values the miner can measure on its own.
            con.execute("INSERT INTO TU_X7 VALUES (?,?,?,?,?)",
                        (f"I{i}", f"V{i % 9}", i, "OPEN", f"VEN{i:03d}"))
            con.execute("INSERT INTO TU_Q2 VALUES (?,?,?)",
                        (f"V{i % 9}", f"Vendor {i % 9}", f"VEN{i:03d}"))
        con.commit()
        con.close()
        self.catalog = _catalog(self.root, "")  # rebuild after data load

    def tearDown(self):
        self.temp.cleanup()

    def test_source_is_required_and_never_derived(self):
        """object_evidence has no ambiguous cross-source path -- unlike
        context(), which will happily resolve across every configured
        source when none is named."""
        with self.assertRaises(Exception) as caught:
            self.catalog.object_evidence("TU_X7", source="")
        self.assertIn("cross-silo", str(caught.exception))

    def test_a_foreign_source_is_refused_not_answered(self):
        result = self.catalog.object_evidence("TU_X7", source="p2go")
        self.assertFalse(result["found"])

    def test_the_source_mismatch_defence_fires_even_if_context_agrees(self):
        """context() already scopes by source and this codepath cannot
        be reached through it today -- confirmed by the sabotage run:
        deleting the check alone left every other test passing. It is
        defence in depth for a context() bug that does not exist YET, so
        it has to be exercised directly rather than through context()'s
        own (correct) behaviour."""
        forged = {
            "available": True, "found": True, "source_database": "default",
            "subject": {"source": "p2go", "object_id": "x",
                       "kind": "table", "schema": "MAIN",
                       "physical_object": "TU_X7"},
            "usefulness": {},
        }
        with patch.object(self.catalog, "context", return_value=forged):
            result = self.catalog.object_evidence(
                "TU_X7", source="default")
        self.assertFalse(result["found"])
        self.assertEqual(result["bucket"], "wrong_source")

    def test_the_party_name_never_reaches_the_vocabulary(self):
        """Screened at extraction (viewharvest), verified again here at
        the packet boundary: a quoted alias is not merely absent from
        THIS packet, it was never written to the catalog at all."""
        packet = self.catalog.object_evidence("TU_X7", source="default")
        terms = {v["means"] for v in packet["view_vocabulary"]}
        self.assertIn("INVOICE_NUMBER", terms)
        self.assertNotIn("ACME MANUFACTURING REBATE", terms)
        raw = (self.root / "c.db").read_bytes()
        self.assertNotIn(b"Acme Manufacturing", raw)

    def test_no_volumetric_leaves_the_packet(self):
        """row_estimate, modified_since_stats, value_score, overlap_pct,
        sampled and cardinality all exist in the underlying catalog for
        this object -- none of them may appear anywhere in the packet."""
        import json
        packet = self.catalog.object_evidence("TU_X7", source="default")
        blob = json.dumps(packet)
        for forbidden in ("row_estimate", "modified_since_stats",
                          "value_score", "overlap_pct", "sampled",
                          "cardinality", "column_count",
                          "populated_columns"):
            self.assertNotIn(forbidden, blob, forbidden)

    def test_the_caveat_is_a_branch_name_not_rendered_prose(self):
        """The rendered sentence interpolates a row-modification COUNT.
        The packet must carry the branch that fired, never that count."""
        packet = self.catalog.object_evidence("TU_X7", source="default")
        self.assertIn(packet["caveat_branch"],
                      {"none", "unmeasured", "unmeasured_but_active",
                       "empty_contradicted_by_dml",
                       "verified_empty_current", "empty_unconfirmed"})
        self.assertNotIn("caveat", packet)

    def test_a_view_declared_join_carries_names_and_booleans_only(self):
        packet = self.catalog.object_evidence("TU_X7", source="default")
        self.assertEqual(len(packet["view_declared_joins"]), 1)
        hop = packet["view_declared_joins"][0]
        self.assertEqual(hop["with"], "MAIN.TU_Q2")
        self.assertEqual(hop["column_pairs"],
                         [{"column": "C2", "references_column": "C1"}])
        self.assertTrue(hop["complete"])
        self.assertIs(hop["has_alternate_conditions"], False)
        self.assertNotIn("alternate_conditions", hop)  # bool only, no count

    def test_a_mined_join_carries_confidence_never_a_percentage(self):
        packet = self.catalog.object_evidence("TU_Q2", source="default")
        self.assertTrue(packet["mined_joins"])
        for hop in packet["mined_joins"]:
            for measurement in hop["measurements"]:
                self.assertIn(measurement["confidence"],
                              {"likely", "possible"})
                self.assertNotIn("overlap_pct", measurement)
                self.assertNotIn("sampled", measurement)

    def test_notes_disclose_a_capped_or_absent_harvest(self):
        """Absent evidence and UNHARVESTED evidence must read differently
        -- otherwise a person cannot tell 'nothing was declared' from
        'the harvest never looked'."""
        packet = self.catalog.object_evidence("TU_X7", source="default")
        self.assertIn("view_vocabulary", packet["notes"])
        self.assertTrue(packet["notes"]["view_vocabulary"])

    def test_a_missing_object_profiles_table_reports_silent_per_object(self):
        con = sqlite3.connect(self.root / "c.db")
        con.execute("DROP TABLE object_profiles")
        con.commit()
        con.close()
        broken = MetadataCatalog(self.root / "c.db")
        packet = broken.object_evidence("TU_X7", source="default")
        self.assertEqual(packet["profiler_status"], "silent")
        self.assertIsNone(packet["liveness"])

    def test_columns_are_never_truncated(self):
        """object_profiles.signature is already the full column list;
        this pins that object_evidence adds no cap on top of it."""
        packet = self.catalog.object_evidence("TU_X7", source="default")
        names = {c.split(":")[0] for c in packet["columns"]}
        self.assertEqual(names, {"C1", "C2", "C4", "C9", "VENDOR_CD"})


class WorklistLoopIsolationTests(unittest.TestCase):
    """A bad source name or an unresolvable identifier must not raise
    out of object_evidence and abort a worklist iterating many objects --
    context() itself raises on this exact case, unlike relationships_of()."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-loop-")
        self.root = Path(self.temp.name)
        self.catalog = _catalog(self.root,
                                "CREATE TABLE TU_X7 (C1 TEXT);")

    def tearDown(self):
        self.temp.cleanup()

    def test_an_unknown_source_returns_a_refusal_not_a_raise(self):
        result = self.catalog.object_evidence("TU_X7", source="nosuch")
        self.assertFalse(result["found"])
        self.assertEqual(result["bucket"], "source_error")


class FoundingSignalTests(unittest.TestCase):
    def test_a_decased_echo_of_the_physical_name_founds_nothing(self):
        self.assertEqual(founding_signals(
            {"object": "TU_X7", "label": "Tu X7",
             "view_vocabulary": []}), set())

    def test_a_real_label_founds_s1(self):
        self.assertIn("S1_record_label", founding_signals(
            {"object": "PS_JRNL_LN", "label": "Journal Line",
             "view_vocabulary": []}))

    def test_two_vocabulary_terms_do_not_found_but_three_do(self):
        two = [{"means": "INVOICE_NUMBER"}, {"means": "VENDOR_NAME"}]
        three = two + [{"means": "PAYMENT_TERMS"}]
        self.assertNotIn("S2_view_vocabulary",
                         founding_signals({"object": "T", "label": None,
                                          "view_vocabulary": two}))
        self.assertIn("S2_view_vocabulary",
                      founding_signals({"object": "T", "label": None,
                                       "view_vocabulary": three}))


class ClassifyObjectTests(unittest.TestCase):
    def _base(self, **overrides):
        packet = {
            "available": True, "found": True, "object": "TU_X7",
            "label": None, "view_vocabulary": [], "liveness": "populated",
            "caveat_branch": "none", "profiler_status": "measured",
            "declared_foreign_keys": [], "view_declared_joins": [],
            "mined_joins": [],
        }
        packet.update(overrides)
        return packet

    def test_zero_founding_signals_is_no_human_wording(self):
        result = classify_object(self._base(), already_proposed=False,
                                 approved_neighbour=lambda oid: False)
        self.assertEqual(result.bucket, "no_human_wording")

    def test_a_relationship_alone_never_founds_a_meaning(self):
        """Declared FKs and mined joins corroborate; they must never be
        read as founding on their own, however many there are."""
        result = classify_object(self._base(
            declared_foreign_keys=[{"with": "TU_Q2"}] * 3,
            mined_joins=[{"with": "TU_Q2"}] * 3,
        ), already_proposed=False, approved_neighbour=lambda oid: False)
        self.assertEqual(result.bucket, "no_human_wording")
        self.assertEqual(result.corroborating_signals,
                         {"declared_foreign_key", "mined_join"})

    def test_a_record_label_is_eligible(self):
        result = classify_object(
            self._base(label="Journal Line"), already_proposed=False,
            approved_neighbour=lambda oid: False)
        self.assertEqual(result.bucket, "eligible")
        self.assertIn("S1_record_label", result.founding_signals)

    def test_an_approved_neighbour_founds_s3(self):
        result = classify_object(
            self._base(view_declared_joins=[
                {"with": "TU_Q2", "with_object_id": "n2"}]),
            already_proposed=False,
            approved_neighbour=lambda oid: oid == "n2")
        self.assertEqual(result.bucket, "eligible")
        self.assertIn("S3_approved_neighbour", result.founding_signals)

    def test_already_spoken_for_outranks_everything_else(self):
        """A decided object must never be re-listed as eligible -- there
        is no bulk-decide in the UI, and re-showing it trains an
        operator to stop reading the worklist."""
        result = classify_object(
            self._base(label="Journal Line"), already_proposed=True,
            approved_neighbour=lambda oid: False)
        self.assertEqual(result.bucket, "already_spoken_for")

    def test_verified_empty_is_refused_before_founding_is_even_checked(self):
        result = classify_object(self._base(
            liveness="empty", caveat_branch="verified_empty_current",
            label="Journal Line"),
            already_proposed=False, approved_neighbour=lambda oid: False)
        self.assertEqual(result.bucket, "empty")

    def test_profiler_silence_is_distinguished_from_no_human_wording(self):
        result = classify_object(
            self._base(profiler_status="silent"), already_proposed=False,
            approved_neighbour=lambda oid: False)
        self.assertEqual(result.bucket, "profiler_silent")


class BuildWorklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pstb-worklist-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_the_worklist_writes_nothing(self):
        catalog = _catalog(self.root, "CREATE TABLE TU_X7 (C1 TEXT);")
        store = _store(self.root)
        store_path = store.path
        before = (store_path.stat().st_mtime_ns, store_path.stat().st_size) \
            if store_path.exists() else None
        result = build_worklist(catalog, store, "default")
        self.assertGreater(result["total"], 0)
        after = (store_path.stat().st_mtime_ns, store_path.stat().st_size) \
            if store_path.exists() else None
        self.assertEqual(before, after)

    def test_a_missing_profiler_table_fails_the_whole_run(self):
        """One object at a time reporting 'profiler_silent' looks like a
        finding about the schema. A missing table is a broken artifact
        and must fail loudly instead."""
        catalog = _catalog(self.root, "CREATE TABLE TU_X7 (C1 TEXT);")
        con = sqlite3.connect(self.root / "c.db")
        con.execute("DROP TABLE object_profiles")
        con.commit()
        con.close()
        broken = MetadataCatalog(self.root / "c.db")
        store = _store(self.root)
        with self.assertRaises(RuntimeError) as caught:
            build_worklist(broken, store, "default")
        self.assertIn("object_profiles", str(caught.exception))

    def test_an_already_decided_object_is_not_relisted_as_eligible(self):
        catalog = _catalog(self.root, """
            CREATE TABLE TU_X7 (C1 TEXT);
        """)
        con = sqlite3.connect(self.root / "c.db")
        row = con.execute(
            "SELECT id FROM nodes WHERE kind='table' AND name='TU_X7'"
        ).fetchone()
        con.close()
        store = _store(self.root)
        store.propose(object_id=row[0], schema="MAIN", object_name="TU_X7",
                      object_kind="table",
                      meaning="Vendor invoice staging rows")
        result = build_worklist(catalog, store, "default")
        row_out = next(r for r in result["rows"] if r["object"] == "TU_X7")
        self.assertEqual(row_out["bucket"], "already_spoken_for")

    def test_two_runs_over_one_artifact_are_byte_identical_in_order(self):
        catalog = _catalog(self.root, """
            CREATE TABLE TU_ZZZ (C1 TEXT);
            CREATE TABLE TU_AAA (C1 TEXT);
        """)
        store = _store(self.root)
        first = [r["object"] for r in
                build_worklist(catalog, store, "default")["rows"]]
        second = [r["object"] for r in
                 build_worklist(catalog, store, "default")["rows"]]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))


class EndpointGateTests(unittest.TestCase):
    def setUp(self):
        import pstb.gui.app as gui
        self.gui = gui
        self.client = TestClient(gui.app, client=("127.0.0.1", 5555),
                                 base_url="http://localhost")

    def test_the_worklist_requires_the_question_log_operator(self):
        from fastapi import HTTPException

        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")
        with patch.object(self.gui, "_require_question_log_operator", refuse):
            r = self.client.get("/api/source/default/meaning-worklist")
        self.assertEqual(r.status_code, 403)

    def test_the_evidence_endpoint_requires_the_question_log_operator(self):
        from fastapi import HTTPException

        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")
        with patch.object(self.gui, "_require_question_log_operator", refuse):
            r = self.client.get(
                "/api/source/default/meaning-evidence?identifier=TU_X7")
        self.assertEqual(r.status_code, 403)

    def test_an_unknown_source_is_refused_by_name(self):
        from types import SimpleNamespace
        registry = SimpleNamespace(
            names=lambda: ["default", "p2go"],
            resolve_name=lambda s="": (s or "default"))
        with patch.object(self.gui.engine, "registry", registry):
            r = self.client.get("/api/source/nosuch/meaning-worklist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("p2go", r.json()["detail"])

    def test_a_missing_identifier_is_a_400_not_a_500(self):
        r = self.client.get("/api/source/default/meaning-evidence")
        self.assertEqual(r.status_code, 400)

    def test_the_worklist_is_unreachable_from_a_model_turn(self):
        """No MCP tool wraps this, and no source-silo list names it --
        it is an operator surface, not a chat capability."""
        from pstb import guards
        for registry in (guards.SOURCE_SILO_TOOLS,
                         getattr(guards, "SOURCE_SILO_CHAT_TOOLS", ()),
                         guards._SOURCE_SCOPED_TOOLS):
            self.assertNotIn("meaning_worklist", registry)
            self.assertNotIn("object_evidence", registry)
        import pstb.server as server
        source = Path(server.__file__).read_text()
        self.assertNotIn("meaning_worklist", source)
        self.assertNotIn("object_evidence", source)


if __name__ == "__main__":
    unittest.main()
