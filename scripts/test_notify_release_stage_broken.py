#!/usr/bin/env python3
"""Unit tests for the release-stage notifier.

Run: cd scripts && python3 -m unittest test_notify_release_stage_broken

The fixtures under `testdata/release_stage_broken/` are recorded from the
runs the notifier exists for: `Staging: Promotion Pipeline` #30 (2026-10-21,
the second of the two silent nights), `RC: Release Pipeline` #404 and
`Release & Publish (GA)` #10, each trimmed to the log lines the script reads.
Five failure modes matter more than the rest: staying quiet on a red run,
which is the gap this closes; opening a second issue for a run already
recorded, which the fifteen-minute sweep would turn into a flood; closing on
a green that is not a recovery, whether an older run read while the red one
is re-run or a run whose failed stage was skipped; leaving an issue open
after the pipeline passes; and posting to Chat without an issue to cite.
"""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

_HERE = Path(__file__).resolve().parent
_RELEASE = _HERE / "release"
for path in (str(_HERE), str(_RELEASE)):
    if path not in sys.path:
        sys.path.insert(0, path)

import notify_release_stage_broken as notifier  # noqa: E402
from github_api import API_ROOT  # noqa: E402

FIXTURES = _HERE / "testdata" / "release_stage_broken"
WORKFLOWS = _HERE.parent / ".github" / "workflows"
NOTIFIER_WORKFLOW = WORKFLOWS / "release-stage-broken-notify.yml"
REPO = "gke-labs/kube-agents"

STAGING = {"id": 365450384, "name": "Staging: Promotion Pipeline"}
STAGING_RUNS = json.loads((FIXTURES / "staging-runs.json").read_text())
STAGING_JOBS = json.loads((FIXTURES / "staging-30-jobs.json").read_text())
# Staging #26 (2026-10-17): a green night whose `continue-on-error` rollback
# leg (step 3b) failed, which the API reports as a failed job inside a
# successful run.
STAGING_26_JOBS = json.loads((FIXTURES / "staging-26-jobs.json").read_text())
GA_JOBS = json.loads((FIXTURES / "ga-10-jobs.json").read_text())
ROLLBACK_LEG = "Step 3b - Roll Back to the Previous GA and Forward Again"
EVAL_STAGE = "Step 5 - Await Release-Candidate Eval"
WITHDRAWAL = "Step 5b - Withdraw an Unsettled Nomination"
TEARDOWN = "Step 7 - Tear Down Nightly Environment / teardown-environment"
RECONCILE_STEP = "Reconcile the release pipelines' issues"
RUN_30 = STAGING_RUNS["workflow_runs"][0]
RUN_29 = STAGING_RUNS["workflow_runs"][1]
STEP1_JOB = STAGING_JOBS["jobs"][0]["id"]
E2E_JOB = STAGING_JOBS["jobs"][2]["id"]
E2E_STAGE = "Step 3 - Run Nightly E2E Matrix / Run E2E (rc)"
# A green run's job list with the stage run and passed, the shape of every
# ordinary green night.
GREEN_JOBS = {
    "total_count": 2,
    "jobs": [
        {"id": 1, "name": "Step 1 - Resolve Promotion Candidate", "conclusion": "success", "steps": []},
        {"id": 2, "name": E2E_STAGE, "conclusion": "success", "steps": []},
    ],
}


def fixture(name):
    return (FIXTURES / name).read_text()


def run_like(number, run_id, conclusion="success", status="completed", attempt=1):
    run = dict(RUN_30)
    run.update(
        id=run_id,
        run_number=number,
        conclusion=conclusion,
        status=status,
        run_attempt=attempt,
        html_url=f"https://github.com/{REPO}/actions/runs/{run_id}",
    )
    return run


def runs_list(*runs):
    return {"total_count": len(runs), "workflow_runs": list(runs)}


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeGitHub:
    """Answers the client's reads from a path map and records every write.

    `responses` maps the API path (query string included) to the JSON
    answer; `logs` maps a job id to its raw log, served on the first hop
    without a redirect (the redirect itself is the flaky-check client's
    tested path). A read with no answer is a 404, so a test that reaches a
    path it did not declare is a test with a hole in it.
    """

    def __init__(self, responses=None, logs=None):
        self.responses = dict(responses or {})
        self.logs = dict(logs or {})
        self.writes = []
        self.log_reads = []
        self.next_issue = 4000

    def opener(self, request):
        path = request.full_url[len(API_ROOT) :]
        method = request.get_method()
        if method == "GET":
            if path not in self.responses:
                raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
            return _Response(json.dumps(self.responses[path]).encode())
        payload = json.loads(request.data)
        self.writes.append((method, path, payload))
        if method == "POST" and path.endswith("/issues"):
            self.next_issue += 1
            return _Response(json.dumps({"number": self.next_issue}).encode())
        return _Response(b"{}")

    def redirect_opener(self, request):
        job_id = int(request.full_url.rsplit("/", 2)[1])
        self.log_reads.append(job_id)
        if job_id not in self.logs:
            raise urllib.error.HTTPError(request.full_url, 410, "Gone", {}, None)
        return _Response(self.logs[job_id].encode())

    def api(self, dry_run=False):
        return notifier.GitHubAPI(REPO, "tok", dry_run=dry_run, opener=self.opener, redirect_opener=self.redirect_opener, sleep=lambda s: None)


class FakeChat:
    """Records what the Chat sender posts and answers 200."""

    def __init__(self):
        self.posted = []

    def __call__(self, request, timeout=None):
        self.posted.append(json.loads(request.data.decode())["text"])
        return _Response(b"{}")


def runs_path(workflow_id=STAGING["id"]):
    return f"/repos/{REPO}/actions/workflows/{workflow_id}/runs?branch=main&exclude_pull_requests=true&per_page={notifier.HISTORY_DEPTH}"


def branch_run(number, run_id, conclusion="failure", status="completed"):
    """A manual dispatch from a feature branch: not the pipeline's state."""
    return dict(run_like(number, run_id, conclusion=conclusion, status=status), head_branch="feat/try-it")


def jobs_path(run_id):
    return f"/repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100&page=1"


def issues_path(state):
    return f"/repos/{REPO}/issues?labels=ci%3Arelease-stage-broken&state={state}&sort=updated&direction=desc&per_page={notifier.ISSUE_PAGE}"


def issue(number, body, state="open"):
    return {"number": number, "body": body, "state": state, "html_url": f"https://github.com/{REPO}/issues/{number}"}


def body_with(run_ids, stage=E2E_STAGE, workflow_id=STAGING["id"]):
    lines = [notifier.marker(workflow_id), notifier.STAMP.format(run_ids=",".join(map(str, run_ids)))]
    if stage:
        lines.append(notifier.STAGE_STAMP.format(stage=stage))
    return "\n".join(lines + ["", "body"])


def staging_github(runs=STAGING_RUNS, open_issues=(), closed_issues=(), logs=None):
    responses = {
        runs_path(): runs,
        jobs_path(RUN_30["id"]): STAGING_JOBS,
        issues_path("open"): list(open_issues),
        issues_path("closed"): list(closed_issues),
    }
    default_logs = {STEP1_JOB: fixture("staging-30-step1.log"), E2E_JOB: fixture("staging-30-e2e.log")}
    return FakeGitHub(responses, logs if logs is not None else default_logs)


