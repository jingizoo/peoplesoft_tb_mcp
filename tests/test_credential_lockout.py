"""A rejected password must be offered to the database exactly once.

The question that produced this file: "can it lock the DB account if we set
[the password] outside?" It could. Connecting is lazy and per-query, so a
wrong ORACLE_PASSWORD was re-offered on every query — and Oracle counts each
rejected logon against FAILED_LOGIN_ATTEMPTS in the account's profile, 10 in
the shipped DEFAULT profile. A few minutes of ordinary clicking would lock
the service account for every other consumer of it, not just this app.

The failure mode does not need a wrong password to reach it: a correct one
containing $ or {}, exported from a shell that expands it, arrives here
already mangled and is indistinguishable from a wrong one.

So: refuse once, remember the refusal, and never hand those credentials to
the server again in this process.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError, _is_credential_failure  # noqa: E402


class MarkerTests(unittest.TestCase):
    def test_the_errors_that_burn_a_login_attempt(self) -> None:
        for msg in ("ORA-01017: invalid username/password; logon denied",
                    "ORA-28000: the account is locked",
                    "ORA-28001: the password has expired",
                    "ORA-01005: null password given; logon denied",
                    "ORA-01045: user LACKS CREATE SESSION privilege",
                    "[28000] Login failed for user 'psreport'."):
            self.assertTrue(_is_credential_failure(msg), msg)

    def test_a_password_about_to_expire_is_not_a_refusal(self) -> None:
        # ORA-28002 rides along with a SUCCESSFUL logon. Blocking on it would
        # take the app down a week before anything was actually wrong.
        self.assertFalse(_is_credential_failure(
            "ORA-28002: the password will expire within 7 days"))

    def test_an_unreachable_listener_is_not_a_refusal(self) -> None:
        # These must keep retrying: they cost the account nothing and they
        # recover on their own when the VPN or the listener comes back.
        for msg in ("ORA-12541: TNS:no listener",
                    "ORA-12170: TNS:Connect timeout occurred",
                    "DPY-6005: cannot connect to database",
                    "ORA-03113: end-of-file on communication channel"):
            self.assertFalse(_is_credential_failure(msg), msg)


class _Rejects:
    """An oracledb stand-in that counts how often it is asked to log on."""

    POOL_GETMODE_WAIT = 1

    def __init__(self, error: str):
        self.error = error
        self.attempts = 0

    def create_pool(self, **_kw):
        self.attempts += 1
        raise Exception(self.error)


class RetryTests(unittest.TestCase):
    def _oracle_db(self) -> Database:
        cfg = load_config(None)
        cfg.db.backend = "oracle"
        cfg.db.oracle_dsn = "dc15-db:1521/FSDEV"
        cfg.db.oracle_user = "psreport"
        cfg.db.oracle_password = "Pa$sw0rd-SENTINEL-9x"
        db = Database(cfg)
        db.dialect = "oracle"
        return db

    def test_a_rejected_password_is_offered_once_and_never_again(self) -> None:
        db = self._oracle_db()
        fake = _Rejects("ORA-01017: invalid username/password; logon denied")
        with patch.dict(sys.modules, {"oracledb": fake}):
            for _ in range(25):        # 25 queries: ~2 dashboards' worth
                with self.assertRaises(DbError):
                    db._oracle_pool()
        self.assertEqual(fake.attempts, 1,
                         "every extra attempt is one closer to a locked "
                         "account, and the profile limit is often 10")

    def test_the_refusal_says_where_the_password_comes_from(self) -> None:
        db = self._oracle_db()
        fake = _Rejects("ORA-01017: invalid username/password; logon denied")
        with patch.dict(sys.modules, {"oracledb": fake}):
            with self.assertRaises(DbError) as ctx:
                db._oracle_pool()
        message = str(ctx.exception)
        self.assertIn("ORACLE_PASSWORD", message)
        self.assertIn(".env", message)
        self.assertIn("FAILED_LOGIN_ATTEMPTS", message)
        self.assertNotIn("SENTINEL", message,
                         "never echo the password itself — this text lands in\n                         a browser and in the question log")

    def test_a_transient_failure_keeps_trying(self) -> None:
        # The listener comes back; the account was never at risk. Latching
        # on these would need a restart to recover from a blip.
        db = self._oracle_db()
        fake = _Rejects("ORA-12541: TNS:no listener")
        with patch.dict(sys.modules, {"oracledb": fake}):
            for _ in range(4):
                with self.assertRaises(DbError):
                    db._oracle_pool()
        self.assertEqual(fake.attempts, 4)

    def test_a_fresh_database_object_may_try_again(self) -> None:
        # Restart and the console's reload both build a new Database, which
        # is how a corrected password takes effect without editing code.
        first = self._oracle_db()
        fake = _Rejects("ORA-01017: invalid username/password; logon denied")
        with patch.dict(sys.modules, {"oracledb": fake}):
            with self.assertRaises(DbError):
                first._oracle_pool()
            with self.assertRaises(DbError):
                self._oracle_db()._oracle_pool()
        self.assertEqual(fake.attempts, 2)


if __name__ == "__main__":
    unittest.main()
