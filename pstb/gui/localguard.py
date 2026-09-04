"""Who is allowed to reach this server, and on what evidence.

The default is unchanged and is still the right default: bind 127.0.0.1,
answer only a caller on this machine. Two browser-side attacks reach a
loopback server from the open internet without needing any network position
at all, and both matter for an owner who works with
an SSH tunnel up while browsing the web on the same laptop.

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
how an unauthenticated ledger ends up on a corporate network.

So a routable bind is allowed with `--share`, and a token is OPTIONAL.
Requiring one was a decision this app made for its owner and then made
awkward — a link nobody asked for, pasted around, invalidated by every
restart. The owner is the person who knows whether their network is
trusted; on an internal VPN with a routable bind the honest answer is
usually that it is. So `--share` alone serves the page at a plain URL and
says clearly what that exposes, and setting PSTB_AUTH_TOKEN turns the
token on for anyone who wants it. When a token IS set it replaces the peer
address as the control everywhere, including for 127.0.0.1, because a
proxy on the server itself would otherwise be a free way in.

What is NOT optional: the configuration console. It writes credentials
behind a confirmation code anyone can compute, so it answers only from
this machine whatever the network policy is. "Let colleagues read the
dashboards" must never silently mean "let colleagues rotate the Oracle
password".

Not defaults, not advice: every request passes through here.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import threading
import time
import urllib.request
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
SIGNIN_PATH = "/api/token-signin"
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


# Named once, used by both the constructor and configure(), because the two
# ways to reach the same mistake deserve the same answer. The first version
# of this raised a bare "a routable bind requires a token" — true, and
# useless: it named the rule and not the remedy, so the next person read it
# as an obstacle and set POLICY by hand with shared=True and no token, which
# turns every check in this module off at once. An error that does not say
# what to do instead is a design that chooses its own bypass.
_NEEDS_TOKEN = (
    "This Policy is shared (routable) with no token and without "
    "unauthenticated=True, which is almost always a mistake rather than a "
    "decision — it turns off the peer check, the Host check and the token "
    "check at once.\n"
    "  Say which one you mean:\n"
    "    python -m pstb.gui --host 0.0.0.0 --port 8016 --share\n"
    "        open on the network, no token. Anyone who can route to this "
    "host reads the ledger.\n"
    "    PSTB_AUTH_TOKEN=<shared secret> python -m pstb.gui "
    "--host 0.0.0.0 --port 8016 --share\n"
    "        same bind, but every request must carry that token.\n"
    "  In code: Policy(..., shared=True, unauthenticated=True) to be open "
    "on purpose."
)


@dataclass(frozen=True)
class Policy:
    """How this process decided to be reachable. Set once, in main().

    ``hosts`` is None only in shared mode with no explicit ``--allow-host``:
    the operator has not told us which name their colleagues will type, and
    guessing wrong locks out the very people the mode exists for. The token
    is the control there — a rebound page is same-origin with the ATTACKER's
    name, so the browser never sends it our cookie.

    Validated in the CONSTRUCTOR, not only in configure(), because the state
    that matters is unauthenticated-and-routable and it must not be
    reachable by any spelling. Assigning POLICY directly is a normal thing
    for a person in a hurry to try; it should fail with the remedy in hand,
    at startup, rather than quietly serving the ledger to the network.
    """

    hosts: Optional[frozenset] = field(default=ALLOWED_HOSTS)
    token: str = ""
    shared: bool = False
    # "Open on the network, and I mean it." Not a synonym for "no token
    # yet": the flag exists so a routable bind with no authentication is
    # always something somebody TYPED, never something a half-built Policy
    # fell into. That is the distinction #108 was reaching for; requiring a
    # token was the wrong way to enforce it.
    unauthenticated: bool = False
    # Behind a managed load balancer (Cloud Run's front end, an ALB) the
    # forwarded headers are INJECTED BY THE PLATFORM, not forged by a
    # client -- refusing them refuses every request the platform can
    # deliver. trusted_proxy says "the thing talking to my socket is the
    # balancer": forwarded headers pass, and the peer address is never
    # treated as an identity again (no loopback privileges exist).
    trusted_proxy: bool = False
    # The balancer's identity-aware front end signs EVERY request it
    # forwards (x-goog-iap-jwt-assertion). trusted_iap says "verify that
    # signature and accept it as the access control": a colleague who
    # passed corporate sign-in needs no token at all. The token stays a
    # valid alternative -- machine callers, and any request that reached
    # this service WITHOUT crossing the front end (a VPC-internal hit on
    # the service URL carries no assertion, which is exactly why the app
    # verifies instead of trusting the network path).
    trusted_iap: bool = False
    # The expected JWT audience, explicit and exact
    # (/projects/NUMBER/regions/REGION/backendServices/ID for a regional
    # backend service). Never derived: a guessed audience that happens to
    # verify somewhere else is an impersonation hole, not a convenience.
    iap_audience: str = ""

    def __post_init__(self) -> None:
        if self.shared and not self.token and not self.unauthenticated:
            raise ValueError(_NEEDS_TOKEN)
        if self.trusted_proxy and (not self.shared or not self.token):
            raise ValueError(
                "trusted_proxy is for a managed balancer in front of a "
                "routable bind, and the balancer forwards ANYONE -- the "
                "token is the only control left. Set PSTB_AUTH_TOKEN and "
                "bind 0.0.0.0, or drop PSTB_TRUSTED_PROXY.")
        if self.trusted_iap and not self.trusted_proxy:
            raise ValueError(
                "trusted_iap verifies the assertion a managed balancer's "
                "identity-aware front end injects; without trusted_proxy "
                "there is no such balancer. Set PSTB_TRUSTED_PROXY=1 as "
                "well, or drop PSTB_TRUSTED_IAP.")
        if self.trusted_iap and not str(self.iap_audience or "").strip():
            raise ValueError(
                "trusted_iap needs the exact JWT audience of the backend "
                "service (PSTB_IAP_AUDIENCE=/projects/<number>/regions/"
                "<region>/backendServices/<id>). It is never derived: a "
                "guessed audience is an impersonation hole.")
        if self.hosts is None and not self.shared:
            # hosts=None means "accept any Host name", which is only safe
            # when a token is the control in its place. Outside shared
            # mode it is a hand-edit spelling that switches the DNS-
            # rebinding check off while looking like a default.
            raise ValueError(
                "Policy(hosts=None) accepts every Host header, which "
                "disables the DNS-rebinding control. That is only safe in "
                "shared mode, where the token replaces it. Use the default "
                "hosts, or run --share with a token.")


POLICY = Policy()


def configure(host: str, token: str = "",
              allowed_hosts: Iterable[str] = (),
              unauthenticated: bool = False,
              trusted_proxy: bool = False,
              trusted_iap: bool = False,
              iap_audience: str = "") -> Policy:
    """Install the policy for this process and return it.

    A routable bind needs either a token or an explicit
    ``unauthenticated=True``; main() passes the latter for --share without
    PSTB_AUTH_TOKEN, which is the ordinary "share it on our VPN" case.
    """
    global POLICY
    extra = {h.strip().lower() for h in (allowed_hosts or ()) if h and h.strip()}
    shared = not peer_is_loopback((host, 0))
    if shared and not token and not unauthenticated:
        raise ValueError(_NEEDS_TOKEN)
    if not shared:
        hosts: Optional[frozenset] = frozenset(ALLOWED_HOSTS | extra)
    elif extra:
        hosts = frozenset(ALLOWED_HOSTS | extra)
    else:
        hosts = None      # any name — we do not know what colleagues type
    POLICY = Policy(hosts=hosts, token=token, shared=shared,
                    unauthenticated=bool(shared and not token),
                    trusted_proxy=bool(trusted_proxy),
                    trusted_iap=bool(trusted_iap),
                    iap_audience=str(iap_audience or "").strip())
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
    """Is this peer the machine itself — and may that still mean anything?

    Behind a trusted proxy the answer is NO by definition, whatever the
    socket says: the platform may dial the container any way it likes,
    and a future runtime that happens to connect over 127.0.0.1 must not
    thereby inherit the console and every machine-local privilege. Peer
    identity is void in that mode; keys are the only identities left.
    """
    if POLICY.trusted_proxy:
        return False
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


def presented_tokens(scope) -> list:
    """EVERY token this request carries, from all four places.

    All of them, not the first found: after a restart mints a new token,
    the browser still holds the OLD one as a cookie. The person pastes the
    fresh URL — carrying the new token in the query — and the stale cookie
    used to shadow it, so the correct link 401'd forever until they found
    the cookie jar. A caller is authenticated when ANY credential they
    presented is right; the middleware then overwrites the stale cookie
    from the query, and the lockout heals itself on the next click.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    found: list = []
    bearer = headers.get("authorization", "")
    if bearer[:7].lower() == "bearer ":
        found.append(bearer[7:].strip())
    if headers.get(TOKEN_HEADER):
        found.append(headers[TOKEN_HEADER].strip())
    for chunk in (headers.get("cookie") or "").split(";"):
        name, _, value = chunk.partition("=")
        if name.strip() == TOKEN_COOKIE:
            found.append(value.strip())
    query = parse_qs(
        (scope.get("query_string") or b"").decode("latin-1", "replace"))
    found.append((query.get(TOKEN_QUERY) or [""])[0].strip())
    return [t for t in found if t]


