"""Tests for dial_state.py: the autonomy dial's board section and its three actions."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dial_state

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
REPO = "https://github.com/org/infra"
KEY = "github.com/org/infra"
ADMIN = "U0ADMIN01"
REAL_REFRESH_ASYNC = dial_state.refresh_pending_async


def done(results, code=0):
    return mock.Mock(returncode=code, stdout=json.dumps(results), stderr="")


class DialCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.dotenv = Path(tempfile.mkdtemp()) / ".env"
        self.dotenv.write_text(f"KUBEAGENTS_DIAL=on\nKUBEAGENTS_DIAL_ADMINS={ADMIN}\n")
        for target, value in (
            (mock.patch.dict(os.environ, {"HERMES_HOME": self.home}), None),
            (mock.patch.object(dial_state.kubeagents_switches, "MANAGED_DOTENV_PATH", self.dotenv), None),
            (mock.patch.object(dial_state, "managed_repos", lambda: [REPO]), None),
            (mock.patch.object(dial_state, "report_level", lambda level: self.reported.append(level) or True), None),
            (mock.patch.object(dial_state, "refresh_pending_async", lambda state: None), None),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.reported, self.runs = [], []
        self.answer = [{"repo": REPO, "state": "proposed", "pr": f"{REPO}/pull/35", "number": 35}]
        runner = mock.patch.object(dial_state, "run_switch", self.fake_run)
        runner.start()
        self.addCleanup(runner.stop)
        dial_state._busy.clear()
        dial_state._last_checked.clear()
        self.db = sqlite3.connect(":memory:")

    def fake_run(self, argv, timeout):
        self.runs.append(argv)
        return done(self.answer)

    def act(self, action, user=ADMIN, **payload):
        return dial_state.handle(action, {"action": action, "user": user, **payload})

    def section(self):
        return dial_state.snapshot_section(self.db, NOW)

    def request(self, action=dial_state.ACTION_REQUEST, **extra):
        return self.act(action, kind="no-pdb", environment="dev", repo_key=KEY, **extra)


class SnapshotTest(DialCase):
    def test_switch_off_is_enabled_false_only(self):
        self.dotenv.write_text("KUBEAGENTS_DIAL=off\n")
        self.assertEqual(self.section(), {"enabled": False})

    def test_the_section_has_the_agreed_shape(self):
        section = self.section()
        self.assertEqual(
            section,
            {
                "enabled": True,
                "level": "propose",
                "set_by": "",
                "set_at": 0,
                "admins": [ADMIN],
                "kinds": [{
                    "kind": "no-pdb", "title": "Disruption budgets", "environment": "dev",
                    "repos": [{
                        "repo": "org/infra", "repo_key": KEY, "clean": 0, "needed": 5, "days": 0,
                        "days_needed": 2, "ready": False, "state": "off",
                    }],
                }],
            },
        )

    def test_clean_merges_are_the_counters(self):
        self.db.execute(
            "CREATE TABLE post_merge_records (repo TEXT, kind TEXT, environment TEXT, outcome TEXT, "
            "merged_at TEXT, human_edited INTEGER, reverted_at TEXT)"
        )
        for day in ("2026-11-14T10:00:00Z", "2026-11-15T10:00:00Z", "2026-11-15T11:00:00Z"):
            self.db.execute("INSERT INTO post_merge_records VALUES (?, 'no-pdb', 'dev', 'verified_fixed', ?, 0, NULL)", (REPO, day))
        row = self.section()["kinds"][0]["repos"][0]
        self.assertEqual((row["clean"], row["days"], row["ready"]), (3, 2, False))

    def test_admins_take_member_ids_only_and_none_is_nobody(self):
        for value, expected in ((f"{ADMIN}, alice@x.com,W0AB12", [ADMIN, "W0AB12"]), ("none", []), ("", [])):
            with self.subTest(value=value):
                self.dotenv.write_text(f"KUBEAGENTS_DIAL=on\nKUBEAGENTS_DIAL_ADMINS={value}\n")
                self.assertEqual(dial_state.admins(), expected)

    def test_a_damaged_record_shows_the_default(self):
        path = Path(self.home) / dial_state.STATE_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"level": "explain", "set_by": "<!channel>", "set_at": True}))
        section = self.section()
        self.assertEqual((section["level"], section["set_by"], section["set_at"]), ("explain", "", 0))
        path.write_text("{broken")
        self.assertEqual(self.section()["level"], "propose")

    def test_an_unreadable_repo_list_is_no_rows_not_no_board(self):
        def broken():
            raise OSError("no mount")

        with mock.patch.object(dial_state, "managed_repos", broken):
            self.assertEqual(self.section()["kinds"][0]["repos"], [])


class LevelTest(DialCase):
    def test_off_and_non_admins_change_nothing(self):
        self.dotenv.write_text(f"KUBEAGENTS_DIAL=off\nKUBEAGENTS_DIAL_ADMINS={ADMIN}\n")
        self.assertEqual(self.act("dial.level", level="explain")["message"], dial_state.OFF)
        self.dotenv.write_text(f"KUBEAGENTS_DIAL=on\nKUBEAGENTS_DIAL_ADMINS={ADMIN}\n")
        for user in ("U0OTHER01", "", None):
            with self.subTest(user=user):
                answer = self.act("dial.level", user=user, level="explain")
                self.assertEqual((answer["ok"], answer["message"]), (False, dial_state.NOT_ADMIN))
        self.assertFalse((Path(self.home) / dial_state.STATE_NAME).exists())
        self.assertEqual(self.reported, [])

    def test_an_admin_pick_is_recorded_and_reported(self):
        answer = self.act("dial.level", level="explain")
        self.assertEqual(answer, {"action": "dial.level", "ok": True, "message": "Set to Explain."})
        section = self.section()
        self.assertEqual((section["level"], section["set_by"]), ("explain", ADMIN))
        self.assertGreater(section["set_at"], 0)
        self.assertEqual(self.reported, ["explain"])

    def test_an_unconfirmed_report_says_so(self):
        with mock.patch.object(dial_state, "report_level", lambda level: False):
            self.assertIn(dial_state.NOT_REPORTED, self.act("dial.level", level="automerge")["message"])

    def test_an_unknown_level_or_unreadable_record_changes_nothing(self):
        self.assertFalse(self.act("dial.level", level="yolo")["ok"])
        path = Path(self.home) / dial_state.STATE_NAME
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        self.assertEqual(self.act("dial.level", level="explain")["message"], dial_state.NOT_SAVED)
        self.assertEqual(path.read_text(), "{broken")


class SwitchTest(DialCase):
    def setUp(self):
        super().setUp()
        self.act("dial.level", level="automerge")

    def test_request_opens_the_rules_pull_request_and_shows_it_pending(self):
        answer = self.request()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(
            self.runs[-1],
            ["python3", dial_state.SWITCH_SCRIPT, "--kind", "no-pdb", "--environment", "dev", "--state", "on", "--repo", REPO],
        )
        row = self.section()["kinds"][0]["repos"][0]
        self.assertEqual((row["state"], row["pending_pr"]), ("pending", {"number": 35, "url": f"{REPO}/pull/35"}))

    def test_a_second_tap_passes_the_pending_number(self):
        self.request()
        self.answer = [{"repo": REPO, "state": "off", "closed": f"{REPO}/pull/35"}]
        self.request(dial_state.ACTION_STOP)
        self.assertEqual(self.runs[-1][-1], f"{REPO}#35")
        self.assertEqual(self.section()["kinds"][0]["repos"][0]["state"], "off")

    def test_refusals_run_nothing(self):
        for payload, message in (
            ({"kind": "no-hpa", "environment": "dev", "repo_key": KEY}, dial_state.UNKNOWN_KIND),
            ({"kind": "no-pdb", "environment": "prod", "repo_key": KEY}, dial_state.UNKNOWN_KIND),
            ({"kind": "no-pdb", "environment": "dev", "repo_key": "github.com/other/repo"}, dial_state.NOT_MANAGED),
            ({"kind": "no-pdb", "environment": "dev", "repo_key": ""}, dial_state.NOT_MANAGED),
        ):
            with self.subTest(payload=payload):
                answer = self.act(dial_state.ACTION_REQUEST, **payload)
                self.assertEqual((answer["ok"], answer["message"]), (False, message))
        self.act("dial.level", level="propose")
        self.assertEqual(self.request()["message"], dial_state.NEEDS_AUTOMERGE)
        self.act("dial.level", level="explain")
        self.assertEqual(self.request(dial_state.ACTION_STOP)["message"], dial_state.NOT_AT_EXPLAIN)
        self.assertEqual(self.request(user="U0OTHER01")["message"], dial_state.NOT_ADMIN)
        self.assertEqual(self.runs, [])

    def test_stop_works_below_auto_merge(self):
        self.act("dial.level", level="propose")
        self.answer = [{"repo": REPO, "state": "proposed", "pr": f"{REPO}/pull/36", "number": 36}]
        self.assertTrue(self.request(dial_state.ACTION_STOP)["ok"])
        self.assertEqual(self.runs[-1][7], "off")

    def test_a_busy_switch_or_a_failed_run_records_nothing(self):
        dial_state._busy.add(("no-pdb", KEY))
        self.assertEqual(self.request()["message"], dial_state.BUSY)
        dial_state._busy.clear()
        with mock.patch.object(dial_state, "run_switch", lambda argv, timeout: done([{"state": "blocked", "reason": "KUBEAGENTS_DIAL is off"}], 1)):
            answer = self.request()
        self.assertEqual(answer["message"], "The switch did not run: KUBEAGENTS_DIAL is off")
        self.answer = [{"repo": REPO, "state": "blocked", "reason": "a person's deny rule"}]
        self.assertIn("a person's deny rule", self.request()["message"])
        self.assertEqual(self.section()["kinds"][0]["repos"][0]["state"], "off")

    def test_a_file_that_already_agrees_is_on_without_a_pull_request(self):
        self.answer = [{"repo": REPO, "state": "on", "reason": "already allows it"}]
        self.assertTrue(self.request()["ok"])
        row = self.section()["kinds"][0]["repos"][0]
        self.assertEqual(row["state"], "on")
        self.assertNotIn("pending_pr", row)


class RefreshTest(DialCase):
    def setUp(self):
        super().setUp()
        self.act("dial.level", level="automerge")
        self.request()

    def pending(self):
        return [(k, key, e) for k, repos in dial_state.read_state()["kinds"].items() for key, e in repos.items()]

    def test_a_merged_rules_pull_request_turns_the_kind_on(self):
        self.answer = [{"repo": REPO, "number": 35, "state": "merged"}]
        dial_state.refresh_pending(self.pending())
        self.assertEqual(self.runs[-1][-3:], ["--check", "--repo", f"{REPO}#35"])
        self.assertEqual(self.section()["kinds"][0]["repos"][0]["state"], "on")

    def test_a_closed_one_puts_the_switch_back(self):
        self.answer = [{"repo": REPO, "number": 35, "state": "closed"}]
        dial_state.refresh_pending(self.pending())
        self.assertEqual(self.section()["kinds"][0]["repos"][0]["state"], "off")

    def test_an_open_one_or_a_failed_check_stays_pending(self):
        self.answer = [{"repo": REPO, "number": 35, "state": "open"}]
        dial_state.refresh_pending(self.pending())
        with mock.patch.object(dial_state, "run_switch", mock.Mock(side_effect=TimeoutError("sandbox"))):
            dial_state.refresh_pending(self.pending())
        self.assertEqual(self.section()["kinds"][0]["repos"][0]["state"], "pending")

    def test_a_tap_since_the_check_wins(self):
        stale = self.pending()
        self.answer = [{"repo": REPO, "state": "proposed", "pr": f"{REPO}/pull/40", "number": 40}]
        self.request(dial_state.ACTION_STOP)
        self.answer = [{"repo": REPO, "number": 35, "state": "merged"}]
        dial_state.refresh_pending(stale)
        row = self.section()["kinds"][0]["repos"][0]
        self.assertEqual(row["pending_pr"]["number"], 40)

    def test_the_check_runs_off_the_request_and_at_most_once_a_minute(self):
        started = []
        with mock.patch.object(dial_state.threading, "Thread", lambda **kw: mock.Mock(start=lambda: started.append(kw["args"]))):
            dial_state._last_checked.clear()
            REAL_REFRESH_ASYNC(dial_state.read_state())
            REAL_REFRESH_ASYNC(dial_state.read_state())
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
