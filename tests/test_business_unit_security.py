"""A user sees the business units PeopleSoft grants them, and no others.

The rules under test, in the order they matter:

  1. An omitted business_unit is the DANGEROUS case, not the safe one — it
     falls through to the site default, which is chosen from the whole
     installation. Before the gate existed, a CA001-only user asking for a
     trial balance with no arguments received US001's complete one, 200 OK.
  2. Filtering has to happen where the catalog is BUILT, not where it is
     rendered, or the unit still becomes the discovered default.
  3. Fail closed: security switched on and unreadable shows nothing and
     says which grant is missing.
  4. None of it is authentication, and the code says so out loud — a user
     ID with no password identifies nobody.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.guards import unit_access_block  # noqa: E402
from pstb.security import (  # noqa: E402
    ACCESS_TTL_SECONDS,
    SOURCE_TTL_SECONDS,
    Access,
    RowSecurity,
    SecurityError,
)

LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}


def _secured_cfg():
    cfg = load_config(str(ROOT / "config.yaml"))
    cfg.security.enabled = True
    cfg.security.privileged_users = ["ADMIN"]
    return cfg


class ResolutionTests(unittest.TestCase):
    """Both PeopleSoft models: by user ID, and by row-security class."""

    def setUp(self) -> None:
        self.cfg = _secured_cfg()
        self.sec = RowSecurity(Database(self.cfg), self.cfg)

    def test_user_level_security_is_discovered_and_applied(self) -> None:
        self.assertEqual(self.sec.source_record()[0], "PS_SEC_BU_OPR")
        access = self.sec.access_for("FIN_US001")
        self.assertEqual(access.units, frozenset({"US001"}))
        self.assertTrue(access.allows("US001"))
        self.assertFalse(access.allows("CA001"))

    def test_class_level_security_resolves_through_ROWSECCLASS(self) -> None:
        # A site that keeps unit security against the permission list, which
        # PSOPRDEFN.ROWSECCLASS points at.
        self.cfg.security.unit_record = "PS_SEC_BU_CLS"
        self.cfg.security.unit_key = "OPRCLASS"
        self.sec.invalidate()
        access = self.sec.access_for("AP_CLERK")
        self.assertEqual(access.units, frozenset({"US001"}))
        self.assertEqual(access.source, "PS_SEC_BU_CLS")

    def test_a_privileged_user_does_not_depend_on_the_tables(self) -> None:
        # This is the account that has to keep working WHILE a grant is
        # being fixed, so it is named in config and never looked up.
        access = self.sec.access_for("ADMIN")
        self.assertTrue(access.all_units)
        self.assertTrue(access.privileged)
        self.assertEqual(access.source, "config")

    def test_an_unknown_user_is_refused_by_name(self) -> None:
        with self.assertRaises(SecurityError) as ctx:
            self.sec.access_for("GHOST")
        self.assertIn("PSOPRDEFN", str(ctx.exception))

    def test_a_user_with_no_grants_gets_nothing_and_is_told_why(self) -> None:
        access = self.sec.access_for("NOACCESS")
        self.assertEqual(access.units, frozenset())
        self.assertFalse(access.allows("US001"))
        self.assertIn("no business units", access.detail)

    def test_security_off_means_everyone_sees_everything(self) -> None:
        self.cfg.security.enabled = False
        self.assertTrue(self.sec.access_for("ANYONE").all_units)


class FailClosedTests(unittest.TestCase):
    """Unreadable security must not degrade to full access by default."""

    class _NoSecurityTables(Database):
        def columns(self, table):        # nothing readable
            return set()

    def setUp(self) -> None:
        self.cfg = _secured_cfg()
        self.db = self._NoSecurityTables(self.cfg)
        self.sec = RowSecurity(self.db, self.cfg)

    def test_no_readable_record_refuses_rather_than_allowing(self) -> None:
        with self.assertRaises(SecurityError) as ctx:
            self.sec.access_for("FIN_US001")
        message = str(ctx.exception)
        self.assertIn("could not be read", message)
        self.assertIn("security.enabled: false", message,
                      "name the way out, or the next person edits the guard")

    def test_a_site_may_choose_to_fail_open_but_must_type_it(self) -> None:
        self.cfg.security.on_unavailable = "allow"
        self.sec.invalidate()
        access = self.sec.access_for("FIN_US001")
        self.assertTrue(access.all_units)
        self.assertIn("could not be read", access.detail)

    def test_a_privileged_user_still_gets_in(self) -> None:
        # Otherwise the one account that can fix the grant is locked out by
        # the grant being broken.
        self.assertTrue(self.sec.access_for("ADMIN").all_units)


class CacheLifecycleTests(unittest.TestCase):
    """Outages recover without a restart or an over-broad cached grant."""

    class _Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    class _RecoveringDb:
        prefix = ""

        def __init__(self) -> None:
            self.catalog_available = True
            self.class_catalog_available = False
            self.units_available = True
            self.units = ["US001"]
            self.source_probes = 0
            self.unit_queries = 0
            self.on_source_probe = None
            self.on_unit_query = None
            self.source_probe_started = None
            self.release_source_probe = None
            self.block_source_probe_number = None
            self.unit_query_started = None
            self.release_unit_query = None
            self.block_unit_query_number = None

        def columns(self, table):
            if table == "PSOPRDEFN":
                return {"OPRID", "ROWSECCLASS"}
            if table == "PS_SEC_BU_OPR":
                self.source_probes += 1
                probe_number = self.source_probes
                available = self.catalog_available
                if self.on_source_probe is not None:
                    self.on_source_probe()
                if (self.source_probe_started is not None
                        and (self.block_source_probe_number is None
                             or probe_number == self.block_source_probe_number)):
                    self.source_probe_started.set()
                    self.release_source_probe.wait(timeout=5)
                if available:
                    return {"OPRID", "BUSINESS_UNIT"}
            if table == "PS_SEC_BU_CLS" and self.class_catalog_available:
                return {"OPRCLASS", "BUSINESS_UNIT"}
            return set()

        def query(self, sql, params, max_rows=0):
            if "FROM PSOPRDEFN" in sql:
                return ([{"oprid": params["who"]}], False)
            if "FROM PS_SEC_BU_OPR" in sql:
                self.unit_queries += 1
                query_number = self.unit_queries
                available = self.units_available
                units = list(self.units)
                if self.on_unit_query is not None:
                    self.on_unit_query()
                if (self.unit_query_started is not None
                        and (self.block_unit_query_number is None
                             or query_number == self.block_unit_query_number)):
                    self.unit_query_started.set()
                    self.release_unit_query.wait(timeout=5)
                if not available:
                    raise DbError("temporary security-table outage")
                return ([{"bu": unit} for unit in units], False)
            raise AssertionError(f"unexpected query: {sql}")

    class _RecoveringClassDb:
        prefix = ""

        def __init__(self) -> None:
            self.operator_available = False
            self.class_unit_queries = 0

        def columns(self, table):
            if table == "PSOPRDEFN" and self.operator_available:
                return {"OPRID", "ROWSECCLASS"}
            return set()

        def query(self, sql, params, max_rows=0):
            if "FROM PSOPRDEFN" in sql:
                if not self.operator_available:
                    raise DbError("temporary PSOPRDEFN outage")
                if "ROWSECCLASS AS cls" in sql:
                    return ([{"cls": "AP_CLERK"}], False)
                return ([{"oprid": params["who"]}], False)
            if "FROM PS_SEC_BU_CLS" in sql:
                self.class_unit_queries += 1
                return ([{"bu": "US001"}], False)
            raise AssertionError(f"unexpected query: {sql}")

    def setUp(self) -> None:
        self.cfg = _secured_cfg()
        self.clock = self._Clock()
        self.db = self._RecoveringDb()
        self.sec = RowSecurity(self.db, self.cfg, clock=self.clock)

    def test_a_negative_source_probe_is_not_cached(self) -> None:
        self.db.catalog_available = False
        self.assertEqual(self.sec.source_record(), ("", "", "none"))
        first_probes = self.db.source_probes

        self.db.catalog_available = True
        self.assertEqual(self.sec.source_record()[0], "PS_SEC_BU_OPR")
        self.assertGreater(self.db.source_probes, first_probes)

    def test_positive_source_cache_expires_and_recovers(self) -> None:
        self.db.on_source_probe = lambda: self.clock.advance(40)
        self.assertEqual(self.sec.source_record()[0], "PS_SEC_BU_OPR")
        first_probes = self.db.source_probes
        self.db.catalog_available = False

        self.clock.advance(SOURCE_TTL_SECONDS - 1)
        self.assertEqual(self.sec.source_record()[0], "PS_SEC_BU_OPR")
        self.assertEqual(self.db.source_probes, first_probes)

        self.clock.advance(1)
        self.assertEqual(self.sec.source_record(), ("", "", "none"))
        self.assertGreater(self.db.source_probes, first_probes)
        failed_probes = self.db.source_probes

        self.db.catalog_available = True
        self.assertEqual(self.sec.source_record()[0], "PS_SEC_BU_OPR")
        self.assertGreater(self.db.source_probes, failed_probes)

    def test_fail_open_unavailable_access_is_never_cached(self) -> None:
        self.cfg.security.on_unavailable = "allow"
        self.db.units_available = False
        degraded = self.sec.access_for("FIN_US001")
        self.assertTrue(degraded.all_units)
        self.assertEqual(degraded.source, "unavailable")

        self.db.units_available = True
        recovered = self.sec.access_for("FIN_US001")
        self.assertFalse(recovered.all_units)
        self.assertEqual(recovered.units, frozenset({"US001"}))
        self.assertEqual(self.db.unit_queries, 2)

    def test_access_ttl_is_bounded_from_query_start(self) -> None:
        self.db.on_unit_query = lambda: self.clock.advance(40)
        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"US001"}))
        self.db.units = ["CA001"]

        self.clock.advance(ACCESS_TTL_SECONDS - 41)
        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"US001"}))
        self.assertEqual(self.db.unit_queries, 1)

        self.clock.advance(1)
        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"CA001"}))
        self.assertEqual(self.db.unit_queries, 2)

    def test_invalidate_clears_access_and_source_caches(self) -> None:
        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"US001"}))
        first_probes = self.db.source_probes
        self.assertEqual(self.db.unit_queries, 1)
        self.db.units = ["CA001"]

        self.sec.invalidate()

        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"CA001"}))
        self.assertGreater(self.db.source_probes, first_probes)
        self.assertEqual(self.db.unit_queries, 2)

    def test_inflight_access_cannot_resurrect_after_invalidate(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.db.unit_query_started = started
        self.db.release_unit_query = release
        self.db.block_unit_query_number = 1
        self.addCleanup(release.set)
        outcome = {}

        def resolve() -> None:
            try:
                outcome["access"] = self.sec.access_for("FIN_US001")
            except BaseException as exc:  # surfaced in the test thread
                outcome["error"] = exc

        worker = threading.Thread(target=resolve, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2), "unit query did not block")
        self.db.units = ["CA001"]
        self.sec.invalidate()
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "unit query did not finish")
        if "error" in outcome:
            raise outcome["error"]
        self.assertEqual(outcome["access"].units, frozenset({"US001"}))

        self.db.unit_query_started = None
        self.db.release_unit_query = None
        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"CA001"}))
        self.assertEqual(self.db.unit_queries, 2)

    def test_inflight_source_cannot_resurrect_after_invalidate(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.db.source_probe_started = started
        self.db.release_source_probe = release
        self.db.block_source_probe_number = 1
        self.addCleanup(release.set)
        outcome = {}

        def discover() -> None:
            try:
                outcome["source"] = self.sec.source_record()
            except BaseException as exc:  # surfaced in the test thread
                outcome["error"] = exc

        worker = threading.Thread(target=discover, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2), "source probe did not block")
        self.db.catalog_available = False
        self.sec.invalidate()
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "source probe did not finish")
        if "error" in outcome:
            raise outcome["error"]
        self.assertEqual(outcome["source"][0], "PS_SEC_BU_OPR")

        self.db.source_probe_started = None
        self.db.release_source_probe = None
        probes_before_retry = self.db.source_probes
        self.assertEqual(self.sec.source_record(), ("", "", "none"))
        self.assertGreater(self.db.source_probes, probes_before_retry)

    def test_class_lookup_outage_is_unavailable_not_an_empty_grant(self) -> None:
        self.cfg.security.unit_record = "PS_SEC_BU_CLS"
        self.cfg.security.unit_key = "OPRCLASS"
        self.cfg.security.on_unavailable = "allow"
        db = self._RecoveringClassDb()
        sec = RowSecurity(db, self.cfg, clock=self.clock)

        degraded = sec.access_for("AP_CLERK")
        self.assertEqual(degraded.source, "unavailable")
        self.assertTrue(degraded.all_units)
        self.assertIn("PS_SEC_BU_CLS", degraded.detail)
        self.assertIn("PSOPRDEFN.ROWSECCLASS", degraded.detail)

        db.operator_available = True
        recovered = sec.access_for("AP_CLERK")
        self.assertEqual(recovered.source, "PS_SEC_BU_CLS")
        self.assertEqual(recovered.units, frozenset({"US001"}))
        self.assertEqual(db.class_unit_queries, 1)

    def test_older_access_attempt_cannot_overwrite_newer_result(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.db.unit_query_started = started
        self.db.release_unit_query = release
        self.db.block_unit_query_number = 1
        self.addCleanup(release.set)
        older = {}

        def resolve_older() -> None:
            try:
                older["access"] = self.sec.access_for("FIN_US001")
            except BaseException as exc:
                older["error"] = exc

        worker = threading.Thread(target=resolve_older, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2), "older query did not block")
        self.db.units = ["CA001"]

        newer = self.sec.access_for("FIN_US001")
        self.assertEqual(newer.units, frozenset({"CA001"}))
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "older query did not finish")
        if "error" in older:
            raise older["error"]
        self.assertEqual(older["access"].units, frozenset({"US001"}))

        self.db.unit_query_started = None
        self.db.release_unit_query = None
        self.assertEqual(
            self.sec.access_for("FIN_US001").units, frozenset({"CA001"}))
        self.assertEqual(self.db.unit_queries, 2)

    def test_older_source_probe_cannot_overwrite_newer_result(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.db.source_probe_started = started
        self.db.release_source_probe = release
        self.db.block_source_probe_number = 1
        self.addCleanup(release.set)
        older = {}

        def discover_older() -> None:
            try:
                older["source"] = self.sec.source_record()
            except BaseException as exc:
                older["error"] = exc

        worker = threading.Thread(target=discover_older, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2), "older probe did not block")
        self.db.catalog_available = False
        self.db.class_catalog_available = True

        newer = self.sec.source_record()
        self.assertEqual(newer[0], "PS_SEC_BU_CLS")
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "older probe did not finish")
        if "error" in older:
            raise older["error"]
        self.assertEqual(older["source"][0], "PS_SEC_BU_OPR")

        self.db.source_probe_started = None
        self.db.release_source_probe = None
        self.assertEqual(self.sec.source_record()[0], "PS_SEC_BU_CLS")
        self.assertEqual(self.db.source_probes, 2)


class ToolGateTests(unittest.TestCase):
    """The agent loop's gate, in front of every tool call."""

    RESTRICTED = Access(oprid="FIN_US001", units=frozenset({"US001"}))
    OPEN = Access(oprid="ADMIN", all_units=True, privileged=True)

    def test_a_granted_unit_passes(self) -> None:
        self.assertEqual(unit_access_block(
            "get_trial_balance", {"business_unit": "US001"},
            self.RESTRICTED), "")

    def test_a_foreign_unit_is_refused_with_what_IS_allowed(self) -> None:
        why = unit_access_block("get_trial_balance",
                                {"business_unit": "CA001"}, self.RESTRICTED)
        self.assertIn("CA001", why)
        self.assertIn("US001", why, "say what they CAN see, not just no")

    def test_ad_hoc_sql_is_off_for_a_restricted_user(self) -> None:
        # run_sql writes its own WHERE clause, so no argument check bounds
        # it to a unit. Pretending otherwise would be the whole feature
        # with a hole in it.
        why = unit_access_block("run_sql", {"business_unit": "US001"},
                                self.RESTRICTED)
        self.assertIn("run_sql", why)
        self.assertIn("curated tools", why)

    def test_unscoped_coupa_diagnostics_are_off_for_restricted_users(self):
        for tool in ("coupa_to_ap_tie", "get_coupa_invoices",
                     "get_coupa_stuck_approvals",
                     "get_coupa_budget_lines",
                     "get_coupa_supplier_spend"):
            why = unit_access_block(tool, {}, self.RESTRICTED)
            self.assertIn(tool, why)
            self.assertIn("no governed business-unit argument", why)
            self.assertEqual(unit_access_block(tool, {}, self.OPEN), "")

    def test_ad_hoc_sql_can_be_allowed_deliberately(self) -> None:
        self.assertEqual(unit_access_block(
            "run_sql", {"business_unit": "US001"}, self.RESTRICTED,
            allow_raw_sql=True), "")

    def test_a_privileged_user_is_never_gated(self) -> None:
        for tool in ("run_sql", "get_trial_balance", "run_ps_query"):
            self.assertEqual(unit_access_block(
                tool, {"business_unit": "CA001"}, self.OPEN), "", tool)

    def test_scope_discovery_is_never_gated(self) -> None:
        # The catalog is filtered at source; refusing the discovery call
        # would leave a restricted user unable to see even their OWN units.
        self.assertEqual(unit_access_block(
            "list_financial_scopes", {}, self.RESTRICTED), "")

    def test_ALL_is_refused_because_nothing_downstream_could_narrow_it(self):
        # This test used to assert the opposite, on the reasoning that "ALL"
        # names no unit anybody was denied. That reasoning was wrong in one
        # specific way: ALL means every unit that EXISTS, and the tool it
        # reaches never asked who was calling — so a US001-only user got
        # another company's customers and amounts out of the cross-unit
        # ranking, 200 OK, no warning.
        #
        # In-process callers (the GUI's own endpoints) are narrowed by the
        # bound caller and told they were narrowed. This gate stands in
        # front of an MCP server in a SEPARATE PROCESS, which cannot know
        # who is asking and must not learn it from a tool argument the model
        # writes. So here it refuses, and names the units that would work.
        why = unit_access_block("get_top_billing_customers",
                                {"business_unit": "ALL"}, self.RESTRICTED)
        self.assertTrue(why)
        self.assertIn("US001", why)

    def test_ALL_is_still_open_to_a_privileged_user(self) -> None:
        self.assertEqual(unit_access_block(
            "get_top_billing_customers", {"business_unit": "ALL"},
            self.OPEN), "")