def presented_token(scope) -> str:
    """First token found, header before cookie before query. Kept for
    callers that only need one; authentication checks all of them."""
    tokens = presented_tokens(scope)
    return tokens[0] if tokens else ""


def token_ok(presented: str, policy: Optional[Policy] = None) -> bool:
    """Constant-time, so a wrong token cannot be discovered one byte at a
    time by an attacker who can already reach the port."""
    expected = (policy or POLICY).token
    if not expected:
        return True
    return hmac.compare_digest(
        str(presented or "").encode("latin-1", "replace"),
        expected.encode("latin-1", "replace"))


def token_in_query(scope) -> bool:
    """Did the token arrive in the URL? Then the middleware sets a cookie,
    so the page's own fetches — which have no query string — keep working."""
    query = parse_qs(
        (scope.get("query_string") or b"").decode("latin-1", "replace"))
    return bool((query.get(TOKEN_QUERY) or [""])[0].strip())


def apply_security_headers(headers) -> None:
    for key, value in _SECURITY_HEADERS.items():
        headers.setdefault(key, value)
    if POLICY.trusted_proxy:
        # The balancer terminates TLS; the browser origin is https. HSTS
        # closes the typed-http first visit an on-path attacker would
        # otherwise intercept before the redirect.
        headers.setdefault("Strict-Transport-Security",
                           "max-age=31536000; includeSubDomains")


