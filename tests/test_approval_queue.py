"""Approving a taught fact should not require a terminal.

Two governed queues existed and both were CLI-only: site facts taught in
conversation, and per-source metadata-meaning proposals. Neither reaches an
answer until a human approves it, which makes the approval step part of the
product rather than an admin chore — and a queue you can only empty over SSH
is a queue that does not get emptied. There were four pending facts on the
development machine, none decided, from 2026-08-08.

The gate is the same one the question-log diagnostics already use:
machine-local (an SSH tunnel arrives as loopback) AND, when row security is
on, a configured privileged operator. Approving is a governance action, not
something a shared-VPN reader should reach.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.testclient import TestClient

from pstb.gui import app as gui
from pstb.config import load_config
from pstb.memory import SiteMemory
from pstb.security import Access


def _js_function(page: str, name: str) -> str:
    """The whole body of one shipped JS function, by brace matching.

    These checks used to slice a fixed number of bytes from the function's
    opening line. That silently stops testing the moment the function
    grows past the window -- which is exactly what happened when
    loadApprovals gained a `source` argument and a few comment lines: the
    slice ended mid-word at "esc(errTex" and the assertion it was carrying
    went from proving something to being unable to see it.
    """
    start = page.index(f"function {name}(")
    open_brace = page.index("{", start)
    depth, i = 0, open_brace
    while i < len(page):
        if page[i] == "{":
            depth += 1
        elif page[i] == "}":
            depth -= 1
            if depth == 0:
                return page[start:i + 1]
        i += 1
    raise AssertionError(f"{name} is not brace-balanced in the shipped page")


def _client():
    # Loopback + a real Host header: the app refuses anything else before a
    # handler is reached, which is a different control and has its own tests.
    return TestClient(gui.app, client=("127.0.0.1", 5555),
                      base_url="http://localhost")


class ApprovalQueueTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "site_memory.json"
        memory = SiteMemory(self.path)
        self.first = memory.propose("PS_TU_FILE_INTFC: holds inbound interface files",
                                    kind="record")["fact"]["id"]
        self.second = memory.propose("PS_TU_FILE_INTFC: the interface configures files",
                                     kind="record")["fact"]["id"]
        self._patch = patch.object(gui, "_site_memory",
                                   lambda: SiteMemory(self.path))
        self._patch.start()
        self.client = _client()

    def tearDown(self):
        self._patch.stop()
        self._dir.cleanup()

    def _counts(self):
        return json.loads(self.path.read_text()) and SiteMemory(
            self.path).list_facts()["counts"]

    # ------------------------------------------------------------ listing
    def test_pending_items_are_listed_with_what_a_decision_needs(self):
        body = self.client.get("/api/approvals").json()
        self.assertEqual(body["pending_total"], 2)
        item = next(i for i in body["items"] if i["id"] == self.first)
        for field in ("queue", "id", "text", "subject", "origin", "status"):
            self.assertIn(field, item)
        self.assertEqual(item["queue"], "memory")
        self.assertEqual(item["status"], "pending")

    def test_status_filter_is_validated(self):
        self.assertEqual(self.client.get("/api/approvals?status=all").status_code,
                         200)
        bad = self.client.get("/api/approvals?status=whenever")
        self.assertEqual(bad.status_code, 400)

    # ----------------------------------------------------------- deciding
    def test_approve_and_reject_persist(self):
        ok = self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.first, "decision": "approve"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["status"], "approved")
        no = self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.second, "decision": "reject"})
        self.assertEqual(no.json()["status"], "rejected")
        self.assertEqual(self._counts(),
                         {"approved": 1, "pending": 0, "rejected": 1})

    def test_a_decision_records_who_made_it(self):
        body = self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.first, "decision": "approve"}).json()
        self.assertTrue(body["decided_by"],
                        "an approval with no attribution is not an audit trail")
        stored = next(f for f in SiteMemory(self.path).list_facts()["facts"]
                      if f["id"] == self.first)
        self.assertEqual(stored["decided_by"], body["decided_by"])

    def test_only_an_approved_fact_becomes_active(self):
        """The whole point: rejecting must not leave it usable."""
        self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.second, "decision": "reject"})
        approved = SiteMemory(self.path).approved()
        self.assertEqual([f["id"] for f in approved], [])

    def test_bad_input_is_refused_by_name(self):
        for payload, expected in (
            ({"queue": "memory", "id": "nope", "decision": "approve"}, 404),
            ({"queue": "memory", "id": self.first, "decision": "maybe"}, 400),
            ({"queue": "memory", "decision": "approve"}, 400),
            ({"queue": "source_knowledge", "id": "x",
              "decision": "approve"}, 400),
            ({"queue": "invented", "id": "x", "decision": "approve"}, 400),
        ):
            with self.subTest(payload=payload):
                r = self.client.post("/api/approvals/decide", json=payload)
                self.assertEqual(r.status_code, expected)
                self.assertTrue(str(r.json().get("detail") or "").strip(),
                                "a refusal with no reason is a dead end")

    def test_an_active_source_exclusion_can_be_restored_from_the_panel(self):
        calls = []

        def revoke(item_id, *, decided_by):
            calls.append((item_id, decided_by))
            return {"id": item_id, "status": "revoked"}

        registry = SimpleNamespace(
            names=lambda: ["default"],
            resolve_name=lambda source="": "default")
        store = SimpleNamespace(revoke=revoke)
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_approval_source_names",
                             return_value=["default"]), \
                patch.object(gui, "_source_knowledge_store",
                             return_value=store):
            out = self.client.post("/api/approvals/decide", json={
                "queue": "source_knowledge", "source": "default",
                "id": "0123456789abcdef", "decision": "revoke",
            })
        self.assertEqual(out.status_code, 200, out.text)
        self.assertEqual(out.json()["status"], "revoked")
        self.assertEqual(calls[0][0], "0123456789abcdef")

    # -------------------------------------------------------------- gate
    def test_both_endpoints_require_the_operator_gate(self):
        """Same gate as the question log: not reachable from a shared VPN."""
        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")

        with patch.object(gui, "_require_approval_operator", refuse):
            self.assertEqual(self.client.get("/api/approvals").status_code, 403)
            self.assertEqual(
                self.client.post("/api/approvals/decide", json={
                    "queue": "memory", "id": self.first,
                    "decision": "approve"}).status_code, 403)
        self.assertEqual(self._counts()["pending"], 2,
                         "a refused request still changed the queue")

    def test_approval_routes_do_not_require_a_finance_unit(self):
        """Governance applies to P2Go even when the operator has no BU rows."""
        self.assertFalse(gui._needs_unit_check("/api/approvals"))
        self.assertFalse(gui._needs_unit_check("/api/approvals/count"))
        self.assertFalse(gui._needs_unit_check("/api/approvals/decide"))

    def test_configured_privileged_user_bypasses_bu_tables(self):
        """BATCH1 is config-authoritative; it must not need a security row."""
        gui.row_security.invalidate("BATCH1")
        try:
            with patch.object(gui.cfg.security, "enabled", True), \
                    patch.object(gui.cfg.security, "privileged_users",
                                 ["BATCH1"]), \
                    patch.object(gui, "_approval_source_names",
                                 return_value=[]), \
                    patch.object(gui.row_security, "_operator_exists",
                                 side_effect=AssertionError(
                                     "privileged sign-in queried PSOPRDEFN")), \
                    patch.object(gui.row_security, "source_record",
                                 side_effect=AssertionError(
                                     "privileged sign-in queried BU security")):
                client = _client()
                signed = client.post("/api/signin", json={"oprid": "BATCH1"})
                self.assertEqual(signed.status_code, 200)
                self.assertTrue(signed.json()["privileged"])
                self.assertTrue(signed.json()["all_units"])
                meta = client.get("/api/meta").json()["security"]
                self.assertTrue(meta["privileged"])
                self.assertTrue(meta["approval_peer_loopback"])
                self.assertTrue(meta["approval_review_ready"])
                self.assertEqual(client.get("/api/approvals").status_code, 200)
        finally:
            gui.row_security.invalidate("BATCH1")

    def test_security_disabled_needs_no_signin_but_stays_machine_local(self):
        with patch.object(gui.cfg.security, "enabled", False), \
                patch.object(gui, "_approval_source_names", return_value=[]):
            self.assertEqual(_client().get("/api/approvals").status_code, 200)


class UnauthenticatedRemoteApprovalTests(unittest.TestCase):
    """The requested BATCH1 testing escape hatch is narrow and explicit."""

    HOST = "pfs1app2.internal"

    class Store:
        def __init__(self):
            self.row = {
                "proposal_id": "0123456789abcdef",
                "source_database": "default",
                "source_fingerprint": "sha256:" + "a" * 64,
                "object_id": "object:default:P2GO:JOB_HDR",
                "schema": "P2GO", "object": "JOB_HDR", "kind": "table",
                "meaning": "Inbound integration job headers",
                "aliases": ["job queue"], "origin": "gui",
                "proposed_at": "2026-08-19T10:00:00+00:00",
                "status": "pending", "decided_by": "",
            }
            self.decisions = []

        def list_proposals(self, status=""):
            if status and self.row["status"] != status:
                return []
            return [dict(self.row)]

        def get(self, proposal_id):
            if proposal_id != self.row["proposal_id"]:
                raise RuntimeError("not found")
            return dict(self.row)

        def decide(self, proposal_id, *, approve, decided_by,
                   current_object=None):
            if proposal_id != self.row["proposal_id"]:
                raise RuntimeError("not found")
            self.row["status"] = "approved" if approve else "rejected"
            self.row["decided_by"] = decided_by
            self.decisions.append((approve, decided_by, current_object))
            return dict(self.row)

    def setUp(self):
        self.store = self.Store()
        gui.row_security.invalidate()

    def tearDown(self):
        gui.row_security.invalidate()

    def _policy(self, *, narrowed=False, token=""):
        hosts = (frozenset(gui.localguard.ALLOWED_HOSTS | {self.HOST})
                 if narrowed else None)
        return gui.localguard.Policy(
            hosts=hosts, token=token, shared=True,
            unauthenticated=not bool(token))

    def _patches(self, *, enabled=True, privileged=("BATCH1",),
                 unsafe=True, narrowed=False, token=""):
        stack = ExitStack()
        stack.enter_context(patch.object(gui.cfg.security, "enabled", enabled))
        stack.enter_context(patch.object(
            gui.cfg.security, "privileged_users", list(privileged)))
        stack.enter_context(patch.object(
            gui.cfg.security, "allow_unauthenticated_remote_approvals",
            unsafe))
        stack.enter_context(patch.object(
            gui.localguard, "POLICY",
            self._policy(narrowed=narrowed, token=token)))
        stack.enter_context(patch.object(
            gui, "_source_knowledge_store", return_value=self.store))
        stack.enter_context(patch.object(
            gui, "_source_catalog_identity", return_value={"proved": True}))
        return stack

    def _client(self):
        return TestClient(gui.app, client=("10.4.1.9", 51000),
                          base_url=f"http://{self.HOST}")

    def _signin(self, client, who="BATCH1"):
        return client.post("/api/signin", json={"oprid": who})

    def _decision_headers(self, *, origin=None, marker=True,
                          content_type="application/json"):
        headers = {"Origin": origin or f"http://{self.HOST}",
                   "Content-Type": content_type}
        if marker:
            headers["X-PSTB-Approval-Request"] = "metadata-review"
        return headers

    def test_configured_batch1_can_list_and_decide_without_bu_queries(self):
        with self._patches(), \
                patch.object(gui.row_security, "_operator_exists",
                             side_effect=AssertionError("queried PSOPRDEFN")), \
                patch.object(gui.row_security, "source_record",
                             side_effect=AssertionError("queried BU security")):
            client = self._client()
            signed = self._signin(client)
            self.assertEqual(signed.status_code, 200)
            self.assertTrue(signed.json()["privileged"])
            listing = client.get(
                "/api/approvals?status=all&source=default")
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.headers["cache-control"],
                             "no-store, private")
            self.assertEqual([i["queue"] for i in listing.json()["items"]],
                             ["source_knowledge"])
            decided = client.post(
                "/api/approvals/decide",
                json={"queue": "source_knowledge",
                      "source": "default", "id": self.store.row["proposal_id"],
                      "decision": "approve"},
                headers=self._decision_headers())
            self.assertEqual(decided.status_code, 200, decided.text)
            self.assertIn("unverified remote selector",
                          decided.json()["decided_by"])
            self.assertEqual(self.store.row["status"], "approved")

    def test_default_off_and_security_off_modes_refuse(self):
        for label, options, expected in (
            ("off", {"unsafe": False}, "option is off"),
            ("security off", {"enabled": False}, "security.enabled"),
        ):
            with self.subTest(label=label), self._patches(**options):
                client = self._client()
                if options.get("enabled", True):
                    self.assertEqual(self._signin(client).status_code, 200)
                else:
                    client.cookies.set(gui.USER_COOKIE, "BATCH1")
                response = client.get(
                    "/api/approvals?status=all&source=default")
                self.assertEqual(response.status_code, 403)
                self.assertIn(expected, str(response.json()).lower())
                self.assertEqual(self.store.row["status"], "pending")

    def test_missing_or_nonprivileged_selector_is_refused(self):
        with self._patches():
            client = self._client()
            missing = client.get(
                "/api/approvals?status=all&source=default")
            self.assertEqual(missing.status_code, 401)
        with self._patches(privileged=("BATCH1",)), \
                patch.object(gui, "access_for_request",
                             return_value=Access(oprid="OTHER",
                                                 privileged=False)):
            client = self._client()
            client.cookies.set(gui.USER_COOKIE, "OTHER")
            denied = client.get(
                "/api/approvals?status=all&source=default")
            self.assertEqual(denied.status_code, 403)
            self.assertIn("security.privileged_users", denied.text)

    def test_remote_post_requires_same_origin_json_and_gui_marker(self):
        with self._patches():
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            body = {"queue": "source_knowledge", "source": "default",
                    "id": self.store.row["proposal_id"],
                    "decision": "approve"}
            cases = (
                (self._decision_headers(marker=False), 403),
                (self._decision_headers(origin="http://evil.example"), 403),
                (self._decision_headers(origin=f"https://{self.HOST}"), 403),
                (self._decision_headers(content_type="text/plain"), 415),
            )
            for headers, expected in cases:
                with self.subTest(headers=headers):
                    result = client.post("/api/approvals/decide",
                                         content=json.dumps(body),
                                         headers=headers)
                    self.assertEqual(result.status_code, expected)
                    self.assertEqual(self.store.row["status"], "pending")

    def test_config_reload_cannot_reclassify_an_admitted_remote_request(self):
        """One gate decision governs filtering, CSRF, audit, and mutation."""
        with self._patches(), patch.object(
                gui, "_unauthenticated_remote_approvals_active",
                side_effect=[True, False, False]) as active:
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            listing = client.get(
                "/api/approvals?status=all&source=default")
            self.assertEqual(listing.status_code, 200)
            self.assertEqual([i["queue"] for i in listing.json()["items"]],
                             ["source_knowledge"])
            self.assertEqual(active.call_count, 1,
                             "testing switch was re-read after admission")

        self.store.row["status"] = "pending"
        with self._patches(), patch.object(
                gui, "_unauthenticated_remote_approvals_active",
                side_effect=[True, False, False]) as active:
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            decided = client.post(
                "/api/approvals/decide",
                json={"queue": "source_knowledge", "source": "default",
                      "id": self.store.row["proposal_id"],
                      "decision": "approve"},
                headers=self._decision_headers())
            self.assertEqual(decided.status_code, 200)
            self.assertIn("unverified remote selector",
                          decided.json()["decided_by"])
            self.assertEqual(active.call_count, 1,
                             "testing switch was re-read after admission")

    def test_remote_mode_does_not_open_site_memory_or_question_diagnostics(self):
        with self._patches():
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            memory = client.post(
                "/api/approvals/decide",
                json={"queue": "memory", "id": "anything",
                      "decision": "approve"},
                headers=self._decision_headers())
            self.assertEqual(memory.status_code, 403)
            self.assertEqual(client.get("/api/question-report").status_code,
                             403)

    def test_signout_immediately_removes_remote_approval_access(self):
        with self._patches():
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            self.assertEqual(client.get(
                "/api/approvals?status=all&source=default").status_code, 200)
            self.assertEqual(client.post("/api/signout").status_code, 200)
            self.assertEqual(client.get(
                "/api/approvals?status=all&source=default").status_code, 401)

    def test_unsafe_flag_never_bypasses_a_configured_access_token(self):
        token = "team-token-123456"
        with self._patches(narrowed=False, token=token):
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 401)
            first = client.get(f"/?token={token}")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(self._signin(client).status_code, 200)
            self.assertEqual(client.get(
                "/api/approvals?status=all&source=default").status_code, 200)

    def test_forwarded_header_is_rejected_before_any_oprid_resolution(self):
        with self._patches(), patch.object(
                gui, "access_for_request",
                side_effect=AssertionError("resolved identity before network")):
            client = self._client()
            client.cookies.set(gui.USER_COOKIE, "BATCH1")
            response = client.get(
                "/api/approvals?status=all&source=default",
                headers={"X-Forwarded-For": "127.0.0.1"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("x-forwarded-for", response.text.lower())

    def test_meta_discloses_unverified_testing_readiness(self):
        with self._patches():
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            security = client.get("/api/meta").json()["security"]
            self.assertTrue(security["approval_review_ready"])
            self.assertTrue(
                security["approval_unauthenticated_remote_configured"])
            self.assertTrue(security["approval_unauthenticated_remote_active"])
            self.assertFalse(security["approval_identity_verified"])
            self.assertEqual(
                security["approval_unauthenticated_remote_expires_at"], "")

    def test_testing_mode_accepts_open_host_policy_without_extra_cli_flag(self):
        with self._patches(narrowed=False):
            client = self._client()
            self.assertEqual(self._signin(client).status_code, 200)
            self.assertEqual(client.get(
                "/api/approvals?status=all&source=default").status_code, 200)


class UnauthenticatedRemoteApprovalConfigTests(unittest.TestCase):
    def _load(self, security_yaml: str):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            path.write_text("security:\n" + security_yaml)
            return load_config(str(path))

    def test_switch_must_be_an_exact_yaml_boolean(self):
        with self.assertRaisesRegex(RuntimeError, "YAML boolean"):
            self._load(
                "  enabled: true\n"
                "  privileged_users: [BATCH1]\n"
                "  allow_unauthenticated_remote_approvals: \"false\"\n")

    def test_switch_requires_security_and_a_privileged_list(self):
        for yaml_text, reason in (
            ("  enabled: false\n"
             "  privileged_users: [BATCH1]\n"
             "  allow_unauthenticated_remote_approvals: true\n",
             "security.enabled"),
            ("  enabled: true\n"
             "  privileged_users: []\n"
             "  allow_unauthenticated_remote_approvals: true\n",
             "privileged_users"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(RuntimeError, reason):
                    self._load(yaml_text)

    def test_privileged_ids_cannot_be_yaml_booleans_or_numbers(self):
        for value in ("true", "123"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "text PeopleSoft"):
                    self._load(
                        "  enabled: true\n"
                        f"  privileged_users: [{value}]\n"
                        "  allow_unauthenticated_remote_approvals: true\n")

    def test_valid_batch1_configuration_loads(self):
        loaded = self._load(
            "  enabled: true\n"
            "  privileged_users: [BATCH1]\n"
            "  allow_unauthenticated_remote_approvals: true\n")
        self.assertTrue(
            loaded.security.allow_unauthenticated_remote_approvals)


class DirectMetadataProposalTests(unittest.TestCase):
    """The visible P2Go form submits an exact inactive proposal directly."""

    def _resources(self):
        calls = []

        def propose(**kwargs):
            calls.append(kwargs)
            return {
                "id": "0123456789abcdef", "status": "pending",
                "source_database": "p2go", "schema": "P2GO",
                "object": "JOB_HDR", "meaning": kwargs["meaning"],
                "aliases": kwargs["aliases"], "already_known": False,
            }

        store = SimpleNamespace(propose=propose)
        catalog = SimpleNamespace(context=lambda identifier, source, limit: {
            "found": True, "source_database": source,
            "subject": {
                "source": source, "object_id": "object:p2go:P2GO:JOB_HDR",
                "schema": "P2GO", "physical_object": "JOB_HDR",
                "kind": "table",
            },
        })
        return calls, store, catalog

    def test_form_submission_is_route_bound_exact_and_pending(self):
        calls, store, catalog = self._resources()
        with patch.object(
                gui, "_metadata_proposal_resources",
                return_value=("p2go", store, catalog)) as resources, \
                patch("pstb.source_knowledge.validate_catalog_aliases",
                      return_value=["job queue"]) as aliases:
            body = gui.create_metadata_proposal("p2go", {
                "identifier": "P2GO.JOB_HDR",
                "meaning": "Inbound integration job headers",
                "aliases": "job queue",
            })
        resources.assert_called_once_with("p2go")
        aliases.assert_called_once_with(
            catalog, "p2go", "object:p2go:P2GO:JOB_HDR", ["job queue"])
        self.assertEqual(calls, [{
            "object_id": "object:p2go:P2GO:JOB_HDR", "schema": "P2GO",
            "object_name": "JOB_HDR", "object_kind": "table",
            "meaning": "Inbound integration job headers",
            "aliases": ["job queue"], "origin": "gui",
            "selection": "prefer",
        }])
        self.assertFalse(body["retrieval_active"])
        self.assertEqual(body["proposal"]["status"], "pending")
        self.assertIn("inactive", body["note"])

    def test_form_can_submit_a_hard_exclusion_without_aliases(self):
        calls, store, catalog = self._resources()
        with patch.object(
                gui, "_metadata_proposal_resources",
                return_value=("p2go", store, catalog)), patch(
                "pstb.source_knowledge.validate_catalog_aliases",
                return_value=[]):
            gui.create_metadata_proposal("p2go", {
                "identifier": "P2GO.JOB_HDR",
                "meaning": "Obsolete scratch copy",
                "aliases": "",
                "selection": "exclude",
            })
        self.assertEqual(calls[0]["selection"], "exclude")
        self.assertEqual(calls[0]["aliases"], [])

    def test_body_cannot_override_route_source_or_catalog_identity(self):
        for key in ("source", "db", "object_id", "source_fingerprint"):
            with self.subTest(key=key), self.assertRaises(HTTPException) as caught:
                gui.create_metadata_proposal("p2go", {
                    "identifier": "P2GO.JOB_HDR", "meaning": "Job headers",
                    "aliases": "", key: "untrusted",
                })
            self.assertEqual(caught.exception.status_code, 400)

    def test_proposal_route_is_unit_free_and_body_bounded(self):
        self.assertFalse(gui._needs_unit_check(
            "/api/source/p2go/metadata-proposals"))
        self.assertFalse(gui._needs_unit_check(
            "/api/source/finance/metadata-proposals"))
        with patch.object(gui.cfg.security, "enabled", False):
            oversized = _client().post(
                "/api/source/p2go/metadata-proposals",
                content=b"{" + b" " * (9 * 1024) + b"}",
                headers={"content-type": "application/json"})
        self.assertEqual(oversized.status_code, 413)


class ApprovalPanelTests(unittest.TestCase):
    """The browser half, checked against the shipped file."""

    @classmethod
    def setUpClass(cls):
        cls.page = (Path(gui.__file__).parent / "static" / "index.html").read_text()

    def test_the_panel_posts_with_a_content_type(self):
        """This FastAPI enforces it; without the header the decide 422s.

        Found by clicking the button in a browser — the request reached the
        error path and the row showed a badge instead of the decision.
        """
        block = self.page[self.page.index("/api/approvals/decide"):][:400]
        self.assertIn("Content-Type", block)
        self.assertIn("application/json", block)

    def test_an_error_object_is_not_rendered_as_object_Object(self):
        """FastAPI's 422 detail is a list, not a string.

        The first cut interpolated e.message straight into the badge, so the
        one place a reader most needs the reason showed "[object Object]".
        """
        self.assertIn("function errText(e)", self.page)
        self.assertIn("errText(e)", self.page)

    def test_the_panel_replaces_itself_rather_than_nesting(self):
        """A refresh that appends inside the old panel leaves a stale count.

        Observed: the decision reached the server and the heading above it
        still read the previous number of pending items.
        """
        block = _js_function(self.page, "loadApprovals")
        self.assertIn("existing.replaceWith(box)", block,
                      "replaceWith keeps the panel's position; remove()+append "
                      "sent it below cards added after the first load")
        self.assertIn("else holder.append(box)", block,
                      "appending is the first-load fallback only")

    def test_exclusion_controls_are_explicit_and_reversible(self):
        proposal = _js_function(self.page, "metadataProposalPanel")
        approvals = self.page[self.page.index("function renderApprovals("):
                              self.page.index("async function loadApprovals(")]
        self.assertIn("Exclude from answers", proposal)
        self.assertIn("selection:controls.selection.value", proposal)
        self.assertIn("Restore as a candidate", approvals)


class ApprovalBadgeCountTests(unittest.TestCase):
    """The count endpoint deliberately skips the operator gate.

    #157 shipped the panel and the report back was "I still cannot see an
    approval link". The panel lives behind a machine-local gate, so an
    operator reading the app over the VPN got no sign the queue existed --
    four facts sat undecided for eleven days because no screen mentioned
    them. A badge cannot prompt anyone to open a tunnel if the badge is
    itself behind the tunnel.

    So /api/approvals/count is ungated where /api/approvals is not. That is
    a deliberate hole in a security boundary and it needs pinning from both
    sides: it must stay reachable when the operator gate refuses, and it
    must never carry anything but the integer.
    """

    SECRET = "PS_TU_SECRET_INTFC: the vendor bank routing record"

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "site_memory.json"
        memory = SiteMemory(self.path)
        self.only = memory.propose(self.SECRET, kind="record")["fact"]["id"]
        self._patch = patch.object(gui, "_site_memory",
                                   lambda: SiteMemory(self.path))
        self._patch.start()
        self.client = _client()

    def tearDown(self):
        self._patch.stop()
        self._dir.cleanup()

    def _refuse_operator(self):
        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")
        return patch.object(gui, "_require_question_log_operator", refuse)

    def test_the_count_survives_the_gate_that_hides_the_queue(self):
        """The entire reason this endpoint exists separately."""
        with self._refuse_operator():
            self.assertEqual(self.client.get("/api/approvals").status_code, 403,
                             "precondition: the queue itself must be gated")
            r = self.client.get("/api/approvals/count")
            self.assertEqual(r.status_code, 200,
                             "a badge behind the tunnel cannot advertise the "
                             "tunnel")
            self.assertEqual(r.json(), {"pending": 1, "readable": True})

    def test_the_count_carries_a_number_and_nothing_else(self):
        """What crosses the gate is one integer -- no content, ever."""
        raw = self.client.get("/api/approvals/count").text
        self.assertNotIn("SECRET_INTFC", raw)
        self.assertNotIn("bank routing", raw)
        self.assertNotIn(self.only, raw)
        self.assertEqual(set(self.client.get("/api/approvals/count").json()),
                         {"pending", "readable"},
                         "a new key here is a new thing crossing the gate")

    def test_count_includes_each_configured_source_knowledge_queue(self):
        registry = SimpleNamespace(names=lambda: ["default", "p2go", "ops"])
        stores = {
            "default": SimpleNamespace(list_proposals=lambda status: [
                {"id": "f1", "status": status},
            ]),
            "p2go": SimpleNamespace(list_proposals=lambda status: [
                {"id": "p1", "status": status},
                {"id": "p2", "status": status},
            ]),
            "ops": SimpleNamespace(list_proposals=lambda status: [
                {"id": "o1", "status": status},
            ]),
        }
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             side_effect=lambda name: stores[name]):
            body = self.client.get("/api/approvals/count").json()
        self.assertEqual(body, {"pending": 5, "readable": True})

    def test_listing_includes_default_and_p2go_metadata_queues(self):
        registry = SimpleNamespace(names=lambda: ["default", "p2go"])
        stores = {
            name: SimpleNamespace(list_proposals=lambda status, name=name: [{
                "id": ("f1" if name == "default" else "p1"),
                "meaning": name + " meaning", "status": "pending",
            }]) for name in ("default", "p2go")
        }
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             side_effect=lambda name: stores[name]):
            body = self.client.get("/api/approvals").json()
        metadata = [item for item in body["items"]
                    if item["queue"] == "source_knowledge"]
        self.assertEqual({item["source"] for item in metadata},
                         {"default", "p2go"})
        self.assertTrue(body["readable"])

    def test_listing_uses_recognizable_physical_names_and_aliases(self):
        registry = SimpleNamespace(names=lambda: ["p2go"])
        store = SimpleNamespace(list_proposals=lambda status: [{
            "id": "p1", "meaning": "Inbound integration jobs",
            "schema": "P2GO", "object": "JOB_HDR",
            "object_id": "object:p2go:P2GO:JOB_HDR",
            "aliases": ["job queue", "inbound jobs"],
            "status": "pending",
        }])
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             return_value=store):
            item = next(i for i in self.client.get("/api/approvals").json()[
                "items"] if i["queue"] == "source_knowledge")
        self.assertEqual(item["subject"], "P2GO.JOB_HDR")
        self.assertEqual(item["schema"], "P2GO")
        self.assertEqual(item["object"], "JOB_HDR")
        self.assertEqual(item["aliases"], ["job queue", "inbound jobs"])

    def test_listing_discloses_an_unreadable_source_queue(self):
        registry = SimpleNamespace(names=lambda: ["default"])
        private = "/shared/secret/source_knowledge.db: ORA-01017"
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             side_effect=OSError(private)):
            body = self.client.get("/api/approvals").json()
        self.assertFalse(body["readable"])
        self.assertEqual(body["source_errors"][0]["source"], "default")
        self.assertEqual(body["source_errors"][0]["error"],
                         "proposal queue could not be read")
        self.assertNotIn(private, json.dumps(body))

    def test_one_unreadable_source_does_not_blank_the_badge(self):
        """An unreadable source is skipped, not treated as unknowable.

        This replaces an assertion that a partial total reports
        readable=False. The intent behind it was right -- do not present an
        incomplete count as authoritative -- but readable=False does not
        render as "partial", it renders as NOTHING:
        index.html does `n = readable ? pending : 0; dot.hidden = !n`. So a
        single broken sidecar hid the badge while a site fact sat genuinely
        waiting, which is the exact invisibility the badge was added to
        remove.

        An undercount still says "there is something here", which is the
        badge's whole job, and the operator who opens the panel sees the
        source named in `source_errors` straight away. readable=False stays
        reserved for the site-memory read, the one failure that leaves
        nothing countable at all.
        """
        registry = SimpleNamespace(names=lambda: ["default", "p2go"])
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             side_effect=OSError("knowledge store unavailable")):
            body = self.client.get("/api/approvals/count").json()
        self.assertEqual(body, {"pending": 1, "readable": True})
        self.assertEqual(set(body), {"pending", "readable"},
                         "nothing but an integer crosses this ungated route")

    def test_the_panel_still_names_a_source_it_could_not_read(self):
        """The other half of the trade above: the badge undercounts
        silently, so the gated listing must say what it could not see."""
        registry = SimpleNamespace(names=lambda: ["default", "p2go"])
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             side_effect=OSError("knowledge store unavailable")):
            body = self.client.get("/api/approvals").json()
        self.assertEqual(
            sorted(e["source"] for e in body.get("source_errors") or []),
            ["default", "p2go"])

    def test_the_count_route_is_not_in_the_open_paths_set(self):
        """What actually keeps it non-public.

        Sign-in is enforced by _row_security_guard for every /api/ path
        outside _OPEN_PATHS. Adding this route there -- an easy thing to
        reach for, since the badge is meant to be widely visible -- would
        make the count reachable with no session at all.
        """
        self.assertNotIn("/api/approvals/count", gui._OPEN_PATHS)

    def test_ungated_does_not_mean_public(self):
        """Skipping the operator gate must not skip signing in."""
        def not_signed_in(_request):
            raise HTTPException(status_code=401, detail="sign in")
        with patch.object(gui.cfg.security, "enabled", True), \
                patch.object(gui, "access_for_request", not_signed_in):
            self.assertEqual(
                self.client.get("/api/approvals/count").status_code, 401)

    def test_an_unreadable_queue_shows_nothing_not_an_error(self):
        """A badge is an affordance. A broken one must not become a card."""
        def boom():
            raise RuntimeError("site memory is on a disk that went away")
        with patch.object(gui, "_site_memory", boom):
            r = self.client.get("/api/approvals/count")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"pending": 0, "readable": False})

    def test_a_decided_item_stops_being_counted(self):
        self.client.post("/api/approvals/decide", json={
            "queue": "memory", "id": self.only, "decision": "reject"})
        self.assertEqual(
            self.client.get("/api/approvals/count").json()["pending"], 0)


class RefusalIsActionableTests(unittest.TestCase):
    """"Use an SSH tunnel" is a direction, not a remedy.

    The reader still had to work out the port and the flag order. The
    refusal now prints the line they can paste, using the port this
    process is really serving on rather than a placeholder.
    """

    def test_the_hint_quotes_the_port_actually_being_served(self):
        with patch.object(gui, "_SERVED_PORT", 8642):
            hint = gui.tunnel_hint()
        self.assertIn("8642", hint)
        self.assertIn("ssh -L", hint)
        self.assertNotIn("8000", hint,
                         "a hardcoded default is the bug, not the fix")

    def test_main_binds_the_hint_to_the_real_port(self):
        """A hint that lies is worse than no hint."""
        src = Path(gui.__file__).read_text()
        body = src[src.index("def main()"):]
        self.assertIn("_SERVED_PORT = int(args.port)", body)

    def test_the_operator_refusal_hands_over_a_pasteable_command(self):
        """The gate in front of the queue, refusing a non-loopback peer."""
        request = SimpleNamespace(scope={"client": ("10.4.1.9", 51000)})
        with patch.object(gui, "_SERVED_PORT", 8642):
            with self.assertRaises(HTTPException) as caught:
                gui._require_question_log_operator(request)
        detail = str(caught.exception.detail)
        self.assertIn("ssh -L 8642:localhost:8642", detail)
        self.assertIn("http://localhost:8642", detail)

    def test_the_loopback_refusal_names_the_port_it_is_serving(self):
        """The outer middleware had its own copy, hardcoded to 8000.

        The CLI has defaulted to 8016 since it grew a --port flag, so the
        one line a locked-out reader was told to paste could not have
        worked on any default deployment. It now reads the bound port off
        the ASGI scope.
        """
        client = TestClient(gui.app, client=("10.4.1.9", 51000),
                            base_url="http://localhost:8642")
        r = client.get("/api/approvals")
        self.assertEqual(r.status_code, 403)
        reason = str(r.json().get("error") or "")
        self.assertIn("ssh -L 8642:localhost:8642", reason)
        self.assertNotIn("8000", reason)

    def test_one_formatter_words_the_remedy(self):
        """Three copies of this sentence had already drifted apart."""
        self.assertEqual(gui.localguard.tunnel_command(8642),
                         "ssh -L 8642:localhost:8642 <this-host>")
        app_src = Path(gui.__file__).read_text()
        guard_src = Path(gui.localguard.__file__).read_text()
        self.assertEqual(app_src.count("ssh -L"), 1,
                         "main()'s startup banner is the only other one")
        self.assertEqual(guard_src.count("ssh -L"), 1,
                         "the formatter itself")

    def test_the_fallback_port_matches_what_the_cli_actually_defaults_to(self):
        app_src = Path(gui.__file__).read_text()
        flag = app_src[app_src.index('ap.add_argument("--port"'):][:160]
        self.assertIn(f"default={gui.localguard.DEFAULT_PORT}", flag)


class ApprovalDiscoverabilityPanelTests(unittest.TestCase):
    """The browser half, checked against the shipped file."""

    @classmethod
    def setUpClass(cls):
        cls.page = (Path(gui.__file__).parent / "static"
                    / "index.html").read_text()

    def test_the_nav_carries_the_badge(self):
        nav = self.page[self.page.index('data-v="diag"'):][:200]
        self.assertIn('id="approvalbadge"', nav,
                      "the count has to appear where the operator is looking")

    def test_chat_chrome_has_a_direct_metadata_approval_button(self):
        """Ask is the visible product; nav is intentionally display:none."""
        self.assertIn("nav{display:none}", self.page)
        start = self.page.index("function viewChat()")
        block = self.page[start:start + 5000]
        self.assertIn("Metadata meanings", block)
        self.assertIn("id='chat-approvals'", block)
        self.assertIn("openApprovals(silo.source)", block)
        self.assertIn("data-approval-badge", block)

    def test_metadata_drawer_has_a_source_bound_direct_form(self):
        start = self.page.index("function metadataProposalPanel(")
        end = self.page.index("async function viewDiag()", start)
        block = self.page[start:end]
        self.assertIn("Exact schema.object", block)
        self.assertIn("Short business meaning", block)
        self.assertIn("Business aliases", block)
        self.assertIn("Submit meaning for review", block)
        self.assertIn("Exclude from answers", block)
        self.assertIn("not sent through the chat model", block)
        self.assertIn("metadataProposalUrl(source)", block)
        self.assertIn("/metadata-proposals", self.page)

    def test_direct_approval_drawer_does_not_run_finance_diagnostics(self):
        start = self.page.index("function openApprovals(source)")
        end = self.page.index("async function viewDiag()", start)
        block = self.page[start:end]
        self.assertIn(
            "loadApprovals(holder,canonical,drawerGeneration)", block)
        self.assertNotIn("/api/diagnostics", block)
        self.assertNotIn("ensureScopeDiscovered", block)

    def test_existing_approvals_and_access_state_are_above_the_form(self):
        start = self.page.index("function openApprovals(source)")
        end = self.page.index("async function viewDiag()", start)
        block = self.page[start:end]
        self.assertIn("metadataApprovalAccessNotice()", block)
        self.assertIn(
            "loadApprovals(holder,canonical,drawerGeneration)", block)
        self.assertIn("metadataProposalPanel(", block)
        self.assertLess(block.index("body.append(holder)"),
                        block.index("body.append(metadataProposalPanel("))

    def test_privilege_notice_explains_submit_without_review_access(self):
        start = self.page.index("function metadataApprovalAccessNotice()")
        end = self.page.index("function openApprovals(source)", start)
        block = self.page[start:end]
        self.assertIn("security.privileged_users", block)
        self.assertIn("Submission can work while old proposals", block)
        self.assertIn("bypasses configured BU-security rows", block)
        self.assertIn("unauthenticated remote review", block)
        self.assertIn("no password or SSO identity was checked", block)
        self.assertIn("allow_unauthenticated_remote_approvals", block)

    def test_remote_decision_sends_the_same_origin_gui_marker(self):
        block = self.page[self.page.index("/api/approvals/decide"):][:500]
        self.assertIn("X-PSTB-Approval-Request", block)
        self.assertIn("metadata-review", block)

    def test_queue_request_is_bound_to_the_active_source(self):
        block = self.page[self.page.index("async function loadApprovals("):
                          self.page.index("function metadataProposalUrl(")]
        self.assertIn("source='+", block)
        self.assertIn("encodeURIComponent(canonical)", block)

    def test_pending_proposals_render_before_decision_history(self):
        start = self.page.index("function renderApprovals(")
        end = self.page.index("async function loadApprovals(", start)
        block = self.page[start:end]
        self.assertIn("const ordered=[...pending,...history]", block)
        self.assertIn("Previous decisions", block)

    def test_drawer_async_writes_are_generation_fenced(self):
        self.assertIn("let DRAWER_GENERATION=0", self.page)
        for start_text, end_text in (
            ("async function openCustomer(", "async function viewAR("),
            ("function openApprovals(", "async function viewDiag("),
            ("async function openAccount(", "async function postAnswerFeedback("),
        ):
            with self.subTest(open=start_text):
                block = self.page[self.page.index(start_text):
                                  self.page.index(end_text,
                                                  self.page.index(start_text))]
                self.assertIn("beginDrawer()", block)
                if start_text != "function openApprovals(":
                    self.assertIn("drawerIsCurrent(drawerGeneration)", block)
        loader = self.page[self.page.index("async function loadApprovals("):
                           self.page.index("function metadataProposalUrl(")]
        self.assertIn("drawerIsCurrent(drawerGeneration)", loader)
        closer = self.page[self.page.index("function closeDrawer()"):
                           self.page.index("async function openAccount(")]
        self.assertIn("++DRAWER_GENERATION", closer)

    def test_older_badge_refresh_cannot_restore_a_stale_count(self):
        start = self.page.index("async function refreshApprovalBadge()")
        end = self.page.index("/* A thrown API error", start)
        block = self.page[start:end]
        self.assertIn("++APPROVAL_REFRESH_GENERATION", block)
        self.assertIn("generation!==APPROVAL_REFRESH_GENERATION", block)
        self.assertLess(block.index("generation!==APPROVAL_REFRESH_GENERATION"),
                        block.index("APPROVAL_PENDING=pending"))

    def test_chat_chrome_has_signout_when_the_session_is_enabled(self):
        start = self.page.index("function viewChat()")
        block = self.page[start:start + 6000]
        self.assertIn("META.security.enabled&&META.security.signed_in", block)
        self.assertIn("id='chat-signout'", block)
        self.assertIn("signout.onclick=signOutSession", block)
        signout = self.page[self.page.index("async function signOutSession()"):
                            self.page.index("/* ---------- boot ---------- */")]
        self.assertIn("/api/signout", signout)

    def test_the_badge_never_delays_the_first_paint(self):
        """Fire and forget: an affordance must not cost a round trip."""
        boot = self.page[self.page.index("bootSay('Drawing the workspace')"):]
        call = boot[:400]
        self.assertIn("refreshApprovalBadge()", call)
        self.assertNotIn("await refreshApprovalBadge()", call)

    def test_a_decision_updates_the_badge(self):
        """Otherwise the count still says 4 after you have emptied the queue."""
        block = self.page[self.page.index("/api/approvals/decide"):][:700]
        self.assertIn("refreshApprovalBadge()", block)

    def test_the_panel_does_not_paraphrase_the_servers_refusal(self):
        """Two copies of a remedy drift; the server's is the one with the port."""
        block = _js_function(self.page, "loadApprovals")
        self.assertIn("errText(e)", block)
        self.assertNotIn("SSH tunnel", block,
                         "the panel restated the remedy in its own words, so "
                         "it could not learn the real port")

