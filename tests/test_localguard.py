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


class BindTests(unittest.TestCase):
    def test_main_refuses_a_non_loopback_bind(self) -> None:
        # A flag must not be able to hand an unauthenticated ledger to the
        # network. docs used to advise exactly this.
        import argparse
        from unittest.mock import patch

        from pstb.gui import app as gapp
        args = argparse.Namespace(host="0.0.0.0", port=8000, open=False)
        with patch("argparse.ArgumentParser.parse_args", return_value=args):
            with self.assertRaises(SystemExit) as ctx:
                gapp.main()
        message = str(ctx.exception)
        self.assertIn("no authentication", message)
        self.assertIn("ssh -L", message, "refusing without the alternative "
                                         "just moves the problem")


if __name__ == "__main__":
    unittest.main()