# The CLI default, and the fallback when a scope carries no server address.
DEFAULT_PORT = 8016


def served_port(scope=None, default: int = DEFAULT_PORT) -> int:
    """The port this process is really answering on, read from the scope.

    The refusal below used to name a hardcoded 8000 while the CLI has
    defaulted to 8016 since it grew a --port flag, so the one line a
    locked-out reader was told to paste could not have worked. The ASGI
    scope carries the bound address, which is both accurate and available
    here without importing the app (this module stays a pure function of
    the scope so it can be tested without a server).
    """
    server = (scope or {}).get("server") or ()
    port = server[1] if len(server) > 1 else None
    try:
        return int(port) if port else int(default)
    except (TypeError, ValueError):
        return int(default)


def tunnel_command(port: int = DEFAULT_PORT) -> str:
    """The single place the SSH remedy is worded.

    Three copies of this sentence had drifted apart. A remedy the reader
    can paste is the whole difference between a refusal they can act on
    and one they give up at, so it gets one formatter.
    """
    return f"ssh -L {port}:localhost:{port} <this-host>"


IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
# kid -> PEM public keys, published by the front end's operator. Rotated
# rarely; cached here so verification costs no network round trip.
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_IAP_CERTS_TTL = 6 * 3600
_IAP_CERTS_STALE_MAX = 24 * 3600     # beyond this, fail closed
_IAP_MAX_ASSERTION_BYTES = 8192

_iap_cache = {"certs": None, "fetched_at": 0.0}
_iap_cache_lock = threading.Lock()