class WebTests(unittest.TestCase):
    """End to end through the app, which is where the leak actually was."""

    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.TestClient, cls.gapp = TestClient, gapp

    def setUp(self) -> None:
        g = self.gapp
        self.saved = (g.cfg.security.enabled,
                      list(g.cfg.security.privileged_users))
        g.cfg.security.enabled = True
        g.cfg.security.privileged_users = ["ADMIN"]
        g.row_security.invalidate()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        g = self.gapp
        g.cfg.security.enabled, g.cfg.security.privileged_users = self.saved
        g.row_security.invalidate()

    def _as(self, oprid: str):
        client = self.TestClient(self.gapp.app, **LOOP)
        response = client.post("/api/signin", json={"oprid": oprid})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_signed_out_gets_no_data_and_no_unit_names(self) -> None:
        client = self.TestClient(self.gapp.app, **LOOP)
        self.assertEqual(client.get("/api/trial-balance").status_code, 401)
        # Not even the catalog: the list of units IS information.
        self.assertEqual(client.get("/api/meta").json()["financial_scopes"],
                         [])

    def test_an_omitted_unit_resolves_INSIDE_the_users_grant(self) -> None:
        """The one that was actually broken.

        No business_unit named is not "no data" — it is the site default,
        and the site default is picked from every unit. A CA001-only user
        received US001's complete trial balance with a 200."""
        body = self._as("FIN_CA001").get("/api/trial-balance").json()
        self.assertEqual(body.get("business_unit"), "CA001")

    def test_a_foreign_unit_is_403_not_empty_rows(self) -> None:
        response = self._as("FIN_US001").get(
            "/api/trial-balance?business_unit=CA001")
        self.assertEqual(response.status_code, 403)
        self.assertIn("CA001", response.json()["error"])

    def test_the_chooser_only_offers_units_the_user_holds(self) -> None:
        units = [s["business_unit"]
                 for s in self._as("FIN_US001").get("/api/scopes").json()["scopes"]]
        self.assertEqual(units, ["US001"])
        self.assertEqual(
            [s["business_unit"]
             for s in self._as("FIN_CA001").get("/api/scopes").json()["scopes"]],
            [], "CA001 holds no ledger data — an empty list is the honest "
                "answer, not US001's")

    def test_a_user_with_no_grants_is_refused_rather_than_defaulted(self):
        self.assertEqual(
            self._as("NOACCESS").get("/api/trial-balance").status_code, 403)

    def test_a_privileged_user_sees_every_unit(self) -> None:
        client = self._as("ADMIN")
        for bu in ("US001", "CA001"):
            self.assertEqual(
                client.get(f"/api/trial-balance?business_unit={bu}").status_code,
                200, bu)

    def test_export_checks_the_unit_in_the_BODY(self) -> None:
        # The query-string gate cannot see it, and export re-runs the tool
        # at the full population ceiling — more rows than the screen showed.
        response = self._as("FIN_US001").post("/api/export", json={
            "tool": "get_trial_balance",
            "args": {"business_unit": "CA001"}})
        self.assertEqual(response.status_code, 403)

    def test_batch_worker_inherits_the_verified_user_grant(self) -> None:
        """A context variable is not inherited by a bare pool thread."""
        from unittest.mock import patch
        from pstb.security import current_access

        observed = []

        def fake_export(_tool, _args, _registry, *, path, progress,
                        **_kwargs):
            access = current_access()
            observed.append((access.oprid, sorted(access.units)))
            Path(path).write_text("account\r\n1000\r\n", encoding="utf-8")
            progress(1)
            return {"rows": 1, "columns": 1, "truncated": False,
                    "filename": "safe.csv", "note": "complete"}

        client = self._as("FIN_US001")
        with patch("pstb.export.batch_to_file", side_effect=fake_export):
            started = client.post(
                "/api/source/finance/batch-exports", json={
                    "tool": "get_trial_balance",
                    "args": {"business_unit": "US001", "ledger": "ACTUALS",
                             "fiscal_year": 2026, "period": 6},
                })
            self.assertEqual(started.status_code, 202, started.text)
            job = started.json()
            for _ in range(100):
                status = client.get(
                    f"/api/batch-exports/{job['job_id']}").json()
                if status.get("state") not in {"queued", "running"}:
                    break
                time.sleep(0.01)
        self.assertEqual(status.get("state"), "ready", status)
        self.assertEqual(observed, [("FIN_US001", ["US001"])])

    def test_an_unknown_user_cannot_sign_in(self) -> None:
        client = self.TestClient(self.gapp.app, **LOOP)
        self.assertEqual(
            client.post("/api/signin", json={"oprid": "GHOST"}).status_code,
            403)

    def test_sign_out_ends_the_session(self) -> None:
        client = self._as("FIN_US001")
        self.assertEqual(client.get("/api/trial-balance").status_code, 200)
        client.post("/api/signout")
        self.assertEqual(client.get("/api/trial-balance").status_code, 401)

    def test_the_app_says_this_is_not_authentication(self) -> None:
        # Stated in the payload, not only in a comment: a form that looks
        # like a login while checking nothing teaches people it is one.
        client = self._as("FIN_US001")
        self.assertIs(client.get("/api/session").json()["is_authentication"],
                      False)
        self.assertIs(
            client.get("/api/meta").json()["security"]["is_authentication"],
            False)

    def test_wiki_and_diagnostics_are_not_gated_by_unit(self) -> None:
        # A policy question carries no business unit; refusing it because
        # the person has no ledger grant answers a question nobody asked.
        client = self._as("NOACCESS")
        self.assertNotIn(client.get("/api/wiki/health").status_code,
                         (401, 403))


