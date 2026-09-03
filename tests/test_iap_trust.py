"""The signature is the proof, not the route.

trusted_iap mode verifies the assertion the balancer's identity-aware
front end signs onto every request it forwards, and accepts it as the
access control -- corporate sign-in instead of a token paste. These
tests hold the properties that make that safe: verification is a real
ES256 signature check against pinned keys (a forged, expired,
wrong-audience or wrong-issuer assertion is refused), the check runs on
the same rung as the token so every earlier guard rule still applies, a
request that reached the service WITHOUT crossing the front end (no
assertion -- the VPC-internal path) falls back to requiring the token,
key-fetch failure fails CLOSED beyond the stale window, and the mode
cannot be constructed without the balancer posture and an explicit
audience.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth import jwt as gjwt
    from google.auth.crypt import es256
    HAVE_IAP_DEPS = True
except ImportError:                              # pragma: no cover
    HAVE_IAP_DEPS = False

if not HAVE_IAP_DEPS:                            # pragma: no cover
    # A bare [gui] install has no ES256 stack; the FEATURE refuses at
    # startup with the remedy, and these tests skip with the same one.
    # CI installs the iap extra, so the skips never hide the suite
    # where it matters.
    raise unittest.SkipTest(
        "iap extra not installed (pip install -e '.[iap]')")

from pstb.gui import localguard
from pstb.gui.localguard import (IAP_HEADER, IAPRejected, Policy,
                                 rejection, verify_iap_assertion)

TOKEN = "a-perfectly-valid-token-value"
AUDIENCE = "/projects/7415/regions/us-central1/backendServices/12345"


def _keypair():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, pub.decode()


_PEM, _PUB = _keypair()
_OTHER_PEM, _OTHER_PUB = _keypair()

IAP_POLICY = Policy(hosts=None, token=TOKEN, shared=True,
                    trusted_proxy=True, trusted_iap=True,
                    iap_audience=AUDIENCE)
PROXY_ONLY = Policy(hosts=None, token=TOKEN, shared=True,
                    trusted_proxy=True)
# The shape where the mode gate is the ONLY guard: an audience is
# configured (so verification could succeed) but the mode is off. With
# no audience the verifier refuses everything anyway, and a gate test
# against that policy is toothless -- the sabotage run proved it.
AUD_NO_MODE = Policy(hosts=None, token=TOKEN, shared=True,
                     trusted_proxy=True, iap_audience=AUDIENCE)


def _assertion(*, aud=AUDIENCE, iss="https://cloud.google.com/iap",
               email="colleague@example.com", exp_offset=600,
               pem=_PEM, kid="k1"):
    now = int(time.time())
    claims = {"aud": aud, "iss": iss, "iat": now - 30,
              "exp": now + exp_offset, "email": email,
              "sub": "accounts.example.com:1188"}
    signer = es256.ES256Signer.from_string(pem, key_id=kid)
    return gjwt.encode(signer, claims,
                       header={"alg": "ES256", "kid": kid}).decode()


def _certs(mapping=None):
    return patch.object(localguard, "_iap_certs",
                        lambda force=False: dict(mapping or {"k1": _PUB}))


def scope_with(headers, path="/", method="GET"):
    return {"client": ("10.4.1.9", 51000), "path": path,
            "method": method, "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


HOST = [("host", "pstb.example.com")]


class PolicyShapeTests(unittest.TestCase):
    def test_the_mode_needs_the_balancer_posture(self):
        with self.assertRaises(ValueError):
            Policy(hosts=None, token=TOKEN, shared=True,
                   trusted_iap=True, iap_audience=AUDIENCE)

    def test_the_mode_needs_an_explicit_audience(self):
        with self.assertRaises(ValueError):
            Policy(hosts=None, token=TOKEN, shared=True,
                   trusted_proxy=True, trusted_iap=True)


class VerificationTests(unittest.TestCase):
    def test_a_genuine_assertion_verifies_to_its_identity(self):
        with _certs():
            identity = verify_iap_assertion(_assertion(), IAP_POLICY)
        self.assertEqual(identity, "colleague@example.com")

    def test_a_forged_signature_is_refused(self):
        with _certs():
            with self.assertRaises(IAPRejected):
                verify_iap_assertion(_assertion(pem=_OTHER_PEM),
                                     IAP_POLICY)

    def test_the_wrong_audience_is_refused(self):
        """An assertion minted for ANOTHER backend service must not
        admit here -- audience confusion is cross-service replay."""
        wrong = _assertion(
            aud="/projects/7415/regions/us-central1/backendServices/999")
        with _certs():
            with self.assertRaises(IAPRejected):
                verify_iap_assertion(wrong, IAP_POLICY)

    def test_an_expired_assertion_is_refused(self):
        with _certs():
            with self.assertRaises(IAPRejected):
                verify_iap_assertion(_assertion(exp_offset=-120),
                                     IAP_POLICY)

    def test_the_wrong_issuer_is_refused(self):
        with _certs():
            with self.assertRaises(IAPRejected):
                verify_iap_assertion(
                    _assertion(iss="https://evil.example.net"),
                    IAP_POLICY)

    def test_an_empty_or_oversized_assertion_is_refused(self):
        with _certs():
            with self.assertRaises(IAPRejected):
                verify_iap_assertion("", IAP_POLICY)
            with self.assertRaises(IAPRejected):
                verify_iap_assertion("x" * 9000, IAP_POLICY)

    def test_an_identityless_assertion_is_refused(self):
        now = int(time.time())
        signer = es256.ES256Signer.from_string(_PEM, key_id="k1")
        anonymous = gjwt.encode(
            signer, {"aud": AUDIENCE,
                     "iss": "https://cloud.google.com/iap",
                     "iat": now, "exp": now + 600},
            header={"alg": "ES256", "kid": "k1"}).decode()
        with _certs():
            with self.assertRaises(IAPRejected):
                verify_iap_assertion(anonymous, IAP_POLICY)

    def test_a_rotated_key_triggers_one_forced_refetch(self):
        calls = []

        def certs(force=False):
            calls.append(force)
            return {"k2": _PUB} if force else {"k1": _OTHER_PUB}

        with patch.object(localguard, "_iap_certs", certs):
            identity = verify_iap_assertion(_assertion(kid="k2"),
                                            IAP_POLICY)
        self.assertEqual(identity, "colleague@example.com")
        self.assertEqual(calls, [False, True])

    def test_the_refusal_never_contains_the_assertion(self):
        assertion = _assertion(pem=_OTHER_PEM)
        with _certs():
            try:
                verify_iap_assertion(assertion, IAP_POLICY)
                self.fail("forgery verified")
            except IAPRejected as exc:
                self.assertNotIn(assertion[:40], str(exc))


class KeyFetchTests(unittest.TestCase):
    def setUp(self):
        with localguard._iap_cache_lock:
            self._saved = dict(localguard._iap_cache)
            localguard._iap_cache.update(certs=None, fetched_at=0.0)

    def tearDown(self):
        with localguard._iap_cache_lock:
            localguard._iap_cache.update(self._saved)

    def test_fetch_failure_with_no_cache_fails_closed(self):
        def refuse(*a, **k):
            raise OSError("no route to host")
        with patch.object(localguard.urllib.request, "urlopen", refuse):
            with self.assertRaises(IAPRejected) as caught:
                localguard._iap_certs()
        self.assertIn("unavailable", str(caught.exception))

    def test_a_transient_outage_serves_stale_keys_briefly(self):
        with localguard._iap_cache_lock:
            localguard._iap_cache.update(
                certs={"k1": _PUB},
                fetched_at=(time.monotonic()
                            - localguard._IAP_CERTS_TTL - 60))

        def refuse(*a, **k):
            raise OSError("transient")
        with patch.object(localguard.urllib.request, "urlopen", refuse):
            certs = localguard._iap_certs()
        self.assertEqual(certs, {"k1": _PUB})

    def test_beyond_the_stale_window_it_fails_closed(self):
        """The literal is the witness: 25 hours is beyond the 24-hour
        doctrine whatever the constant says -- deriving the offset from
        the constant under test let an inflated window pass its own
        check in the sabotage run."""
        with localguard._iap_cache_lock:
            localguard._iap_cache.update(
                certs={"k1": _PUB},
                fetched_at=time.monotonic() - 25 * 3600)

        def refuse(*a, **k):
            raise OSError("outage")
        with patch.object(localguard.urllib.request, "urlopen", refuse):
            with self.assertRaises(IAPRejected):
                localguard._iap_certs()


class RejectionLadderTests(unittest.TestCase):
    def test_a_verified_assertion_admits_without_a_token(self):
        with _certs():
            status, _ = rejection(
                scope_with(HOST + [(IAP_HEADER, _assertion())]),
                IAP_POLICY)
        self.assertEqual(status, 0)

    def test_no_assertion_falls_back_to_the_token_requirement(self):
        """The VPC-internal path: a request that never crossed the
        front end carries no assertion and must present the token."""
        status, reason = rejection(scope_with(HOST), IAP_POLICY)
        self.assertEqual(status, 401)
        self.assertIn("front end", reason)
        with_token = scope_with(
            HOST + [("authorization", f"Bearer {TOKEN}")])
        status, _ = rejection(with_token, IAP_POLICY)
        self.assertEqual(status, 0)

    def test_a_forged_assertion_is_a_refusal_not_a_fallthrough_pass(self):
        with _certs():
            status, _ = rejection(
                scope_with(HOST + [(IAP_HEADER,
                                    _assertion(pem=_OTHER_PEM))]),
                IAP_POLICY)
        self.assertEqual(status, 401)

    def test_the_header_means_nothing_outside_iap_mode(self):
        for policy in (PROXY_ONLY, AUD_NO_MODE):
            with self.subTest(audience=bool(policy.iap_audience)):
                with _certs():
                    status, _ = rejection(
                        scope_with(HOST + [(IAP_HEADER, _assertion())]),
                        policy)
                self.assertEqual(status, 401)

    def test_iap_admits_itself_refuses_outside_the_mode(self):
        """Both layers of the mode gate, tested separately: the call
        site in rejection() and this inner check are redundant with each
        other, and symmetric redundancy hides single-layer sabotage
        unless each layer has its own witness."""
        with _certs():
            self.assertFalse(localguard.iap_admits(
                scope_with(HOST + [(IAP_HEADER, _assertion())]),
                AUD_NO_MODE))

    def test_the_assertion_does_not_bypass_the_earlier_rules(self):
        """Same rung as the token: a bad Host header still refuses a
        request whose assertion is perfectly genuine."""
        pinned = Policy(token=TOKEN, shared=True, trusted_proxy=True,
                        trusted_iap=True, iap_audience=AUDIENCE,
                        hosts=frozenset({"pstb.example.com"}))
        with _certs():
            status, _ = rejection(
                scope_with([("host", "evil.example.net"),
                            (IAP_HEADER, _assertion())]), pinned)
        self.assertEqual(status, 400)

    def test_the_url_token_refusal_still_outranks_the_assertion(self):
        scope = scope_with(HOST + [(IAP_HEADER, _assertion())])
        scope["query_string"] = f"token={TOKEN}".encode()
        with _certs():
            status, _ = rejection(scope, IAP_POLICY)
        self.assertEqual(status, 401)

    def test_key_outage_refuses_assertions_but_not_tokens(self):
        """Fail closed for identities, stay open for the token: an
        outage of a public key URL must not lock out the operator."""
        def refuse(force=False):
            raise IAPRejected("identity verification keys unavailable")
        with patch.object(localguard, "_iap_certs", refuse):
            status, _ = rejection(
                scope_with(HOST + [(IAP_HEADER, _assertion())]),
                IAP_POLICY)
            self.assertEqual(status, 401)
            status, _ = rejection(
                scope_with(HOST + [("authorization",
                                    f"Bearer {TOKEN}")]), IAP_POLICY)
            self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