class IAPRejected(Exception):
    """The assertion did not verify. The reason never includes the
    assertion itself."""


def _iap_certs(force: bool = False) -> dict:
    """The verification keys, cached; stale keys serve for a bounded
    window when the fetch fails (a transient outage must not lock the
    whole team out), and beyond that window verification fails CLOSED --
    admitting an unverified assertion because a fetch failed would turn
    a network error into an authentication bypass."""
    now = time.monotonic()
    with _iap_cache_lock:
        certs = _iap_cache["certs"]
        age = now - _iap_cache["fetched_at"]
        if certs is not None and age < _IAP_CERTS_TTL and not force:
            return certs
    try:
        with urllib.request.urlopen(IAP_CERTS_URL, timeout=5) as reply:
            fetched = json.loads(reply.read(65536).decode("utf-8"))
        if not isinstance(fetched, dict) or not fetched:
            raise ValueError("key document is not a non-empty object")
    except Exception as exc:                      # noqa: BLE001
        with _iap_cache_lock:
            certs = _iap_cache["certs"]
            age = now - _iap_cache["fetched_at"]
            if certs is not None and age < _IAP_CERTS_STALE_MAX:
                return certs
        raise IAPRejected(
            f"identity verification keys unavailable "
            f"({type(exc).__name__})") from exc
    with _iap_cache_lock:
        _iap_cache["certs"] = fetched
        _iap_cache["fetched_at"] = now
    return fetched


def verify_iap_assertion(assertion: str,
                         policy: Optional[Policy] = None) -> str:
    """Signature, audience, expiry, issuer -- or IAPRejected. Returns
    the verified identity (email when present, else subject).

    The signature check is the whole point: this runs INSIDE the app so
    a request that reached the service without crossing the identity-
    aware front end -- a VPC-internal call on the service URL -- has no
    signed assertion and is refused, whatever network path admitted it.
    """
    policy = policy or POLICY
    token = str(assertion or "").strip()
    if not token:
        raise IAPRejected("no assertion presented")
    if len(token.encode("latin-1", "replace")) > _IAP_MAX_ASSERTION_BYTES:
        raise IAPRejected("assertion is too large")
    try:
        from google.auth import jwt as _gjwt
    except ImportError as exc:                    # pragma: no cover
        raise IAPRejected("identity verification is not installed "
                          "(google-auth is missing)") from exc
    try:
        claims = _gjwt.decode(token, certs=_iap_certs(),
                              audience=policy.iap_audience)
    except IAPRejected:
        raise
    except Exception:                             # noqa: BLE001
        # One forced key refresh covers rotation (an unknown kid), then
        # the failure is the failure. Reasons stay generic on purpose:
        # a precise oracle helps only an attacker refining a forgery.
        try:
            claims = _gjwt.decode(token, certs=_iap_certs(force=True),
                                  audience=policy.iap_audience)
        except IAPRejected:
            raise
        except Exception as exc:                  # noqa: BLE001
            raise IAPRejected("the assertion did not verify") from exc
    if str(claims.get("iss") or "") != IAP_ISSUER:
        raise IAPRejected("wrong issuer")
    identity = str(claims.get("email") or claims.get("sub") or "")
    if not identity:
        raise IAPRejected("no identity in the assertion")
    return identity


def verified_identity(scope, policy: Optional[Policy] = None) -> str:
    """The identity the sign-in front end PROVED on this request, or "".

    Recomputed from the request's own header every time -- never stashed,
    so no copy exists that could outlive revocation or disagree with the
    admission check. Outside trusted_iap mode the header means nothing
    and this returns "" without touching the network: the assertion
    header is not in _FORWARDED, so a real production assertion replayed
    at a laptop is not even refused -- it is ignored, and must not paint
    a verified identity for a deployment that has no front door.
    """
    policy = policy or POLICY
    if not policy.trusted_iap:
        return ""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    try:
        return verify_iap_assertion(headers.get(IAP_HEADER, ""), policy)
    except IAPRejected:
        return ""


def iap_admits(scope, policy: Optional[Policy] = None) -> bool:
    """True when this request carries a VERIFIED front-end assertion.

    Delegates so the identity the page displays and the identity that
    admits are one code path, not two that could drift apart.
    (verify_iap_assertion never returns "" -- it raises instead -- so
    the delegation is exactly the old check.)
    """
    return verified_identity(scope, policy) != ""


