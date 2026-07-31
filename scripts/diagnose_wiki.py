#!/usr/bin/env python3
"""Prove whether the wiki is connected AND returning the right content.

    .venv/bin/python scripts/diagnose_wiki.py
    .venv/bin/python scripts/diagnose_wiki.py --query "suspense policy"
    .venv/bin/python scripts/diagnose_wiki.py --page 123456789

Stages: which provider is really active -> credentials -> connectivity/auth ->
space and label scoping -> a real search -> a real page fetch with provenance
and a content excerpt you can compare against Confluence in your browser.

Never prints the API token.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.wiki import ConfluenceWiki, WikiError, make_wiki  # noqa: E402

OK, WARN, BAD = "  [ok]  ", "  [warn]", "  [FAIL]"


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose the wiki connection")
    ap.add_argument("--query", default="suspense policy",
                    help="search phrase to test (default: 'suspense policy')")
    ap.add_argument("--page", default="", help="fetch a specific page id")
    ap.add_argument("--chars", type=int, default=600,
                    help="content excerpt length (default 600)")
    args = ap.parse_args()

    cfg = load_config(os.environ.get('PSTB_CONFIG')
                      or str(ROOT / 'config.yaml'))
    w = cfg.wiki
    problems: list[str] = []

    print("1. Configuration")
    print(f"     wiki.provider        = {w.provider}")
    print(f"     confluence_space     = {w.confluence_space or '(none)'}")
    print(f"     confluence_labels    = {w.confluence_labels or '(none)'}")
    print(f"     localdocs_path       = {w.localdocs_path}")

    print("\n2. Credentials (presence only)")
    have_url = bool(w.confluence_base_url)
    have_tok = bool(w.confluence_api_token)
    print(f"     CONFLUENCE_BASE_URL  = {w.confluence_base_url or '(empty)'}")
    print(f"     CONFLUENCE_EMAIL     = {w.confluence_email or '(empty — DC/PAT mode)'}")
    print(f"     CONFLUENCE_API_TOKEN = {'SET (' + str(len(w.confluence_api_token)) + ' chars)' if have_tok else '(empty)'}")
    if have_url != have_tok:
        print(f"{BAD} PARTIAL credentials: base URL and token must BOTH be set. "
              "With provider=auto this silently falls back to demo pages.")
        problems.append("partial credentials")

    print("\n3. Which provider is ACTUALLY active")
    try:
        wiki = make_wiki(cfg)
    except WikiError as e:
        print(f"{BAD} wiki disabled: {e}")
        print("\n     provider=confluence fails closed by design — it will not "
              "serve demo pages. Fix the credentials above.")
        return 1
    active = wiki.provider_name
    print(f"     ACTIVE PROVIDER = {active}")
    if active == "localdocs":
        if have_url or have_tok:
            print(f"{BAD} Confluence is (partly) configured but the LOCAL provider "
                  "is active — answers are coming from demo files, not your wiki.")
            problems.append("silent fallback to local docs")
        else:
            print(f"{WARN} No Confluence credentials, so local files are serving.")
    else:
        print(f"{OK} Confluence provider is active.")

    print("\n4. Health check")
    h = wiki.health()
    for k in ("base_url", "auth_mode", "authenticated_as", "http_status",
              "space_ok", "space_name", "labelled_page_count",
              "labelled_examples", "page_count", "is_bundled_demo_content"):
        if k in h and h[k] is not None:
            print(f"     {k:<24} = {h[k]}")
    if h.get("stage_failed"):
        print(f"{BAD} failed at stage: {h['stage_failed']}")
        problems.append(f"health stage {h['stage_failed']}")
    if h.get("scope_warning"):
        print(f"{WARN} {h['scope_warning']}")
    if h.get("is_bundled_demo_content"):
        problems.append("bundled demo content")
    print(f"     VERDICT: {h.get('verdict')}")

    print(f"\n5. Search test — {args.query!r}")
    try:
        hits = wiki.search(args.query, limit=5)
    except Exception as e:
        print(f"{BAD} search raised: {type(e).__name__}: {e}")
        return 1
    if not hits:
        print(f"{BAD} no results. If scoped by space/labels, the pages may not "
              "carry them; try widening confluence_labels temporarily.")
        problems.append("search returned nothing")
    for i, hit in enumerate(hits, 1):
        bits = [f"id={hit.get('id')}"]
        for k in ("space", "version"):
            if hit.get(k) is not None:
                bits.append(f"{k}={hit[k]}")
        print(f"     {i}. {hit.get('title')}  ({', '.join(bits)})")
        if hit.get("url"):
            print(f"        {hit['url']}")

    page_id = args.page or (hits[0].get("id") if hits else "")
    if page_id:
        print(f"\n6. Fetch test — page {page_id}")
        try:
            pg = wiki.get_page(str(page_id))
        except Exception as e:
            print(f"{BAD} fetch raised: {type(e).__name__}: {e}")
            return 1
        for k in ("title", "space", "version", "last_modified", "retrieved_at",
                  "content_sha256", "url", "truncated"):
            if pg.get(k) is not None:
                print(f"     {k:<15} = {pg[k]}")
        text = (pg.get("text") or "").strip()
        print(f"     chars           = {len(text)}")
        print("\n     --- content excerpt (compare this against Confluence) ---")
        for line in text[: args.chars].splitlines():
            print(f"     {line}")
        if len(text) > args.chars:
            print(f"     ... (+{len(text) - args.chars} more chars)")
        if len(text) < 100:
            print(f"\n{WARN} very little text extracted — the page may be mostly "
                  "macros, tables or attachments, which the storage-format "
                  "extractor cannot read as prose.")
            problems.append("thin page text")

    print("\n" + "=" * 68)
    if problems:
        print("NOT TRUSTWORTHY YET — issues found:")
        for p in problems:
            print(f"  - {p}")
        print("""
Fix order:
  1. Put CONFLUENCE_BASE_URL and CONFLUENCE_API_TOKEN in .env (both, or neither).
     Cloud: base URL usually ends in /wiki, CONFLUENCE_EMAIL = your account email.
     Data Center: leave CONFLUENCE_EMAIL empty, token = personal access token.
  2. Set wiki.provider: confluence in config.yaml — NOT auto. Explicit
     confluence fails closed instead of silently serving demo pages.
  3. Set confluence_space (e.g. FIN) and label the pages the agent may cite
     (gl-policy / gl-close / gl-coa), then set confluence_labels to match.
  4. Re-run this script; then ask a real question and confirm the cited page
     title is one of yours.""")
        return 1
    print("Wiki looks correctly connected and scoped.")
    print("Final confirmation: open the URL above in a browser and check the")
    print("excerpt matches, then ask a policy question and verify the citation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
