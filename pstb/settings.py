"""What the console is allowed to change, and how a change reaches disk.

Two rules shape this module.

AN ALLOWLIST, NOT A DENYLIST. A setting is editable because someone decided
it is safe to edit, not because nobody thought to forbid it. Anything that
changes WHAT CODE RUNS or WHERE DATA GOES stays out: the database DSN and
driver, named sources, the wiki's base URL, whether raw SQL is enabled,
file paths, the server's own bind. Those are the settings an attacker who
reached the console would actually want, and none of them is something the
owner needs to change between coffees.

AN OVERLAY, NOT A REWRITE. config.yaml is hand-edited and its comments
carry the reasoning for values that took measurement to find — the Ollama
context size, the adjustment-period rules. Regenerating it from a template
would silently delete all of that. So the console writes a separate
config.local.yaml holding only what it changed, load_config merges it over
the base, and the file the operator maintains is never touched. Reverting
a console change is deleting a line from a short file, or the file itself.
"""
from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .client.llm_base import PROVIDERS

OVERLAY_NAME = "config.local.yaml"


class SettingsError(ValueError):
    """A value the console must refuse, with a reason for the reader."""


@dataclass(frozen=True)
class Setting:
    key: str                      # dotted path into Config
    label: str
    help: str
    kind: str                     # str | int | bool | choice
    choices: tuple = ()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    # True when the running process cannot honestly adopt the new value
    # without an external restart. See pstb/gui/console.py for why the
    # answer is almost always True here.
    restart: bool = False
    env_var: str = ""             # set in .env, and therefore shadowing


def _int(name, lo, hi):
    def check(raw):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise SettingsError(f"{name} must be a whole number")
        if lo is not None and value < lo:
            raise SettingsError(f"{name} must be at least {lo}")
        if hi is not None and value > hi:
            raise SettingsError(f"{name} must be at most {hi}")
        return value
    return check


# The editable surface. Deliberately small.
SETTINGS: tuple = (
    Setting("defaults.business_unit", "Default business unit",
            "Used when a question names none. The scope bar still wins.",
            "str"),
    Setting("defaults.ledger", "Default ledger",
            "Usually ACTUALS.", "str"),
    Setting("llm.provider", "Model provider",
            "ollama runs locally and needs no cloud credentials; gemini "
            "needs Application Default Credentials; claude needs an "
            "Anthropic API key, which you can set below.",
            "choice", choices=PROVIDERS,
            restart=True, env_var="PSTB_LLM_PROVIDER"),
    Setting("llm.ollama_model", "Ollama model", "e.g. llama3.1:8b",
            "str", restart=True),
    Setting("llm.ollama_num_ctx", "Ollama context window",
            "Tokens. The fixed prompt alone is ~19k — see "
            "docs/CONTEXT_BUDGET.md before lowering this.",
            "int", minimum=2048, maximum=131072, restart=True),
    Setting("llm.gemini_model", "Gemini model", "e.g. gemini-2.5-pro",
            "str", restart=True),
    Setting("llm.claude_model", "Claude model", "e.g. claude-opus-5",
            "str", restart=True),
    Setting("llm.claude_effort", "Claude effort",
            "How hard the model works before answering. Higher costs more "
            "and takes longer; measure with scripts/eval.py before moving "
            "it.",
            "choice", choices=("low", "medium", "high", "xhigh", "max"),
            restart=True),
    Setting("llm.temperature", "Temperature",
            "0 is greedy. Routing already forces 0 where it matters. "
            "Ignored by Claude, which does not accept it.",
            "int", minimum=0, maximum=2, restart=True),
    Setting("ps_api.enabled", "Run PSQuery through QAS",
            "Off means run_ps_query answers from bundled fixtures. Needs "
            "PSFT_QAS_USER and PSFT_QAS_PASSWORD below.",
            "bool", restart=True),
    Setting("ps_api.timeout_seconds", "QAS timeout (seconds)",
            "How long to wait for a query to come back.",
            "int", minimum=5, maximum=600, restart=True),
    Setting("ps_api.max_rows", "QAS row cap",
            "Upper bound on rows returned by one query.",
            "int", minimum=1, maximum=100000, restart=True),
    Setting("tools.max_rows", "Tool row cap",
            "Upper bound on rows any curated tool returns.",
            "int", minimum=1, maximum=100000, restart=True),
)

