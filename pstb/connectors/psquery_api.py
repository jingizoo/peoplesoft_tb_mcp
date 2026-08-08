"""Execute an existing PSQuery through the Query Access Service.

The companion to pstb/psquery.py: that module DISCOVERS what a site
already built, over plain SQL; this one runs it. The split matters —
discovery needs nothing, execution needs a PeopleSoft user, and only
execution can be slow or wrong.

Three properties carry the safety story, and none of them is a prompt
instruction:

* GET-ONLY, structurally. The inherited transport refuses every method
  except GET (and an OAuth token POST this connector never uses), so
  this client CANNOT invoke a write-capable Integration Broker service
  even if something upstream asked it to. Voucher builds and journal
  loads are unreachable by construction, not by policy.
* ALLOWLISTED OPERATION. The only service operation named here is
  QAS_EXECUTEQUERY, checked against the shared READ_ONLY_OPERATIONS set
  that discovery classifies against, so the two modules cannot drift.
* SECURITY DIVERGENCE IS DISCLOSED. A query runs as the configured
  PeopleSoft user and returns what that user's permission lists allow.
  That is a governance improvement over a direct database account, which
  bypasses row-level security entirely — but it means QAS numbers may
  legitimately differ from tool numbers, and a controller comparing them
  deserves to be told that before they file a bug.

Only the REST listening connector is supported. The SOAP envelope form
would require POST, which the transport refuses on purpose; a SOAP-only
site is told so rather than quietly half-working.
"""
from __future__ import annotations

import base64
import os
import urllib.parse
from pathlib import Path
from typing import Optional

from . import FIXTURE_DIR, ConnectorError, FixtureTransport, RestConnector
from ..psquery import READ_ONLY_OPERATIONS

EXECUTE_OPERATION = "QAS_EXECUTEQUERY"


def rest_base(target_location: str) -> str:
    """Turn the site's discovered service target into the REST base.

    PSIBSVCSETUP publishes the SOAP listening connector; the REST one is
    its sibling on the same gateway. Deriving it keeps the site's own URL
    authoritative instead of asking an operator to retype it.
    """
    loc = (target_location or "").strip().rstrip("/")
    if not loc:
        return ""
    for soap, rest in (("PeopleSoftServiceListeningConnector",
                        "RESTListeningConnector"),
                       ("PeopleSoftListeningConnector",
                        "RESTListeningConnector")):
        if loc.endswith(soap):
            return loc[: -len(soap)] + rest
    return loc


