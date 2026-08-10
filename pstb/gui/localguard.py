"""Who is allowed to reach this server, and on what evidence.

The default is unchanged and is still the right default: bind 127.0.0.1,
answer only a caller on this machine. Two browser-side attacks reach a
loopback server from the open internet without needing any network position
at all, and both matter for an owner who works with
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

SHARED MODE. Refusing a routable bind outright was one control doing two
jobs: it was the access story AND the only answer to "several of us need
this page". Given only those two options a team removes the check, which is
how an unauthenticated ledger ends up on a corporate network. So a routable
bind is now allowed on ONE condition — a shared bearer token, generated for
the operator if they do not supply one. The token replaces the peer address
as the control, and it replaces it everywhere: in shared mode a request
from 127.0.0.1 needs the token too, because a proxy running on the server
itself would otherwise be an unauthenticated way in for the whole network.

Not defaults, not advice: every request passes through here.
"""
from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import parse_qs

# The names a browser can legitimately have been given for a loopback-bound
# server. Anything else is a rebound page or a mistake.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Where a caller may carry the shared token. The query string is included so
# a single pasted URL works on the first click; the middleware turns it into
# a cookie so the page's own fetches do not have to repeat it.
TOKEN_COOKIE = "pstb_token"
TOKEN_HEADER = "x-pstb-token"
TOKEN_QUERY = "token"

# Headers a proxy uses to declare the real client. A browser talking
# straight to this server never sends one, so their presence means either an
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


@dataclass(frozen=True)
class Policy:
    """How this process decided to be reachable. Set once, in main().

    ``hosts`` is None only in shared mode with no explicit ``--allow-host``:
    the operator has not told us which name their colleagues will type, and
    guessing wrong locks out the very people the mode exists for. The token
    is the control there — a rebound page is same-origin with the ATTACKER's
    name, so the browser never sends it our cookie.
    """

    hosts: Optional[frozenset] = field(default=ALLOWED_HOSTS)
    token: str = ""
    shared: bool = False


POLICY = Policy()


def configure(host: str, token: str = "",
              allowed_hosts: Iterable[str] = ()) -> Policy:
    """Install the policy for this process and return it."""
    global POLICY
    extra = {h.strip().lower() for h in (allowed_hosts or ()) if h and h.strip()}
    shared = not peer_is_loopback((host, 0))
    if shared and not token:
        raise ValueError("a routable bind requires a token")
    if not shared:
        hosts: Optional[frozenset] = frozenset(ALLOWED_HOSTS | extra)
    elif extra:
        hosts = frozenset(ALLOWED_HOSTS | extra)
    else:
        hosts = None                      # any name; the token is the control
    POLICY = Policy(hosts=hosts, token=token, shared=shared)
    return POLICY


def host_matches(raw: str, policy: Optional[Policy] = None) -> bool:
    """Is this Host header one of ours?

    Starlette's TrustedHostMiddleware does ``host.split(":")[0]``, which
    turns ``[::1]:8000`` into ``[`` and rejects it. An owner whose browser
    resolves localhost to IPv6 would be locked out by their own security
    header, so the port is split only OUTSIDE brackets.
    """
    allowed = (policy or POLICY).hosts
    if allowed is None:
        return True
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
    return host.lower() in allowed


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
        address = ipaddress.ip_address(str(client[0]))
        # A browser reaching a dual-stack listener over loopback arrives as
        # ::ffff:127.0.0.1, and whether IPv6Address.is_loopback says yes to
        # that depends on the Python version (3.13 started unwrapping
        # IPv4-mapped addresses; 3.12 does not). Leaving it to the
        # interpreter meant the owner's own laptop was admitted or refused
        # according to which python happened to be on the PATH. Unwrap here
        # so the answer is the same everywhere.
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        return address.is_loopback
    except ValueError:
        # Not an address at all — a test harness sends "testclient". Refuse;
        # tests drive real loopback scopes instead.
        return False


def presented_token(scope) -> str:
    """The token this request carries, from any of the three places.

    Header first (what the page's own fetches use once it has a cookie),
    then cookie, then the query string — which exists so ONE pasted URL
    works on the first click rather than needing a header-setting tool.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    bearer = headers.get("authorization", "")
    if bearer[:7].lower() == "bearer ":
        return bearer[7:].strip()
    if headers.get(TOKEN_HEADER):
        return headers[TOKEN_HEADER].strip()
    for chunk in (headers.get("cookie") or "").split(";"):
        name, _, value = chunk.partition("=")
        if name.strip() == TOKEN_COOKIE:
            return value.strip()
    query = parse_qs(
        (scope.get("query_string") or b"").decode("latin-1", "replace"))
    return (query.get(TOKEN_QUERY) or [""])[0].strip()


def token_ok(presented: str, policy: Optional[Policy] = None) -> bool:
    """Constant-time, so a wrong token cannot be discovered one byte at a
    time by an attacker who can already reach the port."""
    expected = (policy or POLICY).token
    if not expected:
        return True
    return hmac.compare_digest(str(presented or ""), expected)


def token_in_query(scope) -> bool:
    """Did the token arrive in the URL? Then the middleware sets a cookie,
    so the page's own fetches — which have no query string — keep working."""
    query = parse_qs(
        (scope.get("query_string") or b"").decode("latin-1", "replace"))
    return bool((query.get(TOKEN_QUERY) or [""])[0].strip())


def apply_security_headers(headers) -> None:
    for key, value in _SECURITY_HEADERS.items():
        headers.setdefault(key, value)


def rejection(scope, policy: Optional[Policy] = None) -> tuple:
    """(status, reason) when this request must not be served, else (0, "").

    Kept as a pure function of the ASGI scope so the rules can be tested
    without a server, and so the middleware stays a thin adapter.
    """
    policy = policy or POLICY
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    if not policy.shared and not peer_is_loopback(scope.get("client")):
        return 403, ("This server answers only on the loopback interface. "
                     "Reach it through an SSH tunnel: "
                     "ssh -L 8000:localhost:8000 <host>")
    for name in _FORWARDED:
        if name in headers:
            return 400, (
                f"Refusing a request carrying {name}. A browser talking "
                "straight to this server never sends it, so either an "
                "unsupported proxy is in front of this server or the client "
                "address is being forged. This app trusts the real peer "
                "address only.")
    if not host_matches(headers.get("host", ""), policy):
        return 400, (
            f"Unexpected Host header {headers.get('host', '')!r}. This "
            "server is reachable as 127.0.0.1 or localhost only — a "
            "different name means a page elsewhere resolved its own "
            "hostname to this machine to reach your data.")
    # Last, so a caller who fails an earlier rule is told about that rule
    # rather than being sent to look for a token they would then also need.
    if policy.token and not token_ok(presented_token(scope), policy):
        return 401, (
            "This server is bound to a routable address, so it requires the "
            "access token printed when it started. Open the URL that "
            "printed with it, or send the token as an Authorization: Bearer "
            "header.")
    return 0, ""
