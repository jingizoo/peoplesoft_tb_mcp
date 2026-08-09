"""The configuration console: change settings without an admin or an SSH key.

WHAT PROTECTS THIS PAGE. The loopback guard, and nothing else. Every
request here has already passed pstb/gui/localguard.py — a peer on this
machine, a Host we recognise, no forwarding headers — and that is the
whole access-control story, exactly as it is for the ledger endpoints.

WHAT THE DATE-HOUR CODE IS. A CONFIRMATION STEP, not authentication. It
cannot be authentication: any person or script can compute the current
hour in India, and a page that had somehow reached this server could
compute it in JavaScript. What it does buy is deliberateness — a colleague
who wanders onto the tunnelled port does not casually rotate a password,
and a stray click does not either. The page says this in those words
rather than implying a lock that is not there. SSO replaces it, and
verify_operator() below is the single function that changes.

WHY WRITES REQUIRE A HEADER. A cross-origin page can POST a form without
being allowed to read the reply, which is enough to change a setting.
Requiring X-PSTB-Console forces a CORS preflight that this server never
answers, so the write never leaves the browser.

WHY THERE IS NO RESTART BUTTON. Deliberate, and the owner's call. A
restart needs a supervisor to bring the process back, a health check to
know it did, and a rollback when it did not — a console that kills the
service and disappears is the worst failure this could have. Until that
exists, the console says plainly which changes need a restart and how to
perform one.
"""
from __future__ import annotations

import datetime as dt
import hmac
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from .. import settings as st

STATIC = Path(__file__).parent / "static"
CONFIRM_HEADER = "x-pstb-console"

# Asia/Kolkata is UTC+5:30 and has no daylight saving, so the offset is a
# constant rather than a rule. zoneinfo is used when the host carries the
# tz database and falls back to the fixed offset when it does not — a
# missing tzdata package must not lock the owner out of their own console.
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


def ist_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return dt.datetime.now(_IST)


def expected_codes(now: dt.datetime = None) -> list:
    """The codes acceptable right now: this hour and the one before.

    The previous hour is honoured because the alternative is cruel — type
    the code at 10:59:58, have it validated at 11:00:01, and be told it is
    wrong with no way to know why. It widens a window that was never
    secret to begin with, so the trade costs nothing real.
    """
    now = now or ist_now()
    prior = now - dt.timedelta(hours=1)
    return [f"{d:%Y%m%d}-{d:%H}" for d in (now, prior)]


def code_hint(now: dt.datetime = None) -> str:
    now = now or ist_now()
    return (f"Today's date and the current hour in India, as "
            f"YYYYMMDD-HH. It is {now:%H:%M} IST now.")


def verify_operator(request: Request) -> bool:
    """The seam SSO replaces.

    Today: has this browser entered the confirmation code. Tomorrow: has
    the identity provider vouched for this person. Every handler asks this
    one question, so the swap is this function and the login page.
    """
    return request.cookies.get("pstb_console") == "confirmed"


def _refuse(status: int, error: str, remedy: str = "") -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": error, "remedy": remedy})


def _needs_confirmation(request: Request):
    if not verify_operator(request):
        return _refuse(401, "This console needs the confirmation code.",
                       code_hint())
    return None


def _needs_header(request: Request):
    """Blocks the cross-origin form POST described in the module docstring."""
    if request.headers.get(CONFIRM_HEADER) != "1":
        return _refuse(400, f"Missing {CONFIRM_HEADER} header.",
                       "Use the console page; this endpoint is not meant "
                       "to be called from another site or by hand.")
    return None