if __name__ == "__main__":
    unittest.main()


class RowSamplingToolTests(unittest.TestCase):
    """A tool that returns ROWS needs a unit gate, whatever else it is for.

    profile_record and compare_records exist for structure discovery, so
    they read as catalog lookups — but their payloads carry `sample`, the
    first rows of whichever table was named, with no unit predicate and no
    argument that could carry one. A US001-only caller profiling PS_LEDGER
    received:

        sample units: ['EU001', 'US001']
        {"business_unit": "EU001", "account": "9999",
         "posted_total_amt": 8675309.0}

    The column masking in profiles.py is a different control: it hides bank
    and tax identifiers wherever they appear and says nothing about WHICH
    ROWS the caller may see.
    """

    ROW_SAMPLERS = ("profile_record", "compare_records")
    # Structure only — columns, indexes, record names. Never values. These
    # must stay available: taking them away costs a restricted user their
    # ability to find anything and protects nothing.
    STRUCTURE_ONLY = ("describe_table", "search_records", "search_metadata",
                      "get_metadata_context", "join_path")

    def setUp(self):
        from pstb.security import Access
        self.restricted = Access(oprid="US001USER",
                                 units=frozenset({"US001"}), all_units=False)
        self.unrestricted = Access(oprid="ADMIN", units=frozenset(),
                                   all_units=True)

    def test_row_samplers_are_refused_for_a_restricted_user(self):
        from pstb.guards import unit_access_block
        for tool in self.ROW_SAMPLERS:
            with self.subTest(tool=tool):
                denied = unit_access_block(
                    tool, {"table": "PS_LEDGER"}, self.restricted)
                self.assertTrue(
                    denied,
                    f"{tool} returns sample rows from any table and cannot "
                    "be bounded to a granted unit")

    def test_the_refusal_gives_the_true_reason_and_a_way_forward(self):
        from pstb.guards import unit_access_block
        denied = unit_access_block(
            "profile_record", {"table": "PS_LEDGER"}, self.restricted)
        self.assertNotIn(
            "arbitrary SQL", denied,
            "profile_record does not run arbitrary SQL; a refusal the "
            "reader can tell is wrong is one they learn to route around")
        self.assertIn("sample ROWS", denied)
        self.assertIn("describe_table", denied)

    def test_structure_only_discovery_stays_available(self):
        from pstb.guards import unit_access_block
        for tool in self.STRUCTURE_ONLY:
            with self.subTest(tool=tool):
                self.assertEqual(
                    unit_access_block(tool, {"table": "PS_LEDGER"},
                                      self.restricted), "",
                    f"{tool} returns no values, so gating it removes "
                    "discovery without protecting anything")

    def test_an_unrestricted_caller_is_unaffected(self):
        from pstb.guards import unit_access_block
        for tool in self.ROW_SAMPLERS + self.STRUCTURE_ONLY:
            with self.subTest(tool=tool):
                self.assertEqual(
                    unit_access_block(tool, {"table": "PS_LEDGER"},
                                      self.unrestricted), "")

    def test_every_row_sampler_is_declared_unscoped(self):
        """The invariant, not just the two names.

        A future tool that samples rows and takes no business_unit must be
        added to _UNSCOPED_DATA_TOOLS or it reopens this hole. Derive the
        candidates from the live tool signatures rather than a second
        hand-kept list.
        """
        import inspect

        from pstb import server
        from pstb.guards import _UNSCOPED_DATA_TOOLS, _TOOL_SCOPE_ARGS

        for name in self.ROW_SAMPLERS:
            handler = getattr(server, name, None)
            self.assertIsNotNone(handler, f"{name} is no longer a tool")
            self.assertNotIn(
                "business_unit", inspect.signature(handler).parameters,
                f"{name} now takes a business unit — gate it through "
                "_TOOL_SCOPE_ARGS instead of refusing it outright")
            self.assertIn(name, _UNSCOPED_DATA_TOOLS)
            # They ARE in _TOOL_SCOPE_ARGS, carrying the `source` binding
            # that keeps a /p2go silo on its own database. That is a
            # different axis; what must not appear is a business_unit
            # binding, which would imply an argument gate they cannot have.
            self.assertNotIn("business_unit", _TOOL_SCOPE_ARGS.get(name, {}))


