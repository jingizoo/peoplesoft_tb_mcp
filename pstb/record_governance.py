"""Shared semantics for records an operator has excluded from answers.

Approval and selection are different decisions.  Approving the statement
"do not use XX_STAGE" must activate a veto, not turn the words "stage" and
"use" into positive search vocabulary.  This module keeps that distinction
small and deterministic so the legacy site-memory path and the source-bound
metadata path cannot drift apart.
"""
from __future__ import annotations

import re


# Deliberately require an explicit prohibition.  A table described merely as
# "staging" can be the correct source for an interface/error question; staging
# alone is therefore never enough to suppress it.  These forms cover existing
# free-text lessons created before the UI gained an explicit selection choice.
_EXPLICIT_EXCLUSION = re.compile(
    r"(?ix)"
    r"(?:\b(?:do\s+not|don['’]?t['’]?|never)\s+"
    r"(?:use|query|select|choose|recommend|read)\b)"
    r"|(?:\b(?:exclude|avoid)\s+(?:this\s+|the\s+)?"
    r"(?:record|table|view|object)\b)"
    r"|(?:\b(?:junk|obsolete|deprecated)\s+"
    r"(?:record|table|view|object)\b)",
)


def is_explicit_exclusion(text: object) -> bool:
    """Whether free text unambiguously says an object must not be selected."""
    return bool(_EXPLICIT_EXCLUSION.search(str(text or "")))


def selection_effect(text: object, *, active_status: str = "") -> str:
    """Return ``exclude`` or ``prefer`` independently of review lifecycle.

    ``active_status`` supports a future/native excluded status without making
    today's sidecar schema incompatible.  Existing approved negative lessons
    are recognized from their wording and become effective immediately after
    this release, without asking an operator to re-enter them.
    """
    if str(active_status or "").strip().casefold() == "excluded":
        return "exclude"
    return "exclude" if is_explicit_exclusion(text) else "prefer"


def exclusion_reason(text: object) -> str:
    """A bounded, one-line reason suitable for structured tool results."""
    return " ".join(str(text or "").strip().split())[:400]
