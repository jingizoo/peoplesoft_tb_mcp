#!/usr/bin/env python3
"""One-step setup: virtualenv, dependencies, sample ledger, verification.

Works on Windows, macOS, and Linux with nothing but a Python 3.10+ interpreter.

    python scripts/bootstrap.py                # full setup
    python scripts/bootstrap.py --no-llm       # skip ollama/google-genai
    python scripts/bootstrap.py --oracle       # also install the Oracle driver

Safe to re-run: an existing virtualenv is reused and the sample ledger is
rebuilt from scratch.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
MIN_PY = (3, 10)


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(n: int, total: int, msg: str) -> None:
    print(f"\n[{n}/{total}] {msg}", flush=True)


def venv_python() -> Path:
    if platform.system() == "Windows":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(cmd: list, what: str, quiet: bool = True) -> None:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE if quiet else None,
            stderr=subprocess.STDOUT if quiet else None,
            text=True,
        )
    except FileNotFoundError as e:
        raise SystemExit(f"\nFAILED: {what}\n  command not found: {e}")
    if proc.returncode != 0:
        out = (proc.stdout or "").strip()
        tail = "\n".join(out.splitlines()[-15:]) if out else "(no output)"
        raise SystemExit(f"\nFAILED: {what}\n{tail}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up the PeopleSoft TB agent")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the ollama and google-genai clients")
    ap.add_argument("--oracle", action="store_true",
                    help="also install the Oracle driver (python-oracledb)")
    ap.add_argument("--sqlserver", action="store_true",
                    help="also install the SQL Server driver (pyodbc)")
    args = ap.parse_args()

    total = 5
    print("PeopleSoft Trial Balance agent — setup")
    print(f"  project: {ROOT}")
    print(f"  platform: {platform.system()} {platform.machine()}")

    step(1, total, "Checking Python")
    if sys.version_info < MIN_PY:
        raise SystemExit(
            f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required; this is "
            f"{sys.version.split()[0]}. Install a newer Python and re-run."
        )
    say(f"Python {sys.version.split()[0]} OK")

    step(2, total, "Creating virtual environment")
    if venv_python().exists():
        say(f"reusing existing {VENV.name}")
    else:
        run([sys.executable, "-m", "venv", str(VENV)], "create virtualenv")
        say(f"created {VENV.name}")
    py = str(venv_python())

    step(3, total, "Installing dependencies (this can take a few minutes)")
    extras = []
    if not args.no_llm:
        extras.append("llm")
    if args.oracle:
        extras.append("oracle")
    if args.sqlserver:
        extras.append("sqlserver")
    target = f".[{','.join(extras)}]" if extras else "."
    run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], "upgrade pip")
    run([py, "-m", "pip", "install", "-q", "-e", target], f"install {target}")
    say(f"installed {target}")

    step(4, total, "Building the sample ledger")
    run([sys.executable, str(ROOT / "scripts" / "seed_sample_data.py")],
        "seed sample data")
    say("sample_data/ps_sample.db created")

    step(5, total, "Verifying")
    run([sys.executable, str(ROOT / "scripts" / "smoke_test.py")], "smoke test")
    say("engine checks passed")
    run([py, str(ROOT / "scripts" / "mcp_probe.py")], "MCP probe")
    say("MCP server and tool discovery passed")

    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        say("created .env from .env.example")

    rel = os.path.relpath(venv_python(), ROOT)
    print("\nSetup complete.\n")
    print("Next steps:")
    print("  1. Install a local model, then chat:")
    print("       ollama pull llama3.1:8b")
    print(f"       {rel} -m pstb.client.chat")
    print("     (or use Gemini: set GOOGLE_CLOUD_PROJECT in .env, then")
    print(f"       {rel} -m pstb.client.chat --provider gemini)")
    print("  2. Ask something:")
    print(f"       {rel} -m pstb.client.chat --ask \"Does the trial balance balance?\"")
    print("\nEverything above runs on the bundled sample ledger — no PeopleSoft")
    print("connection needed. See README.md to point it at a real database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