BY_KEY = {s.key: s for s in SETTINGS}

# NON-SECRET .env values the console may read AND write.
#
# These live in .env rather than config.yaml because they belong beside the
# credential they go with, and because config.py reads them from the
# environment last — writing them to the overlay would appear to work and
# change nothing. A username is not a secret: this one is deliberately
# disclosed everywhere it appears, because it explains whose permission
# lists shaped the rows a query returned.
ENV_SETTINGS: tuple = (
    ("PSFT_QAS_USER", "PeopleSoft QAS user",
     "A PeopleSoft user, not a database account. Queries run AS this user "
     "and return what their permission lists allow."),
    ("PSFT_QAS_NODE", "PeopleSoft IB node",
     "The node fronting the Query Access Service. A wrong node is the "
     "usual first 404; sites use PSFT_FS, PSFT_HR, or a custom one. "
     "Blank means PSFT_FS."),
)
ENV_KEYS = frozenset(k for k, _, _ in ENV_SETTINGS)

# Secrets the console may SET. Never read back, never returned, never
# logged. Anything not here is refused by the writer itself, so a bug in a
# handler cannot widen the set.
SECRET_KEYS = frozenset({"PSFT_QAS_PASSWORD", "ORACLE_PASSWORD",
                         "CONFLUENCE_API_TOKEN", "ANTHROPIC_API_KEY"})

SECRET_LABELS = {
    "PSFT_QAS_PASSWORD": "PeopleSoft QAS password",
    "ORACLE_PASSWORD": "Oracle database password",
    "CONFLUENCE_API_TOKEN": "Confluence API token",
    "ANTHROPIC_API_KEY": "Anthropic API key",
}


def _label(key: str) -> str:
    if key in SECRET_LABELS:
        return SECRET_LABELS[key]
    return next((lab for k, lab, _ in ENV_SETTINGS if k == key), key)


def read_env_values(env_path: Path) -> dict:
    """Current value of each non-secret .env setting, from the FILE.

    os.environ is a startup snapshot and would report a value that has
    since been changed or cleared on disk.
    """
    found = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in ENV_KEYS:
                found[name.strip()] = value.strip().strip("'\"")
    return {k: {"value": found.get(k, ""), "label": lab, "help": help_}
            for k, lab, help_ in ENV_SETTINGS}


