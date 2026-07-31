"""Build identity, so a running instance can be matched to source.

Most deployment confusion on this project has been the same question: is the
code that is RUNNING the code I think I deployed? A ZIP download carries no
git metadata, an old `nohup` process outlives a pull, and a browser serves
cached JavaScript — each looks identical to "the feature is missing".

This reports whatever identity is actually available, in order:
  * git commit, when the deployment is a clone (most reliable)
  * BUILD_INFO written by scripts/setup.py at install time
  * a content fingerprint of the source files, which always works

The fingerprint is the fallback that matters: it is derived from the code on
disk, so two deployments showing the same fingerprint ARE running the same
code, ZIP or clone, no git required.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parents[1]
BUILD_INFO = ROOT / "BUILD_INFO.json"

# Files whose content defines behavior. Sample data, logs and the venv are
# excluded so a reseed or a log write does not look like a code change.
_FINGERPRINT_GLOBS = ("pstb/**/*.py", "pstb/gui/static/*.html",
                      "scripts/*.py", "reports/*.json", "sql/oracle/*.sql")

_cache: dict = {}


def _git(*args: str) -> str:
    try:
        out = subprocess.run(("git",) + args, cwd=str(ROOT), capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def source_fingerprint() -> str:
    """Short hash of the source on disk — works without git."""
    digest = hashlib.sha256()
    for pattern in _FINGERPRINT_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            try:
                digest.update(path.relative_to(ROOT).as_posix().encode())
                digest.update(path.read_bytes())
            except OSError:
                continue
    return digest.hexdigest()[:12]


def build_info() -> dict:
    """Identity of the running build. Cached: the fingerprint reads every
    source file, which is cheap once but not per request."""
    if _cache:
        return _cache

    info = {"version": __version__, "fingerprint": source_fingerprint()}

    commit = _git("rev-parse", "--short", "HEAD")
    if commit:
        info["commit"] = commit
        info["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
        info["commit_date"] = _git("log", "-1", "--format=%cd", "--date=short")
        info["subject"] = _git("log", "-1", "--format=%s")[:80]
        # A dirty tree means the running code is NOT what the commit says.
        info["dirty"] = bool(_git("status", "--porcelain"))
        info["source"] = "git"
    elif BUILD_INFO.exists():
        try:
            info.update(json.loads(BUILD_INFO.read_text(encoding="utf-8")))
            info["source"] = "build file"
        except (OSError, ValueError):
            info["source"] = "fingerprint only"
    else:
        info["source"] = "fingerprint only (ZIP deployment, no git metadata)"

    info["root"] = str(ROOT)
    info["pid"] = os.getpid()
    _cache.update(info)
    return info


def label() -> str:
    """One-line identity for a header, a log line, or a support question."""
    info = build_info()
    if info.get("commit"):
        return "{0} {1}{2}".format(info["version"], info["commit"],
                                   "+local-changes" if info.get("dirty") else "")
    return "{0} build {1}".format(info["version"], info["fingerprint"])


def write_build_info() -> Path:
    """Record identity at install time, so a ZIP deployment still reports a
    commit. Called by scripts/setup.py."""
    info = {k: v for k, v in build_info().items()
            if k in ("version", "commit", "branch", "commit_date", "subject")}
    info["fingerprint"] = source_fingerprint()
    BUILD_INFO.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return BUILD_INFO


def main() -> int:
    """python -m pstb.version — identity of this deployment."""
    import sys as _sys

    info = build_info()
    print(label())
    for key in ("branch", "commit_date", "subject", "fingerprint", "source",
                "root"):
        if info.get(key):
            print("  {0:<12} {1}".format(key, info[key]))
    if info.get("dirty"):
        print("  WARNING      working tree has uncommitted changes; the "
              "running code does not match the commit above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