class QasConnector(RestConnector):
    name = "psquery"
    ping_path = "/"

    def __init__(self, base_url: str = "", user: str = "",
                 password: str = "", node: str = "PSFT_FS",
                 timeout_seconds: int = 60, max_rows: int = 5000,
                 transport=None):
        super().__init__(base_url, transport=transport, cache_ttl=0)
        self._user = user
        self._password = password
        self.node = (node or "PSFT_FS").strip()
        self.timeout_seconds = int(timeout_seconds or 60)
        self.max_rows = int(max_rows or 5000)

    def auth_failure_remedy(self, status: int) -> str:
        """Name the RIGHT credentials. The inherited message talks about
        client ids and API keys, which do not exist here — an operator
        following it would search .env for settings this connector never
        reads."""
        return (
            f"PeopleSoft rejected the Query Access Service credentials "
            f"(HTTP {status}) for user "
            f"{self._user or '(none configured)'!r}. Check PSFT_QAS_USER "
            "and PSFT_QAS_PASSWORD in .env (values are never logged — "
            "re-enter rather than hunting them). If the credentials are "
            "right, the user may lack the QAS web-service permission list "
            "or access to this query: both are PeopleSoft security "
            "settings, not settings here. The curated database tools are "
            "unaffected and still answer.")

    def http_failure_remedy(self, status: int, url: str) -> str:
        """404 is the likeliest first-connection failure, and 'HTTP 404'
        alone tells an operator nothing about which of three causes it
        is."""
        if status == 404:
            return (
                f"The Query Access Service returned HTTP 404 at "
                f"{url.split('?')[0]}. Three usual causes, in order: the "
                f"node name is wrong (this call used {self.node!r} — sites "
                "commonly use PSFT_FS, PSFT_HR or a custom node; set "
                "PSFT_QAS_NODE in .env); the REST listening connector is "
                "not enabled on the gateway; or the query name does not "
                "exist as a PUBLIC query — confirm it with "
                "search_ps_queries. The curated database tools are "
                "unaffected and still answer.")
        if status in (500, 502, 503, 504):
            return (
                f"The Query Access Service is unavailable (HTTP {status}). "
                "The gateway answered but the service did not — usually "
                "the app server or the IB domain is down or recycling. "
                "This does not affect the curated database tools, which "
                "read the database directly.")
        return (f"The Query Access Service returned HTTP {status} for "
                f"{url.split('?')[0]}.")

    def _auth_header(self) -> dict:
        """PeopleSoft REST uses basic auth. Built here rather than stored,
        so the credential never sits on the instance as a header string."""
        if not self._user:
            return {}
        raw = f"{self._user}:{self._password}".encode("utf-8")
        return {"Authorization": "Basic "
                + base64.b64encode(raw).decode("ascii")}

    def execute(self, query: str, prompts: Optional[dict] = None,
                private_owner: str = "", max_rows: int = 0) -> dict:
        """Run one existing query and return its rows.

        prompts maps the query's BIND NAMES (from describe_ps_query) to
        values. Discovery is the intended first step: this refuses a
        blank name rather than guessing one.
        """
        name = (query or "").strip()
        if not name:
            raise ConnectorError(
                "A query name is required. Find one with search_ps_queries "
                "and read its prompts with describe_ps_query first.")
        if EXECUTE_OPERATION not in READ_ONLY_OPERATIONS:
            # Belt and braces: if someone ever narrows the shared
            # allowlist, execution must stop, not silently continue.
            raise ConnectorError(
                f"{EXECUTE_OPERATION} is no longer on the read-only "
                "allowlist; execution is disabled.")
        if not self.base_url and self.mode != "fixtures":
            raise ConnectorError(
                "No REST gateway is configured or discoverable. Run "
                "list_integration_endpoints to see the site's target "
                "location, or set ps_api.target_location in config.yaml.")
        cap = min(int(max_rows or self.max_rows), self.max_rows)
        scope = (private_owner or "").strip() or "PUBLIC"
        path = (f"/{self.node}/{EXECUTE_OPERATION}.v1/{scope}/"
                f"{urllib.parse.quote(name)}/JSON/NONFILE/")
        params = {"isconnectedquery": "n", "maxrows": str(cap)}
        for bind, value in (prompts or {}).items():
            params[f"prompt_{bind}"] = str(value)
        payload = self.get(path, params, cache_ttl=0)
        rows, columns = _rows_from(payload)
        truncated = len(rows) >= cap
        return {
            "source": "peoplesoft_query", "mode": self.mode,
            "query": name, "scope": scope,
            "prompts_supplied": dict(prompts or {}),
            "columns": columns, "rows": rows[:cap],
            "row_count": len(rows[:cap]), "truncated": truncated,
            "executed_as": self._user or "(no user configured)",
            "operation": EXECUTE_OPERATION,
            "note": (
                f"Ran the existing query {name} — cite it as the source; "
                "it is logic this site built and validated, not SQL we "
                "wrote. Results reflect the PERMISSION LISTS of the "
                "executing PeopleSoft user, so they may legitimately "
                "differ from figures the curated tools return through the "
                "database account, which bypasses row-level security."
                + (" Rows were capped; narrow the prompts for the rest."
                   if truncated else "")
                + (" SAMPLE fixture data, not a live instance."
                   if self.mode == "fixtures" else "")
            ),
        }


def _rows_from(payload) -> tuple:
    """QAS JSON varies by tools release; accept the common shapes and
    refuse clearly rather than guessing at an unknown one."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
        for key in ("rows", "Rows", "queryrows", "results"):
            rows = payload.get(key)
            if isinstance(rows, list):
                clean = [r for r in rows if isinstance(r, dict)]
                cols: list = []
                for r in clean:
                    for k in r:
                        if k not in cols:
                            cols.append(str(k))
                return clean, cols
    if isinstance(payload, list) and all(isinstance(r, dict)
                                         for r in payload):
        cols = []
        for r in payload:
            for k in r:
                if k not in cols:
                    cols.append(str(k))
        return payload, cols
    raise ConnectorError(
        "The Query Access Service returned a shape this client does not "
        "recognise. Check that the REST listening connector is enabled "
        "and that the service operation returns JSON (not XML) at this "
        "tools release.")


def from_config(cfg, target_location: str = "") -> QasConnector:
    """Live when enabled and a gateway is known; fixtures otherwise."""
    api = getattr(cfg, "ps_api", None)
    enabled = bool(getattr(api, "enabled", False))
    base = rest_base(getattr(api, "target_location", "") or target_location)
    user = os.environ.get("PSFT_QAS_USER", getattr(api, "user", "") or "")
    password = os.environ.get("PSFT_QAS_PASSWORD",
                              getattr(api, "password", "") or "")
    if enabled and base and user:
        return QasConnector(
            base, user=user, password=password,
            node=os.environ.get("PSFT_QAS_NODE", "PSFT_FS"),
            timeout_seconds=getattr(api, "timeout_seconds", 60),
            max_rows=getattr(api, "max_rows", 5000))
    return QasConnector(
        transport=FixtureTransport(Path(FIXTURE_DIR) / "psquery.json"),
        max_rows=getattr(api, "max_rows", 5000) if api else 5000)
