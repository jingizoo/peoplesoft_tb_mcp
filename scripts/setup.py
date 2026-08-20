#!/usr/bin/env python3
"""Interactive setup for the PeopleSoft finance agent.

Run this once after a fresh pull or ZIP deployment. It performs every step of
the port and verifies each one before moving on:

    python3 scripts/setup.py

  * check the Python version
  * create the virtualenv and install the package (+ the Oracle driver)
  * build and verify the bundled sample ledger
  * collect Oracle credentials and prove the connection works
  * discover business units, ledgers, calendar and SetID FROM the database
  * check the SELECT grants the agent needs, and print what to ask the DBA for
  * write .env (mode 600) and config.yaml (previous file backed up)
  * run the database pre-flight gate
  * configure the wiki and the language model
  * print how to start and reach the web UI

Safe to re-run: existing config.yaml values become the defaults at every
prompt, so pressing Enter throughout keeps what you already have. Nothing is
written to config.yaml until the run succeeds; on failure the original file is
left exactly as it was.

--non-interactive keeps every existing value and asks nothing. It is for
re-verifying an installed system, and it cannot change the backend or set a
password.

Deliberately stdlib-only and written in conservative syntax with ASCII-only
output, because it must run on the system interpreter BEFORE any virtualenv
exists - including one too old to run the agent, where its job is to say so.
"""
import argparse
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

MIN_PYTHON = (3, 10)
ROOT = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")
# The tracked defaults. config.yaml is per-deployment and git-ignored, so on
# a fresh clone this is the only config on disk and the wizard seeds from it.
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
ENVFILE = os.path.join(ROOT, ".env")
PROBE_TIMEOUT = 60

REQUIRED_RECORDS = [
    ("PS_LEDGER", "ledger balances - the trial balance itself"),
    ("PS_GL_ACCOUNT_TBL", "account descriptions and types"),
    ("PS_BUS_UNIT_TBL_GL", "business units and their base currency"),
    ("PS_SET_CNTRL_REC", "SetID resolution for chartfields"),
    ("PS_CAL_DETP_TBL", "accounting calendar (date -> fiscal period)"),
]
OPTIONAL_RECORDS = [
    ("PS_BUS_UNIT_LED", "fast scope discovery (avoids scanning PS_LEDGER)"),
    ("PS_LED_GRP_TBL", "fast scope discovery (ledger groups)"),
    ("PSRECDEFN", "record search by description (custom records)"),
    ("PSRECFIELD", "record search by field name"),
    ("PS_JRNL_HEADER", "journal drill-down"),
    ("PS_JRNL_LN", "journal drill-down (lines)"),
    ("PSTREEDEFN", "tree rollups (Total Assets, statement captions)"),
    ("PSTREENODE", "tree rollups (nodes)"),
    ("PSTREELEAF", "tree rollups (account ranges)"),
    ("PS_ITEM", "AR aging"),
    ("PS_CUSTOMER", "customer names"),
    ("PS_BI_HDR", "billing pipeline"),
    ("PS_INTFC_BI", "billing interface errors"),
    ("PS_RT_RATE_TBL", "currency conversion"),
]

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text, code):
    if not _COLOR:
        return text
    return "\033[{0}m{1}\033[0m".format(code, text)


def bold(t):
    return _c(t, "1")


def green(t):
    return _c(t, "32")


def yellow(t):
    return _c(t, "33")


def red(t):
    return _c(t, "31")


def dim(t):
    return _c(t, "2")


class SetupError(Exception):
    pass


STEP_N = [0]


def step(title):
    STEP_N[0] += 1
    print("")
    print(bold("[{0}] {1}".format(STEP_N[0], title)))
    print(dim("-" * (len(title) + 6)))


def ok(msg):
    print("  " + green("ok") + "  " + msg)


def warn(msg):
    print("  " + yellow("!!") + "  " + msg)


def fail(msg):
    print("  " + red("XX") + "  " + msg)


def info(msg):
    print("      " + dim(msg))


