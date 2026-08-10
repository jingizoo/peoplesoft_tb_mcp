"""The follow-up review of PRs #106-#108: fixes that make those fixes hold.

Each test here pins a defect a structured review confirmed in the merged
code — mostly places where the PR's own description promised a behavior
the code did not deliver on the path a real deployment takes.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.guards import (payload_numbers,  # noqa: E402
                         tagged_payload_numbers, ungrounded_figures)
from pstb.gui import localguard, progress  # noqa: E402


class ComputedTotalGroundingTests(unittest.TestCase):
    """"Revenue of this parent across all the children" is a SUM of rows the
    tool really returned. The guard grounded only literal payload numbers,
    so a correct total was withheld with "these numbers are not from a
    tool" — a caveat firing on a correct answer, which teaches the user
    that nothing works."""

    PAYLOAD = ("run_sql", """
        {"rows": [
            {"customer": "ACME EAST", "revenue": 1200.50},
            {"customer": "ACME WEST", "revenue": 800.25},
            {"customer": "ACME SOUTH", "revenue": 500.00}
        ], "row_count": 3}
    """)

    def test_a_column_total_is_grounded(self) -> None:
        answer = "Total revenue across the three children is 2,500.75."
        self.assertEqual(ungrounded_figures(answer, [self.PAYLOAD]), [])

    def test_the_totals_rounded_restatement_is_grounded_too(self) -> None:
        self.assertEqual(
            ungrounded_figures("about 2,501 across the group",
                               [self.PAYLOAD]), [])

    def test_string_typed_numeric_columns_sum_too(self) -> None:
        # Oracle NUMBER columns can arrive as strings through json paths.
        payload = ("run_sql",
                   '{"rows": [{"amt": "100.10"}, {"amt": "200.20"}]}')
        self.assertEqual(ungrounded_figures("that is 300.30 in total",
                                            [payload]), [])

    def test_an_invented_figure_is_still_caught(self) -> None:
        answer = "Total revenue is 9,999,999.99."
        self.assertEqual(ungrounded_figures(answer, [self.PAYLOAD]),
                         ["9,999,999.99"])

    def test_a_single_row_is_not_a_sum(self) -> None:
        # One value is already grounded as itself; no phantom "totals" from
        # single-row payloads.
        grounded = payload_numbers(
            [("run_sql", '{"rows": [{"amt": 41.00}]}')])
        self.assertIn("41", grounded)
        self.assertNotIn("82", grounded)


class SummedTotalProvenanceTests(unittest.TestCase):
    """Grounding a computed total must not lend it a SOURCE.

    A sum is arithmetic this code did, not a value a system reported. The
    first cut let one widen the source set of a figure another system
    really produced — swept against the real sample ledger, that made nine
    figures vouch for systems they never came from, and the attribution
    guard would then accept "per the general ledger" for an AP-only
    number. Sums may create a key; only real figures may own one.
    """

    AP = ("get_vendor_intelligence",
          '{"vendors": [{"paid": 600.0}, {"paid": 400.0}]}')     # sums 1000
    GL = ("get_trial_balance",
          '{"rows": [{"ending": 1000.0}, {"ending": 25.0}]}')    # states 1000

    def test_a_sum_does_not_widen_a_real_figures_sources(self) -> None:
        tagged = tagged_payload_numbers([self.GL, self.AP])
        self.assertEqual(tagged["1000"], {"peoplesoft_gl"},
                         "a computed AP total must not make 1000 look like "
                         "it came from the general ledger too")

    def test_a_real_figure_takes_over_a_key_a_sum_created(self) -> None:
        # Same two payloads, other order: the sum lands first. The real
        # figure must still own the attribution when it arrives.
        tagged = tagged_payload_numbers([self.AP, self.GL])
        self.assertEqual(tagged["1000"], {"peoplesoft_gl"})

    def test_the_total_is_still_grounded_either_way(self) -> None:
        for order in ([self.AP, self.GL], [self.GL, self.AP], [self.AP]):
            self.assertEqual(ungrounded_figures("that is 1,000.00", order),
                             [], f"order {[t for t, _ in order]}")

    def test_non_additive_columns_are_not_summed(self) -> None:
        # Nobody reports "total days late". Summing them grounded nothing a
        # real answer says and only widened the collision surface.
        grounded = payload_numbers(
            [("get_ar_aging",
              '{"rows": [{"days_late": 30, "pct_of_total": 25},'
              ' {"days_late": 61, "pct_of_total": 75}]}')])
        self.assertNotIn("91", grounded)     # 30 + 61
        self.assertNotIn("100", grounded)    # 25 + 75
        self.assertIn("30", grounded)        # the real values still are


class StaleCookieTests(unittest.TestCase):
    """After a restart mints a new token, the browser still holds the old
    one as a cookie. The pasted fresh URL must WIN, not lose to the stale
    cookie — the first cut checked the cookie before the query string and
    locked out everyone with yesterday's cookie until they found the
    cookie jar."""

    def setUp(self) -> None:
        self.saved = localguard.POLICY
        self.addCleanup(setattr, localguard, "POLICY", self.saved)
        localguard.configure("0.0.0.0", "fresh-token-123456")

    @staticmethod
    def _scope(cookie: str = "", query: str = ""):
        headers = [(b"host", b"finhost:8016")]
        if cookie:
            headers.append((b"cookie",
                            f"{localguard.TOKEN_COOKIE}={cookie}".encode()))
        return {"type": "http", "headers": headers,
                "client": ("10.0.0.5", 40000),
                "query_string": query.encode(), "path": "/"}

    def test_a_fresh_query_token_beats_a_stale_cookie(self) -> None:
        status, _ = localguard.rejection(self._scope(
            cookie="yesterdays-token-9999",
            query="token=fresh-token-123456"))
        self.assertEqual(status, 0)

    def test_a_wrong_token_everywhere_is_still_refused(self) -> None:
        status, _ = localguard.rejection(self._scope(
            cookie="wrong", query="token=also-wrong"))
        self.assertEqual(status, 401)

    def test_the_cookie_alone_still_works(self) -> None:
        status, _ = localguard.rejection(
            self._scope(cookie="fresh-token-123456"))
        self.assertEqual(status, 0)


class SharedModeConsoleTests(unittest.TestCase):
    """The token grants colleagues READ access to the dashboards. The
    console writes credentials behind a confirmation code that is
    computable by anyone — its own docstring assumes only the loopback
    guard admits callers. In shared mode that assumption broke: every
    token holder could rotate the Oracle password. The console stays
    machine-local, token or not."""

    def setUp(self) -> None:
        self.saved = localguard.POLICY
        self.addCleanup(setattr, localguard, "POLICY", self.saved)
        localguard.configure("0.0.0.0", "team-token-123456")

    @staticmethod
    def _scope(path: str, client=("10.0.0.5", 40000)):
        return {"type": "http",
                "headers": [(b"host", b"finhost:8016"),
                            (b"x-pstb-token", b"team-token-123456")],
                "client": client, "path": path, "query_string": b""}

    def test_a_remote_token_holder_reads_the_ledger(self) -> None:
        self.assertEqual(
            localguard.rejection(self._scope("/api/trial-balance"))[0], 0)

    def test_a_remote_token_holder_cannot_reach_the_console(self) -> None:
        for path in ("/console", "/api/console/status",
                     "/api/console/unlock", "/api/console/secrets",
                     "/api/console/settings"):
            status, why = localguard.rejection(self._scope(path))
            self.assertEqual(status, 403, path)
            self.assertIn("ssh", why.lower())

    def test_the_machine_itself_still_reaches_the_console(self) -> None:
        # The operator on the box (or through their tunnel) is exactly who
        # the console is for.
        self.assertEqual(
            localguard.rejection(
                self._scope("/api/console/status",
                            client=("127.0.0.1", 40000)))[0], 0)


class PolicySpellingTests(unittest.TestCase):
    def test_hosts_none_outside_shared_mode_is_refused(self) -> None:
        # hosts=None means "any Host header", which switches the DNS-
        # rebinding control off. Only safe when a token replaces it.
        with self.assertRaises(ValueError):
            localguard.Policy(hosts=None, shared=False)

    def test_hosts_none_with_a_token_in_shared_mode_still_builds(self) -> None:
        policy = localguard.Policy(hosts=None, token="team-token-123456",
                                   shared=True)
        self.assertTrue(localguard.host_matches("anything", policy))


class ProgressNoteTests(unittest.TestCase):
    """The diagnosed engine-failure reason must reach the boot bar.

    step() records the raw exception at raise time — "ExceptionGroup:
    unhandled errors in a TaskGroup" — and the diagnosis (ORA-01017 out of
    the subprocess's stderr) lands seconds later via a second end(). That
    second call was a guaranteed no-op, so the bar showed the shrug
    forever and app.py's enrichment line was dead code."""

    def setUp(self) -> None:
        progress.reset()

    def tearDown(self) -> None:
        progress.reset()

    def _engine_step(self):
        return [s for s in progress.snapshot()["steps"]
                if s["key"] == "engine"][0]

    def test_a_failed_step_can_learn_the_real_reason(self) -> None:
        with self.assertRaises(RuntimeError):
            with progress.step("engine"):
                raise RuntimeError("unhandled errors in a TaskGroup")
        progress.end("engine", ok=False,
                     note="ORA-01017: invalid username/password")
        self.assertIn("ORA-01017", self._engine_step()["note"])

    def test_a_failed_step_can_recover(self) -> None:
        # The worker retries; a reconnect flips the step so a reloaded
        # page stops reporting a failure that has been fixed.
        with self.assertRaises(RuntimeError):
            with progress.step("engine"):
                raise RuntimeError("boom")
        progress.end("engine", ok=True, note="recovered on retry 1")
        step = self._engine_step()
        self.assertEqual(step["status"], "done")
        self.assertIn("recovered", step["note"])

    def test_a_done_step_is_never_restated(self) -> None:
        with progress.step("engine"):
            pass
        progress.end("engine", ok=False, note="late failure elsewhere")
        self.assertEqual(self._engine_step()["status"], "done")

    def test_a_never_begun_step_reports_zero_not_process_uptime(self) -> None:
        progress.end("defaults", note="served from cache")
        step = [s for s in progress.snapshot()["steps"]
                if s["key"] == "defaults"][0]
        self.assertEqual(step["ms"], 0)