class AbsentColumnVersusOutageTests(unittest.TestCase):
    """A missing column and a missing grant need OPPOSITE answers.

    On Oracle a table you lack SELECT on DESCRIBES AS ZERO COLUMNS rather
    than raising, so db.columns() cannot separate "this site has no
    ROWSECCLASS" from "this account cannot read PSOPRDEFN". Both readings
    of that ambiguity are fail-open in different directions:

      absence read as failure  -> _unavailable() -> all_units on a
                                  fail-open site, for a stable and
                                  legitimate schema shape
      failure read as absence  -> frozenset() -> an authoritative empty
                                  grant manufactured out of an outage

    Measured before the fix, on a site whose PSOPRDEFN view omits the
    column with on_unavailable=allow: main gave all_units=False units=[],
    the slice gave all_units=True source='unavailable'.

    So the catalog is not consulted. The class query runs; if it fails we
    ask whether PSOPRDEFN is readable at all, which is a question no
    describe can fudge.
    """

    def _sec(self, drop_column=False, oprdefn_unreadable=False):
        import shutil
        import sqlite3
        import tempfile
        from pathlib import Path

        from pstb.config import load_config
        from pstb.db import Database, DbError
        from pstb.security import RowSecurity

        root = Path(__file__).resolve().parents[1]
        sample = root / "sample_data" / "ps_sample.db"
        if not sample.exists():
            raise unittest.SkipTest("run scripts/seed_sample_data.py first")
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        shutil.copy(sample, d / "x.db")
        if drop_column:
            con = sqlite3.connect(d / "x.db")
            cols = [c[1] for c in con.execute("PRAGMA table_info(PSOPRDEFN)")]
            keep = [c for c in cols if c != "ROWSECCLASS"]
            con.execute("CREATE TABLE _t AS SELECT %s FROM PSOPRDEFN"
                        % ",".join(keep))
            con.execute("DROP TABLE PSOPRDEFN")
            con.execute("ALTER TABLE _t RENAME TO PSOPRDEFN")
            con.commit()
            con.close()
        (d / "config.yaml").write_text(
            f"db:\n  backend: sqlite\n  sqlite_path: {d / 'x.db'}\n"
            "defaults:\n  business_unit: US001\n  ledger: ACTUALS\n"
            "security:\n  enabled: true\n  unit_record: PS_SEC_BU_CLS\n"
            "  unit_key: ROWSECCLASS\n  on_unavailable: allow\n")
        cfg = load_config(str(d / "config.yaml"))
        sec = RowSecurity(Database(cfg), cfg)
        if oprdefn_unreadable:
            real = sec.db.query

            def query(sql, binds=None, **kw):
                if "PSOPRDEFN" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return real(sql, binds, **kw)

            sec.db.query = query
            # Oracle's shape for an ungranted table: describes as nothing.
            sec.db.columns = lambda rec: (
                set() if rec.upper().endswith("PSOPRDEFN") else [])
        return sec

    def test_an_absent_column_is_an_authoritative_empty_grant(self):
        access = self._sec(drop_column=True).access_for("FIN_US001")
        self.assertFalse(
            access.all_units,
            "a legitimate schema shape handed the user every business unit")
        self.assertEqual(access.source, "PS_SEC_BU_CLS")

    def test_an_unreadable_record_is_unavailable_not_an_empty_grant(self):
        access = self._sec(oprdefn_unreadable=True).access_for("FIN_US001")
        self.assertEqual(
            access.source, "unavailable",
            "an outage was reported as an authoritative empty grant")

    def test_the_normal_site_is_unaffected(self):
        access = self._sec().access_for("FIN_US001")
        self.assertEqual(access.source, "PS_SEC_BU_CLS")
        self.assertFalse(access.all_units)


