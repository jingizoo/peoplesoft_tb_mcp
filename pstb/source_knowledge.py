"""Governed, source-bound semantic annotations for metadata discovery.

Native catalogs can prove that a table and its columns exist, but a custom
application name often says nothing about its business purpose.  This module
stores the missing vocabulary as a *separate* per-database overlay.  It never
edits the derived metadata artifact and never stores rows, SQL, joins, status
semantics, credentials, or financial values.

The lifecycle is deliberately asymmetric:

* a chat may only PROPOSE a meaning for one exact catalog object;
* pending/rejected/revoked proposals have zero retrieval effect;
* a host operator approves after reviewing the private local file;
* every approved read is still bound to the canonical source, object id and
  secret-free endpoint/schema fingerprint captured from the catalog.

Approved meanings are pointers for object selection.  They do not alter the
catalog's structural confidence and cannot become relationship or SQL edges.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path
from typing import Iterable

try:  # POSIX advisory locking; Windows uses msvcrt below.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by Windows CI/deployments
    _fcntl = None
    import msvcrt as _msvcrt


def _lock_exclusive(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        return
    if os.fstat(fd).st_size == 0:  # Windows byte-range locks need one byte.
        os.write(fd, b"\0")
        os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)


def _unlock(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


SCHEMA_VERSION = 1
MAX_PROPOSALS = 500
MAX_PENDING = 100
MAX_MEANING_CHARS = 400
MAX_ALIASES = 12
MAX_ALIAS_CHARS = 80
MAX_SEARCH_RESULTS = 20
MAX_STORE_BYTES = 16 * 1024 * 1024

_WORD = re.compile(r"[A-Za-z0-9_$#]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SQL_START = re.compile(
    r"(?is)^\s*(?:select|with|insert|update|delete|merge|create|alter|drop|"
    r"truncate|grant|revoke|begin|declare|exec(?:ute)?)\b")
_SQL_FRAGMENT = re.compile(
    r"(?is)\bselect\b.{0,200}\bfrom\b|"
    r"\b(?:insert\s+into|update\s+[A-Za-z]|delete\s+from|merge\s+into|"
    r"create\s+(?:table|view)|alter\s+table|drop\s+(?:table|view))\b")
_SECRET = re.compile(
    r"(?i)\b(?:password|passwd|pwd|token|secret|api[_ -]?key|dsn)"
    r"\s*(?::|=|\bis\b)"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"
    r"|\b[A-Za-z][A-Za-z0-9_.-]{1,64}/[^\s/@]+@[^\s]+")
_PRIVATE_VALUE = re.compile(
    r"(?i)\b\d{3}-\d{2}-\d{4}\b|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_INSTRUCTION = re.compile(
    r"(?i)\b(?:ignore|override|disregard)\b.{0,30}\b(?:instruction|prompt|"
    r"policy|rule)s?\b|\b(?:system|developer)\s+prompt\b|"
    r"\b(?:call|invoke|run)\s+(?:the\s+)?(?:tool|command|sql)\b|"
    r"\b(?:run_sql|explain_query|join_path|search_metadata|"
    r"get_metadata_context|propose_metadata_meaning)\b")
_RELATION_OR_RULE = re.compile(
    r"(?i)\b(?:join(?:s|ed|ing)?|foreign\s+key|on\s+clause|"
    r"relationship\s+between|relates?\s+to|where|having|predicate|"
    r"above|below|greater\s+than|less\s+than|at\s+least|at\s+most|"
    r"equals?|equal\s+to|means?|signif(?:y|ies)|indicates?|represents?|"
    r"denotes?|maps?\s+to)\b|"
    r"\bmarked\s+[A-Za-z0-9_$#]+\s+(?:is|are)\b|"
    r"\bstatus\b.{0,30}\b(?:semantics?|is|means?|indicates?|represents?)\b|"
    r"(?:=|<>|!=|<=|>=)")
_LITERAL_VALUE = re.compile(
    r"(?i)(?<!\w)[$€£]\s*\d|"
    r"(?<![A-Za-z])\d+(?![A-Za-z])|"
    r"(?<!\w)\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|"
    r"(?<!\w)\d+\.\d+\b|"
    r"\b[A-Z]{1,5}\d{3,}\b")


class SourceKnowledgeError(RuntimeError):
    pass


class _SnapshotConnection(sqlite3.Connection):
    """Private staging DB persisted through a pinned directory fd."""

    _persist_snapshot = None
    _store_dir_fd: int | None = None
    _store_lock_fd: int | None = None
    _staging_path: str | None = None
    _staging_identity: tuple[int, int] | None = None

    def configure_store(self, *, persist_snapshot=None,
                        dir_fd: int | None = None,
                        lock_fd: int | None = None,
                        staging_path: str | None = None,
                        staging_identity: tuple[int, int] | None = None) -> None:
        self._persist_snapshot = persist_snapshot
        self._store_dir_fd = dir_fd
        self._store_lock_fd = lock_fd
        self._staging_path = staging_path
        self._staging_identity = staging_identity

    def commit(self) -> None:
        super().commit()
        if self._persist_snapshot is not None:
            try:
                self._persist_snapshot()
            except SourceKnowledgeError:
                raise
            except Exception as exc:
                raise SourceKnowledgeError(
                    "source knowledge could not be persisted safely") from exc

    def close(self) -> None:
        try:
            super().close()
        finally:
            staging_path, self._staging_path = self._staging_path, None
            staging_identity = self._staging_identity
            self._staging_identity = None
            if staging_path:
                try:
                    entry = os.lstat(staging_path)
                    if (stat.S_ISREG(entry.st_mode)
                            and (entry.st_dev, entry.st_ino)
                            == staging_identity):
                        os.unlink(staging_path)
                    elif stat.S_ISLNK(entry.st_mode):
                        os.unlink(staging_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            lock_fd, self._store_lock_fd = self._store_lock_fd, None
            dir_fd, self._store_dir_fd = self._store_dir_fd, None
            if lock_fd is not None:
                try:
                    _unlock(lock_fd)
                finally:
                    os.close(lock_fd)
            if dir_fd is not None:
                os.close(dir_fd)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _source_filename(source: str) -> str:
    canonical = str(source or "default").strip() or "default"
    slug = re.sub(r"[^a-z0-9]+", "-", canonical.casefold()).strip("-")
    slug = (slug or "source")[:40]
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.db"


def source_knowledge_path(cfg, source: str) -> Path:
    """Private sidecar path for exactly one canonical database source."""
    root = Path(getattr(cfg, "root", ".") or ".")
    return root / "source_knowledge" / _source_filename(source)


def _one_line(value: object, *, label: str, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise SourceKnowledgeError(f"{label} is required")
    if len(text) > limit:
        raise SourceKnowledgeError(
            f"{label} is limited to {limit} characters")
    if _CONTROL.search(str(value or "")):
        raise SourceKnowledgeError(f"{label} cannot contain control characters")
    if (_SQL_START.search(text) or _SQL_FRAGMENT.search(text)
            or _SECRET.search(text) or _PRIVATE_VALUE.search(text)
            or _INSTRUCTION.search(text) or _RELATION_OR_RULE.search(text)
            or _LITERAL_VALUE.search(text)):
        raise SourceKnowledgeError(
            f"{label} looks like SQL, a join/rule, an instruction, a "
            "credential, or a literal row value; "
            "metadata learning "
            "accepts only a short business description")
    return text


def normalize_aliases(values: object) -> list[str]:
    if values in (None, ""):
        return []
    if isinstance(values, str):
        raw: Iterable = re.split(r"[,;]", values)
    elif isinstance(values, (list, tuple, set, frozenset)):
        raw = values
    else:
        raise SourceKnowledgeError("aliases must be a list or comma-separated text")
    aliases = []
    for value in raw:
        alias = _one_line(value, label="alias", limit=MAX_ALIAS_CHARS)
        key = alias.casefold()
        if key not in {item.casefold() for item in aliases}:
            aliases.append(alias)
        if len(aliases) > MAX_ALIASES:
            raise SourceKnowledgeError(
                f"at most {MAX_ALIASES} aliases may be proposed at once")
    return aliases


def validate_catalog_aliases(catalog, source: str, object_id: str,
                             aliases: object) -> list[str]:
    """Refuse an alias that already identifies a different native object.

    Approved vocabulary may fill a semantic gap; it must never override an
    exact physical/logical identifier already proven by the source catalog.
    This check runs both before a chat proposal is stored and again at
    approval/use time so a later catalog rebuild cannot create a silent
    search/context disagreement.
    """
    normalized = normalize_aliases(aliases)
    target = str(object_id or "").strip()
    if not target:
        raise SourceKnowledgeError(
            "metadata alias validation needs the exact catalog object id")
    for alias in normalized:
        result = catalog.context(alias, source=source, limit=20)
        if not isinstance(result, dict):
            raise SourceKnowledgeError(
                "the current source catalog could not validate aliases")
        if result.get("ambiguous"):
            candidates = result.get("candidates")
            ids = {
                str(row.get("object_id") or "")
                for row in (candidates if isinstance(candidates, list) else [])
                if isinstance(row, dict) and row.get("object_id")
            }
            if not ids or ids != {target}:
                raise SourceKnowledgeError(
                    f"alias {alias!r} already matches ambiguous/native "
                    "catalog identifiers; choose business wording that does "
                    "not shadow an existing object")
            continue
        if result.get("found") is not True:
            continue
        subject = result.get("subject")
        existing = (str(subject.get("object_id") or "")
                    if isinstance(subject, dict) else
                    str(result.get("object_id") or ""))
        if existing != target:
            raise SourceKnowledgeError(
                f"alias {alias!r} already identifies another catalog object; "
                "approved vocabulary cannot override native metadata")
    return normalized


def explicit_metadata_lesson(question: str, object_name: str) -> bool:
    """Narrow guard for model-callable proposal tools.

    A proposal is legal only when the user's own turn names the target and
    uses correction/teaching language.  This stops a model from converting
    its inference about a cryptic table into a durable pending record.
    """
    text = " ".join(str(question or "").strip().split())
    target = str(object_name or "").strip()
    leaf = target.rsplit(".", 1)[-1]
    if not leaf:
        return False
    names = [target, leaf] if target.casefold() != leaf.casefold() else [leaf]
    name_pattern = "(?:" + "|".join(
        re.escape(name) for name in names) + ")"
    named = re.search(
        rf"(?i)(?<![A-Za-z0-9_$#]){name_pattern}(?![A-Za-z0-9_$#])",
        text)
    if not named:
        return False
    # A question about an object is not a lesson about it. Explicit
    # remember/note/teach language remains valid even when politely phrased.
    teaching = re.search(
        r"(?i)\b(?:remember|note\s+that|for\s+future(?:\s+reference)?|"
        r"teach|learn\s+that)\b", text)
    if (text.endswith("?") or re.match(
            r"(?i)^(?:what|which|does?|is|are|can|could|would|should|"
            r"where|why|how)\b", text)) and not teaching:
        return False
    statement = re.search(
        rf"(?i)(?<![A-Za-z0-9_$#]){name_pattern}(?![A-Za-z0-9_$#])"
        r".{0,30}\b(?:is|means|holds|contains|stores|is\s+used\s+for|"
        r"is\s+the\s+(?:table|view)\s+for)\b", text)
    correction = re.search(
        rf"(?i)(?:\b(?:actually|no)\b|\b(?:correct|right)\s+"
        rf"(?:table|view)\b).{{0,80}}{name_pattern}", text)
    directive = re.search(
        rf"(?i)\b(?:please\s+)?use\s+{name_pattern}\s+for\b", text)
    return bool(teaching or statement or correction or directive)


class SourceKnowledge:
    """One fingerprint-bound SQLite annotation store."""

    def __init__(self, path: str | Path, *, source: str,
                 source_fingerprint: str):
        self.path = Path(path)
        self.source = str(source or "").strip()
        fingerprint = str(source_fingerprint or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            fingerprint = "sha256:" + fingerprint
        self.source_fingerprint = fingerprint
        self._lock = threading.RLock()
        if not self.source:
            raise SourceKnowledgeError("source knowledge needs a canonical source")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_fingerprint):
            raise SourceKnowledgeError(
                "source knowledge needs the source catalog fingerprint")

    def _open_parent_fd(self, *, write: bool) -> int | None:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not self.path.parent.exists():
            return None
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path.parent, flags)
        except OSError as exc:
            raise SourceKnowledgeError(
                "source knowledge parent must be a real directory") from exc
        try:
            entry = os.fstat(fd)
            if not stat.S_ISDIR(entry.st_mode):
                raise SourceKnowledgeError(
                    "source knowledge parent must be a real directory")
            os.fchmod(fd, 0o700)
            if os.fstat(fd).st_mode & 0o077:
                raise SourceKnowledgeError(
                    "source knowledge parent must be owner-only")
            self._assert_parent_identity(fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def _assert_parent_identity(self, dir_fd: int) -> None:
        try:
            current = self.path.parent.lstat()
            pinned = os.fstat(dir_fd)
        except OSError as exc:
            raise SourceKnowledgeError(
                "source knowledge parent changed during access") from exc
        if (not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (pinned.st_dev, pinned.st_ino)):
            raise SourceKnowledgeError(
                "source knowledge parent changed during access")

    def _lock_writer(self, dir_fd: int) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = None
        try:
            fd = os.open(
                f".{self.path.name}.lock", flags, 0o600, dir_fd=dir_fd)
            entry = os.fstat(fd)
            if not stat.S_ISREG(entry.st_mode):
                raise SourceKnowledgeError(
                    "source knowledge lock must be a regular file")
            os.fchmod(fd, 0o600)
            _lock_exclusive(fd)
            return fd
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if isinstance(exc, SourceKnowledgeError):
                raise
            raise SourceKnowledgeError(
                "source knowledge could not acquire its private write lock"
            ) from exc

    def _read_snapshot(self, dir_fd: int) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path.name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SourceKnowledgeError(
                "source knowledge file must be a regular file, not a link"
            ) from exc
        try:
            entry = os.fstat(fd)
            if not stat.S_ISREG(entry.st_mode):
                raise SourceKnowledgeError(
                    "source knowledge file must be a regular file, not a link")
            os.fchmod(fd, 0o600)
            if os.fstat(fd).st_mode & 0o077:
                raise SourceKnowledgeError(
                    "source knowledge file must be owner-only")
            if entry.st_size > MAX_STORE_BYTES:
                raise SourceKnowledgeError(
                    "source knowledge exceeds its governed local size limit")
            chunks = []
            remaining = MAX_STORE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_STORE_BYTES:
                raise SourceKnowledgeError(
                    "source knowledge exceeds its governed local size limit")
            return data
        finally:
            os.close(fd)

    def _write_snapshot(self, dir_fd: int, data: bytes) -> None:
        self._assert_parent_identity(dir_fd)
        if len(data) > MAX_STORE_BYTES:
            raise SourceKnowledgeError(
                "source knowledge exceeds its governed local size limit")
        temporary = f".{self.path.name}.{os.urandom(8).hex()}.building"
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_NOFOLLOW", 0))
        fd = None
        try:
            fd = os.open(temporary, flags, 0o600, dir_fd=dir_fd)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short source knowledge write")
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.close(fd)
            fd = None
            # The pinned directory keeps persistence inside the intended
            # store even if its pathname is swapped. Replacing a concurrently
            # introduced symlink replaces that link, never its target.
            os.replace(
                temporary, self.path.name,
                src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
            self._assert_parent_identity(dir_fd)
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
            if isinstance(exc, SourceKnowledgeError):
                raise
            raise SourceKnowledgeError(
                "source knowledge could not be persisted safely") from exc

    def _create_staging(self, snapshot: bytes | None) -> tuple[str, tuple[int, int]]:
        fd, name = tempfile.mkstemp(
            prefix="pstb-source-knowledge-", suffix=".db")
        try:
            os.fchmod(fd, 0o600)
            if snapshot:
                view = memoryview(snapshot)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short source knowledge staging write")
                    view = view[written:]
            os.fsync(fd)
            entry = os.fstat(fd)
            return name, (entry.st_dev, entry.st_ino)
        except Exception as exc:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise SourceKnowledgeError(
                "source knowledge could not create private staging") from exc
        finally:
            os.close(fd)

    def _persist_staging(
        self, dir_fd: int, staging_path: str,
        staging_identity: tuple[int, int],
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(staging_path, flags)
        except OSError as exc:
            raise SourceKnowledgeError(
                "source knowledge staging changed during persistence") from exc
        try:
            entry = os.fstat(fd)
            if (not stat.S_ISREG(entry.st_mode)
                    or (entry.st_dev, entry.st_ino) != staging_identity
                    or entry.st_size > MAX_STORE_BYTES):
                raise SourceKnowledgeError(
                    "source knowledge staging changed during persistence")
            chunks = []
            remaining = MAX_STORE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_STORE_BYTES:
                raise SourceKnowledgeError(
                    "source knowledge exceeds its governed local size limit")
        finally:
            os.close(fd)
        self._write_snapshot(dir_fd, data)

    def _initialize_connection(
        self, con: _SnapshotConnection, *, write: bool
    ) -> None:
        con.row_factory = sqlite3.Row
        if write:
            # A copied legacy sidecar may retain WAL as its persistent journal
            # mode.  Snapshot persistence copies one completed SQLite file,
            # not the transient ``-wal`` companion, so force rollback-journal
            # mode before any mutation.  Otherwise commit() could acknowledge
            # rows that still live only in the staging WAL and are then lost.
            journal = con.execute("PRAGMA journal_mode=DELETE").fetchone()
            if not journal or str(journal[0] or "").casefold() != "delete":
                raise SourceKnowledgeError(
                    "source knowledge could not enter its safe journal mode")
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                  id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  source_fingerprint TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  schema_name TEXT NOT NULL,
                  object_name TEXT NOT NULL,
                  object_kind TEXT NOT NULL,
                  meaning TEXT NOT NULL,
                  aliases_json TEXT NOT NULL,
                  proposed_at TEXT NOT NULL,
                  origin TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  proposal_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  decided_at TEXT NOT NULL,
                  decided_by TEXT NOT NULL,
                  FOREIGN KEY (proposal_id) REFERENCES proposals(id)
                );
                CREATE INDEX IF NOT EXISTS proposals_object
                  ON proposals(source, object_id);
                CREATE INDEX IF NOT EXISTS decisions_proposal
                  ON decisions(proposal_id, event_id);
            """)
            existing = dict(con.execute(
                "SELECT key,value FROM meta").fetchall())
            if not existing:
                con.executemany(
                    "INSERT INTO meta(key,value) VALUES (?,?)", (
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("source", self.source),
                        ("source_fingerprint", self.source_fingerprint),
                        ("created_at", _now()),
                    ))
                con.commit()
        else:
            con.execute("PRAGMA query_only=ON")
        meta = dict(con.execute("SELECT key,value FROM meta").fetchall())
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise SourceKnowledgeError(
                "source knowledge schema is incompatible; review or archive "
                "the local sidecar before continuing")
        if meta.get("source") != self.source:
            raise SourceKnowledgeError(
                "source knowledge source mismatch; no annotations were used")
        if meta.get("source_fingerprint") != self.source_fingerprint:
            raise SourceKnowledgeError(
                f"source knowledge for {self.source!r} was created for a "
                "different endpoint/schema boundary; no annotations were used")
        # A single malformed row disables the optional overlay. Native
        # catalog discovery remains available through the caller's fail-soft
        # path, while no later proposal can normalize or overwrite tampering.
        for row in con.execute(self._current_sql()).fetchall():
            self._public(row)

    def _windows_parent_identity(self, *, write: bool) -> tuple[int, int] | None:
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not self.path.parent.exists():
            return None
        entry = self.path.parent.lstat()
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        if (not stat.S_ISDIR(entry.st_mode) or self.path.parent.is_symlink()
                or is_junction(self.path.parent)):
            raise SourceKnowledgeError(
                "source knowledge parent must be a real directory")
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise SourceKnowledgeError(
                "source knowledge parent could not be made private") from exc
        return entry.st_dev, entry.st_ino

    def _assert_windows_parent(self, identity: tuple[int, int]) -> None:
        current = self._windows_parent_identity(write=False)
        if current != identity:
            raise SourceKnowledgeError(
                "source knowledge parent changed during access")

    def _windows_lock_writer(self) -> int:
        path = self.path.parent / f".{self.path.name}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = None
        try:
            fd = os.open(path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SourceKnowledgeError(
                    "source knowledge lock must be a regular file")
            os.chmod(path, 0o600)
            _lock_exclusive(fd)
            return fd
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if isinstance(exc, SourceKnowledgeError):
                raise
            raise SourceKnowledgeError(
                "source knowledge could not acquire its private write lock"
            ) from exc

    def _windows_read_snapshot(self) -> bytes | None:
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode) or self.path.is_symlink():
            raise SourceKnowledgeError(
                "source knowledge file must be a regular file, not a link")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise SourceKnowledgeError(
                "source knowledge file could not be opened safely") from exc
        try:
            entry = os.fstat(fd)
            if ((entry.st_dev, entry.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(entry.st_mode)
                    or entry.st_size > MAX_STORE_BYTES):
                raise SourceKnowledgeError(
                    "source knowledge file changed during access")
            os.chmod(self.path, 0o600)
            chunks = []
            remaining = MAX_STORE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_STORE_BYTES:
                raise SourceKnowledgeError(
                    "source knowledge exceeds its governed local size limit")
            return data
        finally:
            os.close(fd)

    def _windows_write_snapshot(
        self, data: bytes, parent_identity: tuple[int, int]
    ) -> None:
        self._assert_windows_parent(parent_identity)
        if len(data) > MAX_STORE_BYTES:
            raise SourceKnowledgeError(
                "source knowledge exceeds its governed local size limit")
        temporary = self.path.parent / (
            f".{self.path.name}.{os.urandom(8).hex()}.building")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        fd = None
        try:
            fd = os.open(temporary, flags, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short source knowledge write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            self._assert_windows_parent(parent_identity)
            os.replace(temporary, self.path)
            self._assert_windows_parent(parent_identity)
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                temporary.unlink()
            except OSError:
                pass
            if isinstance(exc, SourceKnowledgeError):
                raise
            raise SourceKnowledgeError(
                "source knowledge could not be persisted safely") from exc

    def _windows_persist_staging(
        self, staging_path: str, staging_identity: tuple[int, int],
        parent_identity: tuple[int, int],
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(staging_path, flags)
        try:
            entry = os.fstat(fd)
            if (not stat.S_ISREG(entry.st_mode)
                    or (entry.st_dev, entry.st_ino) != staging_identity
                    or entry.st_size > MAX_STORE_BYTES):
                raise SourceKnowledgeError(
                    "source knowledge staging changed during persistence")
            data = b""
            while len(data) <= MAX_STORE_BYTES:
                chunk = os.read(fd, min(
                    1024 * 1024, MAX_STORE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > MAX_STORE_BYTES:
                raise SourceKnowledgeError(
                    "source knowledge exceeds its governed local size limit")
        finally:
            os.close(fd)
        self._windows_write_snapshot(data, parent_identity)

    def _connect_windows(self, *, write: bool) -> sqlite3.Connection | None:
        with self._lock:
            parent_identity = self._windows_parent_identity(write=write)
            if parent_identity is None:
                return None
            lock_fd = None
            con: _SnapshotConnection | None = None
            staging_path = None
            staging_identity = None
            try:
                if write:
                    lock_fd = self._windows_lock_writer()
                snapshot = self._windows_read_snapshot()
                if snapshot is None and not write:
                    return None
                if snapshot is not None and not snapshot:
                    raise SourceKnowledgeError(
                        "source knowledge is unreadable; no annotations were used")
                staging_path, staging_identity = self._create_staging(snapshot)
                con = sqlite3.connect(
                    staging_path, factory=_SnapshotConnection)
                persist_snapshot = (
                    (lambda staged=staging_path, identity=staging_identity,
                            parent=parent_identity:
                     self._windows_persist_staging(staged, identity, parent))
                    if write else None)
                con.configure_store(
                    persist_snapshot=persist_snapshot,
                    lock_fd=lock_fd,
                    staging_path=staging_path,
                    staging_identity=staging_identity,
                )
                lock_fd = None
                staging_path = None
                staging_identity = None
                self._initialize_connection(con, write=write)
                self._assert_windows_parent(parent_identity)
                return con
            except SourceKnowledgeError:
                if con is not None:
                    con.close()
                raise
            except sqlite3.DatabaseError as exc:
                if con is not None:
                    con.close()
                raise SourceKnowledgeError(
                    "source knowledge is unreadable; no annotations were used"
                ) from exc
            finally:
                if con is None:
                    if lock_fd is not None:
                        try:
                            _unlock(lock_fd)
                        finally:
                            os.close(lock_fd)
                    if staging_path is not None:
                        try:
                            entry = os.lstat(staging_path)
                            if (stat.S_ISREG(entry.st_mode)
                                    and (entry.st_dev, entry.st_ino)
                                    == staging_identity):
                                os.unlink(staging_path)
                            elif stat.S_ISLNK(entry.st_mode):
                                os.unlink(staging_path)
                        except OSError:
                            pass

    def _connect(self, *, write: bool) -> sqlite3.Connection | None:
        if os.name == "nt":  # pragma: no cover - exercised by Windows CI
            return self._connect_windows(write=write)
        with self._lock:
            dir_fd = self._open_parent_fd(write=write)
            if dir_fd is None:
                return None
            lock_fd = None
            con: _SnapshotConnection | None = None
            staging_path = None
            staging_identity = None
            try:
                if write:
                    lock_fd = self._lock_writer(dir_fd)
                snapshot = self._read_snapshot(dir_fd)
                if snapshot is None and not write:
                    os.close(dir_fd)
                    return None
                if snapshot is not None and not snapshot:
                    raise SourceKnowledgeError(
                        "source knowledge is unreadable; no annotations were used")
                staging_path, staging_identity = self._create_staging(snapshot)
                con = sqlite3.connect(
                    staging_path, factory=_SnapshotConnection)
                persist_snapshot = (
                    (lambda pinned_fd=dir_fd, staged=staging_path,
                            identity=staging_identity:
                     self._persist_staging(pinned_fd, staged, identity))
                    if write else None)
                con.configure_store(
                    persist_snapshot=persist_snapshot,
                    dir_fd=dir_fd,
                    lock_fd=lock_fd,
                    staging_path=staging_path,
                    staging_identity=staging_identity,
                )
                dir_fd = None
                lock_fd = None
                staging_path = None
                staging_identity = None
                self._initialize_connection(con, write=write)
                self._assert_parent_identity(con._store_dir_fd)
                return con
            except SourceKnowledgeError:
                try:
                    if con is not None:
                        con.close()
                except Exception:
                    pass
                raise
            except sqlite3.DatabaseError as exc:
                try:
                    if con is not None:
                        con.close()
                except Exception:
                    pass
                raise SourceKnowledgeError(
                    "source knowledge is unreadable; no annotations were used") from exc
            finally:
                if con is None:
                    if lock_fd is not None:
                        try:
                            _unlock(lock_fd)
                        finally:
                            os.close(lock_fd)
                    if dir_fd is not None:
                        os.close(dir_fd)
                    if staging_path is not None:
                        try:
                            entry = os.lstat(staging_path)
                            if (stat.S_ISREG(entry.st_mode)
                                    and (entry.st_dev, entry.st_ino)
                                    == staging_identity):
                                os.unlink(staging_path)
                            elif stat.S_ISLNK(entry.st_mode):
                                os.unlink(staging_path)
                        except OSError:
                            pass

    @staticmethod
    def _current_sql() -> str:
        return """
            SELECT P.*,
              COALESCE((SELECT D.status FROM decisions D
                        WHERE D.proposal_id=P.id
                        ORDER BY D.event_id DESC LIMIT 1),'pending') AS status,
              (SELECT D.decided_at FROM decisions D
               WHERE D.proposal_id=P.id
               ORDER BY D.event_id DESC LIMIT 1) AS decided_at
            FROM proposals P
        """

    def _public(self, row: sqlite3.Row) -> dict:
        def raw_field(name: str):
            try:
                return row[name]
            except (IndexError, KeyError) as exc:
                raise SourceKnowledgeError(
                    "source knowledge contains a malformed proposal; "
                    "no annotations were used") from exc

        def field(name: str, *, limit: int = 500) -> str:
            value = raw_field(name)
            text = str(value or "")
            if (not text or len(text) > limit or _CONTROL.search(text)
                    or text != " ".join(text.strip().split())):
                raise SourceKnowledgeError(
                    "source knowledge contains a malformed proposal; "
                    "no annotations were used")
            return text

        def timestamp(name: str) -> str:
            text = field(name, limit=40)
            try:
                parsed = dt.datetime.fromisoformat(text)
            except (TypeError, ValueError) as exc:
                raise SourceKnowledgeError(
                    "source knowledge contains a malformed timestamp; "
                    "no annotations were used") from exc
            if (parsed.tzinfo is None or len(text) > 40
                    or parsed.isoformat(timespec="seconds") != text):
                raise SourceKnowledgeError(
                    "source knowledge contains a malformed timestamp; "
                    "no annotations were used")
            return text

        try:
            aliases = json.loads(row["aliases_json"] or "[]")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise SourceKnowledgeError(
                "source knowledge contains malformed aliases; "
                "no annotations were used") from exc
        if not isinstance(aliases, list) or not all(
                isinstance(alias, str) for alias in aliases):
            raise SourceKnowledgeError(
                "source knowledge contains malformed aliases; "
                "no annotations were used")
        normalized_aliases = normalize_aliases(aliases)
        if normalized_aliases != aliases:
            raise SourceKnowledgeError(
                "source knowledge contains malformed aliases; "
                "no annotations were used")
        source = field("source", limit=80)
        fingerprint = field("source_fingerprint", limit=80)
        if source != self.source or fingerprint != self.source_fingerprint:
            raise SourceKnowledgeError(
                "source knowledge proposal crossed its source boundary; "
                "no annotations were used")
        status = field("status", limit=16)
        if status not in {"pending", "approved", "rejected", "revoked"}:
            raise SourceKnowledgeError(
                "source knowledge contains an invalid decision status; "
                "no annotations were used")
        kind = field("object_kind", limit=16)
        if kind not in {"table", "view"}:
            raise SourceKnowledgeError(
                "source knowledge contains an invalid object kind; "
                "no annotations were used")
        meaning = field("meaning", limit=MAX_MEANING_CHARS)
        if _one_line(
                meaning, label="meaning", limit=MAX_MEANING_CHARS) != meaning:
            raise SourceKnowledgeError(
                "source knowledge contains an unsafe meaning; "
                "no annotations were used")
        decided_at = raw_field("decided_at")
        proposal_id = field("id", limit=64)
        if not re.fullmatch(r"[0-9a-f]{16}", proposal_id):
            raise SourceKnowledgeError(
                "source knowledge contains a malformed proposal id; "
                "no annotations were used")
        proposed_at = timestamp("proposed_at")
        if status == "pending":
            if decided_at not in (None, ""):
                raise SourceKnowledgeError(
                    "source knowledge contains a malformed decision; "
                    "no annotations were used")
            approved_at = ""
        else:
            approved_at = timestamp("decided_at")
        return {
            "id": proposal_id,
            "source_database": source,
            "object_id": field("object_id", limit=500),
            "schema": field("schema_name", limit=256),
            "object": field("object_name", limit=256),
            "kind": kind,
            "meaning": meaning,
            "aliases": normalized_aliases,
            "status": status,
            "proposed_at": proposed_at,
            **({"decided_at": approved_at} if approved_at else {}),
        }

    def _rows(self) -> list[sqlite3.Row]:
        con = self._connect(write=False)
        if con is None:
            return []
        try:
            rows = con.execute(
                self._current_sql() + " ORDER BY P.proposed_at,P.id"
            ).fetchall()
            for row in rows:
                self._public(row)
            return rows
        except SourceKnowledgeError:
            raise
        except sqlite3.DatabaseError as exc:
            raise SourceKnowledgeError(
                "source knowledge is unreadable; no annotations were used"
            ) from exc
        finally:
            con.close()

    def summary(self) -> dict:
        rows = self._rows()
        counts = {status: 0 for status in (
            "pending", "approved", "rejected", "revoked")}
        for row in rows:
            status = str(row["status"] or "pending")
            if status in counts:
                counts[status] += 1
        return {
            "available": True,
            "source_database": self.source,
            "counts": counts,
            "active": counts["approved"],
            "note": (
                "Only operator-approved object meanings affect semantic "
                "retrieval. They are pointers, not row or relationship evidence."),
        }

    def propose(self, *, object_id: str, schema: str, object_name: str,
                object_kind: str, meaning: str, aliases: object = (),
                origin: str = "conversation") -> dict:
        oid = str(object_id or "").strip()
        owner = str(schema or "").strip()
        name = str(object_name or "").strip()
        kind = str(object_kind or "").strip().lower()
        if not oid or not owner or not name or kind not in {"table", "view"}:
            raise SourceKnowledgeError(
                "a proposal needs one exact catalog table/view identity")
        body = _one_line(
            meaning, label="meaning", limit=MAX_MEANING_CHARS)
        alias_list = normalize_aliases(aliases)
        canonical = json.dumps({
            "source": self.source,
            "source_fingerprint": self.source_fingerprint,
            "object_id": oid,
            "meaning": body.casefold(),
            "aliases": sorted(alias.casefold() for alias in alias_list),
        }, sort_keys=True, separators=(",", ":"))
        proposal_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        con = self._connect(write=True)
        assert con is not None
        try:
            existing = con.execute(
                self._current_sql() + " WHERE P.id=?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                return {**self._public(existing), "already_known": True}
            total = int(con.execute(
                "SELECT COUNT(*) FROM proposals").fetchone()[0])
            pending = int(con.execute(
                "SELECT COUNT(*) FROM proposals P WHERE NOT EXISTS "
                "(SELECT 1 FROM decisions D WHERE D.proposal_id=P.id)"
            ).fetchone()[0])
            if total >= MAX_PROPOSALS:
                raise SourceKnowledgeError(
                    f"source knowledge is full ({MAX_PROPOSALS} proposals); "
                    "review or archive it before adding more")
            if pending >= MAX_PENDING:
                raise SourceKnowledgeError(
                    f"source knowledge has {MAX_PENDING} pending proposals; "
                    "an operator must review them before adding more")
            con.execute(
                "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    proposal_id, self.source, self.source_fingerprint, oid,
                    owner, name, kind, body,
                    json.dumps(alias_list, separators=(",", ":")),
                    _now(), str(origin or "conversation")[:40],
                ))
            con.commit()
            row = con.execute(
                self._current_sql() + " WHERE P.id=?", (proposal_id,)
            ).fetchone()
            return {**self._public(row), "already_known": False}
        finally:
            con.close()

    def get(self, proposal_id: str) -> dict:
        candidate = str(proposal_id or "").strip()
        for row in self._rows():
            if row["id"] == candidate:
                return self._public(row)
        raise SourceKnowledgeError(f"no metadata proposal {candidate!r}")

    def decide(self, proposal_id: str, *, approve: bool,
               decided_by: str = "operator",
               current_object: dict | None = None) -> dict:
        candidate = str(proposal_id or "").strip()
        actor = _one_line(
            decided_by, label="reviewer", limit=80)
        con = self._connect(write=True)
        assert con is not None
        try:
            row = con.execute(
                self._current_sql() + " WHERE P.id=?", (candidate,)
            ).fetchone()
            if row is None:
                raise SourceKnowledgeError(f"no metadata proposal {candidate!r}")
            current_status = str(row["status"] or "pending")
            wanted = "approved" if approve else "rejected"
            if current_status == wanted:
                return self._public(row)
            if current_status != "pending":
                raise SourceKnowledgeError(
                    f"metadata proposal is already {current_status}; "
                    "decisions do not move backward")
            if approve:
                observed = current_object if isinstance(current_object, dict) else {}
                expected = {
                    "source_database": row["source"],
                    "source_fingerprint": row["source_fingerprint"],
                    "object_id": row["object_id"],
                    "schema": row["schema_name"],
                    "object": row["object_name"],
                }
                if any(str(observed.get(key) or "") != str(value)
                       for key, value in expected.items()):
                    raise SourceKnowledgeError(
                        "the current source catalog no longer proves the exact "
                        "proposal target; rebuild/narrow it before approval")
                if observed.get("aliases_safe") is not True:
                    raise SourceKnowledgeError(
                        "the current source catalog did not validate every "
                        "proposed alias; no approval was recorded")
            con.execute(
                "INSERT INTO decisions(proposal_id,status,decided_at,decided_by) "
                "VALUES (?,?,?,?)", (candidate, wanted, _now(), actor))
            con.commit()
            decided = con.execute(
                self._current_sql() + " WHERE P.id=?", (candidate,)
            ).fetchone()
            return self._public(decided)
        finally:
            con.close()

    def revoke(self, proposal_id: str, *, decided_by: str = "operator") -> dict:
        candidate = str(proposal_id or "").strip()
        actor = _one_line(decided_by, label="reviewer", limit=80)
        con = self._connect(write=True)
        assert con is not None
        try:
            row = con.execute(
                self._current_sql() + " WHERE P.id=?", (candidate,)
            ).fetchone()
            if row is None:
                raise SourceKnowledgeError(f"no metadata proposal {candidate!r}")
            if row["status"] == "revoked":
                return self._public(row)
            if row["status"] != "approved":
                raise SourceKnowledgeError(
                    "only an approved metadata proposal can be revoked")
            con.execute(
                "INSERT INTO decisions(proposal_id,status,decided_at,decided_by) "
                "VALUES (?,?,?,?)", (candidate, "revoked", _now(), actor))
            con.commit()
            revoked = con.execute(
                self._current_sql() + " WHERE P.id=?", (candidate,)
            ).fetchone()
            return self._public(revoked)
        finally:
            con.close()

    def approved_for_object(self, object_id: str) -> list[dict]:
        wanted = str(object_id or "").strip()
        return [self._public(row) for row in self._rows()
                if row["status"] == "approved" and row["object_id"] == wanted]

    def resolve_alias(self, identifier: str) -> list[dict]:
        asked = str(identifier or "").strip().casefold()
        if not asked:
            return []
        out = []
        for row in self._rows():
            if row["status"] != "approved":
                continue
            public = self._public(row)
            if asked in {alias.casefold() for alias in public["aliases"]}:
                out.append(public)
        return out[:MAX_SEARCH_RESULTS]

    def search(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
        terms = [match.group(0).casefold() for match in _WORD.finditer(
            str(query or "")) if len(match.group(0)) > 1][:20]
        if not terms:
            return []
        cap = min(max(int(limit or MAX_SEARCH_RESULTS), 1), MAX_SEARCH_RESULTS)
        found = []
        phrase = " ".join(str(query or "").casefold().split())
        for row in self._rows():
            if row["status"] != "approved":
                continue
            public = self._public(row)
            values = [public["meaning"], public["object"],
                      f"{public['schema']}.{public['object']}",
                      *public["aliases"]]
            hay = " ".join(values).casefold()
            matched = sorted({term for term in terms if term in hay})
            if not matched:
                continue
            coverage = len(matched) / len(terms)
            exact_alias = any(phrase == alias.casefold()
                              for alias in public["aliases"])
            found.append({
                **public,
                "matched_terms": matched,
                "term_coverage": round(coverage, 3),
                "semantic_score": int(coverage * 100) + (200 if exact_alias else 0),
                "matched_on": "approved alias" if exact_alias
                              else "approved object meaning",
            })
        found.sort(key=lambda row: (
            -row["semantic_score"], row["schema"], row["object"], row["id"]))
        return found[:cap]

    def list_proposals(self, status: str = "") -> list[dict]:
        wanted = str(status or "").strip().lower()
        allowed = {"", "pending", "approved", "rejected", "revoked"}
        if wanted not in allowed:
            raise SourceKnowledgeError(
                "status must be pending, approved, rejected or revoked")
        return [self._public(row) for row in self._rows()
                if not wanted or row["status"] == wanted]


def _catalog_identity(catalog, source: str, proposal: dict) -> dict:
    identifier = f"{proposal['schema']}.{proposal['object']}"
    result = catalog.context(identifier, source=source, limit=10)
    if not isinstance(result, dict) or result.get("found") is not True:
        raise SourceKnowledgeError(
            "the current source catalog cannot resolve the proposal target")
    subject = result.get("subject") if isinstance(result.get("subject"), dict) else {}
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    object_id = str(subject.get("object_id") or result.get("object_id") or "")
    validate_catalog_aliases(
        catalog, source, object_id, proposal.get("aliases") or ())
    return {
        "source_database": str(result.get("source_database") or ""),
        "source_fingerprint": str(snapshot.get("source_fingerprint") or ""),
        "object_id": object_id,
        "schema": str(subject.get("schema") or result.get("schema") or ""),
        "object": str(subject.get("physical_object") or
                      result.get("physical_object") or ""),
        "aliases_safe": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review source-bound metadata meaning proposals")
    parser.add_argument("--config", default=None)
    parser.add_argument("--source", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--approve")
    actions.add_argument("--reject")
    actions.add_argument("--revoke")
    parser.add_argument("--by", default="operator")
    parser.add_argument("--status", default="")
    args = parser.parse_args(argv)

    from .config import load_config
    from .metadata import MetadataCatalog, source_catalog_path, source_fingerprint

    cfg = load_config(args.config)
    source = str(args.source or "").strip()
    known = ["default", *sorted((cfg.sources or {}).keys())]
    if source not in known:
        parser.error(
            f"unknown source {source!r}; choose one of {', '.join(known)}")
    fingerprint = source_fingerprint(cfg, source)
    store = SourceKnowledge(
        source_knowledge_path(cfg, source), source=source,
        source_fingerprint=fingerprint)
    if args.approve:
        catalog = MetadataCatalog(
            source_catalog_path(cfg, source), source=source,
            expected_fingerprint=fingerprint)
        proposal = store.get(args.approve)
        decided = store.decide(
            args.approve, approve=True, decided_by=args.by,
            current_object=_catalog_identity(catalog, source, proposal))
        print(f"approved {decided['id']} for {source}: "
              f"{decided['schema']}.{decided['object']}")
        return 0
    if args.reject:
        decided = store.decide(
            args.reject, approve=False, decided_by=args.by)
        print(f"rejected {decided['id']} for {source}")
        return 0
    if args.revoke:
        decided = store.revoke(args.revoke, decided_by=args.by)
        print(f"revoked {decided['id']} for {source}")
        return 0
    rows = store.list_proposals(args.status)
    summary = store.summary()
    counts = summary["counts"]
    print(f"{source}: {counts['approved']} approved, "
          f"{counts['pending']} pending, {counts['rejected']} rejected, "
          f"{counts['revoked']} revoked")
    for row in rows:
        aliases = f" aliases={row['aliases']}" if row["aliases"] else ""
        print(f"[{row['id']}] {row['status']} "
              f"{row['schema']}.{row['object']}: {row['meaning']}{aliases}")
    if counts["pending"]:
        print("\nApprove: python -m pstb.source_knowledge --source "
              f"{source} --approve <id>")
        print("Reject : python -m pstb.source_knowledge --source "
              f"{source} --reject <id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
