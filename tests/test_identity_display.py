"""The chip can never show an identity the guard would not accept.

The signed-in-as slice surfaces the front end's PROVEN identity in the
GUI, and these tests hold it to the one invariant that makes it safe to
look at: transport.identity is non-empty exactly when mode is
"verified", exactly when ES256 verification passed against the cached
certs on THIS request's own assertion header. Identity is a property of
the request's evidence, not of the admitting rung -- a token-admitted
request that carries a genuine assertion still names its sender, and a
forged, expired, or replayed-at-a-laptop assertion names nobody, ever,
without an error. The second half pins what the field is NOT: not an
authentication, not a grant, not a seed for the OPRID -- the two
identity axes never leak into each other.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

# A bare [gui] install has no ES256 stack; this import re-raises
# test_iap_trust's module-level SkipTest with the same remedy, so the
# whole module skips where the feature itself refuses. CI installs the
# iap extra, so the skip never hides the suite where it matters.
from tests.test_iap_trust import (AUD_NO_MODE, HOST, IAP_POLICY,
                                  PROXY_ONLY, TOKEN, _OTHER_PEM,
                                  _assertion, _certs, scope_with)

from pstb.gui import app as gui
from pstb.gui import localguard
from pstb.gui.localguard import (IAP_HEADER, Policy, access_mode,
                                 iap_admits, verified_identity)

ROOT = Path(__file__).resolve().parent.parent
LOCAL = Policy()
OPEN = Policy(hosts=None, shared=True, unauthenticated=True)
BARE_SHARED = Policy(hosts=None, token=TOKEN, shared=True)
BEARER = ("authorization", "Bearer " + TOKEN)
LOOP = {"base_url": "http://127.0.0.1:8000",
        "client": ("127.0.0.1", 50000)}
PROXY = {"base_url": "https://pstb.example.com",
         "client": ("10.4.1.9", 51000)}
PROXY_HEADERS = {"host": "pstb.example.com"}


def _transport(client, extra=None):
    r = client.get("/api/meta", headers={**PROXY_HEADERS, **(extra or {})})
    assert r.status_code == 200, r.text
    return r.json()["transport"]


class VerifiedIdentityTests(unittest.TestCase):
    """The helper itself, as a pure function of the scope."""

    def test_a_genuine_assertion_yields_the_email(self):
        with _certs():
            self.assertEqual(
                verified_identity(scope_with(
                    HOST + [(IAP_HEADER, _assertion())], IAP_POLICY),
                    IAP_POLICY),
                "colleague@example.com")

    def test_the_trusted_iap_gate_is_load_bearing(self):
        """A real production assertion replayed at a laptop paints
        nothing -- and never touches the network doing it."""
        good = _assertion()
        exploding = patch.object(
            localguard, "_iap_certs",
            lambda force=False: (_ for _ in ()).throw(
                AssertionError("the network was touched past the gate")))
        for policy in (PROXY_ONLY, AUD_NO_MODE, LOCAL):
            with self.subTest(policy=policy), exploding:
                self.assertEqual(
                    verified_identity(
                        scope_with(HOST + [(IAP_HEADER, good)], policy),
                        policy),
                    "")

    def test_bad_assertions_name_nobody_and_raise_nothing(self):
        cases = {
            "forged": _assertion(pem=_OTHER_PEM),
            "expired": _assertion(exp_offset=-600),
            "wrong audience": _assertion(aud="/projects/1/other"),
            "wrong issuer": _assertion(iss="https://evil.example"),
            "absent": "",
        }
        with _certs():
            for label, assertion in cases.items():
                with self.subTest(label=label):
                    headers = HOST + ([(IAP_HEADER, assertion)]
                                      if assertion else [])
                    self.assertEqual(
                        verified_identity(scope_with(headers, IAP_POLICY),
                                          IAP_POLICY),
                        "")

    def test_iap_admits_is_the_same_code_path(self):
        """Display and admission may not drift: across every shape,
        iap_admits is literally verified_identity != ''."""
        good, forged = _assertion(), _assertion(pem=_OTHER_PEM)
        shapes = [(p, a) for p in (IAP_POLICY, PROXY_ONLY, AUD_NO_MODE)
                  for a in (good, forged, "")]
        with _certs():
            for policy, assertion in shapes:
                headers = HOST + ([(IAP_HEADER, assertion)]
                                  if assertion else [])
                scope = scope_with(headers, policy)
                with self.subTest(policy=policy, assertion=assertion[:12]):
                    self.assertEqual(
                        iap_admits(scope, policy),
                        verified_identity(scope, policy) != "")

    def test_access_mode_never_says_verified(self):
        self.assertEqual(access_mode(LOCAL), "local")
        self.assertEqual(access_mode(OPEN), "open")
        self.assertEqual(access_mode(BARE_SHARED), "token")
        self.assertEqual(access_mode(IAP_POLICY), "token")


class TransportFieldTests(unittest.TestCase):
    """/api/meta through the app, where the chip actually reads it."""

    def _sweep(self, transport):
        """The invariant, applied to every payload this suite sees."""
        self.assertEqual(transport["identity"] != "",
                         transport["mode"] == "verified", transport)
        return transport

    def test_a_verified_request_names_its_sender(self):
        with patch.object(localguard, "POLICY", IAP_POLICY), _certs():
            t = self._sweep(_transport(
                TestClient(gui.app, **PROXY),
                {IAP_HEADER: _assertion()}))
        self.assertEqual(t, {"mode": "verified",
                             "identity": "colleague@example.com"})

    def test_a_laptop_session_is_local_and_nameless(self):
        with patch.object(localguard, "POLICY", LOCAL):
            client = TestClient(gui.app, **LOOP)
            r = client.get("/api/meta")
            self.assertEqual(r.status_code, 200, r.text)
            t = self._sweep(r.json()["transport"])
        self.assertEqual(t, {"mode": "local", "identity": ""})

    def test_a_token_admission_names_nobody(self):
        with patch.object(localguard, "POLICY", BARE_SHARED):
            t = self._sweep(_transport(
                TestClient(gui.app, **PROXY),
                {BEARER[0]: BEARER[1]}))
        self.assertEqual(t, {"mode": "token", "identity": ""})

    def test_an_open_deployment_says_so(self):
        with patch.object(localguard, "POLICY", OPEN):
            t = self._sweep(_transport(TestClient(gui.app, **PROXY)))
        self.assertEqual(t, {"mode": "open", "identity": ""})

    def test_evidence_beats_rung(self):
        """The guard admits on the token rung when a cookie rides along
        (localguard checks tokens first); the display still shows the
        PROVEN sender, because identity is a fact about the evidence."""
        with patch.object(localguard, "POLICY", IAP_POLICY), _certs():
            t = self._sweep(_transport(
                TestClient(gui.app, **PROXY),
                {BEARER[0]: BEARER[1], IAP_HEADER: _assertion()}))
        self.assertEqual(t, {"mode": "verified",
                             "identity": "colleague@example.com"})

    def test_a_forged_assertion_never_paints_its_email(self):
        """Token-admitted request, forged assertion alongside: 200,
        token mode, empty identity -- never the forged name, never an
        error page."""
        with patch.object(localguard, "POLICY", IAP_POLICY), _certs():
            for label, assertion in (
                    ("forged", _assertion(pem=_OTHER_PEM)),
                    ("expired", _assertion(exp_offset=-600)),
                    ("wrong audience", _assertion(aud="/projects/1/x")),
                    ("wrong issuer",
                     _assertion(iss="https://evil.example"))):
                with self.subTest(label=label):
                    t = self._sweep(_transport(
                        TestClient(gui.app, **PROXY),
                        {BEARER[0]: BEARER[1], IAP_HEADER: assertion}))
                    self.assertEqual(t["mode"], "token")
                    self.assertEqual(t["identity"], "")

    def test_a_cert_outage_fails_the_display_closed(self):
        """Beyond the stale window the keys are gone: token access
        survives, the display names nobody -- a network error must not
        become either a lockout or a fabricated identity."""
        def outage(force=False):
            raise localguard.IAPRejected(
                "identity verification keys unavailable (URLError)")
        with patch.object(localguard, "POLICY", IAP_POLICY), \
                patch.object(localguard, "_iap_certs", outage):
            t = self._sweep(_transport(
                TestClient(gui.app, **PROXY),
                {BEARER[0]: BEARER[1], IAP_HEADER: _assertion()}))
        self.assertEqual(t, {"mode": "token", "identity": ""})

    def test_the_unsigned_twin_header_means_nothing(self):
        """The balancer also forwards x-goog-authenticated-user-email,
        which any VPC-internal caller can type. The app must not read
        it -- not here, not anywhere."""
        with patch.object(localguard, "POLICY", IAP_POLICY), _certs():
            t = self._sweep(_transport(
                TestClient(gui.app, **PROXY),
                {BEARER[0]: BEARER[1],
                 "x-goog-authenticated-user-email":
                     "accounts.example.com:attacker@evil.example"}))
        self.assertEqual(t["identity"], "")
        for py in (ROOT / "pstb").rglob("*.py"):
            self.assertNotIn("x-goog-authenticated-user-email",
                             py.read_text(encoding="utf-8"),
                             f"{py} reads the unsigned identity header")

    def test_a_refused_request_gets_no_transport_material(self):
        """Proxy mode, no token, no assertion: the 401 body carries the
        remedy and nothing else -- no transport block, no identity."""
        with patch.object(localguard, "POLICY", PROXY_ONLY):
            r = TestClient(gui.app, **PROXY).get(
                "/api/meta", headers=PROXY_HEADERS)
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("transport", r.text)
        self.assertNotIn("colleague@example.com", r.text)


class NotAnAuthenticationTests(unittest.TestCase):
    """The proven email buys no ledger session and seeds no OPRID."""

    def setUp(self):
        self.saved = (gui.cfg.security.enabled,
                      list(gui.cfg.security.privileged_users))
        gui.cfg.security.enabled = True
        gui.cfg.security.privileged_users = ["ADMIN"]
        gui.row_security.invalidate()
        self.addCleanup(self._restore)

    def _restore(self):
        (gui.cfg.security.enabled,
         gui.cfg.security.privileged_users) = self.saved
        gui.row_security.invalidate()

    def test_a_verified_email_is_not_a_ledger_session(self):
        with patch.object(localguard, "POLICY", IAP_POLICY), _certs():
            r = TestClient(gui.app, **PROXY).get(
                "/api/trial-balance",
                headers={**PROXY_HEADERS, IAP_HEADER: _assertion()})
        self.assertEqual(r.status_code, 401,
                         "the proven email bought a ledger session")

    def test_the_two_identity_axes_never_leak_into_each_other(self):
        with patch.object(localguard, "POLICY", IAP_POLICY), _certs():
            client = TestClient(gui.app, **PROXY)
            headers = {**PROXY_HEADERS, IAP_HEADER: _assertion()}
            signed = client.post("/api/signin",
                                 json={"oprid": "FIN_US001"},
                                 headers=headers)
            self.assertEqual(signed.status_code, 200, signed.text)
            body = client.get("/api/meta", headers=headers).json()
        self.assertEqual(body["security"]["oprid"], "FIN_US001")
        self.assertEqual(body["transport"]["identity"],
                         "colleague@example.com")
        # The pinned falsehoods stay false: proving who reached the
        # page does not make the OPRID form an authentication.
        self.assertIs(body["security"]["is_authentication"], False)
        self.assertIs(body["security"]["approval_identity_verified"],
                      False)


class CopyScanTests(unittest.TestCase):
    """The two vocabularies stay apart in the page itself."""

    HTML = (ROOT / "pstb" / "gui" / "static"
            / "index.html").read_text(encoding="utf-8")

    def test_the_oprid_line_never_says_verified(self):
        at = self.HTML.index("signed in as <b>")
        window = self.HTML[max(0, at - 500):at + 500]
        self.assertNotIn("verified", window,
                         "the self-selected OPRID line claims proof")

    def test_the_chip_never_says_signed_in_or_names_a_vendor(self):
        at = self.HTML.index("META.transport||{}")
        window = self.HTML[at:at + 1400]
        self.assertIn("corporate sign-in", window)
        for banned in ("signed in", "Google", "IAP"):
            self.assertNotIn(banned, window,
                             f"the chip says {banned!r}")


if __name__ == "__main__":
    unittest.main()
