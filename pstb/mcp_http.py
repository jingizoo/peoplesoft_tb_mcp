"""The MCP server over streamable HTTP, for platforms with no stdio.

Cloud Run cannot spawn a subprocess per client the way a desktop MCP
client does, so the same 90-tool server that normally speaks stdio is
exposed as an ASGI app here — same tools, same guards, same engine bound
at import. Nothing about the tool surface changes with the transport.

Auth is deliberately two-layer, and this module owns only the inner one:
the platform's balancer (OAuth/IdP, Cloud Armor, ingress rules) decides
who may reach the service at all; PSTB_MCP_TOKEN then proves the caller
is OUR client and not merely someone the balancer admitted. The token is
a bearer header, validated with a constant-time compare, and the service
REFUSES TO START without one — "internal ingress" is a network posture,
not authentication.

/healthz answers unauthenticated with a static 200 so load-balancer
health checks need no secret. It reveals nothing but liveness.

Run:  PSTB_MCP_TOKEN=... python -m pstb.mcp_http     (honors $PORT)
"""
from __future__ import annotations

import hmac
import os
import re


def _token_from_env() -> str:
    token = (os.environ.get("PSTB_MCP_TOKEN") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", token or ""):
        raise SystemExit(
            "\n  PSTB_MCP_TOKEN must be set: 16-128 characters of A-Z, "
            "a-z, 0-9, '-' or '_'. A balancer in front is a network "
            "posture, not authentication — this service will not start "
            "open.\n  Generate one: python3 -c \"import secrets; "
            "print(secrets.token_urlsafe(24))\"\n")
    return token


def build_app(inner, token: str):
    """Wrap any ASGI app in bearer-token auth plus /healthz.

    Kept as a pure function of its inputs so tests can hand in a fake
    inner app and a known token without touching the environment or
    binding the real engine.
    """
    if not token:
        raise ValueError("build_app requires a non-empty token")

    async def app(scope, receive, send):
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path == "/healthz":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        presented = headers.get("authorization", "")
        expected = f"Bearer {token}"
        # latin-1 bytes on both sides: compare_digest raises TypeError on
        # non-ASCII strings, and an attacker-supplied header byte must be
        # a 401, never a 500 on the refusal path.
        if not hmac.compare_digest(
                presented.encode("latin-1", "replace"),
                expected.encode("latin-1", "replace")):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body",
                        "body": b"missing or invalid bearer token"})
            return
        await inner(scope, receive, send)

    return app


def main() -> None:
    import uvicorn

    token = _token_from_env()
    # Import here, not at module top: pstb.server binds the whole engine
    # (database, catalog, wiki) at import, and build_app's tests must not
    # pay that price for wanting the middleware alone.
    from . import server

    # host="0.0.0.0" switches OFF the SDK's localhost-only DNS-rebinding
    # allowlist, which would 421 every request arriving through the
    # balancer with the real domain in Host. That guard defends servers
    # reachable from a browser on the same machine; this one is reached
    # only through the platform ingress, Host is pinned by the ALB's
    # routing rule, and the bearer token is the control.
    app = build_app(server.mcp.streamable_http_app(host="0.0.0.0"), token)
    port = int(os.environ.get("PORT") or 8080)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
