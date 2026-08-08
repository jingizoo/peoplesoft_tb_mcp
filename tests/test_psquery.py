"""PSQuery and Integration Broker discovery.

The properties that matter are about restraint, not retrieval: discovery
must find what exists WITHOUT touching a gateway, must not leak another
user's private query by default, and must never let "I found this
operation" become "I may call it" — Integration Broker carries voucher
builds and journal loads.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.psquery import READ_ONLY_OPERATIONS, QueryCatalog  # noqa: E402


def _catalog(db_path=None):
    cfg = Config.sample(ROOT)
    if db_path:
        cfg.db.sqlite_path = str(db_path)
        cfg.db.use_views = False
    db = Database(cfg)
    return db, QueryCatalog(TBEngine(db, cfg))


class QueryDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.cat = _catalog()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_text_search_finds_prior_art(self) -> None:
        out = self.cat.search_queries(text="aging")
        self.assertEqual([q["query"] for q in out["queries"]],
                         ["AP_AGING_BY_VENDOR"])

    def test_run_counts_rank_what_the_business_uses(self) -> None:
        names = [q["query"] for q in self.cat.search_queries()["queries"]]
        self.assertEqual(names[0], "AP_AGING_BY_VENDOR",
                         "the most-run query should lead — run counts are "
                         "the cheapest signal of what matters here")

    def test_private_queries_are_hidden_by_default(self) -> None:
        public = {q["query"] for q in self.cat.search_queries()["queries"]}
        self.assertNotIn("MY_ADHOC_SPEND", public,
                         "another user's private query must not surface "
                         "unasked")
        widened = self.cat.search_queries(include_private=True)
        by = {q["query"]: q for q in widened["queries"]}
        self.assertIn("MY_ADHOC_SPEND", by)
        self.assertEqual(by["MY_ADHOC_SPEND"]["visibility"], "private")
        self.assertEqual(by["MY_ADHOC_SPEND"]["owner"], "MRAO")

    def test_search_by_record_finds_queries_touching_it(self) -> None:
        # Sites write the record with or without its PS_ prefix.
        for spelling in ("PS_VOUCHER", "VOUCHER", "voucher"):
            out = self.cat.search_queries(record=spelling)
            self.assertIn("AP_AGING_BY_VENDOR",
                          [q["query"] for q in out["queries"]], spelling)

    def test_detail_returns_prompts_in_order_with_types(self) -> None:
        out = self.cat.query_detail("AP_AGING_BY_VENDOR")
        self.assertTrue(out["found"])
        self.assertEqual([(p["position"], p["prompt"], p["type"])
                          for p in out["prompts"]],
                         [(1, "Business Unit", "character"),
                          (2, "As Of Date", "date")])
        self.assertEqual([r["record"] for r in out["records"]],
                         ["VOUCHER", "VENDOR"])

    def test_an_unknown_query_refuses_with_the_next_step(self) -> None:
        out = self.cat.query_detail("NO_SUCH_QUERY")
        self.assertFalse(out["found"])
        self.assertIn("search_ps_queries", out["detail"])

    def test_a_missing_catalog_names_the_grant_needed(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pstb-qry-"))
        try:
            dst = tmp / "s.db"
            shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
            con = sqlite3.connect(dst)
            con.execute("DROP TABLE PSQRYDEFN")
            con.commit(); con.close()
            db, cat = _catalog(dst)
            try:
                out = cat.search_queries(text="aging")
                self.assertFalse(out["available"])
                self.assertIn("PSQRYDEFN", out["detail"])
                self.assertIn("no gateway", out["detail"],
                              "the remedy must say this is a grant, not an "
                              "API problem")
            finally:
                db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IntegrationDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.cat = _catalog()
        cls.out = cls.cat.integration_endpoints()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_the_gateway_comes_from_the_site_not_our_config(self) -> None:
        self.assertIn("PSIGW", self.out["target_location"])
        self.assertIn("PSIBSVCSETUP", self.out["target_source"])

    def test_write_capable_operations_are_found_but_not_callable(self) -> None:
        by = {o["operation"]: o for o in self.out["operations"]}
        loader = by["VOUCHER_LOAD"]
        self.assertFalse(loader["callable"],
                         "discovering a voucher-build service must never "
                         "make it invocable")
        self.assertIn("write-capable", loader["why"])
        self.assertTrue(by["QAS_EXECUTEQUERY"]["callable"])

    def test_the_allowlist_is_exact_names_not_a_pattern(self) -> None:
        # A pattern like "contains GET" would admit
        # GET_APPROVAL_AND_POST. Exact names cannot.
        self.assertTrue(all(op.startswith("QAS_")
                            for op in READ_ONLY_OPERATIONS))
        for o in self.out["operations"]:
            self.assertEqual(o["callable"],
                             o["operation"].upper() in READ_ONLY_OPERATIONS)

    def test_the_security_divergence_is_disclosed(self) -> None:
        self.assertIn("permission lists", self.out["note"])
        self.assertIn("DISCOVERY IS NOT INVOCATION", self.out["note"])


class QasConfigTests(unittest.TestCase):
    def test_credentials_come_from_env_and_default_off(self) -> None:
        cfg = Config.sample(ROOT)
        self.assertFalse(cfg.ps_api.enabled)
        self.assertEqual(cfg.ps_api.user, "")
        self.assertEqual(cfg.ps_api.target_location, "",
                         "blank means DISCOVER the gateway from the site's "
                         "own IB catalog rather than hardcode it")

    def test_config_yaml_never_carries_the_password(self) -> None:
        text = (ROOT / "config.yaml").read_text()
        self.assertNotIn("PSFT_QAS_PASSWORD", text.split("#")[0])
        self.assertNotIn("password:", text.lower().split("ps_api")[-1][:200])


class RealShapesOnlyTests(unittest.TestCase):
    """The sample must carry PeopleSoft's REAL column names.

    The sample database exists so code can be written against it and then
    run unchanged on a real instance. That contract inverted once: the
    seed invented QRYRUNCNT, BNDDESCR and a TARGETLOCATION, code was
    written to match, and the first contact with a real Oracle instance
    was "ORA-00904 invalid identifier" behind a splash screen that never
    cleared. Verified against the PT 8.58 data dictionary
    (go-faster.co.uk/peopletools): these names exist in no release, and
    this test keeps them out of the sample for good.
    """

    FICTIONAL = {
        "PSQRYDEFN": {"QRYRUNCNT"},          # real: PSQRYSTATS.EXECCOUNT
        "PSQRYBIND": {"BNDDESCR", "EDITTYPE"},   # real: HEADING / EDITTABLE
        "PSIBSVCSETUP": {"TARGETLOCATION", "TARGET_LOCATION", "URL",
                         "IB_TARGETLOCATION"},   # real: IB_TGTLOCATION...
        "PSOPERATION": {"SERVICE", "OPERTYPE", "IB_OPERSTATUS"},
        "PSSERVICE": {"SERVICE", "SERVICEALIAS"},  # real: IB_SERVICENAME...
    }
    REQUIRED = {
        "PSQRYSTATS": {"OPRID", "QRYNAME", "EXECCOUNT"},
        "PSQRYBIND": {"BNDNAME", "HEADING", "FIELDTYPE"},
        "PSIBSVCSETUP": {"IB_TGTLOCATION", "IB_RESTTGTLOC"},
        "PSSERVICEOPR": {"IB_SERVICENAME", "IB_OPERATIONNAME"},
        "PSOPERATION": {"IB_OPERATIONNAME", "DESCR"},
    }

    def test_no_fictional_columns_in_the_sample(self) -> None:
        db, _ = _catalog()
        for table, bad in self.FICTIONAL.items():
            cols = set(db.columns(table))
            leaked = cols & bad
            self.assertFalse(
                leaked,
                f"{table} carries invented column(s) {sorted(leaked)} — "
                "these exist in no PeopleTools release, and code written "
                "against them fails on first contact with a real instance")

    def test_real_columns_are_present(self) -> None:
        db, _ = _catalog()
        for table, need in self.REQUIRED.items():
            cols = set(db.columns(table))
            missing = need - cols
            self.assertFalse(missing,
                             f"{table} is missing real column(s) "
                             f"{sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
