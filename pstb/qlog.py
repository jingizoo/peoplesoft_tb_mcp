"""Question log: every chat turn appended to logs/questions.jsonl with
auto-detected failure flags, so unanswered/misrouted prompts become a
reviewable backlog instead of vanishing.

Auto flags per turn:
  tool_error       — at least one tool call returned an error
  no_tool_calls    — a data-sounding question answered with no tool call
  max_rounds       — the agent loop hit its round limit
  gave_up          — the answer says it can't / data not available

The user can also mark a turn bad from the web UI (thumbs-down), which appends
a feedback record referencing the turn id.

Review the backlog:  python -m pstb.qlog [logs/questions.jsonl]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

_DATAISH = re.compile(
    r"(?i)\b(balance|aging|invoice|customer|journal|ledger|budget|revenue|"
    r"expense|account|period|fiscal|report|billing|suspense|variance|rate|"
    r"total|how (much|many)|top \d+)\b"
)
_GAVE_UP = re.compile(
    r"(?i)(not available|cannot (find|answer|determine)|no data|unable to|"
    r"could not find|doesn'?t exist)"
)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_DIGEST = re.compile(r"^[a-fA-F0-9]{12,128}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_REFUSAL_CATEGORIES = frozenset({
    "access_boundary", "catalog_validation", "credentials", "disabled",
    "policy_boundary", "query_cost", "remote_reference", "request_scope",
    "schema_boundary", "source_boundary", "sql_safety", "timeout",
    "tool_error",
})
_COMPLETENESS_STATUSES = frozenset({
    "complete", "incomplete", "partial", "refused", "unavailable",
    "unknown",
})
_RELATION_EVIDENCE = frozenset({
    "foreign_key", "same_object", "shared_columns_and_indexes",
    "view_dependency",
})
_RELATION_CONFIDENCE = frozenset({
    "confirmed", "corroborated", "high", "inconclusive", "inferred",
    "low", "medium", "none", "observed",
})
DEFAULT_MAX_LOG_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 3
MAX_QUESTION_CHARS = 8_000
MAX_FEEDBACK_CHARS = 4_000

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_LOCATOR_ASSIGNMENT = re.compile(
    r"(?i)\b(dsn|data\s+source|server|host|service(?:_name)?|uid|user\s+id)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_CREDENTIAL_URI = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@[^\s]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_QUALIFIED_OBJECT = re.compile(
    r"(?i)\b(?:table|view|record|object)\s+"
    r"[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*)+"
)
_SQL_TEXT = re.compile(
    r"(?is)\bselect\b.{0,8000}?\bfrom\b|"
    r"\bwith\s+[A-Za-z][A-Za-z0-9_$#]*\s+as\s*\(|"
    # Statement-shaped text at the start of a pasted line. This catches DML,
    # DDL/PLSQL, and SQL Server-valid SELECT expressions without treating an
    # ordinary sentence such as 'update the invoice status' as SQL.
    r"(?:^|[\r\n;:])\s*(?:"
    r"select\b|insert\s+into\b|"
    r"update\s+(?:[\[\]\"A-Za-z0-9_$#.]+)\s+set\b|"
    r"delete\s+from\b|merge\s+into\b|"
    r"(?:create|alter|drop|truncate)\s+"
    r"(?:table|view|index|schema|procedure|function|package|trigger|"
    r"sequence|database|user|role)\b|"
    r"grant\b|revoke\b|begin\b|declare\b|call\b|exec(?:ute)?\b)"
)


def redact_private_text(value: object, *, limit: int) -> str:
    """Remove common credentials, locators and literal SQL from local text.

    The question/feedback stream is an owner-only learning aid, not exported
    telemetry, but users paste connection strings and queries into ordinary
    prose. Keeping those verbatim is unnecessary for routing analysis.
    """
    text = str(value or "")[:max(int(limit), 0)]
    if _SQL_TEXT.search(text):
        return "[SQL REDACTED]"
    text = _CREDENTIAL_URI.sub(r"\1[REDACTED]", text)
    text = _BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _LOCATOR_ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _QUALIFIED_OBJECT.sub("object [REDACTED]", text)
    return text


def refusal_category(detail: object) -> str:
    """Classify a refusal without persisting its raw text.

    Database errors often contain SQL fragments, bind values, object names or
    vendor locator details.  Those are useful in the live response but do not
    belong in a long-lived learning log.  The category is enough to count and
    route the failure without retaining that sensitive context.
    """
    low = str(detail or "").casefold()
    if not low:
        return "tool_error"
    if "source_boundary" in low or "database workspace" in low:
        return "source_boundary"
    if ("request_scope_conflict" in low or "requires a user-selected" in low
            or "request scope" in low):
        return "request_scope"
    if ("not authorised" in low or "not authorized" in low
            or "row security" in low):
        return "access_boundary"
    if ("outside the selected source" in low
            or "outside this source" in low
            or "allowed schemas" in low
            or "configured boundary" in low):
        return "schema_boundary"
    if ("database link" in low or "remote rowset" in low
            or "remote database" in low or "three-part" in low
            or "four-part" in low):
        return "remote_reference"
    if ("only select/with" in low or "multiple statements" in low
            or "statement rejected" in low or "read-only" in low
            or "dml" in low or "ddl" in low):
        return "sql_safety"
    if ("does not exist" in low or "verify names" in low
            or "metadata catalog" in low or "catalog is" in low):
        return "catalog_validation"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if ("full scan" in low or "cost gate" in low
            or "query is too expensive" in low):
        return "query_cost"
    if "credential" in low or "logon" in low or "login failed" in low:
        return "credentials"
    if "disabled" in low:
        return "disabled"
    if "policy" in low or "wiki" in low:
        return "policy_boundary"
    return "tool_error"


def _result_completeness(payload: dict, ok: bool) -> dict:
    """Extract only declared coverage signals, never rows or values."""
    if not ok:
        return {"status": "refused"}
    out: dict = {}
    evidence = payload.get("evidence_completeness")
    if isinstance(evidence, dict) and isinstance(evidence.get("complete"), bool):
        out["complete"] = evidence["complete"]
    control_status = str(payload.get("control_status") or "")
    if control_status in {
            "passed", "exceptions_found", "checks_incomplete", "not_run"}:
        out["control_status"] = control_status
        if control_status in {"passed", "exceptions_found"}:
            out.setdefault("complete", True)
        else:
            out.setdefault("complete", False)
    for key in ("truncated", "partial", "available", "graph_truncated"):
        if isinstance(payload.get(key), bool):
            out[key] = payload[key]
    if out.get("available") is False:
        status = "unavailable"
    elif out.get("complete") is False:
        status = "incomplete"
    elif (out.get("truncated") is True or out.get("partial") is True
          or out.get("graph_truncated") is True):
        status = "partial"
    elif out.get("complete") is True:
        status = "complete"
    else:
        # ``truncated=false`` alone says only that a generic display cap was
        # not hit.  It does not prove source population or control evidence
        # completeness, so it must stay unknown absent an explicit contract.
        status = "unknown"
    out["status"] = status
    return out


def _schema_coverage_observation(raw: object, allowed_schemas=()) -> dict:
    if not isinstance(raw, dict):
        return {}
    allowed = []
    for value in allowed_schemas or ():
        schema = str(value or "")
        if _IDENTIFIER.fullmatch(schema) and schema not in allowed:
            allowed.append(schema)
    if not allowed:
        return {}
    raw_counts = raw.get("object_counts")
    raw_counts = raw_counts if isinstance(raw_counts, dict) else {}
    counts = {}
    for schema in allowed:
        value = raw_counts.get(schema)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[schema] = value
    raw_missing = raw.get("missing") or raw.get("missing_schemas")
    raw_missing = raw_missing if isinstance(raw_missing, list) else []
    missing = [
        schema for schema in allowed
        if schema in raw_missing or counts.get(schema) == 0
    ]
    return {
        "default_schema": allowed[0],
        "schema_allowlist": allowed,
        "object_counts": counts,
        "missing_schemas": missing,
        "complete": (raw.get("complete") is True
                     and not missing
                     and len(counts) == len(allowed)),
    }


def _latest_build_observation(raw: object, allowed_schemas=()) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    run_id = str(raw.get("build_run_id") or "")
    if re.fullmatch(r"[a-f0-9]{32}", run_id):
        out["build_run_id"] = run_id
    attempted = str(raw.get("attempted_at") or "")
    if _TIMESTAMP.fullmatch(attempted):
        out["attempted_at"] = attempted
    if isinstance(raw.get("published"), bool):
        out["published"] = raw["published"]
    status = str(raw.get("status") or "")
    if status in {"building", "complete", "partial", "failed"}:
        out["status"] = status
    for key in ("snapshot_id", "previous_snapshot_id"):
        digest = str(raw.get(key) or "")
        if _DIGEST.fullmatch(digest):
            out[key] = digest.lower()
    category = str(raw.get("failure_category") or "")
    if category in {"metadata_unavailable", "build_error",
                    "status_write_error", "snapshot_mismatch"}:
        out["failure_category"] = category
    if isinstance(raw.get("snapshot_matches"), bool):
        out["snapshot_matches"] = raw["snapshot_matches"]
    coverage = _schema_coverage_observation(
        raw.get("schema_coverage"), allowed_schemas)
    if coverage:
        out["schema_coverage"] = coverage
    return out


def _catalog_observation(payload: dict, allowed_schemas=()) -> dict:
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    fingerprint = (snapshot.get("source_fingerprint")
                   or payload.get("source_fingerprint"))
    observed = {
        "fingerprint": fingerprint,
        "snapshot_id": snapshot.get("id"),
        "built_at": snapshot.get("built_at"),
        "version": (snapshot.get("schema_version")
                    or payload.get("schema_version")),
        "status": snapshot.get("status"),
        "stale": snapshot.get("stale"),
        "partial": snapshot.get("partial"),
    }
    raw_coverage = (snapshot.get("schema_coverage")
                    or payload.get("schema_coverage"))
    coverage = _schema_coverage_observation(
        raw_coverage, allowed_schemas)
    if coverage:
        observed["schema_coverage"] = coverage
    latest_build = _latest_build_observation(
        snapshot.get("latest_build") or payload.get("latest_build"),
        allowed_schemas,
    )
    if latest_build:
        observed["latest_build"] = latest_build
    return {key: value for key, value in observed.items()
            if value not in (None, "")}


def _relationship_observation(tool: str, payload: dict) -> dict:
    if tool != "join_path":
        return {}
    out: dict = {}
    if isinstance(payload.get("found"), bool):
        out["found"] = payload["found"]
    if payload.get("found") is not True:
        # These classes describe what the traversal searched, not evidence it
        # observed. Counting them on a not-found result overstates graph
        # coverage, so telemetry retains only the not-found fact.
        return out
    confidences: set[str] = set()
    top_confidence = payload.get("confidence")
    if isinstance(top_confidence, str) and top_confidence:
        confidences.add(top_confidence.casefold())
    evidence: set[str] = set()
    declared_evidence = payload.get("relationship_evidence_classes")
    if isinstance(declared_evidence, list):
        evidence.update(
            str(value) for value in declared_evidence
            if str(value) in _RELATION_EVIDENCE)
    hops = payload.get("hops")
    if isinstance(hops, list):
        for hop in hops:
            if not isinstance(hop, dict):
                continue
            relation = str(hop.get("relationship") or "").casefold()
            if relation in _RELATION_EVIDENCE:
                evidence.add(relation)
            confidence = str(hop.get("confidence") or "").casefold()
            if confidence:
                confidences.add(confidence)
    if not evidence and payload.get("found") is True and not hops:
        evidence.add("same_object")
    if not evidence and ("caveat" in payload or "indexed_both_sides" in payload):
        evidence.add("shared_columns_and_indexes")
    if confidences:
        out["confidence"] = sorted(confidences)
    if evidence:
        out["evidence_class"] = sorted(evidence)
    return out


def observe_tool_call(*, tool: str, output: object, ms: object, ok: bool,
                      problem: object = "", expected_source: str = "",
                      allowed_schemas=()) -> dict:
    """Build the secret-free per-call envelope stored by ``QuestionLog``.

    SQL text, arguments, binds, table/object names, result rows and raw errors
    are intentionally absent.  Only structural boundary and coverage facts
    from the server-issued payload survive.
    """
    try:
        payload = json.loads(output) if isinstance(output, str) else output
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rec: dict = {"tool": str(tool or "")[:80], "ok": bool(ok)}
    if isinstance(ms, (int, float)) and ms >= 0:
        rec["ms"] = int(ms)
    result_source = str(payload.get("source_database") or "").strip()
    expected = str(expected_source or "").strip()
    source_mismatch = bool(result_source and expected
                           and result_source != expected)
    if result_source and expected and result_source == expected:
        rec["result_source"] = result_source[:128]
        rec["result_source_verified"] = True
        rec["result_source_basis"] = "payload"
    elif source_mismatch:
        # Never persist an arbitrary payload label as though it were a
        # configured source.  The mismatch itself is a safe structural fact.
        rec["result_source_verified"] = False
        if not ok:
            rec["refusal_category"] = refusal_category(
                problem or payload.get("error"))
        return rec
    elif not result_source and expected == "default" and ok:
        # Curated PeopleSoft tools have no source argument and are physically
        # bound to the primary engine. Preserve that audited registry basis
        # without pretending a Coupa/wiki/unclassified tool came from the DB.
        from .guards import source_of_tool
        if source_of_tool(str(tool or "")).startswith("peoplesoft_"):
            rec["result_source"] = "default"
            rec["result_source_verified"] = True
            rec["result_source_basis"] = "primary_tool_registry"
    owners = payload.get("target_owners")
    if isinstance(owners, list):
        allowed = {
            str(schema) for schema in (allowed_schemas or ())
            if _IDENTIFIER.fullmatch(str(schema or ""))
        }
        if allowed:
            rec["target_owners"] = sorted({
                str(owner) for owner in owners
                if (_IDENTIFIER.fullmatch(str(owner or ""))
                    and str(owner) in allowed)
            })
    completeness = _result_completeness(payload, bool(ok))
    rec["result_completeness"] = completeness
    catalog = _catalog_observation(payload, allowed_schemas)
    if catalog:
        rec["catalog"] = catalog
    relationship = _relationship_observation(str(tool or ""), payload)
    if relationship:
        rec["relationship_path"] = relationship
    if not ok:
        rec["refusal_category"] = refusal_category(
            problem or payload.get("error"))
    return rec


def _safe_source_context(source_database: str, supplied: object) -> dict:
    ctx = supplied if isinstance(supplied, dict) else {}
    out = {"canonical_source": source_database}
    default = str(ctx.get("default_schema") or "")
    if _IDENTIFIER.fullmatch(default):
        out["default_schema"] = default
    schemas = ctx.get("schema_allowlist")
    if isinstance(schemas, (list, tuple)):
        allowed = []
        for value in schemas:
            schema = str(value or "")
            if _IDENTIFIER.fullmatch(schema) and schema not in allowed:
                allowed.append(schema)
        if allowed:
            out["schema_allowlist"] = allowed
    return out


def _safe_tool_record(call: object, source_database: str,
                      allowed_schemas=()) -> dict:
    """Defensively re-select structural fields at the persistence boundary."""
    c = call if isinstance(call, dict) else {}
    out = {
        "tool": str(c.get("tool") or "")[:80],
        "ok": bool(c.get("ok", True)),
    }
    if isinstance(c.get("ms"), (int, float)) and c["ms"] >= 0:
        out["ms"] = int(c["ms"])
    source = str(c.get("result_source") or "").strip()
    if (source and source == source_database
            and c.get("result_source_verified") is True):
        out["result_source"] = source[:128]
        out["result_source_verified"] = True
        basis = str(c.get("result_source_basis") or "")
        if basis in {"payload", "primary_tool_registry"}:
            out["result_source_basis"] = basis
    elif c.get("result_source_verified") is False:
        out["result_source_verified"] = False
    owners = c.get("target_owners")
    if isinstance(owners, list):
        allowed = {str(schema) for schema in (allowed_schemas or ())}
        safe = sorted({str(v) for v in owners
                       if _IDENTIFIER.fullmatch(str(v or ""))
                       and str(v) in allowed}) if allowed else []
        if safe:
            out["target_owners"] = safe
    category = str(c.get("refusal_category") or "")
    if category in _REFUSAL_CATEGORIES:
        out["refusal_category"] = category
    completeness = c.get("result_completeness")
    if isinstance(completeness, dict):
        safe_comp = {
            key: value for key, value in completeness.items()
            if (key in {"complete", "truncated", "partial", "available",
                        "graph_truncated"} and isinstance(value, bool))
        }
        control_status = str(completeness.get("control_status") or "")
        if control_status in {
                "passed", "exceptions_found", "checks_incomplete", "not_run"}:
            safe_comp["control_status"] = control_status
        status = str(completeness.get("status") or "")
        if status in _COMPLETENESS_STATUSES:
            safe_comp["status"] = status
        if safe_comp:
            out["result_completeness"] = safe_comp
    catalog = c.get("catalog")
    if isinstance(catalog, dict):
        safe_catalog: dict = {}
        digest = str(catalog.get("fingerprint") or "")
        if _DIGEST.fullmatch(digest):
            safe_catalog["fingerprint"] = digest.lower()
        snapshot_id = str(catalog.get("snapshot_id") or "")
        if _DIGEST.fullmatch(snapshot_id):
            safe_catalog["snapshot_id"] = snapshot_id.lower()
        built_at = str(catalog.get("built_at") or "")
        if _TIMESTAMP.fullmatch(built_at):
            safe_catalog["built_at"] = built_at
        version = catalog.get("version")
        if (isinstance(version, int) and not isinstance(version, bool)
                and 0 <= version <= 1_000_000):
            safe_catalog["version"] = version
        elif (isinstance(version, str) and len(version) <= 9
              and version.isdigit()):
            safe_catalog["version"] = int(version)
        status = str(catalog.get("status") or "")
        if status in {"complete", "partial", "unavailable"}:
            safe_catalog["status"] = status
        for key in ("stale", "partial"):
            if isinstance(catalog.get(key), bool):
                safe_catalog[key] = catalog[key]
        coverage = _schema_coverage_observation(
            catalog.get("schema_coverage"), allowed_schemas)
        if coverage:
            safe_catalog["schema_coverage"] = coverage
        latest_build = _latest_build_observation(
            catalog.get("latest_build"), allowed_schemas)
        if latest_build:
            safe_catalog["latest_build"] = latest_build
        if safe_catalog:
            out["catalog"] = safe_catalog
    relationship = c.get("relationship_path")
    if isinstance(relationship, dict):
        safe_relation: dict = {}
        if isinstance(relationship.get("found"), bool):
            safe_relation["found"] = relationship["found"]
        confidence = relationship.get("confidence")
        if isinstance(confidence, list):
            safe_relation["confidence"] = sorted({
                str(v) for v in confidence
                if str(v) in _RELATION_CONFIDENCE
            })
        evidence = relationship.get("evidence_class")
        if isinstance(evidence, list):
            safe_relation["evidence_class"] = sorted({
                str(v) for v in evidence if str(v) in _RELATION_EVIDENCE
            })
        if safe_relation:
            out["relationship_path"] = safe_relation
    return out


class QuestionLog:
    def __init__(self, path: Optional[str], root: Path, *,
                 max_bytes: int = DEFAULT_MAX_LOG_BYTES,
                 backups: int = DEFAULT_LOG_BACKUPS):
        self.path: Optional[Path] = None
        self._lock = threading.Lock()
        self._known_turns: set[str] = set()
        self.max_bytes = max(int(max_bytes or 0), 1024)
        self.backups = min(max(int(backups or 0), 1), 20)
        if path:
            p = Path(path)
            self.path = p if p.is_absolute() else root / p

        self._harden_existing_files()
        self._load_known_turns()

    @staticmethod
    def _open_regular(path: Path, flags: int = os.O_RDONLY) -> int:
        """Open an existing regular file without following a symlink."""
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"refusing non-regular question log {path}")
            # On platforms without O_NOFOLLOW, compare the opened inode to
            # the directory entry so a symlink cannot slip through fallback.
            if not getattr(os, "O_NOFOLLOW", 0):
                before = path.lstat()
                opened = os.fstat(fd)
                if (before.st_dev, before.st_ino) != (
                        opened.st_dev, opened.st_ino):
                    raise OSError(f"refusing linked question log {path}")
            return fd
        except Exception:
            os.close(fd)
            raise

    def _paths(self):
        if not self.path:
            return []
        return [self.path, *[
            self.path.with_name(f"{self.path.name}.{index}")
            for index in range(1, self.backups + 1)
        ]]

    def _harden_existing_files(self) -> None:
        """Upgrade old regular logs/backups to owner-only without links."""
        for path in self._paths():
            try:
                fd = self._open_regular(path)
            except (FileNotFoundError, OSError):
                continue
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)

    def _load_known_turns(self) -> None:
        for path in self._paths():
            try:
                fd = self._open_regular(path)
            except (FileNotFoundError, OSError):
                continue
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    for line in handle:
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        turn_id = str(rec.get("turn_id") or "")
                        if (rec.get("type") == "turn"
                                and re.fullmatch(r"[A-Fa-f0-9]{12,64}",
                                                 turn_id)):
                            self._known_turns.add(turn_id)
            finally:
                if fd >= 0:
                    os.close(fd)

    def _rotate(self, incoming_bytes: int) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            current = self.path.lstat()
            if not stat.S_ISREG(current.st_mode):
                return
            if current.st_size + incoming_bytes <= self.max_bytes:
                return
        except OSError:
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            prior = self.path.with_name(f"{self.path.name}.{index}")
            if prior.exists():
                try:
                    if not stat.S_ISREG(prior.lstat().st_mode):
                        prior.unlink()
                        continue
                except OSError:
                    continue
                prior.replace(self.path.with_name(
                    f"{self.path.name}.{index + 1}"))
        first = self.path.with_name(f"{self.path.name}.1")
        self.path.replace(first)
        try:
            fd = self._open_regular(first)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _append(self, rec: dict) -> bool:
        if not self.path:
            return False
        try:
            # One QuestionLog instance serves concurrent GUI turns. Keep each
            # JSON record as one serialized append so two silo completions
            # cannot interleave into a torn line.
            line = json.dumps(rec, default=str) + "\n"
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate(len(line.encode("utf-8")))
                flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND
                         | getattr(os, "O_NOFOLLOW", 0))
                fd = os.open(self.path, flags, 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("question log is not a regular file")
                    os.fchmod(fd, 0o600)
                except OSError:
                    os.close(fd)
                    raise
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            return True
        except OSError:
            return False  # logging must never break the answer

    def has_turn(self, turn_id: str) -> bool:
        candidate = str(turn_id or "")
        with self._lock:
            return candidate in self._known_turns

    def log_turn(self, *, surface: str, provider: str, question: str,
                 calls: list[dict], rounds: int, answer: str,
                 hit_round_limit: bool = False,
                 scope: Optional[dict] = None,
                 source_context: Optional[dict] = None) -> str:
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        # Timestamp+question collided whenever two silos asked the same thing
        # in one second, making feedback attach to both/the wrong answer.
        # A random 128-bit ID is independent of wording, clock resolution and
        # process concurrency.
        turn_id = uuid.uuid4().hex
        active_scope = dict(scope or {})
        source_database = str(
            active_scope.get("source") or active_scope.get("db") or "default"
        ).strip() or "default"
        logged_scope = {"source": source_database}
        for key in ("business_unit", "ledger", "fiscal_year", "period"):
            value = active_scope.get(key)
            if value not in (None, ""):
                logged_scope[key] = value
        flags = []
        if any(not c.get("ok", True) for c in calls):
            flags.append("tool_error")
        if not calls and _DATAISH.search(question or ""):
            flags.append("no_tool_calls")
        if hit_round_limit:
            flags.append("max_rounds")
        if _GAVE_UP.search(answer or ""):
            flags.append("gave_up")
        safe_context = _safe_source_context(source_database, source_context)
        allowed_schemas = safe_context.get("schema_allowlist") or []
        recorded = self._append({
            "type": "turn", "turn_id": turn_id, "ts": ts,
            "surface": surface, "provider": provider,
            "source_database": source_database,
            "source_context": safe_context,
            "scope": logged_scope,
            "question": redact_private_text(
                question, limit=MAX_QUESTION_CHARS),
            "tools": [_safe_tool_record(
                c, source_database, allowed_schemas) for c in calls],
            "rounds": rounds,
            "answer_chars": len(answer or ""),
            "failed": bool(flags), "flags": flags,
        })
        if recorded:
            with self._lock:
                self._known_turns.add(turn_id)
        return turn_id

    def log_feedback(self, turn_id: str, verdict: str, note: str = "") -> None:
        if not self.has_turn(turn_id):
            return
        self._append({
            "type": "feedback", "turn_id": turn_id,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "verdict": verdict,
            "note": redact_private_text(note, limit=MAX_FEEDBACK_CHARS),
        })


def review(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"No log at {p}")
        return 1
    turns, feedback = [], {}
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "turn":
            turns.append(rec)
        elif rec.get("type") == "feedback":
            feedback[rec.get("turn_id")] = rec.get("verdict")
    bad = [t for t in turns
           if t.get("failed") or feedback.get(t.get("turn_id")) == "bad"]
    print(f"{len(turns)} turns logged, {len(bad)} flagged as failed/bad:\n")
    for t in bad:
        fb = feedback.get(t["turn_id"])
        tags = ",".join(t.get("flags", [])) + (",user_bad" if fb == "bad" else "")
        tools = ",".join(x["tool"] for x in t.get("tools", [])) or "-"
        print(f"  [{t['ts'][:16]}] ({tags}) tools={tools}")
        print(f"      local turn id: {t['turn_id']}")
    if not bad:
        print("  none — nothing flagged")
    return 0


if __name__ == "__main__":
    sys.exit(review(sys.argv[1] if len(sys.argv) > 1 else "logs/questions.jsonl"))
