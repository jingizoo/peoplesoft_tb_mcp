"""Records this site taught the agent about.

A client-specific table is invisible to every discovery mechanism the product
has. It carries no PeopleTools description, its name encodes nothing, and no
wiki page mentions it. `search_records("interface file")` returns nothing, and
no amount of better retrieval changes that — the information was never written
down anywhere the agent can read.

What does exist is someone saying it out loud once. These tests cover that
path: proposed in conversation, approved by an operator, then findable by
wording that differs from what was originally said. A pending claim must not
quietly steer Finance discovery while it waits in the review queue.

The boundary they also enforce: a taught fact is a POINTER, never authority.
It helps locate a record; the live catalog still decides what is in it.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.memory import MemoryError_, SiteMemory  # noqa: E402


class TaughtRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-taught-"))
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)
        self.memory = SiteMemory(self.tmp / "memory.json")
        self.engine.attach_memory(self.memory)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def teach(self, text: str, *, approve: bool = True) -> dict:
        proposed = self.memory.propose(text, kind="record", source="test")
        if approve:
            self.memory.decide(
                proposed["fact"]["id"], approve=True, by="test operator")
        return proposed

    def test_an_unknown_custom_record_is_invisible_until_taught(self) -> None:
        # Use a purpose that is genuinely absent from the sample metadata.
        # "interface file" is deliberately present in PSRECDEFN and the
        # natural-language search now correctly finds it regardless of word
        # order, so it is no longer a valid premise for taught-only lookup.
        before = self.engine.search_records("zephyr quokka intake")
        self.assertEqual(before["records"], [],
                         "the premise of this feature is that discovery "
                         "cannot find this record on its own")
        self.teach("ACME_TXN_HDR: Zephyr quokka intake headers and load status")
        after = self.engine.search_records("zephyr quokka intake")
        self.assertIn("ACME_TXN_HDR",
                      [r["record"] for r in after["records"]])

    def test_taught_company_table_is_never_given_an_invented_ps_prefix(self):
        self.teach("ACME_TXN_HDR: Phoenix interface header")
        entry = self.engine.search_records("Phoenix interface")["records"][0]
        self.assertEqual(entry["record"], "ACME_TXN_HDR")
        self.assertEqual(entry["table"], "ACME_TXN_HDR")
        self.assertNotEqual(entry["table"], "PS_ACME_TXN_HDR")

    def test_metadata_search_is_not_word_order_sensitive(self) -> None:
        forward = self.engine.search_records("file interface")["records"]
        reverse = self.engine.search_records("interface file")["records"]
        for found in (forward, reverse):
            self.assertIn("TU_FILE_INTFC", [r["record"] for r in found])
            hit = next(r for r in found if r["record"] == "TU_FILE_INTFC")
            self.assertTrue({"FILE", "INTERFACE"}.issubset(
                set(hit["matched_terms"])))

    def test_it_is_found_by_wording_the_user_did_not_use(self) -> None:
        # "Load status" was in the explanation, not in the question. Matching
        # only the original phrasing would make this a one-question feature.
        self.teach("TU_FILE_INTFC: holds inbound interface file headers "
                   "and load status")
        hits = self.engine.search_records("load status")
        self.assertIn("TU_FILE_INTFC", [r["record"] for r in hits["records"]])

    def test_a_taught_record_says_where_it_came_from(self) -> None:
        self.teach("XX_REBATE_ACCR: our custom rebate accrual staging table")
        entry = next(r for r in self.engine.search_records("rebate")["records"]
                     if r["record"] == "XX_REBATE_ACCR")
        self.assertEqual(entry["matched_on"], "taught here")
        self.assertIn("rebate accrual", entry["taught"])

    def test_taught_records_rank_above_name_matches(self) -> None:
        # A human naming a table for this purpose beats a substring hit.
        self.teach("XX_CUSTOM_THING: the table that actually holds journal "
                   "approval overrides")
        hits = self.engine.search_records("journal")["records"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["record"], "XX_CUSTOM_THING")

    def test_describe_record_surfaces_the_teaching_without_ceding_authority(self) -> None:
        self.teach("PS_JRNL_LN: our journal lines, one row per chartfield combo")
        out = self.engine.describe_record("PS_JRNL_LN")
        self.assertTrue(out.get("taught_here"))
        # The catalog still supplies the columns; the teaching is a pointer.
        self.assertIn("columns", out)
        self.assertIn("MONETARY_AMOUNT", [c.upper() for c in out["columns"]])
        self.assertIn("live catalog and win any disagreement",
                      out["taught_note"])

    def test_a_record_fact_must_name_its_table(self) -> None:
        # Without a table name the fact cannot be attached to anything, and a
        # free-floating sentence in the record namespace helps nobody.
        with self.assertRaises(MemoryError_) as ctx:
            self.teach("the interface files are somewhere in the TU tables")
        self.assertIn("must start with the table name", str(ctx.exception))

    def test_a_pending_finance_record_fact_never_steers_retrieval(self) -> None:
        proposed = self.teach(
            "XX_PENDING_ONLY: Zephyr quokka settlement staging",
            approve=False,
        )
        self.assertEqual(self.memory.record_facts("XX_PENDING_ONLY"), [])
        self.assertNotIn(
            "XX_PENDING_ONLY",
            [row["record"] for row in
             self.engine.search_records("zephyr quokka settlement")["records"]],
        )

        self.memory.decide(
            proposed["fact"]["id"], approve=True, by="test operator")
        self.assertIn(
            "XX_PENDING_ONLY",
            [row["record"] for row in
             self.engine.search_records("zephyr quokka settlement")["records"]],
        )

    def test_lookup_accepts_the_name_with_or_without_the_ps_prefix(self) -> None:
        self.teach("PS_TU_FILE_INTFC: inbound interface files")
        for asked in ("PS_TU_FILE_INTFC", "TU_FILE_INTFC", "tu_file_intfc"):
            self.assertTrue(self.memory.record_facts(asked),
                            f"lookup by {asked!r} found nothing")

    def test_a_rejected_fact_stops_being_used(self) -> None:
        got = self.teach(
            "XX_WRONG: this table does not hold what I thought", approve=False)
        self.memory.decide(got["fact"]["id"], approve=False, by="test")
        self.assertEqual(self.memory.record_facts("XX_WRONG"), [])
        self.assertEqual(self.engine.search_records("does not hold")["records"],
                         [])

    def test_discovery_works_when_no_memory_is_attached(self) -> None:
        # The engine is constructed without memory in scripts and tests; record
        # search must not depend on it existing.
        cfg = load_config(str(ROOT / "config.yaml"))
        db = Database(cfg)
        try:
            bare = TBEngine(db, cfg)
            self.assertIsInstance(bare.search_records("journal")["records"], list)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
