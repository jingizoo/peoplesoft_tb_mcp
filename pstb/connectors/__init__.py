"""Connector framework: read-only REST sources beside the ledger.

Coupa, Kyriba, Workday and OneStream all answer questions PeopleSoft
cannot, and the reconciliations BETWEEN them and the ledger are the real
prize. Every connector follows the doctrine that made the ledger tools
trustworthy: curated methods returning typed payloads (figures the number
guard can verify), read-only transport, caching so hot paths never pay a
network round trip twice, and honest degradation with a remedy when the
source is unreachable.

Two hard rules live in the transport layer, not in reviewer memory:

* READ-ONLY. The only request this framework will ever send besides GET
  is the POST that exchanges OAuth client credentials for a token. A
  connector method that tries anything else gets ConnectorError, not a
  network call.
* NO CREDENTIALS IN CODE OR LOGS. Secrets come from the environment
  (.env, mode 0600) and never appear in payloads, cache keys, or errors.

Fixture mode is the third rule in practice: with no base URL configured a
connector serves bundled recorded responses (sample_data/
connector_fixtures/), exactly like the SQLite sample stands in for
Oracle. CI, tests and demos run with zero live credentials, and going
live is configuration, not code.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional


class ConnectorError(RuntimeError):
    """A connector problem stated with its remedy."""


class FixtureTransport:
    """Replays recorded responses keyed by URL path.

    Fixtures hold whole collections per endpoint; connector methods filter
    client-side (which they do in live mode too, as a second net under the
    server-side query params). Recording a new fixture is: capture the
    JSON a live GET returns, save it under the path key.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            raise ConnectorError(
                f"Cannot read connector fixtures at {self.path}: {e}")
        self.calls: list[str] = []  # inspected by tests and cache budgets

    def __call__(self, method: str, url: str, headers: dict,
                 body: Optional[bytes] = None) -> tuple[int, str]:
        key = urllib.parse.urlsplit(url).path
        self.calls.append(f"{method} {key}")
        if key not in self.data:
            raise ConnectorError(
                f"No fixture recorded for {key} in {self.path.name}. "
                "Record the live response under that key to extend the "
                "sample.")
        return 200, json.dumps(self.data[key])


def _urllib_transport(method: str, url: str, headers: dict,
                      body: Optional[bytes] = None,
                      timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


class RestConnector:
    """Base for a read-only JSON REST source with caching and OAuth."""

    name = "connector"
    ping_path = "/"

    def __init__(self, base_url: str = "", *, api_key: str = "",
                 client_id: str = "", client_secret: str = "",
                 token_path: str = "/oauth2/token", scope: str = "",
                 cache_ttl: int = 300,
                 transport: Optional[Callable] = None):
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._scope = scope
        self.cache_ttl = cache_ttl
        # A configured timeout that never reaches the socket is a lie the
        # operator only discovers under load: a site setting 120s for a
        # heavy query was still cut off at the transport default.
        self.timeout_seconds = 30
        self.transport = transport or _urllib_transport
        self.mode = "fixtures" if isinstance(transport, FixtureTransport) \
            else "live"
        self._cache: dict[str, tuple[float, Any]] = {}
        self._token: tuple[float, str] = (0.0, "")

    # ------------------------------------------------------------- transport
    def get(self, path: str, params: Optional[dict] = None,
            cache_ttl: Optional[int] = None) -> Any:
        """Cached, authenticated, read-only GET returning parsed JSON."""
        qs = urllib.parse.urlencode(sorted((params or {}).items()))
        url = f"{self.base_url}{path}" + (f"?{qs}" if qs else "")
        key = urllib.parse.urlsplit(url).path + "?" + qs
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now < hit[0]:
            return hit[1]
        status, text = self._request("GET", url)
        try:
            value = json.loads(text) if text.strip() else None
        except ValueError:
            raise ConnectorError(
                f"{self.name} returned non-JSON from {path} — check the "
                "base URL points at the API, not a login page.")
        self._cache[key] = (now + ttl, value)
        return value

    def _request(self, method: str, url: str,
                 body: Optional[bytes] = None,
                 headers: Optional[dict] = None) -> tuple[int, str]:
        is_token_call = url.endswith(self._token_path)
        if method != "GET" and not is_token_call:
            raise ConnectorError(
                f"{self.name} is read-only: {method} {url.split('?')[0]} "
                "was refused by the transport layer.")
        hdr = {"Accept": "application/json", **(headers or {})}
        if not is_token_call:
            auth = self._auth_header()
            if auth:
                hdr.update(auth)
        try:
            try:
                status, text = self.transport(
                    method, url, hdr, body,
                    timeout=int(getattr(self, "timeout_seconds", 30) or 30))
            except TypeError:
                # Fixture and test transports take four arguments.
                status, text = self.transport(method, url, hdr, body)
        except ConnectorError:
            raise
        except Exception as e:  # timeouts, DNS, TLS — name the remedy
            raise ConnectorError(
                f"{self.name} is unreachable at {self.base_url or '(no base '
                'URL configured)'}: {type(e).__name__}. Check the URL, "
                "network access from this host, and the service status.")
        if status in (401, 403):
            raise ConnectorError(self.auth_failure_remedy(status))
        if status == 429:
            raise ConnectorError(
                f"{self.name} rate-limited this client (HTTP 429). The "
                "connector caches for {ttl}s — retry shortly."
                .replace("{ttl}", str(self.cache_ttl)))
        if status >= 400:
            raise ConnectorError(self.http_failure_remedy(status, url))
        return status, text

    def _auth_header(self) -> dict:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        if not self._client_id:
            return {}
        exp, tok = self._token
        if time.monotonic() < exp and tok:
            return {"Authorization": f"Bearer {tok}"}
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            **({"scope": self._scope} if self._scope else {}),
        }).encode()
        status, text = self._request(
            "POST", f"{self.base_url}{self._token_path}", body,
            {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            data = json.loads(text)
            tok = str(data["access_token"])
            ttl = int(data.get("expires_in", 900))
        except (ValueError, KeyError):
            raise ConnectorError(
                f"{self.name} token endpoint did not return an access "
                "token — check the OAuth client is enabled for "
                "client-credentials grant.")
        self._token = (time.monotonic() + max(ttl - 60, 60), tok)
        return {"Authorization": f"Bearer {tok}"}

    # A generic HTTP code is not a remedy. Connectors override these to
    # say what to check AT THIS SERVICE, because "HTTP 404" sends an
    # operator hunting while the real cause has a name.
    def auth_failure_remedy(self, status: int) -> str:
        return (f"{self.name} rejected the credentials (HTTP {status}). "
                "Check the client id/secret or API key in .env — values are "
                "never logged, so re-enter them rather than hunting logs.")

    def http_failure_remedy(self, status: int, url: str) -> str:
        return f"{self.name} returned HTTP {status} for {url.split('?')[0]}."

    # ---------------------------------------------------------------- health
    def health(self) -> dict:
        """Reachability + latency, safe to render in the GUI."""
        t0 = time.perf_counter()
        try:
            self.get(self.ping_path, cache_ttl=0)
            ok, problem = True, ""
        except ConnectorError as e:
            ok, problem = False, str(e)
        return {"source": self.name, "mode": self.mode, "ok": ok,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                **({"problem": problem} if problem else {}),
                **({"base_url": self.base_url} if self.mode == "live" else
                   {"note": "serving bundled sample fixtures — set "
                            f"{self.name.upper()}_BASE_URL in .env to go "
                            "live"})}


FIXTURE_DIR = Path(__file__).resolve().parents[2] / "sample_data" / \
    "connector_fixtures"
