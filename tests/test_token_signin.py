"""The one tokenless door, and everything it must not open.

Behind a managed load balancer the ?token= URL flow is refused (URLs
are logged) and a plain browser cannot attach an Authorization header
to a page load -- so before this feature, a stock browser had no
first-contact path at all. The door is one POST that presents the token
in a request BODY (bodies are not logged by the balancer) and receives
the httponly cookie. These tests pin how narrow the door is: one path,
one method, proxy mode only, every other guard rule still applied,
wrong tokens hintless, the cookie minted from the policy's own value
and never from the caller's echo, and nothing of the application served
to an unauthenticated browser beyond the tiny form itself.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from pstb.gui import app as gui
from pstb.gui import localguard
from pstb.gui.localguard import (SIGNIN_PATH, Policy, rejection,
                                 signin_page, wants_signin_page)

TOKEN = "a-perfectly-valid-token-value"
PROXIED = Policy(hosts=None, token=TOKEN, shared=True, trusted_proxy=True)
BARE_SHARED = Policy(hosts=None, token=TOKEN, shared=True)


def scope_with(headers, client=("10.4.1.9", 51000), path="/",
               method="GET", query=b""):
    return {"client": client, "path": path, "method": method,
            "query_string": query,
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


HOST = [("host", "pstb.example.com")]


class DoorNarrownessTests(unittest.TestCase):
    def test_the_signin_post_passes_without_a_token_in_proxy_mode(self):
        status, _ = rejection(
            scope_with(HOST, path=SIGNIN_PATH, method="POST"), PROXIED)
        self.assertEqual(status, 0)

    def test_a_get_of_the_same_path_is_still_refused(self):
        status, _ = rejection(
            scope_with(HOST, path=SIGNIN_PATH, method="GET"), PROXIED)
        self.assertEqual(status, 401)

    def test_any_other_path_is_still_refused(self):
        for path in ("/", "/api/trial-balance", "/api/token-signin/x",
                     "/api/token-signinx"):
            with self.subTest(path=path):
                status, _ = rejection(
                    scope_with(HOST, path=path, method="POST"), PROXIED)
                self.assertEqual(status, 401, path)

    def test_the_door_does_not_exist_off_the_balancer(self):
        """On a bare shared host the printed ?token= URL flow works and
        stdout printed the token; the tokenless door would only widen
        that deployment for nothing."""
        status, _ = rejection(
            scope_with(HOST, path=SIGNIN_PATH, method="POST",
                       client=("10.4.1.9", 51000)), BARE_SHARED)
        self.assertEqual(status, 401)

    def test_the_door_still_runs_every_earlier_rule(self):
        """The exemption narrows the TOKEN check only: a bad Host header
        refuses the sign-in POST exactly as it refuses everything."""
        bad_host = [("host", "evil.example.net")]
        pinned = Policy(token=TOKEN, shared=True, trusted_proxy=True,
                        hosts=frozenset({"pstb.example.com"}))
        status, _ = rejection(
            scope_with(bad_host, path=SIGNIN_PATH, method="POST"), pinned)
        self.assertEqual(status, 400)

    def test_the_url_token_stays_refused_in_proxy_mode(self):
        status, reason = rejection(
            scope_with(HOST, query=f"token={TOKEN}".encode()), PROXIED)
        self.assertEqual(status, 401)
        self.assertIn("sign-in form", reason)


class SigninPageTests(unittest.TestCase):
    def test_a_browser_navigation_wants_the_page(self):
        scope = scope_with(HOST + [("accept",
                                    "text/html,application/xhtml+xml")])
        self.assertTrue(wants_signin_page(scope, PROXIED))

    def test_api_fetches_and_deep_paths_keep_json(self):
        html_accept = [("accept", "text/html")]
        for scope in (
            scope_with(HOST + [("accept", "application/json")]),
            scope_with(HOST + html_accept, path="/api/trial-balance"),
            scope_with(HOST + html_accept, method="POST"),
        ):
            with self.subTest(scope=scope["path"]):
                self.assertFalse(wants_signin_page(scope, PROXIED))

    def test_the_page_never_appears_off_the_balancer(self):
        scope = scope_with(HOST + [("accept", "text/html")])
        self.assertFalse(wants_signin_page(scope, BARE_SHARED))

    def test_the_page_is_tiny_neutral_and_appless(self):
        """Nothing of the application is served to an unauthenticated
        caller: no page chrome, no other endpoint names, no vendor
        words, and certainly not the token."""
        page = signin_page("reason text here")
        lowered = page.lower()
        for vendor in ("peoplesoft", "oracle", "transunion", "claude",
                       "gemini", "ollama"):
            self.assertNotIn(vendor, lowered)
        self.assertNotIn(TOKEN, page)
        self.assertNotIn("/api/trial", page)
        self.assertIn(SIGNIN_PATH, page)
        self.assertIn("reason text here", page)
        self.assertLess(len(page), 4096)

    def test_the_reason_is_escaped_into_the_page(self):
        page = signin_page('<script>alert("x")</script>')
        self.assertNotIn("<script>alert", page)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(gui.app, client=("10.4.1.9", 51000),
                                 base_url="http://pstb.example.com")

    def _post(self, body):
        return self.client.post(SIGNIN_PATH, json=body,
                                headers={"host": "pstb.example.com"})

    def test_the_right_token_mints_the_cookie_from_policy(self):
        with patch.object(localguard, "POLICY", PROXIED):
            r = self._post({"token": TOKEN})
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("set-cookie", "")
        self.assertIn(f"{localguard.TOKEN_COOKIE}={TOKEN}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_a_wrong_token_is_refused_without_hints_or_cookie(self):
        with patch.object(localguard, "POLICY", PROXIED), \
                patch.object(gui.time, "sleep") as slept:
            r = self._post({"token": "wrong-token-entirely"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("set-cookie", r.headers)
        self.assertNotIn(TOKEN, r.text)
        self.assertNotIn("wrong-token-entirely", r.text)
        slept.assert_called_once()

    def test_missing_and_malformed_tokens_are_400s(self):
        with patch.object(localguard, "POLICY", PROXIED):
            for body in ({}, {"token": ""}, {"token": "x" * 600},
                         {"token": 42}):
                with self.subTest(body=str(body)[:20]):
                    r = self._post(body)
                    self.assertEqual(r.status_code, 400)
                    self.assertNotIn("set-cookie", r.headers)

    def test_the_endpoint_hides_itself_off_the_balancer(self):
        local = TestClient(gui.app, client=("127.0.0.1", 5555),
                           base_url="http://localhost")
        r = local.post(SIGNIN_PATH, json={"token": TOKEN})
        self.assertEqual(r.status_code, 404)

    def test_the_door_is_in_the_bounded_write_family(self):
        self.assertTrue(gui._is_bounded_write_path(SIGNIN_PATH))
        with patch.object(localguard, "POLICY", PROXIED):
            r = self.client.post(
                SIGNIN_PATH,
                content=b"x" * (gui._PROTECTED_WRITE_MAX_BYTES + 1),
                headers={"Content-Type": "application/json",
                         "host": "pstb.example.com"})
        self.assertEqual(r.status_code, 413)


class EndToEndTests(unittest.TestCase):
    def test_navigate_signin_cookie_browse(self):
        """The full journey: a bare navigation gets the form (and only
        the form), the POST mints the cookie, and the cookie then
        admits a page request that was refused before."""
        # https base: the cookie is Secure in proxy mode (TLS ends at
        # the balancer), and the jar rightly refuses to send it over http
        client = TestClient(gui.app, client=("10.4.1.9", 51000),
                            base_url="https://pstb.example.com")
        headers = {"host": "pstb.example.com", "accept": "text/html"}
        with patch.object(localguard, "POLICY", PROXIED):
            first = client.get("/", headers=headers)
            self.assertEqual(first.status_code, 401)
            self.assertIn("text/html",
                          first.headers.get("content-type", ""))
            self.assertIn("Access token required", first.text)

            signed = client.post(SIGNIN_PATH, json={"token": TOKEN},
                                 headers={"host": "pstb.example.com"})
            self.assertEqual(signed.status_code, 200)

            after = client.get("/", headers=headers)
            self.assertNotEqual(after.status_code, 401)

    def test_an_api_fetch_refused_stays_json(self):
        client = TestClient(gui.app, client=("10.4.1.9", 51000),
                            base_url="http://pstb.example.com")
        with patch.object(localguard, "POLICY", PROXIED):
            r = client.get("/api/meta",
                           headers={"host": "pstb.example.com",
                                    "accept": "application/json"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("application/json",
                      r.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()