def validate(key: str, raw: Any) -> Any:
    """Coerce and check one value, or raise with a reason a human can act on."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise SettingsError(f"{key} is not an editable setting")
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise SettingsError(f"{spec.label} must be true or false")
    if spec.kind == "int":
        return _int(spec.label, spec.minimum, spec.maximum)(raw)
    text = str(raw).strip()
    if spec.kind == "choice":
        if text not in spec.choices:
            raise SettingsError(
                f"{spec.label} must be one of {', '.join(spec.choices)}")
        return text
    if not text:
        raise SettingsError(f"{spec.label} cannot be blank")
    if len(text) > 200 or "\n" in text:
        raise SettingsError(f"{spec.label} is not a plausible value")
    return text


def current(cfg) -> dict:
    """What each editable setting reads as right now, with its source.

    A value forced by .env is reported as shadowed and rendered read-only:
    writing the overlay would appear to work and change nothing, because
    load_config applies the environment last.
    """
    out = {}
    for spec in SETTINGS:
        node: Any = cfg
        for part in spec.key.split("."):
            node = getattr(node, part, None)
            if node is None:
                break
        shadowed = bool(spec.env_var and os.environ.get(spec.env_var))
        out[spec.key] = {
            "value": node, "label": spec.label, "help": spec.help,
            "kind": spec.kind, "choices": list(spec.choices),
            "restart": spec.restart,
            "shadowed_by": spec.env_var if shadowed else "",
        }
    return out


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_overlay(values: dict) -> str:
    """The overlay file, nested to match config.yaml's shape."""
    tree: dict = {}
    for key, value in sorted(values.items()):
        section, _, leaf = key.rpartition(".")
        tree.setdefault(section, {})[leaf] = value
    lines = [
        "# Written by the configuration console. Values here OVERRIDE",
        "# config.yaml, which is left exactly as you maintain it — this file",
        "# exists so console edits never rewrite your comments.",
        "#",
        "# Safe to hand-edit, and safe to delete: removing a line falls back",
        "# to config.yaml, and removing the file reverts every console change.",
        "",
    ]
    for section in sorted(tree):
        lines.append(f"{section}:")
        for leaf in sorted(tree[section]):
            lines.append(f"  {leaf}: {_yaml_scalar(tree[section][leaf])}")
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Write via a temp file in the same directory, then replace.

    A partial write must never become the live file. When a mode is given
    it is a FLOOR, not an inheritance: applied at creation so the file
    never exists at the default umask while already holding a secret, and
    forced down on an existing file that is looser.
    """
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    if mode is not None:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
    else:
        handle = open(tmp, "w", encoding="utf-8")
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with_suppress = getattr(os, "unlink", None)
            if with_suppress:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def backup(path: Path) -> Optional[str]:
    """Timestamped copy beside the original, so any save can be undone by
    hand even if the console is unreachable."""
    if not path.exists():
        return None
    dest = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, dest)
    return dest.name


def write_env_keys(env_path: Path, updates: dict, deletes=()) -> None:
    """Set or clear secrets in .env, touching no other line.

    Enforces SECRET_KEYS here rather than trusting the caller: a handler
    bug must not be able to write ORACLE_DSN. Values containing ${ are
    refused because python-dotenv would expand them on the next read and
    the stored secret would not be the one that was typed.
    """
    allowed = SECRET_KEYS | ENV_KEYS
    for key in list(updates) + list(deletes):
        if key not in allowed:
            raise SettingsError(f"{key} is not a value this console sets")
    for key, value in updates.items():
        if "${" in str(value):
            raise SettingsError(
                f"{_label(key)} contains '${{', which .env "
                "expands on the next read — the stored value would not be "
                "the one you typed. Rotate to a value without it, or set "
                "this key by hand in .env.")
        if str(value) != str(value).strip():
            raise SettingsError(
                f"{_label(key)} has leading or trailing "
                "whitespace; that is almost never intended and is invisible "
                "in the file.")
    existing = env_path.read_text(encoding="utf-8").splitlines() \
        if env_path.exists() else []
    out, seen = [], set()
    for line in existing:
        name = line.split("=", 1)[0].strip() if "=" in line else ""
        if name in deletes:
            continue
        if name in updates:
            out.append(f"{name}='{updates[name]}'")
            seen.add(name)
            continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}='{value}'")
    atomic_write(env_path, "\n".join(out).rstrip("\n") + "\n", mode=0o600)


def which_secrets_are_set(env_path: Path) -> dict:
    """Whether each secret has a value — never what it is.

    Read from the FILE, not os.environ: the process's environment is a
    snapshot from startup and would report a secret as set after it had
    been cleared on disk.
    """
    present = set()
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() in SECRET_KEYS and value.strip().strip("'\""):
                present.add(name.strip())
    return {k: {"set": k in present, "label": SECRET_LABELS[k]}
            for k in sorted(SECRET_KEYS)}


def file_is_private(path: Path) -> bool:
    try:
        return not (stat.S_IMODE(os.stat(path).st_mode) & 0o077)
    except OSError:
        return True