def reconcile(github, chat=None, dry_run=False):
    sender = notifier.Sender(space="spaces/AAA", token="t", opener=chat or FakeChat())
    with mock.patch("sys.stderr", new=io.StringIO()) as err:
        outcome = notifier.reconcile(github.api(dry_run), STAGING, sender, dry_run)
    return outcome, err.getvalue()


class LogReadingTest(unittest.TestCase):
    """What the script reads out of the logs the watched pipelines write."""

    def test_the_staging_resolver_names_tag_and_commit(self):
        candidate = notifier.extract_candidate(fixture("staging-30-step1.log"))
        self.assertEqual(candidate, {"tag": "rc_2610210024_8dc552a_validated", "sha": "8dc552a35eb4fa831ea12058868063e530d1e957"})

    def test_the_rc_pipeline_echoes_its_inputs(self):
        candidate = notifier.extract_candidate(fixture("rc-404-step1.log"))
        self.assertEqual(candidate, {"tag": "rc_2610210122_cbbe254", "sha": "cbbe2541ba1fb8b78770b40c09109bf939bf519c"})

    def test_the_ga_gate_names_the_gate_passing_commit_not_the_empty_input(self):
        """`TARGET_COMMIT:` is echoed empty on a scheduled release; the banner
        below it carries the commit the gate resolved."""
        candidate = notifier.extract_candidate(fixture("ga-10-decide.log"))
        self.assertEqual(candidate, {"tag": "staging_2609301327_d817b3b", "sha": "d817b3b2c4e5f92e84d2a62636d6279b571a82d7"})

    def test_a_second_log_fills_only_the_holes(self):
        merged = notifier.extract_candidate(fixture("ga-10-publish.log"), {"tag": "rc_x"})
        self.assertEqual(merged, {"tag": "rc_x", "sha": "d817b3b2c4e5f92e84d2a62636d6279b571a82d7"})

    def test_a_pytest_summary_line_is_the_first_failure(self):
        line = notifier.extract_first_failure(fixture("staging-30-e2e.log"))
        self.assertTrue(line.startswith("FAILED tests/e2e/test_stockout_investigation.py::test_stockout_ingress_alert_smoke"), line)
        self.assertNotIn("2026-10-21T", line)

    def test_the_chat_suite_failure_is_read_the_same_way(self):
        line = notifier.extract_first_failure(fixture("rc-404-e2e.log"))
        self.assertTrue(line.startswith("FAILED tests/e2e/gchat_agent_test.py::test_gchat_agent_math_response"), line)

    def test_a_script_error_line_beats_the_generic_exit_annotation(self):
        line = notifier.extract_first_failure(fixture("ga-10-publish.log"))
        self.assertTrue(line.startswith("❌ ERROR: Required container images"), line)

    def test_an_annotation_beats_a_generic_error_line(self):
        text = "2026-10-21T03:00:01.0Z ERROR: a warning-ish line\n2026-10-21T03:00:02.0Z ##[error]verify.sh: cluster not ready\n"
        self.assertEqual(notifier.extract_first_failure(text), "##[error]verify.sh: cluster not ready")

    def test_the_failing_step_window_is_searched_before_the_whole_log(self):
        """An `ERROR:` an earlier step printed and survived must not stand
        in for what failed the job."""
        text = (
            "2026-10-21T03:00:01.0000000Z ERROR: earlier step grumbled and passed\n"
            "2026-10-21T03:10:05.1234567Z ❌ ERROR: the gate refused the candidate\n"
            "2026-10-21T03:10:06.0000000Z ##[error]Process completed with exit code 1.\n"
        )
        window = ("2026-10-21T03:10:00Z", "2026-10-21T03:10:06Z")
        self.assertEqual(notifier.extract_first_failure(text, window), "❌ ERROR: the gate refused the candidate")
        self.assertEqual(notifier.extract_first_failure(text), "ERROR: earlier step grumbled and passed")
        self.assertEqual(notifier.extract_first_failure(text, (None, None)), "ERROR: earlier step grumbled and passed")

    def test_a_window_with_nothing_in_it_falls_back_to_the_whole_log(self):
        text = "2026-10-21T03:00:01.0Z ##[error]only line\n"
        self.assertEqual(notifier.extract_first_failure(text, ("2026-10-21T04:00:00Z", "2026-10-21T04:00:01Z")), "##[error]only line")

    def test_a_log_with_no_failure_line_yields_none(self):
        self.assertIsNone(notifier.extract_first_failure(fixture("staging-30-step1.log")))

    def test_colour_codes_and_timestamps_are_stripped_and_the_second_kept(self):
        stamp, line = notifier.split_log_line("﻿2026-10-21T02:37:11.1014977Z \x1b[36;1mhello\x1b[0m\r")
        self.assertEqual((stamp, line), ("2026-10-21T02:37:11Z", "hello"))
        self.assertEqual(notifier.split_log_line("no stamp"), (None, "no stamp"))


def jobs_with(jobs_document, completed=None, **conclusions):
    """A copy of a jobs document with the named jobs' conclusions changed,
    and `completed` ({name: completed_at}) moving their completion."""
    jobs = [dict(job) for job in jobs_document["jobs"]]
    for job in jobs:
        if job["name"] in conclusions:
            job["conclusion"] = conclusions[job["name"]]
        if job["name"] in (completed or {}):
            job["completed_at"] = completed[job["name"]]
    return {"total_count": len(jobs), "jobs": jobs}