# ------------------------------------------------------------------ prompting
class Prompter(object):
    def __init__(self, interactive):
        self.interactive = interactive

    def ask(self, question, default="", allow_empty=False):
        if not self.interactive:
            return default
        while True:
            suffix = " [{0}]".format(default) if default != "" else ""
            try:
                raw = input("  {0}{1}: ".format(question, suffix)).strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                raise SetupError("cancelled")
            value = raw or default
            if value or allow_empty:
                return value
            print("      a value is required")

    def ask_int(self, question, default, low=1, high=64):
        """Validate at the prompt: an int() on raw input crashes the run."""
        if not self.interactive:
            return int(default)
        while True:
            raw = self.ask(question, str(default))
            try:
                value = int(str(raw).strip())
            except ValueError:
                print("      enter a whole number between {0} and {1}".format(
                    low, high))
                continue
            if low <= value <= high:
                return value
            print("      must be between {0} and {1}".format(low, high))

    def secret(self, question):
        if not self.interactive:
            return ""
        try:
            return getpass.getpass("  {0}: ".format(question))
        except (EOFError, KeyboardInterrupt):
            print("")
            raise SetupError("cancelled")

    def confirm(self, question, default=True):
        if not self.interactive:
            return default
        hint = "Y/n" if default else "y/N"
        while True:
            try:
                raw = input("  {0} [{1}]: ".format(question, hint)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("")
                raise SetupError("cancelled")
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            print("      answer y or n")

    def choose(self, question, options, default_index=0, free_text=""):
        """options: list of (value, label). free_text, when set, is a hint that
        a typed value is also accepted (used when the list is truncated)."""
        if not self.interactive:
            return options[default_index][0]
        print("  " + question)
        for i, pair in enumerate(options, start=1):
            marker = "*" if i - 1 == default_index else " "
            print("   {0} {1}) {2}".format(marker, i, pair[1]))
        if free_text:
            print("     or type {0}".format(free_text))
        while True:
            raw = self.ask("choice", str(default_index + 1))
            try:
                idx = int(raw) - 1
            except ValueError:
                if free_text:
                    return ("free", raw)
                print("      enter a number from the list")
                continue
            if 0 <= idx < len(options):
                return options[idx][0]
            print("      enter a number from the list"
                  + (" or type a value" if free_text else ""))


# ------------------------------------------------------------- subprocess I/O
def _child_env(extra=None):
    """Environment for a child process.

    PYTHONIOENCODING is forced because a server with LC_ALL=C gives children an
    ASCII stdout, and any script that prints a non-ASCII character then dies
    mid-write - which took out the seeding step on a correctly configured box.
    """
    merged = dict(os.environ)
    merged["PYTHONIOENCODING"] = "utf-8"
    if extra:
        merged.update(extra)
    return merged


def run(cmd, check=True, capture=False, timeout=None):
    kwargs = {"cwd": ROOT, "env": _child_env()}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise SetupError("timed out after {0}s: {1}".format(
            timeout, " ".join(cmd[:3])))
    text = out.decode("utf-8", "replace") if out else ""
    if check and proc.returncode != 0:
        if capture and text:
            print(text.rstrip())
        raise SetupError("command failed: {0}".format(" ".join(cmd[:3])))
    return proc.returncode, text


def venv_python():
    if platform.system() == "Windows":
        return os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    return os.path.join(ROOT, ".venv", "bin", "python")


def run_in_venv(snippet, env=None, timeout=PROBE_TIMEOUT):
    """Run a snippet inside the venv and parse its JSON stdout.

    Secrets travel in the environment, never in argv, so they cannot appear in
    a process listing. Output is parsed, never echoed.
    """
    merged = _child_env(env)
    merged["PYTHONPATH"] = ROOT + os.pathsep + merged.get("PYTHONPATH", "")
    proc = subprocess.Popen([venv_python(), "-c", snippet], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=merged)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"ok": False, "error": "timed out after {0}s - the database did "
                                      "not answer (firewall?)".format(timeout)}
    text = (out or b"").decode("utf-8", "replace").strip()
    errtext = (err or b"").decode("utf-8", "replace").strip()
    if not text:
        return {"ok": False, "error": errtext[-600:] or "no output"}
    try:
        return json.loads(text.splitlines()[-1])
    except ValueError:
        return {"ok": False, "error": (text + "\n" + errtext)[-600:]}


# ------------------------------------------------------------- file emission
def _y(value):
    """Emit a YAML scalar/list safely.

    json.dumps output is valid YAML, and quoting is what keeps a numeric
    PeopleSoft business unit ("10000") a STRING. Unquoted it loads as an int
    and the engine raises AttributeError on .strip(); "01" would become 1 and
    "NO" would become False.
    """
    return json.dumps(value)


def _split(raw):
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _ints(raw):
    out = []
    for item in _split(raw):
        try:
            out.append(int(item))
        except ValueError:
            continue
    return out


def _atomic_write(path, text, mode=None):
    """Write via a temp file in the same directory, then replace.

    A partial write must never become the live file, and .env must never exist
    at the default umask while it already holds the password - so the mode is
    applied at CREATION, not afterwards.
    """
    tmp = path + ".tmp-{0}".format(os.getpid())
    if mode is not None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
    else:
        handle = open(tmp, "w", encoding="utf-8")
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(tmp, path)


def _read_config_values(src=None):
    """Existing config values, so a re-run defaults to what is already set.

    Parsed with PyYAML when the venv has it; otherwise a small line reader
    covers the flat keys this wizard writes.
    """
    src = src or CONFIG
    if not os.path.exists(src):
        return {}
    snippet = (
        "import json,sys\n"
        "try:\n"
        "    import yaml\n"
        "    d = yaml.safe_load(open(sys.argv[0] if False else "
        "    __import__('os').environ['CFG'], encoding='utf-8')) or {}\n"
        "    print(json.dumps({'ok': True, 'data': d}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'error': str(e)[:200]}))\n"
    )
    if os.path.exists(venv_python()):
        res = run_in_venv(snippet, env={"CFG": src}, timeout=30)
        if res.get("ok"):
            data = res.get("data") or {}
            d = data.get("defaults") or {}
            db = data.get("db") or {}
            llm = data.get("llm") or {}
            wiki = data.get("wiki") or {}
            tools = data.get("tools") or {}
            out = {}

            def put(key, value):
                if value not in (None, ""):
                    out[key] = value

            put("business_unit", d.get("business_unit"))
            put("ledger", d.get("ledger"))
            put("setid", d.get("setid"))
            put("calendar", d.get("calendar_id"))
            put("currency", d.get("base_currency"))
            put("retained", d.get("retained_earnings_account"))
            put("tree", d.get("account_tree"))
            if d.get("suspense_accounts"):
                out["suspense"] = ",".join(str(x) for x in d["suspense_accounts"])
            if d.get("ar_control_accounts"):
                out["ar_control"] = ",".join(
                    str(x) for x in d["ar_control_accounts"])
            if d.get("adjustment_periods"):
                out["adjust"] = ",".join(str(x) for x in d["adjustment_periods"])
            put("backend", db.get("backend"))
            put("schema", db.get("schema"))
            put("timeout", db.get("query_timeout_seconds"))
            put("pool_max", db.get("pool_max"))
            put("use_views", db.get("use_views"))
            put("provider", llm.get("provider"))
            put("ollama_model", llm.get("ollama_model"))
            put("gemini_model", llm.get("gemini_model"))
            put("claude_model", llm.get("claude_model"))
            put("wiki_provider", wiki.get("provider"))
            put("wiki_space", wiki.get("confluence_space"))
            put("max_rows", tools.get("max_rows"))
            if tools.get("allow_raw_sql") is not None:
                out["allow_sql"] = bool(tools["allow_raw_sql"])
            return out
    return {}


