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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.guards import unit_access_block  # noqa: E402
from pstb.security import Access, RowSecurity, SecurityError  # noqa: E402

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

    def test_ALL_is_not_a_foreign_unit(self) -> None:
        self.assertEqual(unit_access_block(
            "get_top_billing_customers", {"business_unit": "ALL"},
            self.RESTRICTED), "")


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