def register(app, get_cfg, on_reload) -> None:
    """Mount the console. get_cfg() returns the live Config; on_reload()
    rebuilds what this process can rebuild and returns a report."""

    @app.get("/console")
    def console_page():
        return FileResponse(STATIC / "console.html")

    @app.get("/api/console/status")
    def console_status():
        cfg = get_cfg()
        root = Path(cfg.root)
        env_path = root / ".env"
        overlay = root / st.OVERLAY_NAME
        return {
            "auth": {"method": "confirmation-code",
                     "is_authentication": False,
                     "hint": code_hint(),
                     "note": ("This code is a confirmation step, not a "
                              "password — anyone can compute the hour in "
                              "India. What protects this page is that the "
                              "server answers only to a caller on this "
                              "machine.")},
            "settings": st.current(cfg),
            "secrets": st.which_secrets_are_set(env_path),
            "env_private": st.file_is_private(env_path),
            "overlay": {"path": st.OVERLAY_NAME, "exists": overlay.exists()},
            "restart": {
                "can_restart_here": False,
                "why": ("No supervisor is managing this process, so exiting "
                        "would stop the service with nothing to bring it "
                        "back — and this console with it."),
                "how": ("Stop with Ctrl+C in the terminal running it, then "
                        "start it again: .venv/bin/python -m pstb.gui"),
            },
        }

    @app.post("/api/console/unlock")
    async def console_unlock(request: Request, payload: dict):
        bad = _needs_header(request)
        if bad:
            return bad
        given = str((payload or {}).get("code", "")).strip()
        if not any(hmac.compare_digest(given, ok) for ok in expected_codes()):
            return _refuse(401, "That code does not match.", code_hint())
        response = JSONResponse(content={"ok": True})
        # Session-scoped, not persisted: closing the browser ends it.
        # SameSite=Strict so it is never attached to a cross-site request.
        response.set_cookie("pstb_console", "confirmed", httponly=True,
                            samesite="strict", path="/")
        return response

    @app.post("/api/console/settings")
    async def console_settings(request: Request, payload: dict):
        for check in (_needs_header(request), _needs_confirmation(request)):
            if check:
                return check
        cfg = get_cfg()
        root = Path(cfg.root)
        changes = (payload or {}).get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return _refuse(400, "Nothing to save.")

        clean, restart_needed = {}, []
        for key, raw in changes.items():
            try:
                clean[key] = st.validate(key, raw)
            except st.SettingsError as e:
                return _refuse(400, str(e), f"Fix {key} and save again.")
            spec = st.BY_KEY[key]
            if spec.env_var and os.environ.get(spec.env_var):
                return _refuse(
                    400,
                    f"{spec.label} is currently forced by {spec.env_var} "
                    "in .env, so saving here would change nothing.",
                    f"Remove {spec.env_var} from .env first.")
            if spec.restart:
                restart_needed.append(spec.label)

        # Merge over whatever the overlay already holds, so one save does
        # not silently drop another setting changed earlier.
        overlay_path = root / st.OVERLAY_NAME
        merged = dict(_read_overlay(overlay_path))
        merged.update(clean)
        text = st.render_overlay(merged)

        # Validate by LOADING it, before it becomes the live file. A write
        # that bricks the next start is the one failure this console must
        # not have.
        probe = overlay_path.with_name(".console-probe.yaml")
        try:
            st.atomic_write(probe, text)
            ok, why = _probe_config(root, probe)
            if not ok:
                return _refuse(400, f"That configuration does not load: {why}",
                               "Nothing was written; the running config is "
                               "unchanged.")
        finally:
            if probe.exists():
                probe.unlink()

        saved_as = st.backup(overlay_path)
        st.atomic_write(overlay_path, text)
        report = on_reload()
        return {
            "ok": True, "saved": sorted(clean),
            "backup": saved_as,
            "reloaded": report.get("reloaded", []),
            "restart_required": sorted(set(restart_needed)),
            "restart_note": (
                "These take effect when the service is restarted. The "
                "running answer engine is a separate process that binds its "
                "configuration at startup, so applying them here would leave "
                "two live surfaces disagreeing about the same question."
                if restart_needed else ""),
        }

    @app.post("/api/console/secrets")
    async def console_secrets(request: Request, payload: dict):
        for check in (_needs_header(request), _needs_confirmation(request)):
            if check:
                return check
        cfg = get_cfg()
        env_path = Path(cfg.root) / ".env"
        updates, deletes = {}, []
        for key, entry in ((payload or {}).get("secrets") or {}).items():
            if key not in st.SECRET_KEYS:
                return _refuse(400, f"{key} is not a secret this console sets.")
            action = str((entry or {}).get("action", "")).strip()
            if action == "clear":
                # The only irreversible operation here, so it must be
                # deliberate: the request has to name the key it is clearing.
                if (entry or {}).get("confirm") != key:
                    return _refuse(
                        400, f"Clearing {st.SECRET_LABELS[key]} needs "
                             f"confirmation.",
                        f"Re-send with confirm set to {key}. There is no "
                        "backup of .env — a cleared secret must be typed "
                        "again from wherever it is kept.")
                deletes.append(key)
            elif action == "set":
                value = str((entry or {}).get("value", ""))
                if not value:
                    return _refuse(400,
                                   f"{st.SECRET_LABELS[key]} cannot be blank.",
                                   "To remove it, clear it instead.")
                updates[key] = value
        if not updates and not deletes:
            return _refuse(400, "Nothing to change.")
        try:
            st.write_env_keys(env_path, updates, deletes)
        except st.SettingsError as e:
            return _refuse(400, str(e))
        # Deliberately no echo of any value, not even a length.
        return {
            "ok": True,
            "set": sorted(updates), "cleared": sorted(deletes),
            "restart_required": True,
            "restart_note": (
                "Secrets are read into the environment once at startup, so "
                "the running process still holds the old value. Restart the "
                "service to pick this up."),
        }


def _read_overlay(path: Path) -> dict:
    """Flatten the overlay back to dotted keys, ignoring anything that is
    no longer an editable setting."""
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    for section, block in (data or {}).items():
        if not isinstance(block, dict):
            continue
        for leaf, value in block.items():
            key = f"{section}.{leaf}"
            if key in st.BY_KEY:
                out[key] = value
    return out


def _probe_config(root: Path, overlay: Path) -> tuple:
    """Load a candidate overlay in a SUBPROCESS.

    In-process would leave this one holding whatever the candidate changed
    even when it is rejected. A validator that cannot run is reported as
    such rather than blamed on the value.
    """
    import subprocess
    import sys
    code = (
        "import sys, shutil, tempfile, pathlib, os\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "src, overlay = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "shutil.copy(src / 'config.yaml', d / 'config.yaml')\n"
        "shutil.copy(overlay, d / 'config.local.yaml')\n"
        "from pstb.config import load_config\n"
        "load_config(str(d / 'config.yaml'))\n"
        "print('OK')\n"
    )
    pkg = str(Path(__file__).resolve().parents[2])
    try:
        out = subprocess.run(
            [sys.executable, "-c", code, pkg, str(root), str(overlay)],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        return False, f"the validator could not start: {type(e).__name__}"
    if out.returncode == 0 and "OK" in out.stdout:
        return True, ""
    tail = (out.stderr or out.stdout).strip().splitlines()
    return False, (tail[-1] if tail else "unknown error")[:200]