class JobReadingTest(unittest.TestCase):
    def test_the_stage_is_the_only_failed_job_when_there_is_one(self):
        job = notifier.failing_job(STAGING_JOBS["jobs"])
        self.assertEqual(job["name"], E2E_STAGE)
        step = notifier.failing_step(job)
        self.assertEqual(step["name"], "Execute Blocking E2E Gate")
        self.assertEqual((step["started_at"], step["completed_at"]), ("2026-10-21T03:29:21Z", "2026-10-21T03:37:38Z"))

    def test_the_stage_is_the_failing_job_that_completed_last_not_the_rollback_leg(self):
        """Staging #26's shape with step 5 failing as well, unsettled (its
        withdrawal ran): the rollback leg failed at 03:54 and is listed
        first, the eval wait failed at 05:09 and ended the run. The run
        stopped at the eval."""
        jobs = jobs_with(STAGING_26_JOBS, completed={WITHDRAWAL: "2026-10-17T05:12:00Z"}, **{EVAL_STAGE: "failure", WITHDRAWAL: "success"})["jobs"]
        self.assertEqual([job["name"] for job in jobs if job["conclusion"] == "failure"][0], ROLLBACK_LEG)
        self.assertEqual(notifier.failing_job(jobs)["name"], EVAL_STAGE)

    def test_a_withdrawal_that_failed_after_the_eval_is_not_the_stage(self):
        """Step 5b runs with `always()` because step 5 did not settle, and
        can fail after it; it is skipped on a green night, so naming it
        would hold the issue for good. The stage is the eval wait."""
        jobs = jobs_with(STAGING_26_JOBS, completed={WITHDRAWAL: "2026-10-17T05:12:00Z"}, **{EVAL_STAGE: "failure", WITHDRAWAL: "failure"})["jobs"]
        self.assertEqual(notifier.failing_job(jobs)["name"], EVAL_STAGE)

    def test_a_red_verdict_is_the_eval_failed_with_its_withdrawal_skipped(self):
        """Step 5 fails on a RED verdict and on no verdict alike, and the
        jobs API carries no outputs; step 5b runs only when the verdict did
        not settle. A failed eval with the withdrawal skipped is a RED. The
        withdrawal having run, in any conclusion, is not; nor an eval
        cancelled at its timeout, nor a green night, nor a gating job that
        failed beside it. The rollback leg failing beside it still is."""
        red = jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure"})["jobs"]
        self.assertEqual(next(job["conclusion"] for job in red if job["name"] == ROLLBACK_LEG), "failure")
        self.assertTrue(notifier.red_verdict_night(red))
        for conclusions in (
            {EVAL_STAGE: "failure", WITHDRAWAL: "success"},
            {EVAL_STAGE: "failure", WITHDRAWAL: "failure"},
            {EVAL_STAGE: "cancelled"},
            {},
            {EVAL_STAGE: "failure", "Step 6 - Create Staging Tag": "failure"},
            {EVAL_STAGE: "failure", TEARDOWN: "failure"},
        ):
            self.assertFalse(notifier.red_verdict_night(jobs_with(STAGING_26_JOBS, **conclusions)["jobs"]), conclusions)

    def test_a_teardown_that_failed_beside_a_red_verdict_is_the_stage(self):
        """Step 7 reds a green night, so it reds a RED one; the eval wait
        that failed on the verdict is passed over, and the teardown is named."""
        jobs = jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure", TEARDOWN: "failure"})["jobs"]
        self.assertTrue(notifier.settled_red_verdict(jobs))
        self.assertEqual(notifier.failing_job(jobs)["name"], TEARDOWN)
        self.assertEqual(notifier.failing_job(jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure", WITHDRAWAL: "success"})["jobs"])["name"], EVAL_STAGE)

    def test_a_compensation_job_is_the_stage_when_nothing_else_failed(self):
        jobs = jobs_with(STAGING_26_JOBS, **{ROLLBACK_LEG: "success", WITHDRAWAL: "failure"})["jobs"]
        self.assertEqual(notifier.failing_job(jobs)["name"], WITHDRAWAL)
        self.assertTrue(notifier.is_compensation({"name": "Next mode (non-gating) - Step 7 - Run E2E Tests"}))
        self.assertTrue(notifier.is_compensation({"name": "Step 7 - Tear Down Nightly Environment"}))
        self.assertFalse(notifier.is_compensation({"name": EVAL_STAGE}))

    def test_two_gating_failures_name_the_one_that_completed_last_not_the_first_listed(self):
        """Two matrix legs of the E2E, or a gate and a later stage that ran
        anyway: the stage is the one that ended the run, whatever its place
        in the list. Pinned on its own because every fixture night has one
        failed job per rank, so rank alone decided every other case."""
        jobs = [
            {"name": "gate", "conclusion": "failure", "completed_at": "2026-10-21T05:10:00Z"},
            {"name": "leg-a", "conclusion": "failure", "completed_at": "2026-10-21T05:00:00Z"},
            {"name": "leg-b", "conclusion": "failure", "completed_at": "2026-10-21T05:20:00Z"},
            {"name": "ok", "conclusion": "success", "completed_at": "2026-10-21T05:30:00Z"},
        ]
        self.assertEqual(notifier.failing_job(jobs)["name"], "leg-b")
        self.assertEqual(notifier.failing_job(list(reversed(jobs)))["name"], "leg-b")
        timed_last_but_cancelled = jobs + [{"name": "late-cancel", "conclusion": "cancelled", "completed_at": "2026-10-21T06:00:00Z"}]
        self.assertEqual(notifier.failing_job(timed_last_but_cancelled)["name"], "leg-b")

    def test_a_failing_job_without_a_time_sorts_first_and_ties_keep_list_order(self):
        jobs = [
            {"name": "late", "conclusion": "failure", "completed_at": "2026-10-21T05:00:00Z"},
            {"name": "untimed", "conclusion": "failure"},
            {"name": "late-too", "conclusion": "failure", "completed_at": "2026-10-21T05:00:00Z"},
        ]
        self.assertEqual(notifier.failing_job(jobs)["name"], "late")
        self.assertEqual(notifier.failing_job([{"name": "untimed", "conclusion": "failure"}])["name"], "untimed")

    def test_the_ga_publish_job_and_its_step(self):
        job = notifier.failing_job(GA_JOBS["jobs"])
        self.assertEqual(job["name"], "Publish GA Release")
        self.assertEqual(notifier.failing_step(job)["name"], "Verify Release Eligibility")

    def test_the_four_ranks_gating_failed_gating_cancelled_compensation_failed_any_cancelled(self):
        """A gating job killed at its timeout can read `cancelled`, and
        still outranks the compensation job that failed after it."""
        gating_failed = {"name": "a", "conclusion": "failure", "completed_at": "2026-10-21T01:00:00Z"}
        gating_cancelled = {"name": "b", "conclusion": "cancelled", "completed_at": "2026-10-21T02:00:00Z"}
        compensation_failed = {"name": "Step 7 - Tear Down Nightly Environment", "conclusion": "failure", "completed_at": "2026-10-21T03:00:00Z"}
        compensation_cancelled = {"name": "Step 5b - Withdraw an Unsettled Nomination", "conclusion": "cancelled", "completed_at": "2026-10-21T04:00:00Z"}
        jobs = [compensation_cancelled, compensation_failed, gating_cancelled, gating_failed]
        for expected in ("a", "b", "Step 7 - Tear Down Nightly Environment", "Step 5b - Withdraw an Unsettled Nomination"):
            self.assertEqual(notifier.failing_job(jobs)["name"], expected)
            jobs = [job for job in jobs if job["name"] != expected]
        self.assertIsNone(notifier.failing_job([{"name": "a", "conclusion": "success"}]))

    def test_an_eval_wait_cancelled_at_its_timeout_outranks_the_withdrawal_that_failed_after_it(self):
        jobs = jobs_with(STAGING_26_JOBS, completed={WITHDRAWAL: "2026-10-17T05:12:00Z"}, **{EVAL_STAGE: "cancelled", WITHDRAWAL: "failure"})["jobs"]
        self.assertEqual(notifier.failing_job(jobs)["name"], EVAL_STAGE)

    def test_a_stage_is_found_across_the_reusable_workflow_separator(self):
        """A skipped call is listed under its short name, an expanded one
        under `<caller> / <called>`; the same stage either way."""
        jobs = [{"name": "Step 4 - Tag Validated Release / validate-tag", "conclusion": "success"}]
        self.assertIsNotNone(notifier.job_named(jobs, "Step 4 - Tag Validated Release"))
        self.assertIsNotNone(notifier.job_named([{"name": "Step 4 - Tag Validated Release"}], "Step 4 - Tag Validated Release / validate-tag"))
        self.assertIsNone(notifier.job_named(jobs, "Step 4"))


class RenderTest(unittest.TestCase):
    def test_the_stamps_round_trip(self):
        body = body_with([1, "22:2", 333])
        self.assertEqual(notifier.stamped_runs(body), {1, 22, 333})
        self.assertEqual(notifier.stamped_attempts(body), {(1, 1), (22, 2), (333, 1)})
        self.assertEqual(notifier.stamped_stage(body), E2E_STAGE)
        self.assertEqual(notifier.stamped_runs(""), set())
        self.assertEqual(notifier.stamped_attempts(None), set())
        self.assertIsNone(notifier.stamped_stage(body_with([1], stage=None)))

    def test_a_rendered_body_stamps_the_attempt(self):
        details = {"run": run_like(30, RUN_30["id"], conclusion="failure", attempt=2), "stage": None, "stage_url": None, "step": None, "failure": None, "candidate": {}}
        body = notifier.render_body(STAGING["name"], STAGING["id"], details, [RUN_29, details["run"]])
        self.assertEqual(notifier.stamped_attempts(body), {(RUN_29["id"], 1), (RUN_30["id"], 2)})

    def test_the_body_names_stage_candidate_failure_and_streak(self):
        details = {
            "run": RUN_30,
            "stage": E2E_STAGE,
            "stage_url": "https://example/job",
            "step": "Execute Blocking E2E Gate",
            "failure": "FAILED tests/e2e/x.py::test_y - AssertionError",
            "candidate": {"tag": "rc_1_abc", "sha": "a" * 40},
        }
        body = notifier.render_body(STAGING["name"], STAGING["id"], details, [RUN_29, RUN_30])
        self.assertIn(notifier.marker(STAGING["id"]), body)
        self.assertEqual(notifier.stamped_runs(body), {RUN_29["id"], RUN_30["id"]})
        self.assertEqual(notifier.stamped_stage(body), E2E_STAGE)
        for expected in (E2E_STAGE, "Execute Blocking E2E Gate", "rc_1_abc", "a" * 40, "FAILED tests/e2e/x.py::test_y", RUN_30["html_url"], "[#29]", "[#30]", "2026-10-21 02:36 UTC"):
            self.assertIn(expected, body)

    def test_log_text_cannot_break_the_code_span_or_the_table_cell(self):
        text = notifier.failure_text({"failure": "FAILED t.py::test_x - assert 'a | b' == `c`"})
        self.assertEqual(text, "`FAILED t.py::test_x - assert 'a \\| b' == 'c'`")
        self.assertIn("step 'a \\| b'", notifier.failure_text({"step": "a | b"}).replace("`", "'"))

    def test_an_unknown_candidate_and_stage_are_said_not_omitted(self):
        details = {"run": RUN_30, "stage": None, "stage_url": None, "step": None, "failure": None, "candidate": {}}
        body = notifier.render_body(STAGING["name"], STAGING["id"], details, [RUN_30])
        self.assertIn(notifier.CANDIDATE_UNKNOWN, body)
        self.assertIn(notifier.STAGE_UNKNOWN, body)
        self.assertIsNone(notifier.stamped_stage(body))
        self.assertNotIn("Failing runs in this streak", body)

    def test_the_chat_messages_are_short_and_carry_the_links(self):
        details = {"run": RUN_30, "stage": "Step 3", "stage_url": None, "step": "s", "failure": "FAILED t::x", "candidate": {"tag": "rc_1_abc", "sha": "a" * 40}}
        broken = notifier.render_chat_broken(STAGING["name"], details, 4001)
        self.assertEqual(len(broken.splitlines()), 3)
        self.assertIn("🔴", broken)
        self.assertIn("Staging: Promotion Pipeline", broken)
        self.assertIn("Step 3: FAILED t::x", broken)
        marked = notifier.render_chat_broken(STAGING["name"], dict(details, failure="##[error]Expected *one* replica, got `two` ~now~ _x_ test_a_b"), 4001)
        self.assertIn("Expected ∗one∗ replica, got 'two' ∼now∼ ‗x‗ test_a_b", marked)
        self.assertNotIn("*one*", marked)
        self.assertIn("rc_1_abc (aaaaaaa)", broken)
        self.assertIn("#4001", broken)
        self.assertIn(RUN_30["html_url"], broken)
        recovered = notifier.render_chat_recovered(STAGING["name"], details, 4001)
        self.assertEqual(len(recovered.splitlines()), 2)
        self.assertIn("🟢", recovered)
        self.assertIn("#4001", recovered)
        self.assertIn(f"#{RUN_30['run_number']}", recovered)


class ReconcileTest(unittest.TestCase):
    """The state machine against recorded runs: open, update, close, nothing."""

    def test_a_red_run_with_no_issue_opens_one_and_posts(self):
        github = staging_github()
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "opened")
        method, path, payload = github.writes[0]
        self.assertEqual((method, path, payload["name"]), ("POST", f"/repos/{REPO}/labels", notifier.LABEL))
        method, path, payload = github.writes[1]
        self.assertEqual((method, path), ("POST", f"/repos/{REPO}/issues"))
        self.assertEqual(payload["title"], "🔴 release pipeline stage broken: Staging: Promotion Pipeline")
        self.assertEqual(payload["labels"], [notifier.LABEL])
        self.assertEqual(notifier.stamped_runs(payload["body"]), {RUN_29["id"], RUN_30["id"]})
        self.assertEqual(notifier.stamped_stage(payload["body"]), E2E_STAGE)
        self.assertIn("rc_2610210024_8dc552a_validated", payload["body"])
        self.assertIn("test_stockout_ingress_alert_smoke", payload["body"])
        self.assertEqual(len(github.writes), 2)
        self.assertEqual(len(chat.posted), 1)
        self.assertIn("Tracking #4001", chat.posted[0])
        self.assertIn(E2E_STAGE, chat.posted[0])
        self.assertIn(RUN_30["html_url"], chat.posted[0])

    def test_each_log_is_read_once(self):
        """The failing job is often among the first jobs read for the
        candidate; its log is downloaded once, not twice."""
        jobs = {"total_count": 2, "jobs": [dict(STAGING_JOBS["jobs"][2]), dict(STAGING_JOBS["jobs"][0])]}
        github = staging_github()
        github.responses[jobs_path(RUN_30["id"])] = jobs
        self.assertEqual(reconcile(github)[0], "opened")
        self.assertEqual(github.log_reads, [E2E_JOB, STEP1_JOB])

    def test_a_red_run_already_recorded_writes_nothing(self):
        github = staging_github(open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])

    def test_a_new_red_run_rewrites_the_body_and_comments_once_without_chat(self):
        github = staging_github(open_issues=[issue(4001, body_with([RUN_29["id"]]))])
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "updated")
        # The comment lands before the body rewrite that stamps the attempt:
        # a comment lost after the stamp would never be retried.
        self.assertEqual([(w[0], w[1]) for w in github.writes], [("POST", f"/repos/{REPO}/issues/4001/comments"), ("PATCH", f"/repos/{REPO}/issues/4001")])
        self.assertEqual(notifier.stamped_runs(github.writes[1][2]["body"]), {RUN_29["id"], RUN_30["id"]})
        comment = github.writes[0][2]["body"]
        self.assertIn("Still failing", comment)
        self.assertIn("[#30]", comment)
        self.assertIn("rc_2610210024_8dc552a_validated", comment)
        self.assertEqual(chat.posted, [])

    def test_a_green_run_with_the_stage_run_closes_the_issue_and_posts(self):
        run_31 = run_like(31, 39600000000)
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        github.responses[jobs_path(run_31["id"])] = GREEN_JOBS
        github.logs[1] = fixture("staging-30-step1.log")
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "closed")
        self.assertEqual([(w[0], w[1]) for w in github.writes], [("POST", f"/repos/{REPO}/issues/4001/comments"), ("PATCH", f"/repos/{REPO}/issues/4001")])
        self.assertIn("[#31]", github.writes[0][2]["body"])
        self.assertIn("passed", github.writes[0][2]["body"])
        self.assertEqual(github.writes[1][2], {"state": "closed"})
        self.assertEqual(len(chat.posted), 1)
        self.assertIn("passes again", chat.posted[0])
        self.assertIn("Closed #4001", chat.posted[0])
        self.assertIn("rc_2610210024_8dc552a_validated", chat.posted[0])

    def test_a_green_run_leaves_an_issue_a_person_reopened_alone(self):
        """Reopening is a decision; the sweep must not close it again every
        fifteen minutes with a fresh comment and Chat post."""
        run_31 = run_like(31, 39600000000)
        reopened = dict(issue(4001, body_with([RUN_29["id"], RUN_30["id"]])), state_reason=notifier.REOPENED)
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[reopened])
        github.responses[jobs_path(run_31["id"])] = GREEN_JOBS
        chat = FakeChat()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.reconcile(github.api(), STAGING, notifier.Sender(space="spaces/AAA", token="t", opener=chat), dry_run=False), "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])
        self.assertIn("reopened #4001", err.getvalue())

    def test_a_green_run_whose_stage_was_skipped_is_no_recovery(self):
        """`release-publish.yml` under `schedule_gate=dry-run`, a promotion
        run with `skip_pipeline`, an RC run that skips: the run concludes
        success with the stage skipped, and says nothing about the failure."""
        run_31 = run_like(31, 39600000000)
        skipped = {"total_count": 2, "jobs": [dict(GREEN_JOBS["jobs"][0]), dict(GREEN_JOBS["jobs"][1], conclusion="skipped")]}
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        github.responses[jobs_path(run_31["id"])] = skipped
        chat = FakeChat()
        outcome, err = reconcile(github, chat)
        self.assertEqual(outcome, "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])
        self.assertIn("concluded skipped", err)
        self.assertIn("#4001 stays open", err)

    def test_a_green_run_without_the_stage_at_all_is_a_rename_and_closes_with_a_note(self):
        """A job renamed between the failure and the recovery (the RC
        pipeline's "Next mode" jobs became "Today mode") would otherwise
        leave an issue nothing can ever close."""
        run_31 = run_like(31, 39600000000)
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        github.responses[jobs_path(run_31["id"])] = {"total_count": 1, "jobs": [dict(GREEN_JOBS["jobs"][0])]}
        github.logs[1] = fixture("staging-30-step1.log")
        self.assertEqual(reconcile(github)[0], "closed")
        comment = github.writes[0][2]["body"]
        self.assertIn(f"No job of this run is named `{E2E_STAGE}`", comment)
        self.assertIn("renamed", comment)
        self.assertEqual(github.writes[1][2], {"state": "closed"})

    def test_a_green_run_with_a_failed_non_gating_job_still_closes(self):
        """Staging #26 as the recovery: its rollback leg failed, its eval
        wait passed. Whether the issue recorded the eval wait or (wrongly)
        the rollback leg as the stage, the stage ran, and the run is green."""
        for stage in (EVAL_STAGE, ROLLBACK_LEG):
            with self.subTest(stage=stage):
                run_31 = run_like(31, 39600000000)
                github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]], stage=stage))])
                github.responses[jobs_path(run_31["id"])] = STAGING_26_JOBS
                self.assertEqual(reconcile(github)[0], "closed")
                self.assertEqual(github.writes[1][2], {"state": "closed"})

    def test_a_green_night_with_the_withdrawal_skipped_closes_an_issue_stamped_on_the_eval(self):
        """Staging #26 again: step 5b is skipped on a green night, which
        must not hold an issue whose stage is step 5."""
        self.assertEqual(next(job["conclusion"] for job in STAGING_26_JOBS["jobs"] if job["name"] == WITHDRAWAL), "skipped")
        run_31 = run_like(31, 39600000000)
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]], stage=EVAL_STAGE))])
        github.responses[jobs_path(run_31["id"])] = STAGING_26_JOBS
        self.assertEqual(reconcile(github)[0], "closed")

    def test_a_red_night_where_the_eval_and_its_withdrawal_both_failed_stamps_the_eval(self):
        github = staging_github()
        github.responses[jobs_path(RUN_30["id"])] = jobs_with(STAGING_26_JOBS, completed={WITHDRAWAL: "2026-10-17T05:12:00Z"}, **{EVAL_STAGE: "failure", WITHDRAWAL: "failure"})
        self.assertEqual(reconcile(github)[0], "opened")
        self.assertEqual(notifier.stamped_stage(github.writes[1][2]["body"]), EVAL_STAGE)

    def test_a_red_night_with_the_rollback_leg_and_the_eval_both_failed_names_the_eval(self):
        """The eval did not settle (its withdrawal ran), so the night is a
        stage failure, and the stage is the eval, not the rollback leg."""
        github = staging_github()
        github.responses[jobs_path(RUN_30["id"])] = jobs_with(STAGING_26_JOBS, completed={WITHDRAWAL: "2026-10-17T05:12:00Z"}, **{EVAL_STAGE: "failure", WITHDRAWAL: "success"})
        self.assertEqual(reconcile(github)[0], "opened")
        body = github.writes[1][2]["body"]
        self.assertEqual(notifier.stamped_stage(body), EVAL_STAGE)
        self.assertNotIn(f"stage={ROLLBACK_LEG}", body)

    def test_a_red_verdict_night_opens_nothing_and_posts_nothing(self):
        """Step 5 failed and step 5b was skipped: the eval answered RED. The
        gate rejected the candidate, which is no stage failure, so the night
        reads as green: no issue, no Chat post. The rollback leg that failed
        beside it counts for nothing, as on a green night."""
        run_31 = run_like(31, 39600000000, conclusion="failure")
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"][2:]))
        github.responses[jobs_path(run_31["id"])] = jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure"})
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])

    def test_a_red_verdict_night_closes_an_issue_whose_stage_ran_and_says_what_the_run_was_red_on(self):
        run_31 = run_like(31, 39600000000, conclusion="failure")
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        github.responses[jobs_path(run_31["id"])] = jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure"})
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "closed")
        comment = github.writes[0][2]["body"]
        self.assertIn("rejected by its RED verdict", comment)
        self.assertNotIn("passed on candidate", comment)
        self.assertEqual(len(github.writes), 2)
        self.assertEqual(len(chat.posted), 1)
        self.assertIn("rejected by the eval's RED verdict", chat.posted[0])
        self.assertNotIn("passes again", chat.posted[0])

    def test_a_red_verdict_night_whose_teardown_failed_opens_an_issue_on_the_teardown(self):
        run_31 = run_like(31, 39600000000, conclusion="failure")
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"][2:]))
        github.responses[jobs_path(run_31["id"])] = jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure", TEARDOWN: "failure"})
        self.assertEqual(reconcile(github)[0], "opened")
        self.assertEqual(notifier.stamped_stage(github.writes[1][2]["body"]), TEARDOWN)

    def test_a_red_verdict_night_closes_an_issue_stamped_on_the_eval(self):
        """An issue opened because the eval never settled: a RED is the eval
        answering, so the stage ran, and the issue closes."""
        run_31 = run_like(31, 39600000000, conclusion="failure")
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]], stage=EVAL_STAGE))])
        github.responses[jobs_path(run_31["id"])] = jobs_with(STAGING_26_JOBS, **{EVAL_STAGE: "failure"})
        self.assertEqual(reconcile(github)[0], "closed")

    def test_a_green_run_closes_an_issue_that_recorded_no_stage(self):
        """No stage on record means nothing to hold the close to."""
        run_31 = run_like(31, 39600000000)
        github = staging_github(runs=runs_list(run_31, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]], stage=None))])
        github.responses[jobs_path(run_31["id"])] = {"total_count": 1, "jobs": [dict(GREEN_JOBS["jobs"][0])]}
        self.assertEqual(reconcile(github)[0], "closed")

    def test_a_green_run_with_no_issue_writes_nothing(self):
        github = staging_github(runs=runs_list(run_like(31, 39600000000), *STAGING_RUNS["workflow_runs"]))
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])

    def test_a_red_run_being_re_run_decides_nothing(self):
        """A re-run moves the run out of `completed`; read only completed
        runs, the green before it would be the newest and close the issue
        citing a run that finished before the failure."""
        re_running = run_like(30, RUN_30["id"], conclusion=None, status="in_progress", attempt=2)
        github = staging_github(runs=runs_list(re_running, *STAGING_RUNS["workflow_runs"][1:]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        chat = FakeChat()
        outcome, err = reconcile(github, chat)
        self.assertEqual(outcome, "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])
        self.assertIn("run #30 is in_progress", err)

    def test_a_newer_run_still_queued_decides_nothing(self):
        queued = run_like(31, 39600000000, conclusion=None, status="queued")
        github = staging_github(runs=runs_list(queued, *STAGING_RUNS["workflow_runs"]))
        outcome, err = reconcile(github)
        self.assertEqual(outcome, "nothing")
        self.assertEqual(github.writes, [])
        self.assertIn("run #31 is queued", err)

    def test_a_run_stuck_past_the_ceiling_is_a_warning_annotation_and_still_decides_nothing(self):
        """A run waiting on an environment approval nobody gives would
        otherwise silence the sweep for its workflow with no bound."""
        queued = dict(run_like(31, 39600000000, conclusion=None, status="queued"), created_at="2026-10-21T02:00:00Z")
        github = staging_github(runs=runs_list(queued, *STAGING_RUNS["workflow_runs"]))
        annotate = {notifier.ANNOTATE_ENV: notifier.SWITCH_ON}
        sender = notifier.Sender(space="spaces/AAA", token="t", opener=FakeChat())
        fresh = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)
        stuck = fresh + notifier.STUCK_RUN_AFTER + timedelta(minutes=1)
        for now, annotated in ((fresh, False), (stuck, True)):
            with self.subTest(now=now), mock.patch("sys.stderr", new=io.StringIO()) as err, mock.patch("sys.stdout", new=io.StringIO()) as out:
                self.assertEqual(notifier.reconcile(github.api(), STAGING, sender, dry_run=False, environ=annotate, now=now), "nothing")
                self.assertEqual(github.writes, [])
                self.assertIn("run #31 is queued", err.getvalue())
                self.assertEqual("::warning::" in out.getvalue(), annotated)
                if annotated:
                    self.assertIn(queued["html_url"], out.getvalue())
        with mock.patch("sys.stderr", new=io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()) as out:
            notifier.reconcile(github.api(), STAGING, sender, dry_run=False, environ={}, now=stuck)
        self.assertNotIn("::warning::", out.getvalue())

    def test_a_stuck_run_is_aged_from_its_latest_attempt_not_its_dispatch(self):
        """A re-run keeps the run's `created_at`; `run_started_at` moves."""
        now = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
        rerun = {"created_at": "2026-10-20T22:00:00Z", "run_started_at": "2026-10-21T08:30:00Z"}
        self.assertEqual(notifier.run_age(rerun, now), timedelta(minutes=30))
        self.assertEqual(notifier.run_age({"created_at": "2026-10-20T22:00:00Z"}, now), timedelta(hours=11))
        self.assertIsNone(notifier.run_age({}, now))

    def test_a_stamp_a_person_edited_reads_as_no_stamp_not_as_an_error(self):
        for stamp in ("1::2", ":2", "1:2:3", ":", "1,,2", "x1"):
            with self.subTest(stamp=stamp):
                self.assertEqual(notifier.stamped_attempts(f"<!-- release-stage-broken runs={stamp} -->"), set())
        self.assertEqual(notifier.stamped_attempts("<!-- release-stage-broken runs=1,2:3 -->"), {(1, 1), (2, 3)})
        self.assertEqual(notifier.stamped_attempts("<!-- release-stage-broken runs= -->"), set())

    def test_the_same_attempt_seen_again_is_already_recorded(self):
        github = staging_github(runs=runs_list(RUN_30, *STAGING_RUNS["workflow_runs"][1:]), open_issues=[issue(4001, body_with([RUN_29["id"], f"{RUN_30['id']}:1"]))])
        self.assertEqual(reconcile(github)[0], "nothing")
        self.assertEqual(github.writes, [])

    def test_a_re_run_that_fails_again_is_a_new_attempt_and_rewrites_the_body(self):
        """A second attempt can fail on another stage; the body says what
        the pipeline is failing on now, and the stamp carries the attempt."""
        failed_again = run_like(30, RUN_30["id"], conclusion="failure", attempt=2)
        github = staging_github(runs=runs_list(failed_again, *STAGING_RUNS["workflow_runs"][1:]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        github.responses[jobs_path(RUN_30["id"])] = jobs_with(STAGING_26_JOBS, completed={WITHDRAWAL: "2026-10-17T05:12:00Z"}, **{EVAL_STAGE: "failure", WITHDRAWAL: "success"})
        self.assertEqual(reconcile(github)[0], "updated")
        self.assertIn("Still failing", github.writes[0][2]["body"])
        body = github.writes[1][2]["body"]
        self.assertEqual(notifier.stamped_stage(body), EVAL_STAGE)
        self.assertEqual(notifier.stamped_attempts(body), {(RUN_29["id"], 1), (RUN_30["id"], 2)})

    def test_a_failing_dispatch_from_a_branch_opens_nothing(self):
        github = staging_github(runs=runs_list(branch_run(31, 39600000000), *STAGING_RUNS["workflow_runs"][2:]))
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])

    def test_a_passing_dispatch_from_a_branch_closes_nothing(self):
        github = staging_github(runs=runs_list(branch_run(31, 39600000000, conclusion="success"), *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])

    def test_a_branch_dispatch_in_progress_does_not_silence_the_sweep(self):
        github = staging_github(runs=runs_list(branch_run(31, 39600000000, conclusion=None, status="in_progress"), *STAGING_RUNS["workflow_runs"]))
        self.assertEqual(reconcile(github)[0], "opened")

    def test_a_re_run_that_passes_is_the_recovery(self):
        """The failing run passing on its own candidate ends the streak, as
        it does for the broken-main notifier; the stage has to have run."""
        passed = run_like(30, RUN_30["id"], conclusion="success", attempt=2)
        github = staging_github(runs=runs_list(passed, *STAGING_RUNS["workflow_runs"][1:]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        github.responses[jobs_path(RUN_30["id"])] = GREEN_JOBS
        github.logs[1] = fixture("staging-30-step1.log")
        self.assertEqual(reconcile(github)[0], "closed")
        self.assertEqual(github.writes[1][2], {"state": "closed"})

    def test_a_green_older_than_the_recorded_failure_is_no_recovery(self):
        """A page that lacks the failing run (the history endpoint has
        answered short pages) must not close on the green before it."""
        github = staging_github(runs=runs_list(*STAGING_RUNS["workflow_runs"][2:]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        outcome, err = reconcile(github)
        self.assertEqual(outcome, "nothing")
        self.assertEqual(github.writes, [])
        self.assertIn("older than the failing run", err)

    def test_a_red_run_a_person_closed_the_issue_for_is_not_refiled(self):
        closed = issue(3999, body_with([RUN_29["id"], RUN_30["id"]]), state="closed")
        github = staging_github(closed_issues=[closed])
        chat = FakeChat()
        self.assertEqual(reconcile(github, chat)[0], "nothing")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])

    def test_every_closed_issue_is_checked_not_only_the_newest(self):
        newest = issue(3999, body_with([RUN_29["id"]]), state="closed")
        older = issue(3990, body_with([RUN_30["id"]]), state="closed")
        github = staging_github(closed_issues=[newest, older])
        self.assertEqual(reconcile(github)[0], "nothing")
        self.assertEqual(github.writes, [])

    def test_a_newer_red_run_after_a_closed_issue_opens_a_fresh_one(self):
        github = staging_github(closed_issues=[issue(3999, body_with([RUN_29["id"]]), state="closed")])
        self.assertEqual(reconcile(github)[0], "opened")
        self.assertEqual(github.writes[1][0], "POST")

    def test_a_cancelled_newest_run_says_nothing(self):
        cancelled = run_like(31, 39600000000, conclusion="cancelled")
        github = staging_github(runs=runs_list(cancelled, *STAGING_RUNS["workflow_runs"]), open_issues=[issue(4001, body_with([RUN_29["id"], RUN_30["id"]]))])
        self.assertEqual(reconcile(github)[0], "nothing")
        self.assertEqual(github.writes, [])

    def test_an_unreadable_log_still_files_with_the_candidate_unknown(self):
        github = staging_github(logs={})
        self.assertEqual(reconcile(github)[0], "opened")
        body = github.writes[1][2]["body"]
        self.assertIn(notifier.CANDIDATE_UNKNOWN, body)
        self.assertIn("Execute Blocking E2E Gate", body)

    def test_an_unconfigured_sender_files_the_issue_and_posts_nothing(self):
        github = staging_github()
        sender = notifier.Sender(opener=FakeChat())
        with mock.patch("sys.stderr", new=io.StringIO()) as err, mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(notifier.reconcile(github.api(), STAGING, sender, dry_run=False, environ={}), "opened")
        self.assertEqual(github.writes[1][0], "POST")
        self.assertIn(notifier.NOT_CONFIGURED, err.getvalue())
        self.assertNotIn("::warning::", out.getvalue())

    def test_a_chat_the_workflow_expected_and_could_not_send_is_a_warning_annotation(self):
        """A persistent mint failure would otherwise show nowhere: the issue
        files, the log line scrolls past, and no one hears from Chat."""
        expected = {notifier.CHAT_EXPECTED_ENV: notifier.SWITCH_ON}
        github = staging_github()
        with mock.patch("sys.stderr", new=io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(notifier.reconcile(github.api(), STAGING, notifier.Sender(opener=FakeChat()), dry_run=False, environ=expected), "opened")
        self.assertEqual(github.writes[1][0], "POST")
        self.assertIn("::warning::chat not configured", out.getvalue())

        def refused(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

        github = staging_github()
        with mock.patch("sys.stderr", new=io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(notifier.reconcile(github.api(), STAGING, notifier.Sender(space="spaces/AAA", token="t", opener=refused), dry_run=False, environ=expected), "opened")
        self.assertEqual(github.writes[1][0], "POST")
        self.assertIn("::warning::chat post failed", out.getvalue())

    def test_a_dry_run_writes_and_posts_nothing(self):
        github = staging_github()
        chat = FakeChat()
        outcome, err = reconcile(github, chat, dry_run=True)
        self.assertEqual(outcome, "opened")
        self.assertEqual(github.writes, [])
        self.assertEqual(chat.posted, [])
        self.assertIn("would open an issue", err)
        self.assertIn("would post to chat", err)

    def test_a_transient_server_error_on_a_read_is_retried(self):
        github = staging_github()
        failures = [1]
        real = github.opener

        def flaky(request):
            if failures and request.full_url.endswith(runs_path()):
                failures.pop()
                raise urllib.error.HTTPError(request.full_url, 502, "bad gateway", {}, None)
            return real(request)

        github.opener = flaky
        self.assertEqual(reconcile(github)[0], "opened")

    def _three_watched(self):
        """The three watched workflows, two with no runs yet."""
        workflows = [{"id": index + 1, "name": name} for index, name in enumerate(notifier.WATCHED_WORKFLOWS)]
        responses = {f"/repos/{REPO}/actions/workflows?per_page=100&page=1": {"total_count": 3, "workflows": workflows}}
        for workflow in workflows[1:]:
            responses[runs_path(workflow["id"])] = {"total_count": 0, "workflow_runs": []}
        return workflows, FakeGitHub(responses)

    def test_one_pipelines_incident_does_not_stop_the_others_and_is_a_warning_not_a_red(self):
        """A 5xx past the client's retries on one pipeline's runs read: the
        other two are reconciled, the job stays green, and the warning
        annotation says which one waits for the next sweep."""
        workflows, github = self._three_watched()
        real = github.opener

        def down(request):
            if request.full_url.endswith(runs_path(workflows[0]["id"])):
                raise urllib.error.HTTPError(request.full_url, 502, "bad gateway", {}, None)
            return real(request)

        github.opener = down
        with mock.patch("sys.stderr", new=io.StringIO()) as err, mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = notifier.main(["--sweep"], environ={notifier.ANNOTATE_ENV: notifier.SWITCH_ON}, api=github.api())
        self.assertEqual(code, 0)
        for workflow in workflows[1:]:
            self.assertIn(f"{workflow['name']}: nothing", err.getvalue())
        self.assertIn(f"::warning::Reconciling {workflows[0]['name']} failed", out.getvalue())

    def test_a_refusal_on_one_pipeline_reds_the_run_after_the_others_are_reconciled(self):
        workflows, github = self._three_watched()  # the first workflow's runs path is unanswered: a 404
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            code = notifier.main(["--sweep"], environ={}, api=github.api())
        self.assertEqual(code, 1)
        for workflow in workflows[1:]:
            self.assertIn(f"{workflow['name']}: nothing", err.getvalue())
        self.assertIn(f"GitHub refused a request while reconciling {workflows[0]['name']}", err.getvalue())

    def test_a_read_the_api_refuses_writes_nothing_and_reds_the_run(self):
        """A 404 on the workflows list is not retried; the sweep writes nothing
        and exits 1 so the job is red. (The retried 502 is the test above.)"""
        github = FakeGitHub({})
        with mock.patch("sys.stderr", new=io.StringIO()):
            code = notifier.main(["--sweep"], environ={}, api=github.api())
        self.assertEqual(code, 1)
        self.assertEqual(github.writes, [])


class MainTest(unittest.TestCase):
    def test_a_missing_token_is_refused_before_any_read(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.main(["--sweep"], environ={}), 1)
        self.assertIn("GITHUB_TOKEN or GH_TOKEN", err.getvalue())

    def test_a_run_of_an_unwatched_workflow_is_declined(self):
        github = FakeGitHub({f"/repos/{REPO}/actions/runs/5": {"id": 5, "name": "Docker Build", "workflow_id": 9}})
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.main(["--run-id", "5"], environ={}, api=github.api()), 0)
        self.assertIn("not watched", err.getvalue())
        self.assertEqual(github.writes, [])

    def test_a_run_id_from_a_branch_is_declined(self):
        github = FakeGitHub({f"/repos/{REPO}/actions/runs/6": {"id": 6, "name": STAGING["name"], "workflow_id": STAGING["id"], "head_branch": "feat/try-it"}})
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.main(["--run-id", "6"], environ={}, api=github.api()), 0)
        self.assertIn("not a main run", err.getvalue())
        self.assertEqual(github.writes, [])

    def test_a_run_id_reconciles_that_workflow(self):
        github = staging_github()
        github.responses[f"/repos/{REPO}/actions/runs/{RUN_30['id']}"] = {"id": RUN_30["id"], "name": STAGING["name"], "workflow_id": STAGING["id"], "head_branch": "main"}
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(notifier.main(["--run-id", str(RUN_30["id"])], environ={}, api=github.api(), opener=FakeChat()), 0)
        self.assertEqual(github.writes[1][0], "POST")

    def test_the_sweep_reds_on_a_watched_name_that_matches_no_workflow(self):
        github = FakeGitHub({f"/repos/{REPO}/actions/workflows?per_page=100&page=1": {"total_count": 1, "workflows": [{"id": 1, "name": "Docker Build"}]}})
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(notifier.main(["--sweep"], environ={}, api=github.api()), 1)
        self.assertIn("renamed?", err.getvalue())


class WorkflowContractTest(unittest.TestCase):
    """The watched list matches the workflow files, and the notifier
    workflow has the shape the script and the rules assume."""

    def _document(self, path):
        document = yaml.safe_load(path.read_text())
        return document, (document.get("on") or document.get(True) or {})

    def test_every_watched_name_is_a_release_workflow_in_the_tree(self):
        names = {}
        for path in WORKFLOWS.glob("*.yml"):
            document, _ = self._document(path)
            names[document.get("name")] = path.name
        self.assertEqual(
            {names.get(name) for name in notifier.WATCHED_WORKFLOWS},
            {"rc-release-pipeline.yml", "staging-promotion-pipeline.yml", "release-publish.yml"},
        )

    def test_the_notifier_sweeps_on_a_schedule_and_never_on_workflow_run(self):
        """B4 in tests/conformance forbids the alternative: a workflow_run
        job holding id-token: write has to gate on a successful run."""
        document, triggers = self._document(NOTIFIER_WORKFLOW)
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("workflow_run", triggers)
        self.assertEqual(set(triggers["workflow_dispatch"]["inputs"]), {"dry_run", "run_id"})
        self.assertTrue(triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"])

    def test_every_job_is_guarded_and_runs_the_script(self):
        document, _ = self._document(NOTIFIER_WORKFLOW)
        jobs = document["jobs"]
        self.assertTrue(jobs)
        for name, job in jobs.items():
            self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"], name)
            self.assertEqual(job["permissions"]["issues"], "write", name)
            self.assertEqual(job["permissions"]["id-token"], "write", name)
        text = NOTIFIER_WORKFLOW.read_text()
        self.assertIn("scripts/release/notify_release_stage_broken.py", text)
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", text)
        self.assertTrue((_RELEASE / "notify_release_stage_broken.py").exists())

    def test_every_chat_step_is_best_effort(self):
        """A Chat fault costs the post, never the reconcile: the auth, the
        SDK setup and the mint all continue on error, and the reconcile step
        has no `if:` that could skip it."""
        document, _ = self._document(NOTIFIER_WORKFLOW)
        steps = {step.get("id") or step.get("name"): step for step in document["jobs"]["notify"]["steps"]}
        for step_id in ("auth", "gcloud", "chat_token"):
            self.assertTrue(steps[step_id].get("continue-on-error"), step_id)
            self.assertIn("if", steps[step_id], step_id)
        reconcile_step = steps[RECONCILE_STEP]
        self.assertNotIn("if", reconcile_step)
        self.assertIn(notifier.CHAT_EXPECTED_ENV, reconcile_step["env"])
        self.assertEqual(reconcile_step["env"][notifier.ANNOTATE_ENV], notifier.SWITCH_ON)

    def test_the_reconcile_step_maps_its_inputs_to_the_script_flags(self):
        """The step's `run:` block, lifted out of the YAML and run against a
        stub `python3` that echoes its argv: the dispatch's boolean arrives
        as the string "true", a schedule has no inputs at all, and `run_id`
        replaces the sweep."""
        document, _ = self._document(NOTIFIER_WORKFLOW)
        block = next(step["run"] for step in document["jobs"]["notify"]["steps"] if step.get("name") == RECONCILE_STEP)
        cases = [
            ({"DRY_RUN": "true", "RUN_ID": ""}, ["--sweep", "--dry-run"]),
            ({"DRY_RUN": "false", "RUN_ID": ""}, ["--sweep"]),
            ({"DRY_RUN": "", "RUN_ID": ""}, ["--sweep"]),
            ({"DRY_RUN": "true", "RUN_ID": "39533999681"}, ["--run-id", "39533999681", "--dry-run"]),
            ({"DRY_RUN": "false", "RUN_ID": "39533999681"}, ["--run-id", "39533999681"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "python3"
            stub.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            for env, flags in cases:
                with self.subTest(env=env):
                    proc = subprocess.run(
                        ["bash", "-e", "-c", block],
                        env={**os.environ, **env, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.splitlines(), ["scripts/release/notify_release_stage_broken.py", *flags])

    def test_the_chat_variables_match_the_ci_health_poster(self):
        """One space, one identity: the env names and defaults are the
        ones ci-health.yml declares, so the two posters cannot drift."""
        document, _ = self._document(NOTIFIER_WORKFLOW)
        health, _ = self._document(WORKFLOWS / "ci-health.yml")
        for key in ("WIF_PROVIDER", "CI_HEALTH_SA", "CI_HEALTH_CHAT_SPACE", "CI_HEALTH_MUTE"):
            self.assertEqual(document["env"][key], health["env"][key], key)
        self.assertEqual(notifier.SPACE_ENV, "CI_HEALTH_CHAT_SPACE")
        self.assertEqual(notifier.TOKEN_ENV, "CI_HEALTH_CHAT_TOKEN")

    def test_the_runbook_pointer_names_a_heading_and_bullet_that_exist(self):
        readme = (_RELEASE / "README.md").read_text()
        self.assertIn("## Pipeline Cadence & Execution Flow", readme)
        self.assertIn("**When a stage fails", readme)
        self.assertIn("Pipeline Cadence & Execution Flow", notifier.RUNBOOK)
        self.assertIn("When a stage fails", notifier.RUNBOOK)


if __name__ == "__main__":
    unittest.main()
