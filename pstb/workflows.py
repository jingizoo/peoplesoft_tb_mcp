"""Resumable, human-reviewed workflows above deterministic playbooks.

This is intentionally small and does not require LangGraph.  PeopleSoft
queries and accounting conclusions remain in the existing curated tools and
playbooks.  A workflow only sequences those playbooks, stops at explicit
review gates, and records enough non-financial state to resume after a process
restart.

Checkpoint files never contain tool rows, amounts, names, journal/voucher IDs
or model prose.  They keep scope, phase status, verdict/counts, timestamps and
a SHA-256 digest of the live result.  The live result is returned to the
caller for review but must be re-run if it is needed later.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
MAX_WORKFLOWS = 1_000
_ID = re.compile(r"^[a-f0-9]{16}$")


WORKFLOWS = {
    "month_end_close": {
        "title": "Month-end controller review",
        "description": (
            "AP completeness, close readiness, then post-close monitoring; "
            "a reviewer acknowledges each live result before progression."),
        "phases": ["ap_completeness", "close_readiness", "post_close_watch"],
    },
    "daily_controller_review": {
        "title": "Daily controller review",
        "description": (
            "Daily exception brief followed by receivables health, with an "
            "explicit review checkpoint between them."),
        "phases": ["daily_brief", "receivables_health"],
    },
    "receivables_review": {
        "title": "Receivables review",
        "description": "A resumable review of the receivables-health playbook.",
        "phases": ["receivables_health"],
    },
}


class WorkflowError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _after(minutes: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
            ).isoformat(timespec="seconds")


def _expired(value: str) -> bool:
    try:
        parsed = dt.datetime.fromisoformat(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed <= dt.datetime.now(dt.timezone.utc)
    except (TypeError, ValueError):
        return True


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WorkflowStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _path(self, workflow_id: str) -> Path:
        if not _ID.fullmatch(str(workflow_id or "")):
            raise WorkflowError("invalid workflow id")
        return self.directory / f"{workflow_id}.json"

    @contextmanager
    def _lock(self, path: Path) -> Iterator[None]:
        """Cross-platform best-effort lock using exclusive file creation."""
        lock = path.with_suffix(".lock")
        deadline = time.monotonic() + 3.0
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o600)
                os.write(fd, f"{os.getpid()} {_now()}\n".encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    stale = time.time() - lock.stat().st_mtime > 120
                except OSError:
                    stale = False
                if stale:
                    try:
                        lock.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise WorkflowError("workflow is busy; retry shortly")
                time.sleep(0.025)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def _read(self, workflow_id: str) -> dict:
        path = self._path(workflow_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkflowError(f"workflow {workflow_id!r} does not exist") from exc
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"workflow {workflow_id!r} is unreadable") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError(f"workflow {workflow_id!r} has an unsupported format")
        return data

    @staticmethod
    def _public(state: dict) -> dict:
        # Return a detached copy so callers cannot mutate cached state.
        detached = json.loads(json.dumps(state))
        for phase in detached.get("phases", []):
            phase.pop("execution_token", None)
        return detached

    def _write(self, state: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(state["id"])
        tmp = path.with_suffix(".building")
        fd: int | None = None
        try:
            # Create the temporary checkpoint private before the first byte is
            # written. chmod-after-write leaves a disclosure window under a
            # normal 022 umask.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                         0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None  # fdopen now owns it
                json.dump(state, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def list_workflows(self, limit: int = 100) -> dict:
        cap = min(max(int(limit or 100), 1), 500)
        if not self.directory.exists():
            return {"workflows": [], "count": 0, "truncated": False}
        paths = sorted(self.directory.glob("*.json"),
                       key=lambda item: item.stat().st_mtime, reverse=True)
        rows = []
        for path in paths[:cap + 1]:
            try:
                state = self._read(path.stem)
            except WorkflowError:
                continue
            rows.append({
                "id": state["id"], "workflow": state["workflow"],
                "title": state["title"], "status": state["status"],
                "business_unit": state["scope"]["business_unit"],
                "ledger": state["scope"]["ledger"],
                "fiscal_year": state["scope"]["fiscal_year"],
                "period": state["scope"]["period"],
                "updated_at": state["updated_at"],
            })
        return {"workflows": rows[:cap], "count": len(rows[:cap]),
                "truncated": len(rows) > cap}

    def start(self, workflow: str, *, business_unit: str = "",
              ledger: str = "", fiscal_year: int = 0,
              period: int = 0) -> dict:
        name = str(workflow or "").strip()
        spec = WORKFLOWS.get(name)
        if spec is None:
            raise WorkflowError(
                f"unknown workflow {name!r}; use one of "
                + ", ".join(sorted(WORKFLOWS)))
        if self.directory.exists() and len(list(self.directory.glob("*.json"))) \
                >= MAX_WORKFLOWS:
            raise WorkflowError(
                f"workflow store reached {MAX_WORKFLOWS}; archive old runs")
        workflow_id = uuid.uuid4().hex[:16]
        phases = [{
            "position": index + 1, "playbook": playbook,
            "status": "pending", "attempts": 0,
        } for index, playbook in enumerate(spec["phases"])]
        now = _now()
        state = {
            "schema_version": SCHEMA_VERSION,
            "id": workflow_id, "workflow": name, "title": spec["title"],
            "status": "pending", "revision": 1,
            "scope": {
                "business_unit": str(business_unit or ""),
                "ledger": str(ledger or ""),
                "fiscal_year": int(fiscal_year or 0),
                "period": int(period or 0),
            },
            "phases": phases, "active_phase": 1,
            "created_at": now, "updated_at": now,
            # The current web "operator" selector is not authentication and
            # a free-form CLI label is not reliable audit identity.  Do not
            # persist either as a person's name.  A future SSO-backed audit
            # integration can record identity in its governed system.
            "actor_attribution": "not_recorded_without_authentication",
            "storage_note": (
                "Checkpoint stores statuses and result hashes only; live "
                "financial results, amounts, rows and party details are not "
                "persisted."),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(workflow_id)
        with self._lock(path):
            self._write(state)
        return self._public(state)

    def get(self, workflow_id: str) -> dict:
        return self._public(self._read(workflow_id))

    @staticmethod
    def _active(state: dict) -> dict | None:
        return next((phase for phase in state["phases"]
                     if phase["status"] in ("pending", "running",
                                             "awaiting_review")), None)

    def run_next(self, workflow_id: str, playbook_runner) -> dict:
        path = self._path(workflow_id)
        with self._lock(path):
            state = self._read(workflow_id)
            if state["status"] in ("completed", "cancelled"):
                return {"state": self._public(state), "result": None,
                        "detail": f"workflow is already {state['status']}"}
            phase = self._active(state)
            if phase is None:
                raise WorkflowError("workflow has no runnable phase")
            if phase["status"] == "awaiting_review":
                return {
                    "state": self._public(state), "result": None,
                    "detail": (
                        "The current phase is awaiting human review. Accept "
                        "it, request a rerun, or cancel before continuing."),
                }
            if (phase["status"] == "running"
                    and not _expired(phase.get("lease_expires_at", ""))):
                return {
                    "state": self._public(state), "result": None,
                    "detail": (
                        "This phase is already running in another process. "
                        "Its lease expires at "
                        f"{phase.get('lease_expires_at')}."),
                }
            phase["status"] = "running"
            phase["attempts"] += 1
            phase["started_at"] = _now()
            # A process killed mid-query leaves a recoverable lease rather
            # than a permanently stuck phase. Normal calls cannot run the
            # same control twice concurrently.
            phase["lease_expires_at"] = _after(15)
            execution_token = uuid.uuid4().hex
            phase["execution_token"] = execution_token
            running_position = phase["position"]
            state["status"] = "running"
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write(state)

        scope = state["scope"]
        try:
            result = playbook_runner.run(
                phase["playbook"],
                business_unit=scope["business_unit"],
                ledger=scope["ledger"],
                fiscal_year=scope["fiscal_year"],
                period=scope["period"],
            )
        except Exception as exc:
            # Persist only error TYPE. Database details can include object or
            # connection information and belong in normal application logs.
            result = {
                "verdict": "incomplete", "attention_count": 0,
                "skipped_count": 1,
                "note": f"{type(exc).__name__}: playbook failed",
            }

        verdict = str(result.get("verdict") or "incomplete")
        if verdict not in ("passed", "exceptions_found", "incomplete"):
            verdict = "incomplete"
        with self._lock(path):
            state = self._read(workflow_id)
            if state["status"] == "cancelled":
                return {
                    "state": self._public(state), "result": result,
                    "review_required": False,
                    "detail": (
                        "The workflow was cancelled while this read-only "
                        "phase ran; its live result was not checkpointed."),
                }
            phase = next((item for item in state["phases"]
                          if item["position"] == running_position), None)
            if (phase is None or phase.get("status") != "running"
                    or phase.get("execution_token") != execution_token):
                # A later runner legitimately reclaimed an expired lease.
                # Never let this superseded execution overwrite its result.
                return {
                    "state": self._public(state), "result": None,
                    "review_required": False,
                    "detail": (
                        "This execution lost its lease while the playbook was "
                        "running. Its result was discarded; reload the current "
                        "workflow state."),
                }
            phase.update({
                "status": "awaiting_review", "verdict": verdict,
                "attention_count": int(result.get("attention_count") or 0),
                "skipped_count": int(result.get("skipped_count") or 0),
                "result_as_of": str(result.get("as_of") or "")[:32],
                "result_hash": _digest(result), "finished_at": _now(),
            })
            phase.pop("lease_expires_at", None)
            phase.pop("execution_token", None)
            # Defaults chosen by the engine become explicit after the first
            # live phase, so later phases cannot silently follow a changed
            # deployment default.
            for key in ("business_unit", "ledger", "fiscal_year", "period"):
                value = result.get(key)
                if value not in (None, "", 0):
                    state["scope"][key] = value
            state["status"] = "awaiting_review"
            state["active_phase"] = phase["position"]
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write(state)
        return {
            "state": self._public(state),
            "result": result,
            "review_required": True,
            "review_note": (
                "Acknowledging a phase records that a human reviewed the "
                "live evidence; it is not approval of a journal, payment, or "
                "accounting conclusion."),
        }

    def review(self, workflow_id: str, decision: str,
               expected_revision: int = 0) -> dict:
        action = str(decision or "").strip().lower()
        if action not in ("accept", "rerun", "cancel"):
            raise WorkflowError("decision must be accept, rerun, or cancel")
        try:
            revision = int(expected_revision)
        except (TypeError, ValueError):
            revision = 0
        if revision < 1:
            raise WorkflowError(
                "a positive displayed revision is required for review")
        path = self._path(workflow_id)
        with self._lock(path):
            state = self._read(workflow_id)
            if revision != state["revision"]:
                raise WorkflowError(
                    "workflow changed since it was displayed; reload before review")
            phase = self._active(state)
            if action == "cancel":
                state["status"] = "cancelled"
                state["cancelled_at"] = _now()
                if phase is not None:
                    phase["status"] = "cancelled"
                    phase.pop("lease_expires_at", None)
                    phase.pop("execution_token", None)
            elif phase is None or phase["status"] != "awaiting_review":
                raise WorkflowError("no completed phase is awaiting review")
            elif action == "rerun":
                for key in ("verdict", "attention_count", "skipped_count",
                            "result_as_of", "result_hash", "started_at",
                            "finished_at", "lease_expires_at",
                            "execution_token"):
                    phase.pop(key, None)
                phase["status"] = "pending"
                state["status"] = "pending"
            else:
                phase["status"] = "reviewed"
                phase["reviewed_at"] = _now()
                phase["human_reviewed"] = True
                phase["review_actor_attribution"] = (
                    "not_recorded_without_authentication")
                following = next((item for item in state["phases"]
                                  if item["status"] == "pending"), None)
                if following is None:
                    state["status"] = "completed"
                    state["completed_at"] = _now()
                    state["active_phase"] = None
                else:
                    state["status"] = "pending"
                    state["active_phase"] = following["position"]
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write(state)
        return self._public(state)


def list_workflow_specs() -> dict:
    return {"workflows": [{
        "name": name, "title": spec["title"],
        "description": spec["description"], "phases": list(spec["phases"]),
    } for name, spec in WORKFLOWS.items()]}
