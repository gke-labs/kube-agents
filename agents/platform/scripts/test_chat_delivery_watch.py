"""Unit tests for chat_delivery_watch.py, the ``chat-delivery-watch`` job.

Run: python3 -m unittest agents/platform/scripts/test_chat_delivery_watch.py

Three properties carry the weight.

**The streak counts runs, not ticks.** The job fires every half hour over jobs
that run once a day; a tick that sees the same ``last_run_at`` twice must
leave the ledger byte-identical, or a single failed run reads as a week of
them by lunchtime.

**A silent run is not a recovery.** Hermes clears ``last_delivery_error`` on
any run that did not deliver, including a ``[SILENT]`` one. Governance jobs
are silent on every quiet day, so a watcher that read "no error" as "fixed"
would close its issue each clean morning and reopen it on the next finding.

**The channel never depends on chat, and the tick never raises.** Every
GitHub failure degrades to the log line; every exception in the tick lands on
the same log file as a ``self=error`` line and the exit code stays 0, because
a non-zero exit only produces a chat message that ``deliver: "local"`` drops.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))
cdw = importlib.import_module("chat_delivery_watch")
sandbox_exec = importlib.import_module("sandbox_exec")

REPO_ROOT = Path(__file__).resolve().parents[3]
ROSTER = REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json"
LEDGER_REPO = "example/gitops"

# The sentences deploy/docker/plugins/chat/adapter.py builds, copied verbatim so
# a wording change there fails here before it silently downgrades every partial
# fan-out to "hard".
PARTIAL_ERROR = (
    "delivery error: chat relay partial: the report did not reach slack. "
    "Delivered — do not re-run to resend."
)
DEGRADED_ERROR = (
    "delivery error: chat relay degraded: the report was posted but the Chat Agent turn "
    "failed, so the channel has the raw text marked [unrelayed] rather than a composed "
    "message. Delivered — do not re-run to resend."
)
HARD_ERROR = (
    "delivery error: chat relay answered HTTP 502: chat relay failed: composed but not "
    "delivered to slack, google_chat (target chat:cron-reports)"
)
UNREACHABLE_ERROR = "delivery error: chat relay unreachable: URLError: [Errno 111] Connection refused"
# Written by the scheduler before any adapter runs; both seen live on gkedemos on 2026-09-10.
NOT_CONFIGURED_ERROR = "platform 'google_chat' not configured/enabled"
NO_TARGET_ERROR = "no delivery target resolved for deliver=chat"

RUN_1 = "2026-09-08T06:20:11+00:00"
RUN_2 = "2026-09-09T06:20:09+00:00"
RUN_3 = "2026-09-10T06:20:14+00:00"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job(job_id: str, run_at: str | None, error: str | None = None) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "enabled": True,
        "deliver": "chat",
        "last_run_at": run_at,
        "last_status": "ok",
        "last_delivery_error": error,
    }


class FakeGh:
    """Records every gh call; answers `issue list` and `issue create` plausibly."""

    def __init__(self, open_issues: list[dict] | None = None, fail: set[str] | None = None):
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.open_issues = open_issues or []
        self.fail = fail or set()
        self.next_number = 41

    def __call__(self, argv, repo=None, *, stdin=None):
        self.calls.append(list(argv))
        self.stdins.append(stdin)
        verb = " ".join(argv[:2])
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"boom: {verb}")
        if verb == "issue list":
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.open_issues), stderr="")
        if verb == "issue create":
            self.next_number += 1
            # A created issue is open until closed, as the real one would be.
            self.open_issues.append({"number": self.next_number, "body": stdin or ""})
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"https://github.com/{repo}/issues/{self.next_number}\n", stderr=""
            )
        if verb == "issue close":
            self.open_issues = [i for i in self.open_issues if str(i["number"]) != argv[2]]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def verbs(self) -> list[str]:
        return [" ".join(c[:2]) for c in self.calls]


class WatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.store = self.home / "profiles" / "platform" / "cron" / "jobs.json"
        self.state = self.home / "state.json"
        self.gh = FakeGh()
        for target, value in (
            ("run_gh", self.gh),
            ("get_managed_github_repos", lambda: [LEDGER_REPO]),
            ("agent_home", lambda: str(self.home)),
        ):
            owner = cdw.forge if target == "run_gh" else cdw.gitops_workspace
            patcher = mock.patch.object(owner, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(cdw.THRESHOLD_ENV, None)
        os.environ.pop(cdw.STATE_PATH_ENV, None)
        os.environ.pop(cdw.LEDGER_REPO_ENV, None)

    def write_store(self, *jobs: dict, profile: str = "platform") -> Path:
        path = self.home / "profiles" / profile / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": list(jobs)}), encoding="utf-8")
        return path

    def run_tick(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cdw.main(["--state", str(self.state), *args])
        return rc, out.getvalue()

    def state_data(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def log_lines(self) -> list[str]:
        path = self.home / cdw.LOGS_DIR / cdw.ALERT_FILE_NAME
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class GradingTest(unittest.TestCase):
    def test_partial_names_the_platforms_it_missed(self) -> None:
        self.assertEqual(cdw.grade_error(PARTIAL_ERROR), cdw.GRADE_PARTIAL)
        self.assertEqual(cdw.platforms_from(PARTIAL_ERROR), ["slack"])

    def test_hard_failures_and_their_platforms(self) -> None:
        self.assertEqual(cdw.grade_error(HARD_ERROR), cdw.GRADE_HARD)
        self.assertEqual(cdw.platforms_from(HARD_ERROR), ["slack", "google_chat"])
        self.assertEqual(cdw.grade_error(UNREACHABLE_ERROR), cdw.GRADE_HARD)
        self.assertEqual(cdw.platforms_from(UNREACHABLE_ERROR), [])

    def test_degraded_is_its_own_grade(self) -> None:
        self.assertEqual(cdw.grade_error(DEGRADED_ERROR), cdw.GRADE_DEGRADED)

    def test_joined_errors_from_several_targets_keep_the_platform_list_clean(self) -> None:
        joined = HARD_ERROR + "; delivery to slack:C123 failed: channel_not_found"
        self.assertEqual(cdw.platforms_from(joined), ["slack", "google_chat"])

    def test_the_adapter_still_produces_the_phrases_the_grader_matches(self) -> None:
        # The guard behind the constants above: the phrases are literals here, so
        # this reads the adapter's source and fails if either sentence is reworded.
        adapter = (REPO_ROOT / "deploy" / "docker" / "plugins" / "chat" / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("chat relay partial: the report did not reach {undelivered}.", adapter)
        self.assertIn("chat relay degraded: ", adapter)
        relay = (REPO_ROOT / "agents" / "platform" / "scripts" / "session_kv_server.py").read_text(encoding="utf-8")
        self.assertIn("composed but not delivered to ", relay)

    def test_the_scheduler_s_own_failures_grade_hard_and_name_the_platform(self) -> None:
        self.assertEqual(cdw.grade_error(NOT_CONFIGURED_ERROR), cdw.GRADE_HARD)
        self.assertEqual(cdw.platforms_from(NOT_CONFIGURED_ERROR), ["google_chat"])
        self.assertEqual(cdw.grade_error(NO_TARGET_ERROR), cdw.GRADE_HARD)
        self.assertEqual(cdw.platforms_from(NO_TARGET_ERROR), [])

    def test_label_description_fits_github_s_limit(self) -> None:
        # A 108-character description broke `gh label create` for months on the audit ledger.
        self.assertLess(len(cdw.LABEL_DESCRIPTION), 100)


class AdvanceTest(unittest.TestCase):
    def test_a_new_failing_run_advances_and_records(self) -> None:
        entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, HARD_ERROR), silent=False)
        self.assertEqual((entry["streak"], entry["grade"], entry["first_failure_at"]), (1, "hard", RUN_1))
        entry = cdw.advance(entry, job("a", RUN_2, PARTIAL_ERROR), silent=False)
        self.assertEqual((entry["streak"], entry["grade"], entry["first_failure_at"], entry["last_failure_at"]), (2, "partial", RUN_1, RUN_2))

    def test_the_same_run_seen_twice_changes_nothing(self) -> None:
        entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, HARD_ERROR), silent=False)
        again = cdw.advance(entry, job("a", RUN_1, HARD_ERROR), silent=False)
        self.assertIs(again, entry)

    def test_a_clean_run_resets_and_forgets_the_episode(self) -> None:
        entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, HARD_ERROR), silent=False)
        entry = cdw.advance(entry, job("a", RUN_2), silent=False)
        self.assertEqual(entry["streak"], 0)
        self.assertEqual(entry["last_seen_run_at"], RUN_2)
        self.assertIsNone(entry["first_failure_at"])
        # A later episode starts its own clock rather than inheriting RUN_1.
        entry = cdw.advance(entry, job("a", RUN_3, HARD_ERROR), silent=False)
        self.assertEqual(entry["first_failure_at"], RUN_3)

    def test_an_interrupted_run_is_no_evidence_but_a_delivered_failure_summary_is(self) -> None:
        # A gateway shutdown or an exception before delivery records last_status
        # "error" with last_delivery_error None and saves no document...
        entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, HARD_ERROR), silent=False)
        interrupted = dict(job("a", RUN_2), last_status="error", last_error="Interrupted by gateway shutdown")
        entry = cdw.advance(entry, interrupted, silent=False, has_output=False)
        self.assertEqual((entry["streak"], entry["last_seen_run_at"]), (1, RUN_2))
        # ...whereas a run that failed and had its failure summary delivered did
        # save one, and that delivery proves the leg works.
        failed = dict(job("a", RUN_3), last_status="error", last_error="model quota exhausted")
        entry = cdw.advance(entry, failed, silent=False, has_output=True)
        self.assertEqual(entry["streak"], 0)

    def test_a_note_about_a_report_that_arrived_is_not_a_failure(self) -> None:
        for note in (
            "configured thread_id 12345 was not found; delivered without thread_id",
            "2 media attachment(s) not delivered to slack (live adapter confirmation timed out)",
        ):
            with self.subTest(note=note[:30]):
                self.assertTrue(cdw.is_delivered_note(note))
                entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, HARD_ERROR), silent=False)
                entry = cdw.advance(entry, job("a", RUN_2, note), silent=False)
                self.assertEqual(entry["streak"], 0)
        self.assertFalse(cdw.is_delivered_note(HARD_ERROR))
        self.assertFalse(cdw.is_delivered_note(HARD_ERROR + "; delivered without thread_id"))

    def test_a_silent_run_neither_advances_nor_resets(self) -> None:
        entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, HARD_ERROR), silent=False)
        entry = cdw.advance(entry, job("a", RUN_2), silent=True)
        self.assertEqual((entry["streak"], entry["last_seen_run_at"]), (1, RUN_2))

    def test_no_run_at_is_no_evidence(self) -> None:
        entry = cdw.new_entry()
        self.assertIs(cdw.advance(entry, job("a", None, HARD_ERROR), silent=False), entry)

    def test_the_error_is_clipped(self) -> None:
        entry = cdw.advance(cdw.new_entry(), job("a", RUN_1, "x" * (cdw.MAX_ERROR_CHARS + 50)), silent=False)
        self.assertEqual(len(entry["last_error"]), cdw.MAX_ERROR_CHARS)


class SilentOutputTest(WatchCase):
    def write_output(self, job_id: str, text: str) -> None:
        out = self.store.parent / "output" / job_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "2026-09-09_06-20-09.md").write_text(text, encoding="utf-8")

    def test_every_silence_form_the_scheduler_accepts_counts(self) -> None:
        self.write_store(job("a", RUN_2))
        for text in (
            "Nothing to report.\n\n[SILENT]\n",
            "# run\n\n**Status:** silent\n\nno findings\n",
            "# Cron run\n\n## Prompt\n\naudit\n\n## Response\n\n[SILENT] No changes detected\n",
            "# Cron run\n\n## Response\n\nsilent\n",
            "# Cron run\n\n## Response\n\nNO_REPLY\n",
            "# Cron run\n\n## Response\n\nno reply\n\nfooter line\n",
        ):
            with self.subTest(text=text[-30:]):
                self.write_output("a", text)
                self.assertTrue(cdw.newest_output_is_silent(self.store, "a", utc_now_iso()))

    def test_a_response_that_merely_mentions_silence_is_not_silent(self) -> None:
        self.assertFalse(cdw.is_silent_document("# Cron run\n\n## Response\n\nTwo findings; the [SILENT] rule did not apply.\n"))
        self.assertFalse(cdw.is_silent_document("## Prompt\n\n[SILENT]\n\n## Response\n\nA finding.\n"))

    def test_a_real_report_is_not_silent(self) -> None:
        self.write_store(job("a", RUN_2))
        self.write_output("a", "# Compliance audit\n\nTwo findings.\n")
        self.assertFalse(cdw.newest_output_is_silent(self.store, "a", RUN_2))

    def test_missing_output_errs_toward_recovered(self) -> None:
        self.write_store(job("a", RUN_2))
        self.assertFalse(cdw.newest_output_is_silent(self.store, "a", RUN_2))

    def test_an_output_older_than_the_run_does_not_count(self) -> None:
        self.write_store(job("a", RUN_2))
        self.write_output("a", "[SILENT]\n")
        stale = time.time() - 2 * cdw.SILENT_OUTPUT_SLACK_S
        path = self.store.parent / "output" / "a" / "2026-09-09_06-20-09.md"
        os.utime(path, (stale, stale))
        self.assertFalse(cdw.newest_output_is_silent(self.store, "a", utc_now_iso()))


class TickTest(WatchCase):
    def test_a_quiet_tick_prints_nothing_and_records_that_it_ran(self) -> None:
        self.write_store(job("a", RUN_1))
        rc, out = self.run_tick()
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.state_data()["last_tick_ok"])
        self.assertIsNotNone(self.state_data()["last_tick_at"])
        self.assertEqual(self.gh.calls, [])

    def test_streak_below_threshold_is_silent_and_persisted(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        rc, out = self.run_tick()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.state_data()["jobs"]["platform/a"]["streak"], 1)
        self.assertEqual(self.gh.calls, [])

    def test_crossing_the_threshold_opens_one_issue_and_writes_the_log(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR), job("b", RUN_1, PARTIAL_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR), job("b", RUN_2, PARTIAL_ERROR))
        rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.verbs(), ["issue list", "label create", "issue create"])
        created = self.gh.stdins[-1]
        self.assertIn(cdw.BODY_MARKER, created)
        self.assertIn("`a`", created)
        self.assertIn("`b`", created)
        lines = [line for line in out.splitlines() if line.startswith(cdw.LOG_PREFIX)]
        self.assertEqual(len(lines), 2)
        self.assertIn(f"job=a profile=platform grade=hard streak=2 threshold=2 platforms=slack,google_chat", lines[0])
        self.assertIn(f"ledger={LEDGER_REPO}#42", lines[0])
        self.assertTrue(lines[0].endswith(f"error={json.dumps(HARD_ERROR)}"))
        self.assertEqual(len(self.log_lines()), 2)
        self.assertIn(cdw.LOG_PREFIX, self.log_lines()[0])
        self.assertEqual(self.state_data()["ledger"], {"repo": LEDGER_REPO, "issue_number": 42, "fingerprint": mock.ANY})

    def test_an_unchanged_tick_makes_no_write_and_the_same_run_does_not_advance(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        before = self.state.read_bytes()
        calls = len(self.gh.calls)
        self.run_tick()
        # One read, to notice an issue a person closed by hand; no write.
        self.assertEqual(self.gh.verbs()[calls:], ["issue list"])
        after = self.state_data()
        self.assertEqual(after["jobs"], json.loads(before)["jobs"])
        self.assertEqual(after["jobs"]["platform/a"]["streak"], 2)

    def test_a_changed_picture_edits_the_issue(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_3, HARD_ERROR))
        self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue edit")
        self.assertEqual(self.gh.calls[-1][2], "42")

    def test_recovery_closes_once_with_a_comment(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_3))
        rc, out = self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue close")
        self.assertIn("--comment", self.gh.calls[-1])
        self.assertIn("recovered=true", out)
        self.assertIn(f"ledger={cdw.LEDGER_CLOSED}", out)
        self.assertEqual(self.state_data()["ledger"]["issue_number"], None)
        calls = len(self.gh.calls)
        rc, out = self.run_tick()
        self.assertEqual((rc, out, len(self.gh.calls)), (0, "", calls))

    def test_an_existing_open_ledger_issue_is_reused_highest_number_wins(self) -> None:
        self.gh.open_issues = [
            {"number": 7, "body": f"{cdw.BODY_MARKER}\nold"},
            {"number": 9, "body": "unrelated issue with the label"},
            {"number": 8, "body": f"{cdw.BODY_MARKER}\nolder"},
        ]
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        self.assertNotIn("issue create", self.gh.verbs())
        self.assertEqual(self.gh.calls[-1][:3], ["issue", "edit", "8"])

    def test_an_issue_closed_by_hand_is_replaced_not_edited(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue create")
        # Someone closes #42 by hand. A changed picture must open a new issue
        # rather than edit the closed one.
        self.gh.open_issues = []
        self.write_store(job("a", RUN_3, HARD_ERROR))
        self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue create")
        self.assertNotIn("issue edit", self.gh.verbs())
        self.assertEqual(self.state_data()["ledger"]["issue_number"], 43)

    def test_a_ledger_repository_change_closes_the_old_issue(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        os.environ[cdw.LEDGER_REPO_ENV] = "other/ledger"
        self.write_store(job("a", RUN_3, HARD_ERROR))
        rc, out = self.run_tick()
        closes = [c for c in self.gh.calls if c[:2] == ["issue", "close"]]
        self.assertEqual(len(closes), 1)
        self.assertIn(LEDGER_REPO, closes[0])
        self.assertEqual(self.state_data()["ledger"]["repo"], "other/ledger")
        self.assertIn("ledger=other/ledger#", out)

    def test_a_failed_lookup_never_creates_a_duplicate(self) -> None:
        self.gh.fail = {"issue list"}
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertNotIn("issue create", self.gh.verbs())
        self.assertIn("self=error kind=LedgerError", out)
        self.assertIn("ledger=error:LedgerError", out)

    def test_threshold_env_is_honoured_and_garbage_falls_back(self) -> None:
        os.environ[cdw.THRESHOLD_ENV] = "3"
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        rc, out = self.run_tick()
        self.assertEqual(out, "")
        self.write_store(job("a", RUN_3, HARD_ERROR))
        rc, out = self.run_tick()
        self.assertIn("streak=3 threshold=3", out)
        os.environ[cdw.THRESHOLD_ENV] = "lots"
        self.assertEqual(cdw.threshold(), cdw.THRESHOLD_DEFAULT)
        os.environ[cdw.THRESHOLD_ENV] = "0"
        self.assertEqual(cdw.threshold(), cdw.THRESHOLD_DEFAULT)

    def test_no_forge_means_log_only(self) -> None:
        with mock.patch.object(cdw.gitops_workspace, "get_managed_github_repos", lambda: []):
            self.write_store(job("a", RUN_1, HARD_ERROR))
            self.run_tick()
            self.write_store(job("a", RUN_2, HARD_ERROR))
            rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.calls, [])
        self.assertIn(f"ledger={cdw.LEDGER_NONE}", out)
        self.assertEqual(len(self.log_lines()), 1)

    def test_a_failing_repo_discovery_still_writes_the_log_line(self) -> None:
        def broken() -> list[str]:
            raise AttributeError("module has no attribute get_managed_github_repos")

        with mock.patch.object(cdw.gitops_workspace, "get_managed_github_repos", broken):
            self.write_store(job("a", RUN_1, HARD_ERROR))
            self.run_tick()
            self.write_store(job("a", RUN_2, HARD_ERROR))
            rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertIn("self=error kind=AttributeError", out)
        self.assertIn("job=a profile=platform grade=hard streak=2", out)
        self.assertIn("ledger=error:AttributeError", out)
        self.assertEqual(len(self.log_lines()), 2)

    def test_a_disabled_or_paused_job_holds_no_streak(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR), job("b", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR), job("b", RUN_2, HARD_ERROR))
        self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue create")
        disabled = dict(job("a", RUN_2, HARD_ERROR), enabled=False)
        paused = dict(job("b", RUN_2, HARD_ERROR), state="paused")
        self.write_store(disabled, paused)
        rc, out = self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue close")
        self.assertNotIn("platform/a", self.state_data()["jobs"])

    def test_several_managed_repositories_without_an_override_is_log_only(self) -> None:
        with mock.patch.object(cdw.gitops_workspace, "get_managed_github_repos", lambda: ["z/two", "a/one"]):
            self.write_store(job("a", RUN_1, HARD_ERROR))
            self.run_tick()
            self.write_store(job("a", RUN_2, HARD_ERROR))
            rc, out = self.run_tick()
        self.assertEqual(self.gh.calls, [])
        self.assertIn("self=error kind=LedgerRepoAmbiguous", out)
        self.assertIn("ledger=error:LedgerRepoAmbiguous", out)
        self.assertIn(cdw.LEDGER_REPO_ENV, out)

    def test_the_ledger_repo_env_wins_over_discovery(self) -> None:
        os.environ[cdw.LEDGER_REPO_ENV] = "other/ledger"
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        rc, out = self.run_tick()
        self.assertIn("ledger=other/ledger#42", out)
        self.assertIn("other/ledger", self.gh.calls[-1])

    def test_dry_run_writes_nothing(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        before = self.state.read_bytes()
        rc, out = self.run_tick("--dry-run")
        self.assertIn(f"ledger={cdw.LEDGER_DRY_RUN}", out)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.gh.calls, [])
        # A hand check must not fire the log-based alert channel.
        self.assertEqual(self.log_lines(), [])

    def test_every_store_on_the_volume_is_read_including_cluster_profiles(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.write_store(job("a", RUN_1, HARD_ERROR), profile="cluster-p-c-l")
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text(json.dumps([job("tick", RUN_1)]), encoding="utf-8")
        labels = [profile for profile, _ in cdw.roster_paths(self.home)]
        self.assertEqual(labels, ["default", "platform", "cluster-p-c-l"])
        self.run_tick()
        self.assertEqual(set(self.state_data()["jobs"]), {"platform/a", "cluster-p-c-l/a", "default/tick"})

    def test_an_interrupted_run_seen_by_the_tick_holds_the_streak(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        interrupted = dict(job("a", RUN_2), last_status="error", last_error="Interrupted by gateway shutdown")
        self.write_store(interrupted)
        self.run_tick()
        self.assertEqual(self.state_data()["jobs"]["platform/a"]["streak"], 1)

    def test_a_delivered_failure_summary_resets_through_the_tick(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        failed = dict(job("a", RUN_2), last_status="error", last_error="model quota exhausted")
        self.write_store(failed)
        out = self.store.parent / "output" / "a"
        out.mkdir(parents=True)
        (out / "2026-09-09_06-20-09.md").write_text("# Cron run\n\n## Response\n\nThe run failed: model quota exhausted.\n", encoding="utf-8")
        self.run_tick()
        self.assertEqual(self.state_data()["jobs"]["platform/a"]["streak"], 0)

    def test_a_lost_issue_number_is_looked_up_once_on_recovery(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        state = self.state_data()
        state["ledger"]["issue_number"] = None
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.write_store(job("a", RUN_3))
        rc, out = self.run_tick()
        self.assertEqual(self.gh.verbs()[-2:], ["issue list", "issue close"])
        self.assertIn("42", self.gh.calls[-1])

    def test_dry_run_writes_nothing_even_when_the_tick_raises(self) -> None:
        self.write_store(job("a", RUN_1))
        with mock.patch.object(cdw, "tick", side_effect=RuntimeError("kaboom")):
            rc, out = self.run_tick("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("self=error", out)
        self.assertEqual(self.log_lines(), [])

    def test_an_ambiguous_repository_still_closes_the_issue_it_opened(self) -> None:
        self.write_store(job("a", RUN_1, HARD_ERROR))
        self.run_tick()
        self.write_store(job("a", RUN_2, HARD_ERROR))
        self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue create")
        with mock.patch.object(cdw.gitops_workspace, "get_managed_github_repos", lambda: ["a/one", "z/two"]):
            self.write_store(job("a", RUN_3))
            rc, out = self.run_tick()
        self.assertEqual(self.gh.verbs()[-1], "issue close")
        self.assertIn(LEDGER_REPO, self.gh.calls[-1])
        self.assertIsNone(self.state_data()["ledger"]["issue_number"])

    def test_the_tick_survives_a_corrupt_state_file_and_an_unreadable_store(self) -> None:
        self.state.write_text("{not json", encoding="utf-8")
        self.write_store(job("a", RUN_1, HARD_ERROR))
        bad = self.write_store(job("b", RUN_1), profile="broken")
        bad.write_text("[", encoding="utf-8")
        odd = self.write_store(job("c", RUN_1), profile="odd")
        odd.write_text('{"jobs": 5}', encoding="utf-8")
        rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertIn("unreadable cron store broken", out)
        self.assertIn("unreadable cron store odd", out)
        # Housekeeping, not an alert: it must not match the alert filter.
        self.assertFalse([l for l in out.splitlines() if l.startswith(cdw.LOG_PREFIX)], out)
        self.assertEqual(self.state_data()["jobs"]["platform/a"]["streak"], 1)

    def test_a_sandbox_outage_degrades_to_the_log_line(self) -> None:
        def unavailable(argv, repo=None, *, stdin=None):
            raise sandbox_exec.SandboxUnavailable("ssh: connect refused")

        with mock.patch.object(cdw.forge, "run_gh", unavailable):
            self.write_store(job("a", RUN_1, HARD_ERROR))
            self.run_tick()
            self.write_store(job("a", RUN_2, HARD_ERROR))
            rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertIn("self=error kind=SandboxUnavailable", out)
        self.assertIn("ledger=error:SandboxUnavailable", out)
        self.assertTrue(self.state_data()["last_tick_ok"])

    def test_an_unexpected_github_failure_still_emits_the_alert_lines(self) -> None:
        # Seen live: an older image's forge.run_gh has no `stdin` argument and raises TypeError.
        def old_run_gh(argv, repo=None):
            raise TypeError("run_gh() got an unexpected keyword argument 'stdin'")

        with mock.patch.object(cdw.forge, "run_gh", old_run_gh):
            self.write_store(job("a", RUN_1, HARD_ERROR))
            self.run_tick()
            self.write_store(job("a", RUN_2, HARD_ERROR))
            rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertIn("self=error kind=TypeError", out)
        self.assertIn("job=a profile=platform grade=hard streak=2 threshold=2", out)
        self.assertIn("ledger=error:TypeError", out)
        self.assertTrue(self.state_data()["last_tick_ok"])

    def test_an_unexpected_exception_is_reported_and_exits_zero(self) -> None:
        self.write_store(job("a", RUN_1))
        with mock.patch.object(cdw, "tick", side_effect=RuntimeError("kaboom")):
            rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        self.assertIn("self=error kind=RuntimeError", out)
        self.assertFalse(self.state_data()["last_tick_ok"])
        self.assertIn("kaboom", self.state_data()["last_tick_error"])


class RosterTest(unittest.TestCase):
    def test_the_platform_roster_carries_the_job_as_a_local_no_agent_entry(self) -> None:
        jobs = {j["id"]: j for j in json.loads(ROSTER.read_text(encoding="utf-8"))["jobs"]}
        entry = jobs["chat-delivery-watch"]
        self.assertTrue(entry["no_agent"])
        self.assertEqual(entry["script"], "chat_delivery_watch.py")
        self.assertEqual(entry["deliver"], "local")
        self.assertEqual(entry["schedule"]["expr"], entry["schedule"]["display"])
        self.assertTrue(entry["enabled"])


if __name__ == "__main__":
    unittest.main()