class TurnMeasureTests(unittest.TestCase):
    """The abandoned-turn refusal measures the TURN, not the last tool:
    busy_since resets per tool start, so a turn chaining short queries
    never looked abandoned however long it had held the conversation."""

    def test_turn_for_survives_tool_resets(self) -> None:
        import time as _t

        from pstb.gui.app import _ProviderSession
        entry = _ProviderSession(provider=object(), touched=0.0)
        entry.turn_since = _t.monotonic() - 300     # 5 minutes into the turn
        entry.busy_since = _t.monotonic() - 2       # newest tool: 2s ago
        self.assertGreater(entry.turn_for(), 250)
        self.assertLess(entry.busy_for(), 10)
        self.assertIn("into the question", entry.describe_busy())


class ActivityLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        from pstb.gui import app as gapp
        self.gapp = gapp
        self.saved = dict(gapp._activity)
        self.addCleanup(lambda: (gapp._activity.clear(),
                                 gapp._activity.update(self.saved)))
        gapp._activity.clear()

    def test_a_refused_claim_gives_the_display_back(self) -> None:
        g = self.gapp
        displaced = g._activity_begin("tab1", "turn-A", "working")
        self.assertIsNone(displaced)
        displaced = g._activity_begin("tab1", "turn-B", "checking")
        self.assertEqual(displaced["turn"], "turn-A")
        # Turn B is refused (busy 409) and restores A's live display.
        g._activity_restore("tab1", "turn-B", displaced)
        self.assertEqual(g._activity["tab1"]["turn"], "turn-A")
        self.assertTrue(g._activity["tab1"]["active"])

    def test_restore_never_clobbers_a_newer_claim(self) -> None:
        g = self.gapp
        g._activity_begin("tab1", "turn-A")
        displaced_by_b = g._activity_begin("tab1", "turn-B")
        g._activity_begin("tab1", "turn-C")     # C claimed after B
        g._activity_restore("tab1", "turn-B", displaced_by_b)
        self.assertEqual(g._activity["tab1"]["turn"], "turn-C")

    def test_eviction_prefers_finished_slots(self) -> None:
        g = self.gapp
        # Oldest slot is still ACTIVE; a finished one is newer. Pressure
        # must evict the finished one first.
        g._activity_begin("live-tab", "turn-live")
        for i in range(205):
            g._activity_begin(f"tab{i}", f"t{i}")
            g._activity_done(f"tab{i}", f"t{i}")
        self.assertIn("live-tab", g._activity,
                      "the longest-running LIVE turn was evicted while "
                      "finished slots remained")


if __name__ == "__main__":
    unittest.main()