_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _env_quote(value):
    """Quote a .env value so it round-trips exactly.

    Unquoted, python-dotenv truncates at '#' and strips surrounding spaces -
    so a password like "Winter2026 #ps" silently becomes "Winter2026" AFTER
    this wizard reported a successful connection, and the user then meets
    ORA-01017.

    Single quotes preserve everything but cannot contain a single quote (there
    is no escape for one), so a value containing one is double-quoted with
    backslash escapes instead.
    """
    text = str(value)
    if "'" not in text:
        return "'" + text + "'"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_env(state, quiet=False):
    """Update .env in place: existing lines (and comments) are preserved, and
    only the keys we actually obtained are replaced or appended."""
    updates = {}
    if state.get("backend") == "oracle":
        for key, val in (("ORACLE_DSN", state.get("dsn")),
                         ("ORACLE_USER", state.get("user")),
                         ("TNS_ADMIN", state.get("tns")),
                         ("ORACLE_WALLET_DIR", state.get("wallet")),
                         ("ORACLE_WALLET_PASSWORD", state.get("wallet_pw"))):
            if val:
                updates[key] = val
        # Never blank an existing password because this run did not collect
        # one (--non-interactive has no channel for it).
        if state.get("password"):
            updates["ORACLE_PASSWORD"] = state["password"]
    for key in ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL",
                "CONFLUENCE_API_TOKEN", "GOOGLE_CLOUD_PROJECT", "OLLAMA_HOST"):
        if state.get(key):
            updates[key] = state[key]
    if not updates and os.path.exists(ENVFILE):
        return

    lines = []
    if os.path.exists(ENVFILE):
        with open(ENVFILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    else:
        lines = ["# Secrets for the PeopleSoft finance agent.",
                 "# Never commit this file.", ""]

    seen = set()
    out = []
    for line in lines:
        match = _ENV_KEY.match(line)
        key = match.group(1) if match else None
        if key and key in updates:
            out.append("{0}={1}".format(key, _env_quote(updates[key])))
            seen.add(key)
        else:
            out.append(line)
    for key in sorted(updates):
        if key not in seen:
            out.append("{0}={1}".format(key, _env_quote(updates[key])))

    _atomic_write(ENVFILE, "\n".join(out).rstrip("\n") + "\n", mode=0o600)
    if not quiet:
        if platform.system() == "Windows":
            ok(".env written (secrets are not echoed anywhere)")
            info("Windows does not honour POSIX file modes - restrict access "
                 "to this file yourself")
        else:
            ok(".env written, mode 600 (secrets are not echoed anywhere)")


def _config_text(state):
    return """# PeopleSoft finance agent configuration.
# Secrets (DB password, API tokens) live in .env - never here.
# Written by scripts/setup.py; re-running it keeps these values as defaults.

defaults:
  business_unit: {bu}
  ledger: {ledger}
  setid: {setid}
  calendar_id: {calendar}
  base_currency: {currency}
  rate_type: "CRRNT"
  adjustment_periods: {adjust}
  suspense_accounts: {suspense}
  retained_earnings_account: {retained}
  account_tree: {tree}
  ar_control_accounts: {ar_control}
  aging_buckets: [30, 60, 90]

db:
  backend: {backend}
  sqlite_path: "sample_data/ps_sample.db"
  schema: {schema}
  use_views: {use_views}
  query_timeout_seconds: {timeout}
  pool_max: {pool_max}

llm:
  provider: {provider}
  ollama_host: "http://localhost:11434"
  ollama_model: {ollama_model}
  gemini_model: {gemini_model}
  gemini_location: "us-central1"
  gemini_thinking_budget: -1
  claude_model: {claude_model}
  claude_effort: "high"
  temperature: 0.2
  max_tool_result_chars: 0

wiki:
  provider: {wiki_provider}
  localdocs_path: "sample_wiki"
  confluence_space: {wiki_space}
  confluence_labels: ""

tools:
  allow_raw_sql: {allow_sql}
  max_rows: {max_rows}
  question_log: "logs/questions.jsonl"
  reports_path: "reports"

batch_exports:
  enabled: true
  inline_rows: 100
  max_rows: 1000000
  max_file_mb: 1024
  fetch_size: 2000
  workers: 2
  max_queued: 8
  ttl_minutes: 60
  directory: "logs/batch_exports"
""".format(
        bu=_y(str(state.get("business_unit", "US001"))),
        ledger=_y(str(state.get("ledger", "ACTUALS"))),
        setid=_y(str(state.get("setid", "SHARE"))),
        calendar=_y(str(state.get("calendar", "01"))),
        currency=_y(str(state.get("currency", "USD"))),
        adjust=_y(_ints(state.get("adjust", "998")) or [998]),
        suspense=_y(_split(state.get("suspense", "1999"))),
        retained=_y(str(state.get("retained", "3500"))),
        tree=_y(str(state.get("tree", "ACCOUNT"))),
        ar_control=_y(_split(state.get("ar_control", "1100"))),
        backend=_y(str(state.get("backend", "sqlite"))),
        schema=_y(str(state.get("schema", ""))),
        use_views=str(bool(state.get("use_views", False))).lower(),
        timeout=int(state.get("timeout", 120)),
        pool_max=int(state.get("pool_max", 8)),
        provider=_y(str(state.get("provider", "ollama"))),
        ollama_model=_y(str(state.get("ollama_model", "llama3.1:8b"))),
        gemini_model=_y(str(state.get("gemini_model", "gemini-2.5-pro"))),
        claude_model=_y(str(state.get("claude_model", "claude-opus-5"))),
        wiki_provider=_y(str(state.get("wiki_provider", "localdocs"))),
        wiki_space=_y(str(state.get("wiki_space", ""))),
        allow_sql=str(bool(state.get("allow_sql", True))).lower(),
        max_rows=int(state.get("max_rows", 500)),
    )


BACKUP = [None]


def _write_config(state):
    if os.path.exists(CONFIG) and BACKUP[0] is None:
        BACKUP[0] = CONFIG + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy(CONFIG, BACKUP[0])
        info("previous config saved as {0}".format(os.path.basename(BACKUP[0])))
    _atomic_write(CONFIG, _config_text(state))
    ok("config.yaml written")


def _interim_config(state):
    """A throwaway config for the discovery probe.

    Discovery needs the new credentials readable through load_config, but must
    NOT replace the user's config.yaml - an abort at that point used to leave
    the wizard's template as the live file.
    """
    path = os.path.join(ROOT, ".setup-probe-config.yaml")
    _atomic_write(path, _config_text(state))
    return path


def _validate_config():
    snippet = (
        "import json,os\n"
        "try:\n"
        "    from pstb.config import load_config\n"
        "    c = load_config(os.environ['CFG'])\n"
        "    print(json.dumps({'ok': True, 'bu': str(c.defaults.business_unit),\n"
        "                      'backend': c.db.backend}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'error': str(e)[:300]}))\n"
    )
    return run_in_venv(snippet, env={"CFG": CONFIG}, timeout=60)


# --------------------------------------------------------------------- steps
def step_python():
    step("Python version")
    version = ".".join(str(x) for x in sys.version_info[:3])
    if sys.version_info < MIN_PYTHON:
        fail("this is Python {0}; the agent needs {1}+".format(
            version, ".".join(str(x) for x in MIN_PYTHON)))
        print("")
        print("  On RHEL/Oracle Linux the default python3 is often too old:")
        print("      sudo dnf install python3.12")
        print("      python3.12 scripts/setup.py")
        raise SetupError("Python too old")
    ok("Python {0} at {1}".format(version, sys.executable))


def step_venv(p, backend):
    step("Virtualenv and dependencies")
    py = venv_python()
    if os.path.exists(py):
        ok("virtualenv already present at .venv")
        if not p.confirm("reinstall dependencies?", default=False):
            return
    else:
        info("creating .venv (this takes a minute)")
        run([sys.executable, "-m", "venv", ".venv"], timeout=600)
        ok("virtualenv created")

    extras = ["gui"]
    if backend == "oracle":
        extras.append("oracle")
    target = ".[{0}]".format(",".join(extras))
    info("installing {0}".format(target))
    run([py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        check=False, timeout=600)
    rc, out = run([py, "-m", "pip", "install", "-e", target, "--quiet"],
                  check=False, capture=True, timeout=1800)
    if rc != 0:
        print(out.rstrip()[-2000:])
        fail("dependency install failed")
        print("")
        print("  Behind a corporate proxy, set these and re-run:")
        print("      export HTTPS_PROXY=http://proxy.yourco.com:8080")
        print("      export PIP_INDEX_URL=https://artifactory.yourco.com"
              "/api/pypi/pypi/simple")
        raise SetupError("pip install failed")
    ok("dependencies installed ({0})".format(", ".join(extras)))


def step_sample():
    step("Sample ledger and self-test")
    run([venv_python(), "scripts/seed_sample_data.py"], capture=True,
        timeout=600)
    ok("sample ledger built")
    # Run the suite with the interpreter that will actually serve requests.
    rc, out = run([venv_python(), "scripts/smoke_test.py"], check=False,
                  capture=True, timeout=900)
    if rc != 0:
        print(out[-2000:])
        raise SetupError("sample self-test failed - the code itself is broken")
    last = [l for l in out.splitlines() if "checks passed" in l]
    ok(last[-1].strip() if last else "self-test passed")


def _oracle_probe_snippet():
    return (
        "import json,os\n"
        "from pstb.config import load_config\n"
        "from pstb.db import Database\n"
        "cfg = load_config(os.environ['PSTB_CONFIG'])\n"
        "cfg.db.backend = 'oracle'\n"
        "cfg.db.oracle_dsn = os.environ['ORACLE_DSN']\n"
        "cfg.db.oracle_user = os.environ['ORACLE_USER']\n"
        "cfg.db.oracle_password = os.environ['ORACLE_PASSWORD']\n"
        "cfg.db.schema = os.environ.get('PSTB_SCHEMA','')\n"
        "cfg.db.oracle_config_dir = os.environ.get('TNS_ADMIN','')\n"
        "cfg.db.oracle_wallet_dir = os.environ.get('ORACLE_WALLET_DIR','')\n"
        "cfg.db.oracle_wallet_password = os.environ.get("
        "'ORACLE_WALLET_PASSWORD','')\n"
        "try:\n"
        "    Database(cfg).query('SELECT 1 AS ok FROM DUAL', {}, max_rows=1)\n"
        "    print(json.dumps({'ok': True}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'error': str(e)[:400]}))\n"
    )


def step_backend(p, backend, state):
    step("Database backend")
    state["backend"] = backend
    if backend != "oracle":
        ok("using the bundled SQLite sample (no Oracle credentials needed)")
        return
    if not p.interactive:
        ok("keeping the configured Oracle connection (credentials unchanged)")
        return

    print("  Ask your DBA for a READ-ONLY account and a service-name DSN.")
    info("thin mode needs host:port/SERVICE_NAME - not a SID; Kerberos and OS "
         "authentication are not supported")
    state["dsn"] = p.ask("Oracle DSN", state.get("dsn")
                         or os.environ.get("ORACLE_DSN", ""))
    state["user"] = p.ask("Oracle user", state.get("user")
                          or os.environ.get("ORACLE_USER", ""))
    password = p.secret("Oracle password (not echoed, not logged)")
    if not password:
        raise SetupError("a password is required to verify the connection")
    if password != password.strip():
        warn("the password has leading or trailing spaces; they are preserved "
             "here but some tools strip them")
    if "${" in password:
        warn("the password contains '${' which .env readers expand as a "
             "variable - ask for a password without it")
    state["password"] = password
    state["schema"] = p.ask("Schema that owns the records",
                            state.get("schema") or "SYSADM")
    state["tns"] = p.ask("tnsnames/sqlnet directory (blank unless the DSN is "
                         "an alias)", state.get("tns", ""), allow_empty=True)
    state["wallet"] = p.ask("wallet directory (blank unless the site requires "
                            "mTLS)", "", allow_empty=True)
    if state["wallet"]:
        state["wallet_pw"] = p.secret("wallet password (blank if none)")

    info("testing the connection")
    env = {"ORACLE_DSN": state["dsn"], "ORACLE_USER": state["user"],
           "ORACLE_PASSWORD": state["password"],
           "PSTB_SCHEMA": state["schema"], "PSTB_CONFIG": CONFIG}
    for key, val in (("TNS_ADMIN", state.get("tns")),
                     ("ORACLE_WALLET_DIR", state.get("wallet")),
                     ("ORACLE_WALLET_PASSWORD", state.get("wallet_pw"))):
        if val:
            env[key] = val
    res = run_in_venv(_oracle_probe_snippet(), env=env)
    if not res.get("ok") and "oracledb" in str(res.get("error", "")):
        warn("the Oracle driver is not installed in .venv - installing it now")
        rc, out = run([venv_python(), "-m", "pip", "install", "-e",
                       ".[oracle]", "--quiet"], check=False, capture=True,
                      timeout=1800)
        if rc != 0:
            print(out.rstrip()[-1500:])
            raise SetupError("could not install python-oracledb")
        ok("python-oracledb installed")
        res = run_in_venv(_oracle_probe_snippet(), env=env)
    if not res.get("ok"):
        fail("could not connect")
        info(str(res.get("error", ""))[:400])
        print("")
        print("  Common causes:")
        print("    ORA-12154 / DPY-4000  DSN wrong, or an alias without TNS_ADMIN")
        print("    ORA-01017             bad username or password")
        print("    ORA-28000             account locked - ask the DBA")
        print("    ORA-12541 / timeout   nothing listening, or a firewall")
        raise SetupError("Oracle connection failed")
    ok("connected to Oracle")


def _discovery_snippet():
    return (
        "import json,os\n"
        "from pstb.config import load_config\n"
        "from pstb.db import Database\n"
        "cfg = load_config(os.environ['PSTB_CONFIG'])\n"
        "out = {'ok': True, 'scopes': [], 'missing': [], 'present': [],\n"
        "       'setid': '', 'calendar': '', 'currency': ''}\n"
        "db = Database(cfg)\n"
        "p = db.prefix\n"
        "def q(sql, params=None, n=50):\n"
        "    return db.query(sql, params or {}, max_rows=n)[0]\n"
        "for rec in json.loads(os.environ['RECORDS']):\n"
        "    try:\n"
        "        q(db.exists_sql('SELECT 1 FROM ' + p + rec), {}, 1)\n"
        "        out['present'].append(rec)\n"
        "    except Exception:\n"
        "        out['missing'].append(rec)\n"
        "try:\n"
        "    rows = q('SELECT B.BUSINESS_UNIT AS bu, G.LEDGER AS led FROM '\n"
        "             + p + 'PS_BUS_UNIT_LED B JOIN ' + p + 'PS_LED_GRP_TBL G'\n"
        "             + ' ON G.LEDGER_GROUP = B.LEDGER_GROUP'\n"
        "             + ' ORDER BY B.BUSINESS_UNIT, G.LEDGER', {}, 400)\n"
        "    out['scopes'] = [[str(r['bu']), str(r['led'])] for r in rows]\n"
        "except Exception:\n"
        "    pass\n"
        "if not out['scopes']:\n"
        "    try:\n"
        "        rows = q('SELECT BUSINESS_UNIT AS bu FROM ' + p\n"
        "                 + 'PS_BUS_UNIT_TBL_GL ORDER BY BUSINESS_UNIT', {}, 200)\n"
        "        for r in rows[:25]:\n"
        "            leds = q('SELECT DISTINCT LEDGER AS led FROM ' + p\n"
        "                     + 'PS_LEDGER WHERE BUSINESS_UNIT = :bu',\n"
        "                     {'bu': r['bu']}, 20)\n"
        "            for l in leds:\n"
        "                out['scopes'].append([str(r['bu']), str(l['led'])])\n"
        "    except Exception as e:\n"
        "        out['scope_error'] = str(e)[:200]\n"
        "bu = out['scopes'][0][0] if out['scopes'] else ''\n"
        "if bu:\n"
        "    try:\n"
        "        r = q('SELECT SETID AS s FROM ' + p + 'PS_SET_CNTRL_REC'\n"
        "              + ' WHERE SETCNTRLVALUE = :bu AND RECNAME = :r',\n"
        "              {'bu': bu, 'r': 'GL_ACCOUNT_TBL'}, 1)\n"
        "        out['setid'] = str(r[0]['s']) if r else ''\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        r = q('SELECT BASE_CURRENCY AS c FROM ' + p\n"
        "              + 'PS_BUS_UNIT_TBL_GL WHERE BUSINESS_UNIT = :bu',\n"
        "              {'bu': bu}, 1)\n"
        "        out['currency'] = str(r[0]['c']) if r else ''\n"
        "    except Exception:\n"
        "        pass\n"
        "try:\n"
        "    r = q('SELECT DISTINCT CALENDAR_ID AS c FROM ' + p\n"
        "          + 'PS_CAL_DETP_TBL ORDER BY CALENDAR_ID', {}, 10)\n"
        "    out['calendar'] = str(r[0]['c']) if r else ''\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps(out))\n"
    )


def _ask_site_values(p, state):
    """The values that cannot be discovered. Asked in EVERY oracle run, even
    when scope discovery found nothing - a wrong value here reports no data
    rather than erroring, so it must never be left at a sample default."""
    state["setid"] = p.ask("SetID for chartfields", state.get("setid", "SHARE"))
    state["calendar"] = p.ask("Calendar ID", state.get("calendar", "01"))
    state["currency"] = p.ask("Base currency", state.get("currency", "USD"))
    print("")
    print("  " + yellow("These cannot be discovered and fail SILENTLY if wrong.")
          + " Ask your")
    print("  functional team; a wrong value reports nothing rather than erroring:")
    state["suspense"] = p.ask("Suspense account(s), comma separated",
                              state.get("suspense", "1999"))
    state["retained"] = p.ask("Retained earnings account",
                              state.get("retained", "3500"))
    state["ar_control"] = p.ask("AR control account(s), comma separated",
                                state.get("ar_control", "1100"))
    state["tree"] = p.ask("Account tree for rollups", state.get("tree", "ACCOUNT"))
    state["adjust"] = p.ask("Adjustment period(s), comma separated",
                            state.get("adjust", "998"))


def step_discover(p, state):
    step("Discover scope and check grants")
    records = [r[0] for r in REQUIRED_RECORDS + OPTIONAL_RECORDS]
    probe_cfg = _interim_config(state)
    env = {"RECORDS": json.dumps(records), "PSTB_CONFIG": probe_cfg}
    if state.get("backend") == "oracle" and state.get("password"):
        env.update({"ORACLE_DSN": state.get("dsn", ""),
                    "ORACLE_USER": state.get("user", ""),
                    "ORACLE_PASSWORD": state["password"]})
        for key, val in (("TNS_ADMIN", state.get("tns")),
                         ("ORACLE_WALLET_DIR", state.get("wallet")),
                         ("ORACLE_WALLET_PASSWORD", state.get("wallet_pw"))):
            if val:
                env[key] = val
    try:
        res = run_in_venv(_discovery_snippet(), env=env)
    finally:
        if os.path.exists(probe_cfg):
            os.remove(probe_cfg)

    if not res.get("ok"):
        warn("discovery could not run: {0}".format(
            str(res.get("error", ""))[:300]))
        _ask_site_values(p, state)
        return

    missing = res.get("missing", [])
    purpose = dict(REQUIRED_RECORDS + OPTIONAL_RECORDS)
    required_missing = [r for (r, _) in REQUIRED_RECORDS if r in missing]
    optional_missing = [r for (r, _) in OPTIONAL_RECORDS if r in missing]
    ok("{0} of {1} records readable".format(len(res.get("present", [])),
                                            len(records)))
    if missing:
        if required_missing:
            fail("missing records the agent REQUIRES:")
            for r in required_missing:
                print("        {0:<22} {1}".format(r, purpose[r]))
        if optional_missing:
            warn("missing records that disable features:")
            for r in optional_missing:
                print("        {0:<22} {1}".format(r, purpose[r]))
        owner = state.get("schema") or "SYSADM"
        user = state.get("user") or "<read_only_user>"
        print("")
        print("  Send this to your DBA:")
        print("      GRANT SELECT ON {0}.{1} TO {2};".format(
            owner, (", " + owner + ".").join(missing), user))
        print("")
        if required_missing and not p.confirm(
                "continue anyway (the agent will not work fully)?",
                default=False):
            raise SetupError("missing required grants")

    scopes = res.get("scopes", [])
    if scopes:
        ok("found {0} business-unit/ledger combination(s)".format(len(scopes)))
        shown = scopes[:15]
        options = [(i, "{0} / {1}".format(a, b))
                   for i, (a, b) in enumerate(shown)]
        if len(scopes) > 15:
            info("showing the first 15 of {0}".format(len(scopes)))
        picked = p.choose("Default business unit and ledger:", options, 0,
                          free_text="BUSINESS_UNIT/LEDGER directly")
        if isinstance(picked, tuple):          # typed rather than numbered
            raw = picked[1]
            parts = [x.strip() for x in raw.replace(",", "/").split("/")]
            state["business_unit"] = parts[0]
            state["ledger"] = parts[1] if len(parts) > 1 else state.get(
                "ledger", "ACTUALS")
        else:
            state["business_unit"], state["ledger"] = scopes[int(picked)]
    else:
        warn("no business unit / ledger combinations found")
        info(res.get("scope_error", "check the grants above and the schema name"))
        state["business_unit"] = p.ask("Business unit",
                                       state.get("business_unit", ""))
        state["ledger"] = p.ask("Ledger", state.get("ledger", "ACTUALS"))

    for key, label in (("setid", "SetID"), ("calendar", "calendar"),
                       ("currency", "base currency")):
        if res.get(key):
            state[key] = res[key]
            ok("{0} discovered: {1}".format(label, res[key]))
    _ask_site_values(p, state)


def step_llm(p, state):
    step("Language model")
    current = state.get("provider", "ollama")
    options = [("ollama", "Ollama - runs locally, no data leaves this host"),
               ("gemini", "Gemini on Vertex AI - tool results go to Google Cloud"),
               ("claude", "Claude on the Anthropic API - tool results go to Anthropic")]
    order = [o[0] for o in options]
    default_index = order.index(current) if current in order else 0
    state["provider"] = p.choose("Which provider?", options, default_index)
    if state["provider"] == "ollama":
        host = p.ask("Ollama host", state.get("OLLAMA_HOST",
                                              "http://localhost:11434"))
        state["OLLAMA_HOST"] = host
        state["ollama_model"] = p.ask("Model",
                                      state.get("ollama_model", "llama3.1:8b"))
        snippet = (
            "import json,os,urllib.request\n"
            "try:\n"
            "    u = os.environ['H'].rstrip('/') + '/api/tags'\n"
            "    d = json.loads(urllib.request.urlopen(u, timeout=5).read())\n"
            "    print(json.dumps({'ok': True, 'models':"
            " [m['name'] for m in d.get('models',[])]}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'error': str(e)[:200]}))\n"
        )
        res = run_in_venv(snippet, env={"H": host}, timeout=30)
        if res.get("ok"):
            models = res.get("models", [])
            ok("Ollama reachable ({0} model(s))".format(len(models)))
            if models and state["ollama_model"] not in models:
                warn("{0} is not pulled. Available: {1}".format(
                    state["ollama_model"], ", ".join(models[:5])))
                info("ollama pull {0}".format(state["ollama_model"]))
        else:
            warn("Ollama not reachable at {0}".format(host))
            info("ollama serve   (then: ollama pull {0})".format(
                state["ollama_model"]))
    elif state["provider"] == "claude":
        state["claude_model"] = p.ask("Model",
                                      state.get("claude_model",
                                                "claude-opus-5"))
        # Deliberately not asked for here. The wizard prints what it
        # collects and writes state to disk between steps; a key typed at
        # this prompt would land in both. .env and the console are the two
        # paths that never echo a secret.
        warn("tool results - including ledger figures - are sent to Anthropic")
        info("confirm this is acceptable under your data-governance policy")
        info("set the key in .env as ANTHROPIC_API_KEY=..., or from the "
             "configuration console at /console - this wizard never asks "
             "for a secret it would then print")
    else:
        state["GOOGLE_CLOUD_PROJECT"] = p.ask(
            "GOOGLE_CLOUD_PROJECT",
            state.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
        warn("tool results - including ledger figures - are sent to Google Cloud")
        info("confirm this is acceptable under your data-governance policy")
        info("headless auth: gcloud auth application-default login "
             "--no-launch-browser, or attach a service account")


def step_wiki(p, state):
    step("Company wiki (Confluence)")
    already = state.get("wiki_provider") == "confluence"
    if not p.confirm("Connect Confluence for policy questions?", default=already):
        state["wiki_provider"] = "localdocs"
        warn("using the bundled sample policy pages")
        info("answers carry a demo-content warning; they are NOT your policy")
        return
    state["wiki_provider"] = "confluence"
    state["CONFLUENCE_BASE_URL"] = p.ask(
        "Base URL (Cloud: https://co.atlassian.net/wiki, DC: "
        "https://wiki.co.com)", state.get("CONFLUENCE_BASE_URL", ""))
    state["CONFLUENCE_EMAIL"] = p.ask(
        "Email (Cloud only - blank for a Data Center PAT)", "",
        allow_empty=True)
    token = p.secret("API token / PAT (not echoed)")
    if token:
        state["CONFLUENCE_API_TOKEN"] = token
    state["wiki_space"] = p.ask("Space key to search (blank for all)",
                                state.get("wiki_space", ""), allow_empty=True)
    info("provider is 'confluence' so it FAILS CLOSED - it will never silently "
         "serve the bundled demo pages next to real balances")


def step_preflight(state):
    step("Database pre-flight")
    if state.get("backend") != "oracle":
        ok("sample backend - pre-flight not applicable")
        return True
    rc, out = run([venv_python(), "scripts/diagnose_db.py",
                   "--bu", str(state.get("business_unit", "")),
                   "--ledger", str(state.get("ledger", ""))],
                  check=False, capture=True, timeout=900)
    lines = out.strip().splitlines()
    shown = [l for l in lines
             if any(k in l for k in ("[FAIL", "[EMPTY", "PRE-FLIGHT", "  - "))]
    if shown:
        for line in shown:
            print("      " + line.strip())
    elif rc != 0:
        # The filter matched nothing (e.g. a traceback) - show the tail rather
        # than "fix the items above" with nothing above.
        for line in lines[-20:]:
            print("      " + line)
    if rc == 0:
        ok("pre-flight passed - scope resolves and records are readable")
        return True
    fail("pre-flight failed (exit {0})".format(rc))
    info("re-run it directly: .venv/bin/python scripts/diagnose_db.py")
    return False


def step_summary(state, preflight_ok):
    step("Ready")
    # Run this INSIDE the venv: this script runs on the system interpreter,
    # where pstb is not importable, so importing it here silently skipped the
    # build record on every install — precisely the ZIP deployment the record
    # exists for.
    snippet = ("import json\n"
               "from pstb.version import label, write_build_info\n"
               "write_build_info()\n"
               "print(json.dumps({'ok': True, 'label': label()}))\n")
    res = run_in_venv(snippet, timeout=60)
    if res.get("ok"):
        ok("running build: {0}".format(res.get("label", "?")))
        info("recorded in BUILD_INFO.json so a ZIP deployment still reports "
             "its origin")
    else:
        warn("could not record build identity: {0}".format(
            str(res.get("error", ""))[:120]))
    py = ".venv\\Scripts\\python" if platform.system() == "Windows" \
        else ".venv/bin/python"
    print("  Start the web UI:")
    print("      {0} -m pstb.gui".format(py))
    print("")
    print("  It listens on 0.0.0.0:8016 — colleagues can open it directly.")
    print("  To keep it to this machine, add --host 127.0.0.1, then:")
    print("      ssh -L 8016:127.0.0.1:8016 you@{0}".format(
        platform.node() or "appserver"))
    print("      then open http://127.0.0.1:8016")
    print("")
    print("  " + yellow("Do not pass --host 0.0.0.0")
          + ": there is no login, so that would")
    print("  publish production financials to anyone who can reach the port.")
    print("")
    print("  Terminal instead of the browser:")
    print("      {0} -m pstb.client.chat".format(py))
    print("  Re-check the database at any time:")
    print("      {0} scripts/diagnose_db.py".format(py))
    print("  Review questions the agent failed to answer:")
    print("      {0} -m pstb.qlog".format(py))
    if not preflight_ok:
        print("")
        warn("pre-flight did not pass - expect errors until it does")


def main():
    ap = argparse.ArgumentParser(
        description="Interactive setup for the PeopleSoft finance agent")
    ap.add_argument("--backend", choices=["oracle", "sqlite"], default=None,
                    help="skip the backend question")
    ap.add_argument("--non-interactive", action="store_true",
                    help="keep every existing value and ask nothing")
    args = ap.parse_args()

    print(bold("\nPeopleSoft finance agent - setup"))
    print(dim("working directory: {0}".format(ROOT)))

    p = Prompter(not args.non_interactive)
    # Existing values become the defaults, so a re-run keeps the operator's
    # tuning instead of reverting it to the sample values.
    state = {"timeout": 120, "pool_max": 8, "allow_sql": True, "max_rows": 500}
    try:
        step_python()
        have_venv = os.path.exists(venv_python())
        existing = _read_config_values() if have_venv else {}
        if existing:
            state.update(existing)
            info("existing configuration found; press Enter to keep each value")
        elif have_venv:
            # No config.yaml — a fresh clone, since it is not tracked. Seed
            # from the shipped example so the offered defaults are the
            # documented ones, WITHOUT claiming a configuration exists.
            state.update(_read_config_values(EXAMPLE))

        backend = args.backend
        if backend is None:
            # ALWAYS ask when interactive. Treating an existing value as the
            # answer meant a fresh checkout - whose shipped config.yaml says
            # sqlite - silently stayed on the sample ledger, with no prompt
            # and no clue where to change it. The current value is only the
            # default; --non-interactive keeps it without asking.
            current = (state.get("backend") or "sqlite").lower()
            options = [
                ("oracle", "A real PeopleSoft database (Oracle)"),
                ("sqlite", "The bundled sample ledger (no credentials needed)"),
            ]
            default_index = 0 if current == "oracle" else 1
            if p.interactive and current:
                info("currently configured: {0}".format(current))
            backend = p.choose("What is this installation for?", options,
                               default_index)
            if not p.interactive:
                info("keeping the configured backend: {0}".format(backend))

        step_venv(p, backend)
        if not existing:
            state.update(_read_config_values())
        step_sample()
        step_backend(p, backend, state)
        if state.get("backend") == "oracle" and p.interactive:
            step_discover(p, state)
            state["pool_max"] = p.ask_int(
                "Maximum concurrent chat channels (Oracle sessions)",
                state.get("pool_max", 8), 1, 64)
            state["allow_sql"] = p.confirm(
                "Allow ad-hoc SQL? (needed for custom records; the read-only "
                "account is the only boundary)",
                default=bool(state.get("allow_sql", True)))
        step_llm(p, state)
        step_wiki(p, state)
        _write_env(state)
        _write_config(state)
        check = _validate_config()
        if not check.get("ok"):
            fail("the generated config.yaml does not load:")
            info(str(check.get("error", ""))[:300])
            if BACKUP[0]:
                shutil.copy(BACKUP[0], CONFIG)
                info("restored your previous config.yaml")
            raise SetupError("config generation failed")
        ok("config.yaml loads (business unit {0}, backend {1})".format(
            check.get("bu"), check.get("backend")))
        preflight_ok = step_preflight(state)
        step_summary(state, preflight_ok)
        print("")
        return 0 if preflight_ok else 2
    except SetupError as e:
        print("")
        fail(str(e))
        if BACKUP[0] and os.path.exists(BACKUP[0]):
            shutil.copy(BACKUP[0], CONFIG)
            info("your previous config.yaml has been restored")
        print("")
        print("  Fix the issue above and re-run - this is safe to repeat:")
        print("      python3 scripts/setup.py")
        return 1
    except Exception as e:                    # never a bare traceback
        print("")
        fail("unexpected error: {0}: {1}".format(type(e).__name__, e))
        if BACKUP[0] and os.path.exists(BACKUP[0]):
            shutil.copy(BACKUP[0], CONFIG)
            info("your previous config.yaml has been restored")
        return 1


if __name__ == "__main__":
    sys.exit(main())
