"""Bounded background CSV exports for result sets too large for chat.

Jobs are deliberately process-local and short-lived.  The URL contains a
cryptographically random identifier and, when row security is enabled, the
download is additionally bound to the PeopleSoft operator who created it.
No SQL, binds, result rows or credentials are persisted in job metadata.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class BatchExportError(RuntimeError):
    """A batch-export problem stated without exposing query details."""


@dataclass
class BatchJob:
    job_id: str
    owner: str
    source: str
    tool: str
    created_at: float
    expires_at: float
    state: str = "queued"
    rows: int = 0
    columns: int = 0
    bytes: int = 0
    truncated: bool = False
    filename: str = ""
    note: str = ""
    error: str = ""
    path: Optional[Path] = None
    updated_at: float = field(default_factory=time.time)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


class BatchExportManager:
    """Small in-process queue with streamed files and automatic expiry."""

    HARD_MAX_ROWS = 1_000_000

    def __init__(self, directory: Path, *, max_rows: int = 1_000_000,
                 workers: int = 2, max_queued: int = 8,
                 ttl_seconds: int = 3_600):
        self.directory = Path(directory)
        self.max_rows = max(1, min(int(max_rows), self.HARD_MAX_ROWS))
        self.workers = max(1, min(int(workers), 8))
        self.max_queued = max(self.workers, min(int(max_queued), 64))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._lock = threading.Lock()
        self._jobs: dict[str, BatchJob] = {}
        self._closed = False
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        self._remove_orphans()
        self._pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="pstb-export")
        self._stop = threading.Event()
        self._janitor = threading.Thread(
            target=self._cleanup_loop, name="pstb-export-cleanup", daemon=True)
        self._janitor.start()

    def _cleanup_loop(self) -> None:
        while not self._stop.wait(min(60, max(5, self.ttl_seconds // 4))):
            self.cleanup()

    def _remove_orphans(self) -> None:
        """A restart invalidates in-memory tokens, so old files are useless."""
        for path in self.directory.glob("pstb-export-*.csv*"):
            try:
                path.unlink()
            except OSError:
                pass

    def _delete(self, job: BatchJob) -> None:
        if job.path is not None:
            try:
                job.path.unlink(missing_ok=True)
            except OSError:
                pass
        partial = self.directory / f"pstb-export-{job.job_id}.csv.part"
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass

    def cleanup(self) -> None:
        now = time.time()
        expired: list[BatchJob] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.expires_at <= now and job.state not in {
                        "queued", "running"}:
                    expired.append(self._jobs.pop(job_id))
        for job in expired:
            self._delete(job)

    def submit(self, *, owner: str, source: str, tool: str,
               producer: Callable[[Path, int, Callable[[int], None]], dict]
               ) -> dict:
        self.cleanup()
        with self._lock:
            if self._closed:
                raise BatchExportError("Batch export service is stopping")
            active = sum(j.state in {"queued", "running"}
                         for j in self._jobs.values())
            if active >= self.max_queued:
                raise BatchExportError(
                    "The batch-export queue is full. Wait for an existing "
                    "export to finish, then try again.")
            job_id = secrets.token_urlsafe(24)
            now = time.time()
            job = BatchJob(
                job_id=job_id, owner=str(owner or "local"),
                source=str(source or "default"), tool=str(tool or "export"),
                created_at=now, expires_at=now + self.ttl_seconds)
            self._jobs[job_id] = job
        self._pool.submit(self._run, job_id, producer)
        return self.status(job_id, owner=owner)

    def _run(self, job_id: str,
             producer: Callable[[Path, int, Callable[[int], None]], dict]
             ) -> None:
        partial = self.directory / f"pstb-export-{job_id}.csv.part"
        final = self.directory / f"pstb-export-{job_id}.csv"
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.state = "running"
            job.updated_at = time.time()

        def progress(rows: int) -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None:
                    current.rows = max(current.rows, int(rows or 0))
                    current.updated_at = time.time()

        try:
            meta = producer(partial, self.max_rows, progress) or {}
            if not partial.exists():
                raise BatchExportError("The exporter produced no file")
            os.replace(partial, final)
            try:
                os.chmod(final, 0o600)
            except OSError:
                pass
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    final.unlink(missing_ok=True)
                    return
                job.state = "ready"
                job.rows = int(meta.get("rows") or job.rows)
                job.columns = int(meta.get("columns") or 0)
                job.truncated = bool(meta.get("truncated"))
                job.filename = str(meta.get("filename") or "export.csv")
                job.note = str(meta.get("note") or "")[:1000]
                job.path = final
                job.bytes = final.stat().st_size
                job.updated_at = time.time()
                # The retention window starts when the file becomes useful,
                # not when a long-running database query entered the queue.
                job.expires_at = job.updated_at + self.ttl_seconds
        except Exception as exc:  # the live status is the user's remedy
            try:
                partial.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job.state = "failed"
                    # Do not persist SQL-bearing driver text in a durable log;
                    # this value is process-local and expires with the job.
                    job.error = str(exc)[:1200]
                    job.note = "The file was not created. Narrow the query or retry."
                    job.updated_at = time.time()
                    job.expires_at = job.updated_at + self.ttl_seconds

    def _owned_locked(self, job_id: str, owner: str) -> BatchJob:
        """Validate under ``_lock``; callers snapshot before releasing it."""
        job = self._jobs.get(str(job_id or ""))
        if job is None or job.owner != str(owner or "local"):
            # Same response for a wrong owner and an unknown token: do not
            # reveal whether another person's file exists.
            raise BatchExportError(
                "Export not found or expired. Start it again from the result card.")
        return job

    @staticmethod
    def _status(job: BatchJob) -> dict:
        out = {
            "job_id": job.job_id, "state": job.state,
            "source": job.source, "tool": job.tool,
            "rows": job.rows, "columns": job.columns,
            "bytes": job.bytes, "truncated": job.truncated,
            "filename": job.filename or None,
            "note": job.note or None, "error": job.error or None,
            "created_at": _iso(job.created_at),
            "expires_at": _iso(job.expires_at),
        }
        if job.state == "ready":
            out["download_url"] = f"/api/batch-exports/{job.job_id}/download"
        return out

    def status(self, job_id: str, *, owner: str) -> dict:
        self.cleanup()
        with self._lock:
            return self._status(self._owned_locked(job_id, owner))

    def file(self, job_id: str, *, owner: str) -> tuple[Path, dict]:
        self.cleanup()
        with self._lock:
            job = self._owned_locked(job_id, owner)
            if (job.state != "ready" or job.path is None
                    or not job.path.exists()):
                raise BatchExportError(
                    "Export is not ready. Check its status and download after it completes.")
            # Keep the file alive while FileResponse opens it. Without this,
            # the cleanup thread could remove a file at the exact TTL boundary
            # between this return and Starlette's first read.
            job.expires_at = max(job.expires_at, time.time() + 60)
            return job.path, self._status(job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._stop.set()
        self._janitor.join(timeout=1)
        self._pool.shutdown(wait=False, cancel_futures=True)