def access_mode(policy: Optional[Policy] = None) -> str:
    """How requests reach this process: 'local', 'open' or 'token'.

    'verified' is a per-request fact (verified_identity), never a mode:
    a deployment has no standing state in which every request is proven.
    """
    policy = policy or POLICY
    if not policy.shared:
        return "local"
    if not policy.token:
        return "open"
    return "token"


def _is_signin_door(scope, policy: Optional[Policy] = None) -> bool:
    """Exactly one request may arrive tokenless: the POST that presents
    the token, and only behind a trusted proxy -- everywhere else the
    printed ?token= URL flow already works and this door stays shut."""
    policy = policy or POLICY
    return bool(
        policy.trusted_proxy and policy.token
        and str(scope.get("method") or "").upper() == "POST"
        and str(scope.get("path") or "") == SIGNIN_PATH)


def wants_signin_page(scope, policy: Optional[Policy] = None) -> bool:
    """A browser NAVIGATING to the app without a token should see the
    sign-in form, not raw JSON. Page requests only: an API fetch keeps
    its machine-readable refusal."""
    policy = policy or POLICY
    if not (policy.trusted_proxy and policy.token):
        return False
    if str(scope.get("method") or "GET").upper() != "GET":
        return False
    if str(scope.get("path") or "") != "/":
        return False
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    return "text/html" in headers.get("accept", "")


def signin_page(reason: str) -> str:
    """Self-contained, vendor-neutral, and deliberately tiny: nothing of
    the application -- no page chrome, no script surface, no endpoint
    names beyond the door itself -- is served to an unauthenticated
    caller."""
    import html as _html
    message = _html.escape(" ".join(str(reason or "").split()))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title><style>
