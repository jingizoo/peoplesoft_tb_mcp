"""The trust model behind a managed load balancer, pinned.

On a bare host the peer address is an identity: loopback means the
operator, forwarded headers mean forgery. Behind Cloud Run's front end
both assumptions invert — every request carries forwarded headers the
platform itself injected, and no request is ever loopback. These tests
pin the mode that bridges the two worlds without quietly weakening
either: trusted_proxy is explicit, token-gated, and refuses to exist
without the controls that replace what it turns off.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from pstb.gui import app as gui
from pstb.gui import localguard
from pstb.gui.localguard import Policy, rejection

TOKEN = "a-perfectly-valid-token-value"
OP_KEY = "operator-key-with-16-chars"


def scope_with(headers, client=("10.4.1.9", 51000), path="/"):
    return {"client": client, "path": path,
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


class TrustedProxyPolicyTests(unittest.TestCase):
    def test_the_mode_cannot_exist_without_its_replacement_controls(self):
        """trusted_proxy turns off the forwarded-header refusal; the token
        is the control that replaces it, so a proxy policy without a token
        (or without a routable bind) must refuse to construct."""
        for kwargs in ({"trusted_proxy": True},
                       {"trusted_proxy": True, "shared": True,
                        "unauthenticated": True, "hosts": None},
                       {"trusted_proxy": True, "token": TOKEN}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Policy(**kwargs)

    def test_platform_injected_headers_pass_only_in_proxy_mode(self):
        headers = [("x-forwarded-for", "203.0.113.9, 130.211.0.1"),
                   ("x-forwarded-proto", "https"),
                   ("host", "pstb.example.com")]
        proxied = Policy(hosts=None, token=TOKEN, shared=True,
                         trusted_proxy=True)
        status, _ = rejection(scope_with(headers), proxied)
        self.assertNotEqual(status, 400,
                            "the balancer's own headers are not an attack")
        plain = Policy(hosts=None, token=TOKEN, shared=True)
        status, reason = rejection(scope_with(headers), plain)
        self.assertEqual(status, 400,
                         "off the balancer the refusal must survive intact")
        self.assertIn("PSTB_TRUSTED_PROXY", reason,
                      "the refusal names the deliberate escape hatch")

    def test_proxy_mode_still_requires_the_page_token(self):
        proxied = Policy(hosts=None, token=TOKEN, shared=True,
                         trusted_proxy=True)
        status, _ = rejection(
            scope_with([("x-forwarded-for", "1.2.3.4"),
                        ("host", "pstb.example.com")]), proxied)
        self.assertEqual(status, 401,
                         "no token, no service — proxy mode is not a "
                         "bypass of shared-mode auth")


class OperatorKeyGateTests(unittest.TestCase):
    """The machine-local substitute: a second secret, not the page token."""

    def _request(self, headers=None, client=("10.4.1.9", 51000)):
        head = {k.lower(): v for k, v in (headers or {}).items()}
        return SimpleNamespace(
            scope={"client": client},
            headers=SimpleNamespace(get=lambda k, d="": head.get(k.lower(), d)))

    def _gate(self, request):
        gui._require_question_log_operator(request)

    def test_the_right_key_unlocks_a_non_loopback_request(self):
        with patch.dict(os.environ, {"PSTB_OPERATOR_TOKEN": OP_KEY}):
            self._gate(self._request({"X-PSTB-Operator": OP_KEY}))

    def test_a_wrong_or_missing_key_stays_locked(self):
        with patch.dict(os.environ, {"PSTB_OPERATOR_TOKEN": OP_KEY}):
            for headers in ({}, {"X-PSTB-Operator": "wrong-key-wrong-key"}):
                with self.subTest(headers=headers):
                    with self.assertRaises(HTTPException) as caught:
                        self._gate(self._request(headers))
                    self.assertEqual(caught.exception.status_code, 403)

    def test_a_malformed_configured_key_fails_closed(self):
        """A key with a quote in it must read as NOT CONFIGURED, never be
        compared against whatever characters survived."""
        with patch.dict(os.environ,
                        {"PSTB_OPERATOR_TOKEN": 'bad"key with spaces'}):
            with self.assertRaises(HTTPException):
                self._gate(self._request(
                    {"X-PSTB-Operator": 'bad"key with spaces'}))

    def test_the_page_token_is_not_the_operator_key(self):
        """Reading dashboards and approving durable knowledge are
        different privileges; holding the page token must not unlock
        the approval queue."""
        with patch.dict(os.environ, {"PSTB_OPERATOR_TOKEN": OP_KEY,
                                     "PSTB_AUTH_TOKEN": TOKEN}):
            with self.assertRaises(HTTPException):
                self._gate(self._request({"X-PSTB-Operator": TOKEN}))

    def test_loopback_needs_no_key_at_all(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PSTB_OPERATOR_TOKEN", None)
            self._gate(self._request(client=("127.0.0.1", 40000)))

    def test_the_proxy_mode_refusal_names_the_remedy_not_a_tunnel(self):
        """Behind a balancer 'use an SSH tunnel' is a lie; the refusal
        must name the operator key instead."""
        proxied = Policy(hosts=None, token=TOKEN, shared=True,
                         trusted_proxy=True)
        with patch.object(localguard, "POLICY", proxied), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PSTB_OPERATOR_TOKEN", None)
            with self.assertRaises(HTTPException) as caught:
                self._gate(self._request())
        detail = caught.exception.detail
        self.assertIn("PSTB_OPERATOR_TOKEN", detail)
        self.assertNotIn("ssh", detail.lower())


class McpHttpTests(unittest.TestCase):
    """The inner auth layer of the remote MCP service."""

    def _call(self, app, path="/mcp", headers=()):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "path": path,
                 "headers": [(k.encode(), v.encode()) for k, v in headers]}
        asyncio.run(app(scope, receive, send))
        return sent

    def _app(self, record):
        from pstb.mcp_http import build_app

        async def inner(scope, receive, send):
            record.append(scope["path"])
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})

        return build_app(inner, TOKEN)

    def test_no_bearer_no_service(self):
        reached = []
        sent = self._call(self._app(reached))
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(reached, [], "the tool surface was never touched")

    def test_a_wrong_bearer_is_the_same_as_none(self):
        reached = []
        sent = self._call(self._app(reached),
                          headers=[("authorization", "Bearer wrong-token!")])
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(reached, [])

    def test_the_right_bearer_reaches_the_tools(self):
        reached = []
        sent = self._call(self._app(reached),
                          headers=[("authorization", f"Bearer {TOKEN}")])
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(reached, ["/mcp"])

    def test_healthz_is_open_and_reveals_only_liveness(self):
        reached = []
        sent = self._call(self._app(reached), path="/healthz")
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(sent[1]["body"], b"ok")
        self.assertEqual(reached, [], "health checks never enter the app")

    def test_the_service_refuses_to_start_without_a_token(self):
        from pstb.mcp_http import _token_from_env
        with patch.dict(os.environ, {"PSTB_MCP_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                _token_from_env()

    def test_build_app_refuses_an_empty_token(self):
        from pstb.mcp_http import build_app

        async def inner(scope, receive, send):
            pass

        with self.assertRaises(ValueError):
            build_app(inner, "")


class PortAndWiringTests(unittest.TestCase):
    def test_the_gui_honors_the_platform_port(self):
        source = __import__("pathlib").Path(gui.__file__).read_text()
        self.assertIn('os.environ.get("PORT")', source)

    def test_the_browser_attaches_the_operator_key_when_present(self):
        page = (__import__("pathlib").Path(gui.__file__).parent
                / "static" / "index.html").read_text()
        self.assertIn("X-PSTB-Operator", page)
        self.assertIn("sessionStorage", page)
        self.assertIn("operatorUnlock", page)

    def test_the_key_never_touches_localstorage_or_urls(self):
        page = (__import__("pathlib").Path(gui.__file__).parent
                / "static" / "index.html").read_text()
        start = page.index("function operatorKey()")
        block = page[start:page.index("async function api(")]
        self.assertNotIn("localStorage", block)


class SecurityReviewFixTests(unittest.TestCase):
    """Every defect the 25-agent security review confirmed, pinned."""

    # ── blocker: the approvals queue itself honours the operator key ──
    def test_the_operator_key_unlocks_the_approvals_queue(self):
        """The key unlocked everything EXCEPT approvals — proposals could
        be submitted behind the balancer but never decided, so the queue
        filled forever (review blocker)."""
        head = {"x-pstb-operator": OP_KEY}
        request = SimpleNamespace(
            scope={"client": ("10.4.1.9", 51000)},
            headers=SimpleNamespace(get=lambda k, d="": head.get(k.lower(), d)))
        with patch.dict(os.environ, {"PSTB_OPERATOR_TOKEN": OP_KEY}):
            gui._require_approval_operator(request)
        self.assertEqual(request.scope["pstb.approval_mode"], "local")

    def test_without_the_key_remote_approvals_stay_locked(self):
        request = SimpleNamespace(
            scope={"client": ("10.4.1.9", 51000)},
            headers=SimpleNamespace(get=lambda k, d="": ""))
        with patch.dict(os.environ, {"PSTB_OPERATOR_TOKEN": OP_KEY}):
            with self.assertRaises(HTTPException) as caught:
                gui._require_approval_operator(request)
        self.assertEqual(caught.exception.status_code, 403)

    # ── blocker: the MCP app serves the balancer's Host header ──
    def test_the_mcp_app_accepts_a_real_domain_host(self):
        """The SDK's localhost-only DNS-rebinding allowlist 421'd every
        request the ALB could ever deliver (review blocker)."""
        import pstb.mcp_http as mh
        source = __import__("pathlib").Path(mh.__file__).read_text()
        self.assertIn('streamable_http_app(host="0.0.0.0")', source)

    # ── the refusal path refuses; it does not 500 ──
    def test_a_non_ascii_header_byte_is_a_refusal_not_a_crash(self):
        from pstb.mcp_http import build_app

        reached = []

        async def inner(scope, receive, send):
            reached.append(True)

        app = build_app(inner, TOKEN)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            sent.append(msg)

        scope = {"type": "http", "path": "/mcp",
                 "headers": [(b"authorization", b"Bearer \xff\xffwrong")]}
        asyncio.run(app(scope, receive, send))
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(reached, [])

    def test_a_non_ascii_operator_header_is_403_not_500(self):
        head = {"x-pstb-operator": "\xffbad-key-bad-key-bad"}
        request = SimpleNamespace(
            scope={"client": ("10.4.1.9", 51000)},
            headers=SimpleNamespace(get=lambda k, d="": head.get(k.lower(), d)))
        with patch.dict(os.environ, {"PSTB_OPERATOR_TOKEN": OP_KEY}):
            with self.assertRaises(HTTPException) as caught:
                gui._require_question_log_operator(request)
        self.assertEqual(caught.exception.status_code, 403)

    # ── proxy mode voids peer identity centrally ──
    def test_loopback_grants_nothing_in_proxy_mode(self):
        """If the platform ever dials the container over 127.0.0.1, that
        socket fact must not resurrect the console and every machine-
        local privilege."""
        proxied = Policy(hosts=None, token=TOKEN, shared=True,
                         trusted_proxy=True)
        with patch.object(localguard, "POLICY", proxied):
            self.assertFalse(
                localguard.peer_is_loopback(("127.0.0.1", 40000)))

    # ── the URL token flow is refused where URLs are logged ──
    def test_the_query_token_is_refused_in_proxy_mode(self):
        proxied = Policy(hosts=None, token=TOKEN, shared=True,
                         trusted_proxy=True)
        scope = {"client": ("10.4.1.9", 51000), "path": "/",
                 "query_string": f"token={TOKEN}".encode(),
                 "headers": [(b"host", b"pstb.example.com")]}
        status, reason = rejection(scope, proxied)
        self.assertEqual(status, 401)
        self.assertIn("sign-in form", reason)

    # ── transport hardening behind the TLS-terminating balancer ──
    def test_cookies_are_secure_and_hsts_is_sent_in_proxy_mode(self):
        source = __import__("pathlib").Path(gui.__file__).read_text()
        self.assertEqual(
            source.count("secure=localguard.POLICY.trusted_proxy"), 3,
            "the token cookie (URL mint), the sign-in mint, and the "
            "user cookie")
        proxied = Policy(hosts=None, token=TOKEN, shared=True,
                         trusted_proxy=True)
        headers = {}
        with patch.object(localguard, "POLICY", proxied):
            localguard.apply_security_headers(headers)
        self.assertIn("Strict-Transport-Security", headers)
        plain = {}
        with patch.object(localguard, "POLICY", Policy()):
            localguard.apply_security_headers(plain)
        self.assertNotIn("Strict-Transport-Security", plain,
                         "HSTS on a plain-HTTP VPN host would poison the "
                         "browser against the deployment")

    # ── stdout is a log store on the platform ──
    def test_the_startup_banner_never_prints_the_token_in_proxy_mode(self):
        """Not "the safe message exists" — the tokenized print must be
        REACHABLE only outside proxy mode. The guard is the control, so
        the test reads the guard, not the wording."""
        source = __import__("pathlib").Path(gui.__file__).read_text()
        url_print = source.index('/?token={token}"')
        before = source[max(0, url_print - 500):url_print]
        self.assertIn("POLICY.trusted_proxy", before,
                      "the tokenized URL print must sit behind the "
                      "proxy-mode guard")
        opening = source.index('opening = (f"{url}/?token={token}"')
        window = source[opening:opening + 200]
        self.assertIn("not localguard.POLICY.trusted_proxy", window)

    # ── the unlock retries its own panel ──
    def test_each_unlock_retries_its_own_panel(self):
        page = (__import__("pathlib").Path(gui.__file__).parent
                / "static" / "index.html").read_text()
        approvals = page.index("async function loadApprovals(")
        coverage = page.index("async function loadCoverageGaps(")
        approvals_block = page[approvals:coverage]
        coverage_block = page[coverage:coverage + 3000]
        self.assertIn("operatorUnlock(()=>loadApprovals(", approvals_block)
        self.assertNotIn("operatorUnlock(()=>loadCoverageGaps(",
                         approvals_block)
        self.assertIn("operatorUnlock(()=>loadCoverageGaps(",
                      coverage_block)

    # ── the image installs what the GUI actually imports ──
    def test_the_image_installs_the_gui_extra(self):
        docker = __import__("pathlib").Path(
            "deploy/cloudrun/Dockerfile").read_text()
        self.assertIn('".[oracle,llm,gui,iap]"', docker)
        self.assertIn("seed_sample_data.py", docker)
        self.assertNotIn("PSTB_TRUSTED_PROXY=1", docker,
                         "the proxy switch belongs to the service, not "
                         "the image — a local docker run is a plain host")

    def test_the_mcp_service_deploys_via_the_console_script(self):
        sh = __import__("pathlib").Path(
            "deploy/cloudrun/deploy.sh").read_text()
        self.assertIn("--command pstb-mcp-http", sh)
        self.assertNotIn("--args -m", sh,
                         "gcloud parses a leading dash in --args as a flag")

if __name__ == "__main__":
    unittest.main()
