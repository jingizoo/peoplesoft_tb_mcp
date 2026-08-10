"""The configuration console: change settings without an admin.

Two properties matter more than the feature.

THE CODE IS NOT AUTHENTICATION, and the product must never imply it is.
Anyone can compute the hour in India. What protects this page is the
loopback guard that every request already passed. The code buys
deliberateness — a colleague on the tunnel does not casually rotate a
password — and the page says so in those words.

A BAD SAVE MUST NOT BRICK THE NEXT START. The console writes an overlay,
validates it by loading it in a subprocess BEFORE it becomes live, backs
up what it replaces, and leaves the running configuration alone when the
candidate does not load.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import settings as st  # noqa: E402
from pstb.gui import console  # noqa: E402

LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 50000)}
H = {"X-PSTB-Console": "1"}


def _client():
    from starlette.testclient import TestClient
    from pstb.gui import app as gapp
    return TestClient(gapp.app, **LOOP)


class CodeIsNotAuthTests(unittest.TestCase):
    def test_the_status_says_so_in_a_machine_readable_field(self) -> None:
        body = _client().get("/api/console/status", headers=H).json()
        self.assertFalse(body["auth"]["is_authentication"])
        self.assertIn("not a password", body["auth"]["note"])

    def test_the_page_says_so_in_words(self) -> None:
        text = (ROOT / "pstb/gui/static/console.html").read_text()
        self.assertIn("confirmation step, not a password", text)

    def test_the_previous_hour_is_accepted(self) -> None:
        # Typed at 10:59:58, validated at 11:00:01. Refusing that is cruel
        # and protects nothing that was ever secret.
        self.assertEqual(len(console.expected_codes()), 2)

    def test_a_wrong_code_is_refused_with_the_format(self) -> None:
        r = _client().post("/api/console/unlock", json={"code": "nope"},
                           headers=H)
        self.assertEqual(r.status_code, 401)
        self.assertIn("YYYYMMDD-HH", r.json()["remedy"])

    def test_a_missing_timezone_database_does_not_lock_the_owner_out(self):
        # Asia/Kolkata is a fixed +5:30 with no DST, so the offset is a
        # constant. A host without tzdata must still be configurable.
        from unittest.mock import patch
        with patch("zoneinfo.ZoneInfo", side_effect=Exception("no tzdata")):
            self.assertTrue(console.expected_codes())


class WriteGateTests(unittest.TestCase):
    def test_a_write_needs_the_custom_header(self) -> None:
        # A cross-origin page can POST a form without reading the reply.
        # Requiring a header forces a preflight this server never answers.
        r = _client().post("/api/console/settings",
                           json={"changes": {"tools.max_rows": 5}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("x-pstb-console", r.json()["error"].lower())

    def test_a_write_needs_the_confirmation_first(self) -> None:
        r = _client().post("/api/console/settings",
                           json={"changes": {"tools.max_rows": 5}}, headers=H)
        self.assertEqual(r.status_code, 401)

    def test_reads_are_loopback_only_like_everything_else(self) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        bad = TestClient(gapp.app, base_url="http://evil.example.com",
                         client=("127.0.0.1", 50000))
        self.assertEqual(bad.get("/api/console/status").status_code, 400)


class AllowlistTests(unittest.TestCase):
    def test_nothing_that_changes_where_data_goes_is_editable(self) -> None:
        forbidden = ("db.dsn", "db.driver", "db.oracle_dsn", "db.backend",
                     "db.sqlite_path", "wiki.confluence_base_url",
                     "tools.raw_sql", "sources")
        for key in forbidden:
            self.assertNotIn(key, st.BY_KEY, f"{key} must not be web-editable")

    def test_an_unknown_key_is_refused_by_the_validator(self) -> None:
        with self.assertRaises(st.SettingsError):
            st.validate("db.oracle_dsn", "//evil/db")

    def test_out_of_range_values_are_refused_with_the_bound(self) -> None:
        with self.assertRaises(st.SettingsError) as ctx:
            st.validate("tools.max_rows", 10 ** 9)
        self.assertIn("100000", str(ctx.exception))

    def test_the_writer_enforces_the_secret_allowlist_itself(self) -> None:
        # Not only the handler: a handler bug must not be able to write
        # ORACLE_DSN into .env.
        import tempfile
        env = Path(tempfile.mkdtemp()) / ".env"
        with self.assertRaises(st.SettingsError):
            st.write_env_keys(env, {"ORACLE_DSN": "//evil/db"})


class SecretTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.env = Path(tempfile.mkdtemp()) / ".env"

    def test_a_value_is_never_read_back(self) -> None:
        st.write_env_keys(self.env, {"PSFT_QAS_PASSWORD": "hunter2"})
        state = st.which_secrets_are_set(self.env)
        self.assertTrue(state["PSFT_QAS_PASSWORD"]["set"])
        self.assertNotIn("hunter2", repr(state))

    def test_it_is_written_private_at_creation(self) -> None:
        import os
        import stat
        st.write_env_keys(self.env, {"PSFT_QAS_PASSWORD": "x"})
        self.assertFalse(stat.S_IMODE(os.stat(self.env).st_mode) & 0o077)

    def test_a_dollar_brace_value_is_refused_not_mangled(self) -> None:
        # dotenv expands ${...} on the next read, so the stored secret
        # would not be the one typed — and it would fail at login with a
        # correct-looking file.
        with self.assertRaises(st.SettingsError) as ctx:
            st.write_env_keys(self.env, {"PSFT_QAS_PASSWORD": "a${b}c"})
        self.assertIn("expands", str(ctx.exception))

    def test_other_lines_survive_a_write(self) -> None:
        self.env.write_text("ORACLE_USER='rpt'\nPSFT_QAS_USER='QASUSER'\n")
        st.write_env_keys(self.env, {"PSFT_QAS_PASSWORD": "x"})
        text = self.env.read_text()
        self.assertIn("ORACLE_USER='rpt'", text)
        self.assertIn("PSFT_QAS_USER='QASUSER'", text)

    def test_state_is_read_from_the_file_not_the_environment(self) -> None:
        # os.environ is a startup snapshot and would report a cleared
        # secret as still set.
        import os
        os.environ["PSFT_QAS_PASSWORD"] = "stale"
        try:
            self.assertFalse(
                st.which_secrets_are_set(self.env)["PSFT_QAS_PASSWORD"]["set"])
        finally:
            os.environ.pop("PSFT_QAS_PASSWORD", None)


class OverlayTests(unittest.TestCase):
    def test_it_names_single_keys_never_whole_sections(self) -> None:
        text = st.render_overlay({"llm.ollama_num_ctx": 32768})
        self.assertIn("ollama_num_ctx: 32768", text)
        self.assertNotIn("ollama_model", text,
                         "an overlay that restated a whole section would "
                         "freeze every sibling at today's value")

    def test_it_says_how_to_revert(self) -> None:
        self.assertIn("safe to delete", st.render_overlay({}))

    def test_config_yaml_is_never_the_thing_that_gets_written(self) -> None:
        source = (ROOT / "pstb/gui/console.py").read_text()
        self.assertNotIn('"config.yaml"', source)
        self.assertIn("OVERLAY_NAME", source)

    def test_a_broken_overlay_is_reported_not_ignored(self) -> None:
        import tempfile
        from pstb.config import load_config
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text("defaults:\n  business_unit: US001\n")
        (d / "config.local.yaml").write_text("llm:\n  : : :\n")
        with self.assertRaises(RuntimeError) as ctx:
            load_config(str(d / "config.yaml"))
        self.assertIn("safe to delete", str(ctx.exception))


class QasSetupIsCompleteTests(unittest.TestCase):
    """Every value QAS needs must be settable here.

    The console shipped with the PASSWORD only, which left the one job it
    was asked for — configure PSQuery without an admin — impossible: the
    user and node still needed an SSH session, so the console saved
    nothing and the operator did the whole thing by hand anyway.
    """

    def test_every_qas_value_is_reachable_from_the_console(self) -> None:
        reachable = st.ENV_KEYS | st.SECRET_KEYS
        for key in ("PSFT_QAS_USER", "PSFT_QAS_PASSWORD", "PSFT_QAS_NODE"):
            self.assertIn(key, reachable,
                          f"{key} is read at runtime but cannot be set here")
        self.assertIn("ps_api.enabled", st.BY_KEY)

    def test_the_user_is_readable_and_the_password_is_not(self) -> None:
        # Not an oversight in either direction: the user is disclosed
        # everywhere because it explains whose permission lists shaped the
        # rows; the password is never returned at all.
        self.assertIn("PSFT_QAS_USER", st.ENV_KEYS)
        self.assertNotIn("PSFT_QAS_USER", st.SECRET_KEYS)
        self.assertIn("PSFT_QAS_PASSWORD", st.SECRET_KEYS)
        self.assertNotIn("PSFT_QAS_PASSWORD", st.ENV_KEYS)

    def test_values_are_read_from_the_file_not_the_environment(self) -> None:
        import os
        import tempfile
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text("PSFT_QAS_USER='FROMFILE'\n")
        os.environ["PSFT_QAS_USER"] = "STALE"
        try:
            got = st.read_env_values(env)["PSFT_QAS_USER"]["value"]
            self.assertEqual(got, "FROMFILE")
        finally:
            os.environ.pop("PSFT_QAS_USER", None)

    def test_setting_them_leaves_unrelated_lines_alone(self) -> None:
        import tempfile
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text("ORACLE_USER='rpt'\nCONFLUENCE_EMAIL='a@b.c'\n")
        st.write_env_keys(env, {"PSFT_QAS_USER": "QASRPT",
                                "PSFT_QAS_NODE": "PSFT_HR"})
        text = env.read_text()
        self.assertIn("ORACLE_USER='rpt'", text)
        self.assertIn("CONFLUENCE_EMAIL='a@b.c'", text)
        self.assertIn("PSFT_QAS_USER='QASRPT'", text)

    def test_a_blank_value_clears_rather_than_storing_emptiness(self) -> None:
        import tempfile
        env = Path(tempfile.mkdtemp()) / ".env"
        st.write_env_keys(env, {"PSFT_QAS_NODE": "PSFT_HR"})
        st.write_env_keys(env, {}, ["PSFT_QAS_NODE"])
        self.assertEqual(
            st.read_env_values(env)["PSFT_QAS_NODE"]["value"], "",
            "clearing the node must fall back to the PSFT_FS default, not "
            "pin it to an empty string")

    def test_the_writer_still_refuses_anything_outside_both_lists(self):
        import tempfile
        env = Path(tempfile.mkdtemp()) / ".env"
        for key in ("ORACLE_DSN", "PSTB_CONFIG", "PATH"):
            with self.assertRaises(st.SettingsError, msg=key):
                st.write_env_keys(env, {key: "x"})


class RestartHonestyTests(unittest.TestCase):
    def test_the_console_does_not_offer_a_restart_it_cannot_do(self) -> None:
        body = _client().get("/api/console/status", headers=H).json()
        self.assertFalse(body["restart"]["can_restart_here"])
        self.assertIn("nothing to bring it back", body["restart"]["why"])
        self.assertIn("pstb.gui", body["restart"]["how"])

    def test_no_restart_endpoint_exists(self) -> None:
        from pstb.gui import app as gapp
        paths = {r.path for r in gapp.app.routes}
        self.assertNotIn("/api/console/restart", paths)

    def test_settings_declare_whether_they_need_one(self) -> None:
        self.assertTrue(st.BY_KEY["llm.provider"].restart)
        self.assertFalse(st.BY_KEY["defaults.business_unit"].restart)


if __name__ == "__main__":
    unittest.main()


class AuthTokenIsConsoleManagedTests(unittest.TestCase):
    """Rotating the shared access token must not need a shell.

    The console exists to change settings without an admin or an SSH key.
    PSTB_AUTH_TOKEN was absent from SECRET_KEYS, so write_env_keys refused
    it by design and the one secret that decides who can reach the app at
    all was the one secret only a shell could set.
    """

    def setUp(self) -> None:
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"

    def test_the_writer_accepts_it(self) -> None:
        st.write_env_keys(self.env, {"PSTB_AUTH_TOKEN": "team-token-1"})
        self.assertIn("PSTB_AUTH_TOKEN='team-token-1'",
                      self.env.read_text())

    def test_it_reports_as_set_without_revealing_itself(self) -> None:
        st.write_env_keys(self.env, {"PSTB_AUTH_TOKEN": "team-token-1"})
        status = st.which_secrets_are_set(self.env)
        self.assertTrue(status["PSTB_AUTH_TOKEN"]["set"])
        self.assertNotIn("team-token-1", json.dumps(status))

    def test_it_can_be_cleared_again(self) -> None:
        st.write_env_keys(self.env, {"PSTB_AUTH_TOKEN": "team-token-1"})
        st.write_env_keys(self.env, {}, deletes=["PSTB_AUTH_TOKEN"])
        self.assertFalse(
            st.which_secrets_are_set(self.env)["PSTB_AUTH_TOKEN"]["set"])

    def test_the_env_file_stays_private(self) -> None:
        st.write_env_keys(self.env, {"PSTB_AUTH_TOKEN": "team-token-1"})
        self.assertTrue(st.file_is_private(self.env))

    def test_only_a_secret_this_app_defines_offers_generation(self) -> None:
        # A generated Oracle password is simply wrong, and a Generate button
        # beside one would be a trap.
        status = st.which_secrets_are_set(self.env)
        self.assertTrue(status["PSTB_AUTH_TOKEN"]["generate"])
        for key in ("ORACLE_PASSWORD", "PSFT_QAS_PASSWORD",
                    "CONFLUENCE_API_TOKEN", "ANTHROPIC_API_KEY"):
            self.assertFalse(status[key]["generate"], key)

    def test_the_field_says_what_holding_the_token_means(self) -> None:
        help_text = st.which_secrets_are_set(self.env)["PSTB_AUTH_TOKEN"]["help"]
        self.assertIn("shared password", help_text)
        self.assertIn("--share", help_text)
        self.assertIn("invalidates every link", help_text)

    def test_a_token_containing_a_dollar_brace_is_refused_not_mangled(self) -> None:
        # .env expands ${...} on the next read, so a stored token would not
        # be the one that was typed — and a mangled token is indistinguishable
        # from a wrong one at the door.
        with self.assertRaises(st.SettingsError):
            st.write_env_keys(self.env, {"PSTB_AUTH_TOKEN": "ab${x}cd"})
