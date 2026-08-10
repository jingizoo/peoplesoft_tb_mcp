"""This server answers only to a caller on this machine.

The default bind was 127.0.0.1, which felt like enough. It is not: two
browser-side attacks reach a loopback server from the open internet with no
network position at all, and both are live for an owner who keeps
`ssh -L 8000:localhost:8000` up while browsing the web on the same laptop.

Everything served here is UNAUTHENTICATED — every balance, every customer,
the ad-hoc SQL tool — so these checks are the whole access-control story.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.gui import localguard  # noqa: E402

LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}


def _scope(host="127.0.0.1:8000", client=("127.0.0.1", 50000), extra=None):
    headers = [(b"host", host.encode())]
    for k, v in (extra or {}).items():
        headers.append((k.lower().encode(), v.encode()))
    return {"type": "http", "headers": headers, "client": client}


class HostTests(unittest.TestCase):
    """The DNS-rebinding control. The browser sends the NAME it was asked
    for, not the address it reached, which is the one thing a rebound page
    cannot forge."""

    def test_the_names_this_server_really_has(self) -> None:
        for host in ("127.0.0.1", "127.0.0.1:8000", "localhost",
                     "localhost:8000", "[::1]", "[::1]:8000", "::1"):
            self.assertTrue(localguard.host_matches(host), host)

    def test_a_rebound_hostname_is_refused(self) -> None:
        for host in ("evil.example.com", "evil.example.com:8000",
                     "127.0.0.1.nip.io", "localhost.evil.com", ""):
            self.assertFalse(localguard.host_matches(host), host)

    def test_an_ipv6_literal_is_not_mangled_into_a_lockout(self) -> None:
        # Starlette's TrustedHostMiddleware does host.split(":")[0], which
        # turns "[::1]:8000" into "[". An owner whose browser resolves
        # localhost to IPv6 would be locked out by their own security
        # header — the first thing this would ship is an outage.
        self.assertTrue(localguard.host_matches("[::1]:8000"))
        self.assertEqual("[::1]:8000".split(":")[0], "[")


class PeerTests(unittest.TestCase):
    def test_loopback_in_every_spelling(self) -> None:
        for addr in ("127.0.0.1", "127.0.0.53", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(localguard.peer_is_loopback((addr, 1)), addr)

    def test_a_routable_peer_is_refused(self) -> None:
        for addr in ("10.0.0.5", "192.168.1.20", "203.0.113.9", "0.0.0.0"):
            self.assertFalse(localguard.peer_is_loopback((addr, 1)), addr)

    def test_a_non_address_is_refused_rather_than_trusted(self) -> None:
        self.assertFalse(localguard.peer_is_loopback(("testclient", 1)))

    def test_a_unix_socket_has_no_peer_and_is_local_by_definition(self) -> None:
        self.assertTrue(localguard.peer_is_loopback(None))


class RejectionTests(unittest.TestCase):
    def test_a_normal_local_request_passes(self) -> None:
        self.assertEqual(localguard.rejection(_scope()), (0, ""))

    def test_a_forged_client_address_is_refused(self) -> None:
        # Uvicorn's ProxyHeadersMiddleware would rewrite scope["client"]
        # from these OUTSIDE this app, where no middleware here could
        # recover the truth. A real loopback browser never sends one.
        for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded",
                       "X-Forwarded-Host"):
            status, why = localguard.rejection(
                _scope(extra={header: "127.0.0.1"}))
            self.assertEqual(status, 400, header)
            self.assertIn("forged", why)

    def test_the_remedy_names_the_tunnel(self) -> None:
        _, why = localguard.rejection(_scope(client=("10.0.0.5", 1)))
        self.assertIn("ssh -L", why)


class WholeAppTests(unittest.TestCase):
    """Scoped to every route, not an admin prefix — a rebound page reads
    the general ledger from /api/trial-balance just as readily."""

    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.TestClient, cls.gapp = TestClient, gapp

    def test_the_ledger_is_behind_the_guard_too(self) -> None:
        bad = self.TestClient(self.gapp.app, base_url="http://evil.example.com",
                              client=("127.0.0.1", 50000))
        for path in ("/api/trial-balance", "/api/scopes", "/api/diagnostics",
                     "/api/meta", "/"):
            self.assertEqual(bad.get(path).status_code, 400, path)

    def test_a_local_request_still_works(self) -> None:
        ok = self.TestClient(self.gapp.app, **LOOP)
        self.assertEqual(ok.get("/api/meta").status_code, 200)

    def test_every_response_refuses_to_be_framed(self) -> None:
        # Clickjacking: an invisible iframe of this app over a decoy page,
        # loaded because the URL really is 127.0.0.1.
        r = self.TestClient(self.gapp.app, **LOOP).get("/api/meta")
        self.assertIn("frame-ancestors 'none'",
                      r.headers.get("content-security-policy", ""))
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")

    def test_a_refusal_is_also_unframeable(self) -> None:
        bad = self.TestClient(self.gapp.app, base_url="http://evil.example.com",
                              client=("127.0.0.1", 50000))
        self.assertEqual(bad.get("/api/meta").headers.get("x-frame-options"),
                         "DENY")


class PolicyTests(unittest.TestCase):
    """Shared mode: a routable bind is allowed, but only WITH a token.

    Refusing the bind outright made one control do two jobs — the access
    story and the answer to "three of us need this page" — and a team given
    only those two options deletes the control. These tests pin the shape of
    the replacement: the bind can widen, the authentication cannot vanish.
    """

    def setUp(self) -> None:
        self.addCleanup(setattr, localguard, "POLICY", localguard.POLICY)

    def test_the_default_is_still_loopback_only_and_open(self) -> None:
        self.assertFalse(localguard.POLICY.shared)
        self.assertEqual(localguard.POLICY.token, "")
        self.assertEqual(localguard.rejection(_scope()), (0, ""))

    def test_a_routable_bind_without_a_token_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            localguard.configure("0.0.0.0", "")

    def test_shared_mode_admits_a_routable_peer_carrying_the_token(self) -> None:
        localguard.configure("0.0.0.0", "s3cret")
        status, _ = localguard.rejection(
            _scope(host="finhost:8016", client=("10.0.0.5", 1),
                   extra={"Authorization": "Bearer s3cret"}))
        self.assertEqual(status, 0)

    def test_shared_mode_refuses_a_routable_peer_without_the_token(self) -> None:
        localguard.configure("0.0.0.0", "s3cret")
        status, why = localguard.rejection(
            _scope(host="finhost:8016", client=("10.0.0.5", 1)))
        self.assertEqual(status, 401)
        self.assertIn("token", why)

    def test_shared_mode_refuses_a_WRONG_token(self) -> None:
        localguard.configure("0.0.0.0", "s3cret")
        for wrong in ("", "s3cre", "S3CRET", "s3cretx", "s3 cret"):
            status, _ = localguard.rejection(
                _scope(host="finhost", client=("10.0.0.5", 1),
                       extra={"X-PSTB-Token": wrong}))
            self.assertEqual(status, 401, wrong)

    def test_a_pasted_token_may_carry_stray_whitespace(self) -> None:
        # Generated tokens are urlsafe base64 and never contain a space, so
        # trimming one cannot admit a different token — and a copy-paste
        # that picks up a trailing space is otherwise a mystery failure.
        localguard.configure("0.0.0.0", "s3cret")
        status, _ = localguard.rejection(
            _scope(host="finhost", client=("10.0.0.5", 1),
                   extra={"X-PSTB-Token": " s3cret "}))
        self.assertEqual(status, 0)

    def test_loopback_needs_the_token_too_in_shared_mode(self) -> None:
        # Otherwise any proxy or script ON the server is an unauthenticated
        # way in for the whole network, which is the hole the token closes.
        localguard.configure("0.0.0.0", "s3cret")
        status, _ = localguard.rejection(_scope(client=("127.0.0.1", 1)))
        self.assertEqual(status, 401)

    def test_the_token_may_arrive_in_the_url_or_a_cookie(self) -> None:
        localguard.configure("0.0.0.0", "s3cret")
        by_query = _scope(host="finhost", client=("10.0.0.5", 1))
        by_query["query_string"] = b"token=s3cret"
        self.assertEqual(localguard.rejection(by_query)[0], 0)
        self.assertTrue(localguard.token_in_query(by_query))
        by_cookie = _scope(host="finhost", client=("10.0.0.5", 1),
                           extra={"Cookie": "theme=dark; pstb_token=s3cret"})
        self.assertEqual(localguard.rejection(by_cookie)[0], 0)
        self.assertFalse(localguard.token_in_query(by_cookie))

    def test_shared_mode_accepts_any_host_until_one_is_named(self) -> None:
        # The Host check exists to stop DNS rebinding, and a rebound page is
        # same-origin with the ATTACKER's name — so it never receives our
        # cookie and cannot pass the token check anyway. Guessing which name
        # colleagues will type, and refusing the rest, would only reproduce
        # the lockout this mode exists to end.
        localguard.configure("0.0.0.0", "s3cret")
        self.assertTrue(localguard.host_matches("finance.corp.example"))
        localguard.configure("0.0.0.0", "s3cret", ["finance.corp.example"])
        self.assertTrue(localguard.host_matches("finance.corp.example:8016"))
        self.assertFalse(localguard.host_matches("evil.example"))
        self.assertTrue(localguard.host_matches("localhost"),
                        "the operator's own tunnel must keep working")

    def test_a_forged_peer_header_is_still_refused_in_shared_mode(self) -> None:
        localguard.configure("0.0.0.0", "s3cret")
        status, why = localguard.rejection(
            _scope(host="finhost", client=("10.0.0.5", 1),
                   extra={"X-Forwarded-For": "127.0.0.1",
                          "Authorization": "Bearer s3cret"}))
        self.assertEqual(status, 400)
        self.assertIn("forged", why)


class BindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(setattr, localguard, "POLICY", localguard.POLICY)

    @staticmethod
    def _run_main(**overrides):
        import argparse
        from unittest.mock import patch

        from pstb.gui import app as gapp
        args = argparse.Namespace(host="0.0.0.0", port=8000, open=False,
                                  share=False, allow_host=[])
        for key, value in overrides.items():
            setattr(args, key, value)
        return gapp, args, patch

    def test_main_refuses_a_non_loopback_bind_with_no_authentication(self) -> None:
        # A flag must not be able to hand an UNAUTHENTICATED ledger to the
        # network. docs used to advise exactly this.
        gapp, args, patch = self._run_main()
        with patch("argparse.ArgumentParser.parse_args", return_value=args):
            with self.assertRaises(SystemExit) as ctx:
                gapp.main()
        message = str(ctx.exception)
        self.assertIn("without authentication", message)
        self.assertIn("ssh -L", message, "refusing without the alternative "
                                         "just moves the problem")
        self.assertIn("--share", message, "the refusal has to name the "
                                          "supported way to do this, or the "
                                          "next person deletes the check")

    def test_share_binds_the_network_and_mints_a_token(self) -> None:
        import os
        from unittest.mock import patch as _patch

        gapp, args, patch = self._run_main(share=True)
        with patch("argparse.ArgumentParser.parse_args", return_value=args):
            with _patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PSTB_AUTH_TOKEN", None)
                with _patch("uvicorn.run") as served:
                    gapp.main()
        served.assert_called_once()
        self.assertEqual(served.call_args.kwargs["host"], "0.0.0.0")
        self.assertFalse(served.call_args.kwargs["proxy_headers"],
                         "a forwarded header must never rewrite the peer")
        self.assertTrue(localguard.POLICY.shared)
        self.assertGreaterEqual(len(localguard.POLICY.token), 16)

    def test_share_reuses_a_configured_token_across_restarts(self) -> None:
        import os
        from unittest.mock import patch as _patch

        gapp, args, patch = self._run_main(share=True)
        with patch("argparse.ArgumentParser.parse_args", return_value=args):
            # 16+ chars of URL-safe characters: a shorter or fancier token
            # is refused at startup now, because it travels inside a URL
            # and a cookie where '&' or spaces silently split it.
            with _patch.dict(os.environ,
                             {"PSTB_AUTH_TOKEN": "team-token-123456"}):
                with _patch("uvicorn.run"):
                    gapp.main()
        self.assertEqual(localguard.POLICY.token, "team-token-123456")

    def test_a_loopback_bind_never_asks_for_a_token(self) -> None:
        from unittest.mock import patch as _patch

        gapp, args, patch = self._run_main(host="127.0.0.1")
        with patch("argparse.ArgumentParser.parse_args", return_value=args):
            with _patch("uvicorn.run"):
                gapp.main()
        self.assertEqual(localguard.POLICY.token, "")
        self.assertFalse(localguard.POLICY.shared)
        self.assertEqual(localguard.rejection(_scope()), (0, ""))


class SharedModeAppTests(unittest.TestCase):
    """The whole app under a token, not just the pure function."""

    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.TestClient, cls.gapp = TestClient, gapp

    def setUp(self) -> None:
        self.addCleanup(setattr, localguard, "POLICY", localguard.POLICY)
        localguard.configure("0.0.0.0", "s3cret")

    def _client(self, peer=("10.0.0.5", 51000)):
        return self.TestClient(self.gapp.app, base_url="http://finhost:8016",
                               client=peer)

    def test_the_ledger_is_behind_the_token(self) -> None:
        anon = self._client()
        for path in ("/api/trial-balance", "/api/scopes", "/api/meta", "/"):
            self.assertEqual(anon.get(path).status_code, 401, path)

    def test_one_pasted_url_authenticates_the_whole_session(self) -> None:
        browser = self._client()
        first = browser.get("/?token=s3cret")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.cookies.get(localguard.TOKEN_COOKIE), "s3cret")
        # The page's own fetches carry no query string; the cookie carries them.
        self.assertEqual(browser.get("/api/meta").status_code, 200)

    def test_a_wrong_token_is_never_stored_as_a_cookie(self) -> None:
        # Otherwise a typo becomes a cookie that is then re-sent forever and
        # the retry that would have worked keeps failing.
        r = self._client().get("/?token=nope")
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(r.cookies.get(localguard.TOKEN_COOKIE))

    def test_a_refusal_is_still_unframeable(self) -> None:
        r = self._client().get("/api/meta")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")


class UnauthenticatedRoutableBindTests(unittest.TestCase):
    """The one state that must not be reachable by any spelling.

    A deployment hit `ValueError: a routable bind requires a token`, read it
    as an obstacle rather than an instruction — it named the rule and not the
    remedy — and set POLICY by hand with shared=True and no token. That does
    not weaken one control. It turns off the peer check, the Host check and
    the token check together, leaving the general ledger, every customer, the
    ad-hoc SQL tool and the configuration console readable by anyone who can
    route to the host: strictly more open than the loopback default it
    replaced.

    So the constructor validates too, and every refusal carries the command
    that works.
    """

    def setUp(self) -> None:
        self.addCleanup(setattr, localguard, "POLICY", localguard.POLICY)

    def test_the_hand_built_bypass_raises(self) -> None:
        with self.assertRaises(ValueError):
            localguard.Policy(hosts=None, token="", shared=True)

    def test_configure_raises_the_same_way(self) -> None:
        with self.assertRaises(ValueError):
            localguard.configure("0.0.0.0", "")

    def test_every_refusal_names_a_command_that_works(self) -> None:
        # The remedy has to be in the error. A person who has to go and look
        # for it is a person who edits the guard instead.
        for build in (lambda: localguard.Policy(shared=True),
                      lambda: localguard.configure("0.0.0.0", "")):
            with self.assertRaises(ValueError) as ctx:
                build()
            message = str(ctx.exception)
            self.assertIn("--share", message)
            self.assertIn("PSTB_AUTH_TOKEN", message)
            self.assertIn("ad-hoc SQL", message,
                          "say what is exposed, not just that a rule failed")

    def test_the_supported_shared_mode_still_builds(self) -> None:
        policy = localguard.configure("0.0.0.0", "a-real-token")
        self.assertTrue(policy.shared)
        self.assertEqual(policy.token, "a-real-token")
        self.assertEqual(
            localguard.rejection(
                _scope(host="finhost", client=("10.0.0.5", 1),
                       extra={"Authorization": "Bearer a-real-token"}))[0], 0)

    def test_the_loopback_default_is_untouched(self) -> None:
        policy = localguard.Policy()
        self.assertFalse(policy.shared)
        self.assertEqual(policy.token, "")
        self.assertEqual(policy.hosts, localguard.ALLOWED_HOSTS)


if __name__ == "__main__":
    unittest.main()