class LiveSecuritySettingsTests(unittest.TestCase):
    """The console must not claim a restart for what it already applied.

    _console_reload rebuilds RowSecurity, so these three take effect the
    moment they are saved. Nothing else reads them — pstb/server.py never
    touches cfg.security — so there is no second live surface to disagree,
    which was the stated reason for the restart flag.
    """

    LIVE = ("security.enabled", "security.on_unavailable",
            "security.raw_sql_for_restricted")

    def test_security_settings_do_not_claim_a_restart(self):
        import pstb.settings as st
        for key in self.LIVE:
            with self.subTest(key=key):
                self.assertFalse(
                    st.BY_KEY[key].restart,
                    f"{key} is applied live by the console reload, so a "
                    "'restart to apply' pill on it is false")

    def test_settings_the_reload_does_not_touch_still_need_one(self):
        """Guard the guard: not a blanket removal of the restart flag."""
        import pstb.settings as st
        for key in ("llm.provider", "tools.max_rows"):
            with self.subTest(key=key):
                self.assertTrue(st.BY_KEY[key].restart)

    def test_the_reload_really_rebuilds_security(self):
        """The claim above is only true while this stays true."""
        import inspect

        from pstb.gui import app as gui
        source = inspect.getsource(gui._console_reload)
        self.assertIn("RowSecurity(new_db", source)
        self.assertIn("business-unit security", source)