class UnknownSourceOnDecideTests(unittest.TestCase):
    """resolve_name returns unknown names unchanged rather than raising.

    So the `except DbError` guarding it could never fire, the name flowed
    on to source_fingerprint(), and its MetadataError escaped the handler
    as a text/plain 500. index.html parses a failure body as JSON, so the
    one place a reader most needs the reason rendered a red badge reading
    "bad response".
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.mem = Path(self._dir.name) / "site_memory.json"
        SiteMemory(self.mem)
        self._p = patch.object(gui, "_site_memory",
                               lambda: SiteMemory(self.mem))
        self._p.start()
        self.client = _client()

    def tearDown(self):
        self._p.stop()
        self._dir.cleanup()

    def test_an_unknown_source_is_refused_by_name_not_by_crash(self):
        registry = SimpleNamespace(
            names=lambda: ["default", "p2go"],
            resolve_name=lambda source="": (source or "").strip() or "default")
        with patch.object(gui.engine, "registry", registry):
            out = self.client.post("/api/approvals/decide", json={
                "queue": "source_knowledge", "source": "nosuch",
                "id": "abc", "decision": "approve"})
        self.assertEqual(out.status_code, 404)
        detail = out.json()["detail"]
        self.assertIn("nosuch", detail)
        self.assertIn("p2go", detail, "name the sources that would have worked")

    def test_a_store_that_cannot_be_built_is_not_a_client_error(self):
        """An unbuilt catalog or an unreachable TNS alias is infrastructure.

        It used to escape as a 500; labelling it 400 would be just as wrong,
        because nothing about the request was malformed.
        """
        registry = SimpleNamespace(
            names=lambda: ["default"],
            resolve_name=lambda source="": "default")
        with patch.object(gui.engine, "registry", registry), \
                patch.object(gui, "_source_knowledge_store",
                             side_effect=OSError("TNS alias will not resolve")):
            out = self.client.post("/api/approvals/decide", json={
                "queue": "source_knowledge", "source": "default",
                "id": "abc", "decision": "approve"})
        self.assertEqual(out.status_code, 503)
        self.assertIn("TNS", out.json()["detail"])

class CatalogRefusalNamesTheRemedyTests(unittest.TestCase):
    """One sentence covered four states and read as "your proposal is stale".

    Catalog never built, catalog mid-rebuild, catalog rebound to another
    endpoint, and a genuine miss all produced the same words -- and the
    remedy for the first three is the opposite of the remedy for the last.
    It reaches the browser verbatim as a 400 badge, so it invited a reject
    on a proposal that was fine.

    Fixed in _catalog_identity rather than in app.py: the CLI's --approve
    calls the same function, so patching the handler would have fixed one
    of two callers and grown a second copy of the string.
    """

    def test_an_absent_catalog_is_named_with_the_command_that_builds_it(self):
        from pstb.source_knowledge import SourceKnowledgeError, _catalog_identity

        class AbsentCatalog:
            def context(self, identifier, source="", limit=10):
                return {"available": False, "found": False,
                        "source_database": source,
                        "detail": "No readable metadata catalog at foo.db.",
                        "how_to_build": "python scripts/build_metadata_catalog.py"}

        with self.assertRaises(SourceKnowledgeError) as caught:
            _catalog_identity(AbsentCatalog(), "default",
                              {"schema": "SYSADM", "object": "PS_VOUCHER"})
        message = str(caught.exception)
        self.assertIn("No readable metadata catalog", message)
        self.assertIn("build_metadata_catalog.py", message,
                      "a refusal with no remedy is a dead end")

    def test_a_genuine_miss_does_not_tell_anyone_to_rebuild(self):
        """The opposite remedy. A built catalog that simply does not hold
        the object must not send the reader off to rebuild it."""
        from pstb.source_knowledge import SourceKnowledgeError, _catalog_identity

        class BuiltButMissing:
            def context(self, identifier, source="", limit=10):
                return {"available": True, "found": False,
                        "detail": f"{identifier} is not in this catalog."}

        with self.assertRaises(SourceKnowledgeError) as caught:
            _catalog_identity(BuiltButMissing(), "default",
                              {"schema": "SYSADM", "object": "PS_VOUCHER"})
        message = str(caught.exception)
        self.assertIn("not in this catalog", message)
        self.assertNotIn("Build it with", message)

if __name__ == "__main__":
    unittest.main()
