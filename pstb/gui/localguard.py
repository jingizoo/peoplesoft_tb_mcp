"""This server answers only to a caller on the loopback interface.

That sounds like it is already true — the default bind is 127.0.0.1 — but a
default is not a control, and two browser-side attacks reach a loopback
server from the open internet without needing any network position at all.
Both matter here specifically because the owner works with
``ssh -L 8000:localhost:8000`` up while browsing the web on the same laptop.

DNS REBINDING. A page on evil.example resolves its own hostname to
127.0.0.1 and issues same-origin requests to this server. The connection
genuinely comes from loopback, so a peer check alone passes it. What does
not survive is the ``Host`` header: the browser sends the name it was asked
for, not the address it reached. Refusing a Host we did not expect is the
control, and it has to cover the FINANCIAL routes, not just an admin page —
a rebound tab reads the general ledger from /api/trial-balance just as
readily.

CLICKJACKING. An invisible iframe of this app over a decoy page. The frame
loads because the URL really is 127.0.0.1. ``frame-ancestors 'none'`` is the
control; X-Frame-Options is the older spelling and both are sent.

Not defaults, not advice: every request passes through here.
"""
from __future__ import annotations

import ipaddress

# The names a browser can legitimately have been given for this server.
# Anything else is a rebound page or a mistake.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Headers a proxy uses to declare the real client. A browser talking
# straight to loopback never sends one, so their presence means either an
# unsupported proxy in front of us or an attempt to forge the peer that the
# loopback check trusts. Uvicorn's ProxyHeadersMiddleware would apply them
# OUTSIDE this app, where nothing here could recover the truth, so main()
# also passes proxy_headers=False.
_FORWARDED = ("x-forwarded-for", "x-real-ip", "forwarded",
              "x-forwarded-host", "x-forwarded-proto")

# Sent on every response.
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'self'"),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def host_matches(raw: str) -> bool:
    """Is this Host header one of ours?

    Starlette's TrustedHostMiddleware does ``host.split(":")[0]``, which
    turns ``[::1]:8000`` into ``[`` and rejects it. An owner whose browser
    resolves localhost to IPv6 would be locked out by their own security
    header, so the port is split only OUTSIDE brackets.
    """
    host = (raw or "").strip()
    if not host:
        return False
    if host.startswith("["):                       # [::1] or [::1]:8000
        close = host.find("]")
        if close < 0:
            return False
        host = host[:close + 1]
    elif host.count(":") == 1:                     # name:port or v4:port
        host = host.split(":", 1)[0]
    # A bare IPv6 literal with no brackets ("::1") has several colons and is
    # left whole; it cannot carry a port in that form.
    return host.lower() in ALLOWED_HOSTS


def peer_is_loopback(client) -> bool:
    """Did this connection actually come from this machine?

    ``client`` is ASGI ``scope["client"]``. Checked rather than the BIND:
    scope["server"] reports the local address of the connection, which reads
    127.0.0.1 for a loopback client even under --host 0.0.0.0, so it answers
    the wrong question. Only the peer says who is calling.
    """
    if client is None:               # unix socket: no peer address to check
        return True
    try:
        return ipaddress.ip_address(str(client[0])).is_loopback
    except ValueError:
        # Not an address at all — a test harness sends "testclient". Refuse;
        # tests drive real loopback scopes instead.
        return False


def apply_security_headers(headers) -> None:
    for key, value in _SECURITY_HEADERS.items():
        headers.setdefault(key, value)


def rejection(scope) -> tuple:
    """(status, reason) when this request must not be served, else (0, "").

    Kept as a pure function of the ASGI scope so the rules can be tested
    without a server, and so the middleware stays a thin adapter.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    if not peer_is_loopback(scope.get("client")):
        return 403, ("This server answers only on the loopback interface. "
                     "Reach it through an SSH tunnel: "
                     "ssh -L 8000:localhost:8000 <host>")
    for name in _FORWARDED:
        if name in headers:
            return 400, (
                f"Refusing a request carrying {name}. A browser on loopback "
                "never sends it, so either an unsupported proxy is in front "
                "of this server or the client address is being forged. This "
                "app trusts the real peer address only.")
    if not host_matches(headers.get("host", "")):
        return 400, (
            f"Unexpected Host header {headers.get('host', '')!r}. This "
            "server is reachable as 127.0.0.1 or localhost only — a "
            "different name means a page elsewhere resolved its own "
            "hostname to this machine to reach your data.")
    return 0, ""
