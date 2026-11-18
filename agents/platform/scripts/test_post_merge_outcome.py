"""Tests for post_merge_outcome: one outcome per merged record, and the cron run that drives every step."""

import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kubeagents_switches
import post_merge_check
import post_merge_outcome
import post_merge_scope
from test_post_merge_check import MERGE, REPO, FakeCluster, FakeForge, argo_app, schema

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
URL = f"{REPO}/pull/1"


def record(**columns):
    base = {"url": URL, "repo": REPO, "number": 1, "kind": "no-pdb", "project": "p", "location": "l",
            "cluster": "prod", "namespace": "shop", "object": "Deployment/checkout",
            "merged_at": NOW.isoformat(), "merge_checked_at": NOW.isoformat(), "merge_commit": MERGE,
            "applied_at": None, "sync_state": None, "health_result": None, "health_detail": None, "health_at": None}
    return {**base, **columns}


def clean(_record):
    return {"outcome": "clean"}


class DecideTest(unittest.TestCase):
    def decide(self, minutes, rec, recheck=clean, refusal=None):
        return post_merge_outcome.decide(rec, NOW + timedelta(minutes=minutes), recheck, refusal)

    def test_in_flight_records_have_no_outcome(self):
        self.assertIsNone(self.decide(30, record()))
        self.assertIsNone(self.decide(10, record(applied_at=NOW.isoformat())))

    def test_not_live_by_the_apply_deadline_is_not_applied(self):
        outcome, detail = self.decide(61, record(sync_state="pending: OutOfSync"))
        self.assertEqual(outcome, post_merge_outcome.NOT_APPLIED)
        self.assertTrue(detail.startswith("apply: not live 60 minutes"), detail)
        outcome, detail = self.decide(61, record(sync_state="unreadable: connection refused"))
        self.assertEqual((outcome, detail.split(":")[0]), (post_merge_outcome.NOT_APPLIED, "unreadable"))

    def test_a_merge_noticed_late_still_gets_its_whole_deadline(self):
        late = record(merge_checked_at=(NOW + timedelta(minutes=50)).isoformat())
        self.assertIsNone(self.decide(61, late))

    def test_a_failed_window_is_still_broken_and_a_cannot_judge_is_not_rechecked(self):
        applied = NOW.isoformat()
        outcome, _ = self.decide(10, record(applied_at=applied, health_result="failed", health_detail="restarts rose"))
        self.assertEqual(outcome, post_merge_outcome.STILL_BROKEN)
        outcome, detail = self.decide(10, record(applied_at=applied, health_result="failed",
                                                 health_detail="cannot judge: deployments.apps not found"))
        self.assertEqual(outcome, post_merge_outcome.APPLIED_NOT_RECHECKED)
        self.assertTrue(detail.startswith("unreadable: cannot judge"), detail)

    def test_a_passed_window_is_settled_by_the_recheck(self):
        passed = record(applied_at=NOW.isoformat(), health_result="passed", health_detail="healthy")
        for answer, outcome in (({"outcome": "clean"}, post_merge_outcome.VERIFIED_FIXED),
                                ({"outcome": "reproduces"}, post_merge_outcome.STILL_BROKEN),
                                ({"outcome": "unchecked", "reason": "no on-demand check"},
                                 post_merge_outcome.APPLIED_NOT_RECHECKED)):
            with self.subTest(answer=answer):
                self.assertEqual(self.decide(10, passed, lambda r, a=answer: a)[0], outcome)

    def test_an_unread_or_crashed_recheck_retries_until_the_deadline(self):
        passed = record(applied_at=NOW.isoformat(), health_result="passed", health_detail="healthy")
        for error in (post_merge_scope.RecheckUnread("the read failed its gate"), RuntimeError("hop dropped")):
            def failing(_r, error=error):
                raise error

            with self.subTest(error=error):
                with self.assertLogs("post_merge_check", level="WARNING"):
                    self.assertIsNone(self.decide(10, passed, failing))
                outcome, _ = self.decide(66, passed, failing)
                self.assertEqual(outcome, post_merge_outcome.APPLIED_NOT_RECHECKED)

    def test_a_deferred_recheck_waits(self):
        def deferred(_r):
            raise post_merge_scope.RecheckDeferred

        passed = record(applied_at=NOW.isoformat(), health_result="passed")
        self.assertIsNone(self.decide(10, passed, deferred))

    def test_the_declared_scope_ends_or_holds_a_record(self):
        outcome, detail = self.decide(1, record(), refusal=lambda r: "p/l/prod is excluded by spec.scope.exclude.clusters")
        self.assertEqual((outcome, detail.split(":")[0]), (post_merge_outcome.NOT_APPLIED, "out of scope"))
        outcome, _ = self.decide(1, record(applied_at=NOW.isoformat()), refusal=lambda r: "outside")
        self.assertEqual(outcome, post_merge_outcome.APPLIED_NOT_RECHECKED)
        unknown = lambda r: "unknown: p could not be read"
        self.assertIsNone(self.decide(100, record(), refusal=unknown))
        outcome, detail = self.decide(121, record(), refusal=unknown)
        self.assertEqual((outcome, detail), (post_merge_outcome.NOT_APPLIED, "scope unknown: p could not be read"))

    def test_the_categories_are_what_watch_health_writes(self):
        self.assertEqual(post_merge_outcome.UNREADABLE, "unreadable")
        self.assertEqual(post_merge_scope.UNKNOWN_PREFIX, post_merge_check.SCOPE_UNKNOWN_PREFIX)


class DecideOutcomesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())

    def add(self, number, **columns):
        row = {"url": f"{REPO}/pull/{number}", "repo": REPO, "number": number, "kind": "no-pdb", "project": "p",
               "location": "l", "cluster": "prod", "namespace": "shop", "object": "Deployment/checkout",
               "opened_at": NOW.isoformat(), **columns}
        self.conn.execute(f"INSERT INTO post_merge_records ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))

    def outcome(self, number):
        return self.conn.execute("SELECT outcome, outcome_detail FROM post_merge_records WHERE number = ?",
                                 (number,)).fetchone()

    def test_an_outcome_is_written_once_and_never_changed(self):
        self.add(1, merged_at=NOW.isoformat(), merge_commit=MERGE, applied_at=NOW.isoformat(), health_result="passed")
        seen = post_merge_outcome.decide_outcomes(self.conn, clean, NOW + timedelta(minutes=10))
        self.assertEqual(seen[post_merge_outcome.VERIFIED_FIXED], [f"{REPO}/pull/1"])
        seen = post_merge_outcome.decide_outcomes(self.conn, lambda r: {"outcome": "reproduces"},
                                                  NOW + timedelta(minutes=20))
        self.assertEqual(seen[post_merge_outcome.STILL_BROKEN], [])
        self.assertEqual(self.outcome(1)[0], post_merge_outcome.VERIFIED_FIXED)

    def test_a_pull_request_closed_without_a_merge_never_ends_not_applied(self):
        # #5800: past CLOSED_GRACE it ends with no outcome, never as a fix that was not applied.
        self.add(1, closed_at=NOW.isoformat())
        post_merge_outcome.decide_outcomes(self.conn, clean, NOW + timedelta(days=3))
        self.assertEqual(self.outcome(1), (None, None))

    def test_at_most_limit_rechecks_run_and_an_unread_one_goes_to_the_back(self):
        for n in range(1, 6):
            self.add(n, merged_at=NOW.isoformat(), merge_commit=MERGE, applied_at=NOW.isoformat(),
                     health_result="passed", health_checked_at=f"2026-11-12T08:0{n}:00+00:00")
        ran = []

        def unread(r):
            ran.append(r["number"])
            raise post_merge_scope.RecheckUnread("the read failed its gate")

        with self.assertLogs("post_merge_check", level="WARNING"):
            post_merge_outcome.decide_outcomes(self.conn, unread, NOW + timedelta(minutes=10), limit=2)
        self.assertEqual(ran, [1, 2])
        ran.clear()
        with self.assertLogs("post_merge_check", level="WARNING"):
            post_merge_outcome.decide_outcomes(self.conn, unread, NOW + timedelta(minutes=11), limit=2)
        self.assertEqual(ran, [3, 4], "the two that did not answer went behind the rest")

    def test_one_record_s_fault_is_an_error_and_the_rest_are_decided(self):
        self.add(1, merged_at=NOW.isoformat(), merge_commit=MERGE, applied_at=NOW.isoformat(), health_result="passed")
        self.add(2, merged_at=NOW.isoformat(), merge_commit=MERGE, applied_at=NOW.isoformat(), health_result="passed")

        def broken_scope(r):
            if r["number"] == 1:
                raise KeyError("project")

        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = post_merge_outcome.decide_outcomes(self.conn, clean, NOW + timedelta(minutes=10), broken_scope)
        self.assertEqual((seen["errors"], seen[post_merge_outcome.VERIFIED_FIXED]),
                         ([f"{REPO}/pull/1"], [f"{REPO}/pull/2"]))


class TickTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.conn.execute(
            "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, object, "
            "opened_at) VALUES (?, ?, 1, 'no-pdb', 'p', 'l', 'prod', 'shop', 'Deployment/checkout', ?)",
            (URL, REPO, NOW.isoformat()))
        self.forge = FakeForge({1: {"state": "merged", "mergeCommit": MERGE, "closedAt": NOW.isoformat()}})
        self.cluster = FakeCluster()
        self.cluster.apps = [argo_app()]

    def fakes(self, ran):
        return {name: (lambda c, name=name: ran.append(name) or {})
                for name in ("fill_edited", "fill_environments", "report", "followups", "reverts")}

    def tick(self, minutes, **overrides):
        args = {"view": self.forge, "kubectl_json": self.cluster, "recheck": clean, "scope_refusal": lambda r: None,
                "now": NOW + timedelta(minutes=minutes), **self.fakes([]), **overrides}
        return post_merge_outcome.tick(self.conn, "silent", **args)

    def outcome(self):
        return self.conn.execute("SELECT outcome FROM post_merge_records").fetchone()[0]

    def test_a_fix_goes_from_merge_to_verified_fixed(self):
        self.tick(0)  # merged and applied
        self.tick(4)  # inside the window
        self.assertIsNone(self.outcome())
        self.tick(6)  # first healthy reading past the window
        self.tick(7)  # second: passed, re-checked, decided
        self.assertEqual(self.outcome(), post_merge_outcome.VERIFIED_FIXED)
        self.assertEqual(post_merge_outcome.errored(self.tick(8)), [])

    def test_an_unreadable_scope_reads_no_cluster_and_still_notices_merges(self):
        with mock.patch.object(post_merge_scope, "install_scope", side_effect=RuntimeError("render unreadable")), \
                self.assertLogs("post_merge_check", level="WARNING"):
            ran = []
            seen = post_merge_outcome.tick(self.conn, "report", view=self.forge, kubectl_json=self.cluster, now=NOW,
                                           **self.fakes(ran))
        self.assertEqual(seen["notice"]["merged"], [URL])
        self.assertEqual(self.cluster.calls, [])
        self.assertNotIn("outcomes", seen)
        self.assertEqual(ran, ["fill_edited", "report"], "outcomes already reached are still reported")
        self.assertTrue(post_merge_outcome.errored(seen))

    def test_every_step_runs_in_order_with_the_environment_before_the_follow_ups(self):
        ran = []
        seen = self.tick(0, **self.fakes(ran))
        self.assertEqual(ran, ["fill_edited", "fill_environments", "report", "followups", "reverts"])
        self.assertEqual(list(seen), ["notice", "edited", "health", "outcomes", "environments", "reports", "followups",
                                      "reverts"])

    def test_the_report_step_gets_the_switch_s_mode(self):
        import post_merge_report

        modes = []
        fakes = self.fakes([])
        del fakes["report"]
        with mock.patch.object(post_merge_report, "report_outcomes",
                               lambda conn, mode, **kw: modes.append(mode) or {}):
            post_merge_outcome.tick(self.conn, "report", view=self.forge, kubectl_json=self.cluster, recheck=clean,
                                    scope_refusal=lambda r: None, now=NOW, **fakes)
        self.assertEqual(modes, ["report"])

    def test_a_step_that_raises_is_kept_and_the_rest_run(self):
        def broken(conn, view, now, limit):
            raise RuntimeError("forge module missing")

        with mock.patch.object(post_merge_check, "notice_merges", broken), \
                self.assertLogs("post_merge_check", level="WARNING"):
            seen = self.tick(1)
        self.assertEqual(seen["notice"], {"errors": ["RuntimeError: forge module missing"]})
        self.assertIn("outcomes", seen)


class CronRunTest(unittest.TestCase):
    def run_main(self, switch, **overrides):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
            with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": switch, "SESSION_KV_DB_PATH": db}), \
                    mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                    mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                    mock.patch("script_log.append") as log, redirect_stdout(out), redirect_stderr(err):
                code = post_merge_check.main([], view=FakeForge({}), kubectl_json=FakeCluster(), **overrides)
        return code, out.getvalue(), err.getvalue(), log

    def test_off_prints_nothing_and_reads_nothing(self):
        code, out, err, log = self.run_main("off")
        self.assertEqual((code, out, err), (0, "", ""))
        log.assert_not_called()

    def test_a_clean_run_prints_nothing_and_logs_its_summary(self):
        code, out, err, log = self.run_main("silent", recheck=clean, scope_refusal=lambda r: None)
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertIn("post-merge check (silent)", log.call_args[0][1][0])

    def test_a_run_with_errors_exits_one_with_a_line_for_chat(self):
        with mock.patch.object(post_merge_scope, "install_scope", side_effect=RuntimeError("render unreadable")), \
                self.assertLogs("post_merge_check", level="WARNING"):
            code, out, err, _ = self.run_main("report")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("failed", err)
        self.assertIn("logs/post_merge_check.log", err)


if __name__ == "__main__":
    unittest.main()
