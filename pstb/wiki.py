"""Wiki context providers: Confluence (REST API) and a local markdown folder.

`auto` mode uses Confluence when CONFLUENCE_BASE_URL + CONFLUENCE_API_TOKEN are
set, otherwise falls back to the local docs folder — so the demo works before
any wiki credentials exist.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from .config import Config

MAX_PAGE_CHARS = 12_000


class WikiError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    ex = _TextExtractor()
    ex.feed(html or "")
    text = "".join(ex.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class LocalDocsWiki:
    provider_name = "localdocs"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _files(self) -> list[Path]:
        if not self.root.exists():
            return []
        out = []
        for ext in ("*.md", "*.txt", "*.rst"):
            out.extend(self.root.rglob(ext))
        return sorted(out)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
        results = []
        for f in self._files():
            try:
                body = f.read_text(errors="replace")
            except OSError:
                continue
            low = body.lower()
            title = f.stem.replace("-", " ").replace("_", " ").title()
            score = sum(low.count(t) for t in terms) + sum(
                3 for t in terms if t in title.lower()
            )
            if not terms:
                score = 1
            if score <= 0:
                continue
            first_hit = min(
                (low.find(t) for t in terms if t in low), default=0
            )
            snippet = re.sub(r"\s+", " ", body[max(0, first_hit - 80) : first_hit + 240]).strip()
            results.append(
                {
                    "id": str(f.relative_to(self.root)),
                    "title": title,
                    "snippet": snippet,
                    "score": score,
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[: max(int(limit or 5), 1)]

    def health(self) -> dict:
        files = self._files()
        is_bundled = self.root.name == "sample_wiki"
        return {
            "provider": "localdocs",
            "connected": bool(files),
            "path": str(self.root),
            "page_count": len(files),
            "pages": [f.name for f in files[:20]],
            "is_bundled_demo_content": is_bundled,
            "verdict": ("DEMO CONTENT — these are the fictional sample policy "
                        "pages shipped with this repo, not your company wiki. "
                        "Any policy figure quoted from them (30-day suspense "
                        "rule, $5,000 capitalization threshold) is made up."
                        if is_bundled else
                        f"Serving {len(files)} local file(s) from {self.root}."),
        }

    def get_page(self, page_id: str) -> dict:
        target = (self.root / page_id).resolve()
        if not str(target).startswith(str(self.root.resolve())) or not target.exists():
            raise WikiError(f"Page not found: {page_id}")
        body = target.read_text(errors="replace")
        return {
            "id": page_id,
            "title": target.stem.replace("-", " ").title(),
            "text": body[:MAX_PAGE_CHARS],
            "truncated": len(body) > MAX_PAGE_CHARS,
            "source": str(target),
        }


class ConfluenceWiki:
    provider_name = "confluence"

    def __init__(self, base_url: str, email: str, token: str, space: str = "",
                 labels: Optional[list] = None):
        import httpx  # deferred so the sample works without httpx installed

        self.base = base_url.rstrip("/")
        self.space = space
        self.labels = [l for l in (labels or []) if l]
        self._email = email or ""
        if email:
            auth: object = (email, token)
            headers = {}
        else:  # Data Center personal access token
            auth = None
            headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.Client(
            base_url=self.base, auth=auth, headers=headers, timeout=20.0,
            follow_redirects=True,
        )

    def health(self) -> dict:
        """Staged connectivity/auth/scope check. Never raises; never returns
        the token. Each stage explains its own failure."""
        out: dict = {
            "provider": "confluence",
            "base_url": self.base,
            "auth_mode": "cloud (email + API token)" if self._email
                         else "bearer token (Data Center PAT)",
            "space_filter": self.space or None,
            "label_filter": self.labels or None,
            "connected": False,
        }
        # 1. reachability + auth
        try:
            r = self.client.get("/rest/api/space", params={"limit": 1})
        except Exception as e:
            out["stage_failed"] = "connect"
            out["error"] = f"{type(e).__name__}: {e}"
            out["verdict"] = (
                "Cannot reach the Confluence base URL. Check CONFLUENCE_BASE_URL, "
                "network/VPN access from this host, and any proxy settings."
            )
            return out
        out["http_status"] = r.status_code
        if r.status_code in (401, 403):
            out["stage_failed"] = "auth"
            out["verdict"] = (
                f"Reached the server but authentication failed ({r.status_code}). "
                "For Cloud: CONFLUENCE_EMAIL must be your account email and the "
                "token an API token from id.atlassian.com. For Data Center: "
                "leave CONFLUENCE_EMAIL empty and use a personal access token."
            )
            return out
        if r.status_code >= 400:
            out["stage_failed"] = "api"
            out["verdict"] = (
                f"Unexpected HTTP {r.status_code} from /rest/api/space. If the "
                "base URL points at a page rather than the API root, fix it "
                "(Cloud usually ends in /wiki)."
            )
            out["body_excerpt"] = r.text[:200]
            return out
        out["connected"] = True

        # 2. who am I (best effort; endpoint differs across deployments)
        try:
            u = self.client.get("/rest/api/user/current")
            if u.status_code < 400:
                j = u.json()
                out["authenticated_as"] = (j.get("email") or j.get("username")
                                           or j.get("displayName") or j.get("accountId"))
        except Exception:
            pass

        # 3. does the configured space exist and is it visible to this token?
        if self.space:
            try:
                sp = self.client.get(f"/rest/api/space/{self.space}")
                if sp.status_code < 400:
                    out["space_ok"] = True
                    out["space_name"] = sp.json().get("name")
                else:
                    out["space_ok"] = False
                    out["verdict"] = (
                        f"Connected, but space {self.space!r} returned HTTP "
                        f"{sp.status_code} — wrong key, or this token cannot see "
                        "it. Space keys are case-sensitive."
                    )
                    return out
            except Exception as e:
                out["space_ok"] = False
                out["space_error"] = str(e)[:200]
        else:
            out["space_ok"] = None
            out["scope_warning"] = (
                "No space filter set — searches run across everything this token "
                "can read, so an unrelated page can outrank the finance policy."
            )

        # 4. do the configured labels match any pages at all?
        if self.labels:
            joined = ", ".join(f'"{l}"' for l in self.labels)
            cql = f"type = page AND label IN ({joined})"
            if self.space:
                cql += f' AND space = "{self.space}"'
            try:
                lr = self.client.get("/rest/api/content/search",
                                     params={"cql": cql, "limit": 5})
                if lr.status_code < 400:
                    res = lr.json().get("results", [])
                    out["labelled_page_count"] = lr.json().get("size", len(res))
                    out["labelled_examples"] = [x.get("title") for x in res]
                    if not res:
                        out["verdict"] = (
                            f"Connected, but NO pages carry the configured "
                            f"labels {self.labels}. Policy lookups will return "
                            "nothing. Add those labels to the pages in "
                            "Confluence (see docs/SETUP.md section 7.3)."
                        )
                        return out
                else:
                    out["label_check_status"] = lr.status_code
            except Exception as e:
                out["label_error"] = str(e)[:200]

        out["verdict"] = (
            "Connected and scoped. Run a real question through wiki_search to "
            "confirm the right pages rank first."
        )
        return out

    def search(self, query: str, limit: int = 5) -> list[dict]:
        clean = (query or "").replace('"', " ").strip()
        cql = f'type = page AND text ~ "{clean}"'
        if self.space:
            cql += f' AND space = "{self.space}"'
        if self.labels:
            # Label scoping keeps finance answers inside pages Finance owns,
            # instead of whatever the full-text index happens to rank first.
            joined = ", ".join(f'"{l}"' for l in self.labels)
            cql += f" AND label IN ({joined})"
        r = self.client.get(
            "/rest/api/content/search",
            params={"cql": cql, "limit": max(int(limit or 5), 1),
                    "expand": "space,version,history.lastUpdated"},
        )
        if r.status_code >= 400:
            raise WikiError(f"Confluence search failed ({r.status_code}): {r.text[:300]}")
        out = []
        for item in r.json().get("results", []):
            link = item.get("_links", {}).get("webui", "")
            out.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "space": (item.get("space") or {}).get("key"),
                    "version": (item.get("version") or {}).get("number"),
                    "url": f"{self.base}{link}" if link else None,
                }
            )
        return out

    def get_page(self, page_id: str) -> dict:
        r = self.client.get(
            f"/rest/api/content/{page_id}",
            params={"expand": "body.storage,space,version"},
        )
        if r.status_code >= 400:
            raise WikiError(f"Confluence get_page failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        space = (data.get("space") or {}).get("key")
        if self.space and space and space != self.space:
            # A page id can point anywhere; re-check scope after the fetch so a
            # configured space filter cannot be bypassed by id alone.
            raise WikiError(
                f"Page {page_id} is in space {space!r}, outside the configured "
                f"space {self.space!r}"
            )
        html = data.get("body", {}).get("storage", {}).get("value", "")
        text = html_to_text(html)
        link = data.get("_links", {}).get("webui", "")
        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "space": space,
            "version": (data.get("version") or {}).get("number"),
            "last_modified": ((data.get("version") or {}).get("when")),
            "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "content_sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
            "text": text[:MAX_PAGE_CHARS],
            "truncated": len(text) > MAX_PAGE_CHARS,
            "url": f"{self.base}{link}" if link else None,
        }


def make_wiki(cfg: Config):
    """Build the wiki provider.

    'confluence' fails closed: if it is configured but unreachable the server
    reports no wiki rather than silently serving the bundled sample policies,
    which would pair a live ledger with fictional thresholds. Use 'auto' only
    for local development.
    """
    w = cfg.wiki
    mode = (w.provider or "auto").lower()
    confluence_ready = bool(w.confluence_base_url and w.confluence_api_token)
    labels = [l.strip() for l in (w.confluence_labels or "").split(",") if l.strip()]
    if mode == "confluence" or (mode == "auto" and confluence_ready):
        if not confluence_ready:
            raise WikiError(
                "wiki.provider is 'confluence' but CONFLUENCE_BASE_URL / "
                "CONFLUENCE_API_TOKEN are not set"
            )
        return ConfluenceWiki(
            w.confluence_base_url, w.confluence_email, w.confluence_api_token,
            w.confluence_space, labels,
        )
    if mode == "localdocs" or mode == "auto":
        return LocalDocsWiki(cfg.resolve_path(w.localdocs_path))
    raise WikiError(f"Unknown wiki.provider {mode!r} — use confluence, localdocs, or auto")