body{{margin:0;display:flex;align-items:center;justify-content:center;
min-height:100vh;background:#111418;color:#e8eaed;
font:15px/1.5 system-ui,sans-serif}}
main{{max-width:420px;padding:32px}}
h1{{font-size:19px;margin:0 0 6px}}
p{{color:#9aa0a6;font-size:13px;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;
border-radius:8px;border:1px solid #3c4043;background:#1b1f24;
color:inherit;font-size:15px}}
button{{margin-top:10px;padding:10px 18px;border-radius:8px;border:0;
background:#8ab4f8;color:#111418;font-size:14px;font-weight:600;
cursor:pointer}}
#err{{color:#f28b82;font-size:13px;margin-top:10px;min-height:1.2em}}
</style></head><body><main>
<h1>Access token required</h1>
<p>{message}</p>
<form id="f"><input id="t" type="password" autocomplete="off"
placeholder="access token" autofocus>
<button>Sign in</button><div id="err"></div></form>
<script>
document.getElementById('f').onsubmit=async function(e){{
  e.preventDefault();
  var err=document.getElementById('err');err.replaceChildren();
  try{{
    var r=await fetch('{SIGNIN_PATH}',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{token:document.getElementById('t').value}})}});
    if(r.ok){{location.replace('/');return;}}
    err.textContent=r.status===403?'That token was not accepted.'
      :'Sign-in failed ('+r.status+').';
  }}catch(x){{err.textContent='Sign-in failed.';}}
}};
</script></main></body></html>"""


def rejection(scope, policy: Optional[Policy] = None) -> tuple:
    """(status, reason) when this request must not be served, else (0, "").

    Kept as a pure function of the ASGI scope so the rules can be tested
    without a server, and so the middleware stays a thin adapter.
    """
    policy = policy or POLICY
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    if policy.trusted_proxy and token_in_query(scope):
        # The ALB and the platform both log request URLs. On a bare VPN
        # host the pasted ?token= link was a deliberate local trade-off;
        # here it would land the only remaining credential in a log store
        # with its own audience and retention.
        return 401, (
            "This deployment does not accept the token in the URL: the "
            "load balancer logs request URLs, and the token is the access "
            "control. Open the page without the token and enter it on the "
            "sign-in form -- the form sends it in a request body, which "
            "the balancer does not log. (An Authorization: Bearer header "
            "works too, and the cookie takes over either way.)")
    if not policy.shared and not peer_is_loopback(scope.get("client")):
        return 403, ("This server answers only on the loopback interface. "
                     "Reach it through an SSH tunnel: "
                     f"{tunnel_command(served_port(scope))}")
    if not policy.trusted_proxy:
        for name in _FORWARDED:
            if name in headers:
                return 400, (
                    f"Refusing a request carrying {name}. A browser talking "
                    "straight to this server never sends it, so either an "
                    "unsupported proxy is in front of this server or the "
                    "client address is being forged. This app trusts the "
                    "real peer address only. (A managed load balancer "
                    "deployment sets PSTB_TRUSTED_PROXY=1, where the token "
                    "is the control instead.)")
    if not host_matches(headers.get("host", ""), policy):
        if policy.shared:
            # The loopback advice is wrong in shared mode — the caller is
            # legitimately remote, and the remedy is naming their host.
            allowed = ", ".join(sorted(policy.hosts or ()))
            return 400, (
                f"Unexpected Host header {headers.get('host', '')!r}. In "
                f"shared mode this server accepts: {allowed}. Restart with "
                "--allow-host <name> to add the name your colleagues use.")
        return 400, (
            f"Unexpected Host header {headers.get('host', '')!r}. This "
            "server is reachable as 127.0.0.1 or localhost only — a "
            "different name means a page elsewhere resolved its own "
            "hostname to this machine to reach your data.")
    # Last, so a caller who fails an earlier rule is told about that rule
    # rather than being sent to look for a token they would then also need.
    if policy.token and not any(token_ok(t, policy)
                                for t in presented_tokens(scope)):
        if policy.trusted_iap and iap_admits(scope, policy):
            # Corporate sign-in already happened at the front end and
            # the signature proves it. Same rung of the ladder as the
            # token on purpose: every earlier rule has already run.
            return 0, ""
        if _is_signin_door(scope, policy):
            # The one request allowed through WITHOUT a token: the POST
            # that presents it. Every earlier rule above already ran --
            # this narrows only the token check, and only in proxy mode,
            # where no printed-URL flow exists.
            return 0, ""
        if policy.trusted_proxy:
            # The startup banner deliberately does not print the token on
            # this deployment (stdout persists in the platform's logs).
            if policy.trusted_iap:
                return 401, (
                    "This request carried no verifiable identity from "
                    "the sign-in front end and no access token. Reach "
                    "the app through its public URL so corporate "
                    "sign-in runs, or present the token as an "
                    "Authorization: Bearer header.")
            return 401, (
                "This deployment requires its access token. Open the app "
                "in a browser and enter the token on the sign-in form, or "
                "send it as an Authorization: Bearer header; the cookie "
                "takes over either way.")
        return 401, (
            "This server is bound to a routable address, so it requires the "
            "access token printed when it started. Open the URL that "
            "printed with it, or send the token as an Authorization: Bearer "
            "header.")
    # The configuration console stays MACHINE-LOCAL even in shared mode.
    # The token grants colleagues read access to the app; the console
    # WRITES credentials and settings behind a confirmation code that is
    # computable by anyone (it is deliberateness, not authentication — its
    # own docstring says so on the assumption that only the loopback guard
    # admits callers). Handing every token holder the console would turn
    # "share the dashboards" into "share the ability to change the Oracle
    # password", which nothing in the printed warning promised.
    if policy.shared:
        path = str(scope.get("path") or "")
        if ((path == "/console" or path.startswith("/console/")
             or path.startswith("/api/console"))
                and not peer_is_loopback(scope.get("client"))):
            if policy.trusted_proxy:
                return 403, (
                    "The configuration console is disabled on load-"
                    "balancer deployments: there is no machine-local path "
                    "behind one, and configuration belongs to the deploy "
                    "environment (env vars and the platform's secret "
                    "store) there.")
            return 403, (
                "The configuration console answers only from the machine "
                "itself, even in shared mode. Reach it through an SSH "
                f"tunnel: {tunnel_command(served_port(scope))}")
    return 0, ""
