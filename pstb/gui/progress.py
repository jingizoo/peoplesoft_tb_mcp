"""What the server is doing while the page is still blank.

The GUI's first paint waits on /api/meta, and /api/meta waits on a database
that can be a WAN round trip away. That is a legitimate wait — but the page
showed a looping logo animation for the whole of it, which is
indistinguishable from a hang. Two people concluded the app was broken when
it was reading PS_LEDGER.

So the boot sequence is DECLARED here, up front, and each step reports when
it starts and how long it took. /api/boot serves the snapshot, the page
polls it while its own /api/meta call is in flight, and the wait stops being
a mystery: "Finding the last posted period — 38s" is a slow database, and
the reader can tell that from a bug.

The step list is fixed rather than discovered so the bar can be determinate
(step 3 of 5) before the server has reached step 3. Steps a deployment
never runs finish as "skipped" instead of hanging at "running".

Thread-safe because the steps are touched from three places at once: the
lifespan task, FastAPI's request threadpool, and the background scope
refresh thread.
"""
from __future__ import annotations

import contextlib
import threading
import time

# (key, label) in the order a boot performs them.
BOOT_STEPS: tuple = (
    ("server", "Starting the web server"),
    ("engine", "Connecting the answer engine"),
    ("defaults", "Reading ledger defaults"),
    ("period", "Finding the last posted period"),
    ("scopes", "Discovering business units and ledgers"),
)

PENDING, RUNNING, DONE, FAILED, SKIPPED = (
    "pending", "running", "done", "failed", "skipped")

_lock = threading.RLock()
_state: dict = {}
_started: float = 0.0


def reset() -> None:
    """Back to a fresh boot. Called at import and by tests."""
    global _started
    with _lock:
        _state.clear()
        for key, label in BOOT_STEPS:
            _state[key] = {"key": key, "label": label, "status": PENDING,
                           "ms": 0, "note": "", "at": 0.0}
        _started = time.monotonic()


def begin(key: str, note: str = "") -> None:
    with _lock:
        slot = _state.get(key)
        if slot is None or slot["status"] in (DONE, FAILED):
            return                      # a repeat visit is not a fresh boot
        slot.update({"status": RUNNING, "at": time.monotonic(), "note": note})


def end(key: str, ok: bool = True, note: str = "") -> None:
    with _lock:
        slot = _state.get(key)
        # Only a step still in flight can finish. Later refreshes reuse the
        # same code path as the boot-time one and must not restate a step
        # the page already ticked off.
        if slot is None or slot["status"] not in (PENDING, RUNNING):
            return
        started = slot["at"] or _started
        slot.update({"status": DONE if ok else FAILED,
                     "ms": int((time.monotonic() - started) * 1000)})
        if note:
            slot["note"] = note


def skip(key: str, note: str = "") -> None:
    with _lock:
        slot = _state.get(key)
        if slot is not None and slot["status"] in (PENDING, RUNNING):
            slot.update({"status": SKIPPED, "note": note,
                         "ms": slot["ms"] or 0})


@contextlib.contextmanager
def step(key: str, note: str = ""):
    """Run a boot step, recording the failure reason if it raises.

    The exception still propagates: this reports the boot, it does not
    change what the caller does about a failure.
    """
    begin(key, note)
    try:
        yield
    except BaseException as e:          # noqa: BLE001 - reported, not handled
        end(key, ok=False, note=f"{type(e).__name__}: {e}"[:300])
        raise
    end(key)


def snapshot() -> dict:
    """The whole boot, safe to serve on every poll.

    A RUNNING step carries its live elapsed time so the page can say how
    long the current wait has been rather than only reporting finished
    steps — the running one is the interesting one.
    """
    now = time.monotonic()
    with _lock:
        steps = []
        for key, _ in BOOT_STEPS:
            slot = _state[key]
            ms = (int((now - slot["at"]) * 1000)
                  if slot["status"] == RUNNING and slot["at"] else slot["ms"])
            steps.append({"key": key, "label": slot["label"],
                          "status": slot["status"], "ms": ms,
                          "note": slot["note"]})
        elapsed = int((now - _started) * 1000)
    settled = [s for s in steps if s["status"] in (DONE, FAILED, SKIPPED)]
    return {
        "steps": steps,
        "completed": len(settled),
        "total": len(steps),
        "ready": len(settled) == len(steps),
        "failed": [s["key"] for s in steps if s["status"] == FAILED],
        "elapsed_ms": elapsed,
    }


reset()
