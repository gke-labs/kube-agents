"""nightly_bugs.py: a case failing every graded repetition on three graded
nights in a row gets one `nightly-red` issue, assigned to the case's
effective owner (scripts/case_owner.py), and one comment when it passes again.

The nights are synthetic, built like test_eval_dashboard_nightly.py's; the
repository facts (task.yaml owners and markers, domains.yaml) come from
testdata_nightly_bugs/, copied into a temporary checkout.
"""

import contextlib
import copy
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import typing
import unittest

import yaml
from eval_dashboard import nightly_bugs

TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_nightly_bugs"
JOB = "ci-kube-agents-eval-nightly"
WRITERS_JOB = "ci-kube-agents-eval-nightly-writers"
REPO = "gke-labs/kube-agents"
ISSUES = f"repos/{REPO}/issues"
FAIL_REASON = "VerificationCorrectness=0.0 (floor 1.0) -- names-the-pool: required phrases absent"
STORM_REASON = "agent run failed: http 429 RESOURCE_EXHAUSTED"
CASES = ["case-red", "case-red-extended", "case-maint", "case-outsider", "case-reviewer", "case-noowner", "case-xfail", "case-xfail-cap", "case-green"]
WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-health.yml"
STEP_NAME = "File bugs for persistent nightly failures (dry run unless armed)"


def rep(n, result, reason=None):
    return {"n": n, "result": result, "reason": reason}


def task(name, *results, reason=FAIL_REASON):
    reps = [rep(i + 1, r, None if r == "pass" else (STORM_REASON if r == "infra" else reason)) for i, r in enumerate(results)]
    return {"name": name, "result": "pass" if all(r == "pass" for r in results) else "fail", "reps": reps}


# Passing cases every night carries beside the ones under test, so a night
# with a red or two is not a run-level event (classify.is_run_event).
FILLER = ("filler-1", "filler-2", "filler-3", "filler-4")


def night(day, tasks, build=None, job=JOB, truncated=False, hour="00", filler=True, lost=False):
    """One nightly build started at <hour>:00 UTC on 2026-10-<day>; ``lost``
    is a pod that died under it (no tasks, NodeNotReady)."""
    tasks = [] if lost else list(tasks) + ([green(name) for name in FILLER] if filler else [])
    build = build or f"30000000000000{day:02d}{hour}0{1 if job == JOB else 2}"
    run = {"build_id": build, "tier": "nightly", "job": job, "pr": None, "started": f"2026-10-{day:02d}T{hour}:00:30+00:00",
           "finished": f"2026-10-{day:02d}T{hour}:59:00+00:00", "result": "FAILURE" if truncated else "SUCCESS",
           "eval_verdict": None if truncated else "GREEN", "duration_s": 3500, "tasks": tasks}
    if lost:
        run.update(result="FAILURE", eval_verdict=None, pod_node="node-7", pod_last_event="NodeNotReady")
    return run


def data(*runs, inactive=()):
    cases = [{"name": name, "domain": "x", "active": False, "nightly_active": name not in inactive} for name in CASES]
    return {"schema_version": 1, "generated_at": "2026-10-20T12:00:00+00:00", "cases": cases, "runs": list(runs)}


def green(name="case-green"):
    return task(name, "pass", "pass", "pass")


def red(name="case-red"):
    return task(name, "fail", "fail", "fail")


class GhResult:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class FakeGh:
    """A `gh api` runner: open issues and per-issue comments for reads, a
    freshly numbered object for writes; records every call."""

    def __init__(self, open_issues=(), closed_issues=(), comments=None, fail_list=False, fail_closed=False, refuse_assignees=False, lands_then_fails=False, fail_writes=False, fail_comments=False):
        self.calls = []
        self.open_issues = list(open_issues)
        self.closed_issues = list(closed_issues)
        self.comments = comments or {}
        self.fail_list = fail_list
        self.fail_closed = fail_closed
        self.refuse_assignees = refuse_assignees
        self.lands_then_fails = lands_then_fails
        self.fail_writes = fail_writes
        self.fail_comments = fail_comments
        self.next_number = 4000

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        body = json.loads(input) if input else None
        self.calls.append((method, path, body))
        if method == "GET" and "/comments" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            if self.fail_comments:
                return GhResult(rc=1, stderr="HTTP 502")
            return GhResult(stdout="\n".join(json.dumps(c) for c in self.comments.get(number, [])))
        if method == "GET" and "state=closed" in path:
            if self.fail_closed:
                return GhResult(rc=1, stderr="HTTP 502")
            return GhResult(stdout="\n".join(json.dumps(i) for i in self.closed_issues))
        if method == "GET":
            if self.fail_list:
                return GhResult(rc=1, stderr="HTTP 502")
            return GhResult(stdout="\n".join(json.dumps(i) for i in self.open_issues))
        if self.fail_writes:
            return GhResult(rc=1, stderr="HTTP 403")
        # True refuses every login; a tuple, the logins in it. gh prints the
        # status to stderr and GitHub's JSON, naming the field, to stdout.
        refused = self.refuse_assignees is True or set(body.get("assignees") or []) & set(self.refuse_assignees or ()) if body else False
        if refused and body.get("assignees"):
            errors = [{"value": body["assignees"][0], "resource": "Issue", "field": "assignees", "code": "invalid"}]
            return GhResult(rc=1, stderr="gh: Validation Failed (HTTP 422)", stdout=json.dumps({"message": "Validation Failed", "errors": errors}))
        if self.lands_then_fails and body and body.get("assignees"):
            # The write landed and the call still failed (a timeout, a 5xx).
            self.open_issues.append({"number": 4100, "title": body["title"], "body": body["body"]})
            return GhResult(rc=1, stderr="HTTP 504")
        number = self.next_number
        self.next_number += 1
        return GhResult(stdout=json.dumps({"number": number, "html_url": f"https://github.com/{REPO}/issues/{number}"}))

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "repo"
        shutil.copytree(TESTDATA, self.root)
        self.dir = pathlib.Path(tmp.name)

    def run_once(self, doc, gh=None, live=True):
        gh = gh or FakeGh()
        path = self.dir / "data.json"
        path.write_text(json.dumps(doc))
        argv = ["--data", str(path), "--repo-root", str(self.root)] + (["--live"] if live else [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nightly_bugs.main(argv, gh_runner=gh)
        self.assertEqual(rc, 0, err.getvalue())
        return gh, out.getvalue(), err.getvalue()

    def filed(self, gh, infra=False):
        """The issues POSTed: domain bugs, or with ``infra`` the Infra ones."""
        return [b for m, p, b in gh.writes() if m == "POST" and p == ISSUES and ("Infra" in b["labels"]) == infra]

    def comments(self, gh):
        return [(p, b["body"]) for m, p, b in gh.writes() if m == "POST" and p.endswith("/comments")]


class StreakTest(Harness):
    def test_three_red_nights_file_one_issue_with_labels_assignee_and_evidence(self):
        gh, out, _ = self.run_once(data(night(15, [red(), green()]), night(16, [red(), green()]), night(17, [red(), green()])))
        [issue] = self.filed(gh)
        self.assertEqual(issue["labels"], ["nightly-red", "domain:capacity", "needs-triage"])
        self.assertEqual(issue["assignees"], ["alice"])
        self.assertIn("case-red", issue["title"])
        body = issue["body"]
        self.assertIn("<!-- kube-agents-bot:nightly-red case=case-red -->", body)
        self.assertIn("@carol", body, "the domain owner is mentioned")
        self.assertIn("backup @dave", body)
        for day in (15, 16, 17):
            self.assertIn(f"2026-10-{day}: 0/3 passed", body)
        self.assertIn(f"/logs/{JOB}/30000000000000170001", body, "the run link")
        self.assertIn("eval_case-red_rep1.log", body, "the transcript link")
        self.assertIn("failed: `names-the-pool: required phrases absent`", body, "the check's reason, score prefix dropped")
        self.assertIn("**Broken test:**", body)
        self.assertLess(body.index("2026-10-17"), body.index("2026-10-15"), "newest night first")
        self.assertIn("filed 1,", out)

    def test_two_red_nights_do_not_file(self):
        gh, out, _ = self.run_once(data(night(15, [green("case-red")]), night(16, [red()]), night(17, [red()])))
        self.assertEqual(gh.writes(), [])
        self.assertIn("filed 0,", out)

    def test_a_partial_night_breaks_the_streak(self):
        gh, _, _ = self.run_once(data(night(14, [red()]), night(15, [task("case-red", "pass", "fail", "fail")]), night(16, [red()]), night(17, [red()])))
        self.assertEqual(self.filed(gh), [])
        gh, _, _ = self.run_once(data(night(15, [task("case-red", "pass", "fail", "fail")]), night(16, [red()]), night(17, [red()]), night(18, [red()])))
        self.assertEqual(len(self.filed(gh)), 1, "three full reds after it do file")

    def test_expected_fail_case_is_skipped(self):
        gh, out, _ = self.run_once(data(*(night(d, [red("case-xfail"), red("case-xfail-cap")]) for d in (15, 16, 17))))
        self.assertEqual(gh.writes(), [])
        self.assertIn("skipped: case-xfail (expected_fail: true)", out)
        self.assertIn("skipped: case-xfail-cap (expected_fail: true)", out, "any YAML spelling of true")

    def test_case_off_the_nightly_matrix_is_skipped(self):
        gh, out, _ = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17)), inactive=("case-red",)))
        self.assertEqual(gh.writes(), [])
        self.assertIn("skipped: case-red", out)

    def test_truncated_night_neither_counts_nor_breaks(self):
        # Red on a truncated night does not make the third night.
        gh, _, _ = self.run_once(data(night(15, [red()]), night(16, [red()]), night(17, [red()], truncated=True)))
        self.assertEqual(self.filed(gh), [])
        # A pass on a truncated night does not break the streak either.
        gh, _, _ = self.run_once(data(night(14, [red()]), night(15, [red()]), night(16, [green("case-red")], truncated=True), night(17, [red()])))
        [issue] = self.filed(gh)
        self.assertNotIn("2026-10-16", issue["body"])
        self.assertIn("2026-10-14", issue["body"])

    def test_writers_part_is_judged_on_its_own_truncation(self):
        # The main part cut short; the writers part finished: its case counts.
        nights = [night(d, [green()], truncated=True) for d in (15, 16, 17)]
        writers = [night(d, [red()], job=WRITERS_JOB) for d in (15, 16, 17)]
        gh, _, _ = self.run_once(data(*sorted(nights + writers, key=lambda r: r["build_id"])))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_an_ungraded_line_in_one_part_does_not_hide_the_other_parts_verdict(self):
        # The main part lost case-red to infra; the writers part graded it red.
        nights = [night(d, [task("case-red", "infra", "infra", "infra")]) for d in (15, 16, 17)]
        writers = [night(d, [red()], job=WRITERS_JOB, filler=False) for d in (15, 16, 17)]
        gh, _, _ = self.run_once(data(*sorted(nights + writers, key=lambda r: r["build_id"])))
        self.assertEqual(len(self.filed(gh)), 1)
        self.assertEqual(self.filed(gh, infra=True), [], "graded by writers: not kept from running either")

    def test_infra_reps_are_not_failures_and_an_all_infra_night_is_skipped(self):
        all_infra = task("case-red", "infra", "infra", "infra")
        storm_graded_fail = task("case-red", "fail", "fail", "fail", reason=STORM_REASON)
        # An all-infra night and a night whose "fails" are storm phrasings
        # say nothing: three real reds around them still file.
        gh, _, _ = self.run_once(data(night(13, [red()]), night(14, [all_infra]), night(15, [storm_graded_fail]), night(16, [red()]), night(17, [task("case-red", "fail", "infra", "fail")])))
        [issue] = self.filed(gh)
        self.assertIn("2026-10-17: 0/2 passed, 1 infra", issue["body"])
        self.assertNotIn("2026-10-14", issue["body"])
        self.assertNotIn("2026-10-15", issue["body"])
        # Without the real reds, the infra nights alone file no domain bug
        # (they are an Infra issue: InfraCaseTest).
        gh, _, _ = self.run_once(data(night(15, [all_infra]), night(16, [all_infra]), night(17, [all_infra])))
        self.assertEqual(self.filed(gh), [])

    def test_run_level_event_night_is_skipped(self):
        # Every graded case failed on the 17th: a run-level event, which says
        # nothing about case-red.
        gh, _, _ = self.run_once(data(night(15, [red(), green()]), night(16, [red(), green()]), night(17, [red(), red("case-green")], filler=False)))
        self.assertEqual(self.filed(gh), [])
        # Nor does it break one: reds on either side still make three.
        event = night(16, [red(), red("case-green")], filler=False)
        gh, _, _ = self.run_once(data(night(14, [red(), green()]), night(15, [red(), green()]), event, night(17, [red(), green()])))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_a_same_date_rerun_is_one_night(self):
        gh, _, _ = self.run_once(data(night(16, [red()]), night(17, [red()]), night(17, [red()], hour="09")))
        self.assertEqual(self.filed(gh), [])
        # The newer build's verdict stands, whichever it is.
        reds = (night(15, [red()]), night(16, [red()]))
        gh, _, _ = self.run_once(data(*reds, night(17, [red()]), night(17, [green("case-red")], hour="09")))
        self.assertEqual(self.filed(gh), [], "the green re-run stands")
        gh, _, _ = self.run_once(data(*reds, night(17, [green("case-red")]), night(17, [red()], hour="09")))
        self.assertEqual(len(self.filed(gh)), 1, "the red re-run stands")

    def test_a_reason_cannot_carry_a_bot_marker(self):
        marker = nightly_bugs.MARKER.format(case="case-maint")
        gh, _, _ = self.run_once(data(*(night(d, [task("case-red", "fail", "fail", "fail", reason=f"agent said {marker}")]) for d in (15, 16, 17))))
        [body] = self.filed(gh)
        self.assertNotIn(marker, body["body"])
        self.assertIsNone(nightly_bugs.tracking_issue([{"number": 4000, **body}], "case-maint"), "case-red's issue does not track case-maint")


class DedupeTest(Harness):
    THREE_RED = staticmethod(lambda: data(*(night(d, [red(), green()]) for d in (15, 16, 17))))

    def test_bot_issue_found_by_marker_is_not_filed_again(self):
        issue = {"number": 3999, "title": "edited by a human", "body": nightly_bugs.MARKER.format(case="case-red")}
        gh, out, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[issue]))
        self.assertEqual(gh.writes(), [])
        self.assertIn("tracked: case-red by #3999", out)

    def test_human_issue_naming_the_case_is_adopted(self):
        issue = {"number": 3998, "title": "case-red is broken on main", "body": "see the nightly"}
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[issue]))
        self.assertEqual(gh.writes(), [])

    def test_a_case_name_ending_a_sentence_is_a_whole_word(self):
        for text in ("Case-Red keeps failing", "CASE-RED.", "The nightly keeps failing on case-red.", "fails (see case-red.)", "case-red...", "case-red, again"):
            with self.subTest(text=text):
                gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[{"number": 3998, "title": "nightly red", "body": text}]))
                self.assertEqual(gh.writes(), [])
        for text in ("case-red-extended.", "case-red.v2 fails", "case-red_old"):
            with self.subTest(text=text):
                self.assertFalse(nightly_bugs.names_case({"title": "", "body": text}, "case-red"))

    def test_an_issue_naming_the_case_only_by_its_transcript_link_is_adopted(self):
        link = f"https://prow.example/view/gs/bucket/logs/{JOB}/30000000000000170001/artifacts/eval_case-red_rep1.log"
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[{"number": 3994, "title": "nightly failure, see transcript", "body": link}]))
        self.assertEqual(gh.writes(), [])
        # Another case's transcript, or a longer name's, is not this case.
        for text in ("artifacts/eval_case-red_old_rep1.log", "artifacts/eval_case-red-extended_rep2.log", "artifacts/xeval_case-red_rep1.log"):
            with self.subTest(text=text):
                self.assertFalse(nightly_bugs.names_case({"title": "", "body": text}, "case-red"))

    def test_an_issue_naming_a_longer_case_name_is_not_a_match(self):
        issue = {"number": 3997, "title": "case-red-extended flakes", "body": nightly_bugs.MARKER.format(case="case-red-extended")}
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[issue]))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_a_second_pass_after_filing_does_not_file_again(self):
        gh, _, _ = self.run_once(self.THREE_RED())
        [body] = self.filed(gh)
        again, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[{"number": 4000, "title": body["title"], "body": body["body"]}]))
        self.assertEqual(again.writes(), [])

    def test_unreadable_issues_write_nothing(self):
        for gh in (FakeGh(fail_list=True), FakeGh(fail_closed=True)):
            gh, _, err = self.run_once(self.THREE_RED(), gh)
            self.assertEqual(gh.writes(), [])
            self.assertIn("could not list the nightly-red issues", err)

    def test_a_bug_closed_after_the_newest_red_night_is_not_filed_again(self):
        marker = nightly_bugs.MARKER.format(case="case-red")
        # Closed on the 17th after that night's run started (a fix merged).
        closed = {"number": 3996, "title": "t", "body": marker, "closed_at": "2026-10-17T12:00:00Z"}
        gh, out, _ = self.run_once(self.THREE_RED(), FakeGh(closed_issues=[closed]))
        self.assertEqual(gh.writes(), [])
        self.assertIn("tracked: case-red by #3996 (closed, still tracking)", out)
        reads = [p for m, p, _ in gh.calls if m == "GET" and "state=closed" in p]
        self.assertEqual(reads, [f"{ISSUES}?labels=nightly-red&state=closed&since=2026-10-15T00:00:30Z&per_page=100"])
        # A newer failing night after the close files again.
        later = data(*(night(d, [red(), green()]) for d in (15, 16, 17, 18)))
        gh, _, _ = self.run_once(later, FakeGh(closed_issues=[closed]))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_a_bug_handed_off_to_an_infra_issue_is_not_filed_again_until_the_case_passes(self):
        # Closed "not planned" in favour of an infra issue on the 17th; the
        # 18th is red again: still the other issue's problem, nothing filed.
        closed = {"number": 3997, "title": "t", "body": nightly_bugs.MARKER.format(case="case-red"), "closed_at": "2026-10-17T12:00:00Z",
                  "state_reason": "not_planned"}
        later = data(*(night(d, [red(), green()]) for d in (15, 16, 17, 18)))
        gh, out, _ = self.run_once(later, FakeGh(closed_issues=[closed]))
        self.assertEqual(self.filed(gh), [])
        self.assertIn("tracked: case-red by #3997", out)
        # A pass after the close clears it; a new streak after that files.
        cleared = data(*(night(d, [red(), green()]) for d in (15, 16, 17)), night(18, [green("case-red"), green()]),
                       *(night(d, [red(), green()]) for d in (19, 20, 21)))
        gh, _, _ = self.run_once(cleared, FakeGh(closed_issues=[closed]))
        self.assertEqual(len(self.filed(gh)), 1)
        # A hand-off closed before the window's first night cannot be
        # confirmed (the case may have passed since): it holds nothing back.
        old = dict(closed, closed_at="2026-10-01T12:00:00Z")
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(closed_issues=[old]))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_a_bug_closed_while_the_newest_red_night_ran_is_filed_again(self):
        # Closed at 00:30 on the 17th, between that night's start (00:00:30)
        # and its finish (00:59): the close did not see the night's verdict.
        closed = {"number": 3996, "title": "t", "body": nightly_bugs.MARKER.format(case="case-red"), "closed_at": "2026-10-17T00:30:00Z"}
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(closed_issues=[closed]))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_another_cases_bot_issue_quoting_this_case_is_not_a_match(self):
        other = {"number": 3995, "title": "Nightly: case-maint fails every run", "body": nightly_bugs.MARKER.format(case="case-maint") + "\nreason: see case-red too"}
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[other]))
        self.assertEqual(len(self.filed(gh)), 1)


class RecoveryTest(Harness):
    ISSUE: typing.ClassVar[dict] = {"number": 3990, "title": "Nightly: case-red", "body": nightly_bugs.MARKER.format(case="case-red"), "created_at": "2026-10-16T12:00:00Z"}

    def recovered_data(self):
        return data(night(14, [red()]), night(15, [red()]), night(16, [red()]), night(17, [green("case-red")]), night(18, [green("case-red")]))

    def test_recovery_comments_once_with_the_run_link(self):
        gh, out, _ = self.run_once(self.recovered_data(), FakeGh(open_issues=[self.ISSUE]))
        [(_, path, body)] = gh.writes()
        self.assertEqual(path, f"{ISSUES}/3990/comments")
        self.assertIn("2026-10-17 nightly", body["body"], "the first passing night, not the newest")
        self.assertIn(f"/logs/{JOB}/30000000000000170001", body["body"])
        self.assertIn("will not close", body["body"])
        self.assertIn("recovered: case-red on #3990", out)
        # The next pass reads its own comment and stays quiet.
        again, _, _ = self.run_once(self.recovered_data(), FakeGh(open_issues=[self.ISSUE], comments={3990: [{"body": body["body"]}]}))
        self.assertEqual(again.writes(), [])
        self.assertFalse([c for c in again.calls if c[0] == "PATCH"], "never closes or edits the issue")

    def test_a_same_date_rerun_does_not_comment_on_the_recovery_again(self):
        gh, _, _ = self.run_once(self.recovered_data(), FakeGh(open_issues=[self.ISSUE]))
        [(_, _, posted)] = gh.writes()
        rerun = self.recovered_data()
        rerun["runs"].append(night(17, [green("case-red")], hour="09"))
        again, _, _ = self.run_once(rerun, FakeGh(open_issues=[self.ISSUE], comments={3990: [{"body": posted["body"]}]}))
        self.assertEqual(again.writes(), [], "one night, one recovery comment, whichever build of it")

    def test_a_recovery_night_still_running_when_the_issue_opened_is_commented(self):
        # The 10-17 night started 00:00:30 and finished 00:59; the issue
        # opened in between, so the night's verdict reached it afterwards.
        issue = dict(self.ISSUE, created_at="2026-10-17T00:10:00Z")
        gh, _, _ = self.run_once(self.recovered_data(), FakeGh(open_issues=[issue]))
        [(_, path, body)] = gh.writes()
        self.assertIn("2026-10-17 nightly", body["body"])

    def test_a_recovery_before_the_issue_opened_is_not_commented(self):
        human = {"number": 3989, "title": "case-red is broken", "body": "", "created_at": "2026-10-18T12:00:00Z"}
        gh, out, _ = self.run_once(self.recovered_data(), FakeGh(open_issues=[human]))
        self.assertEqual(gh.writes(), [], "the 10-17 pass predates the issue")
        self.assertNotIn("recovered:", out)

    def test_a_pass_the_issue_already_saw_does_not_hide_a_later_one(self):
        # Opened after the 17th and 18th passes finished; the 19th's pass
        # is the first one news to the issue, and is commented.
        human = {"number": 3989, "title": "case-red is broken", "body": "", "created_at": "2026-10-18T12:00:00Z"}
        doc = self.recovered_data()
        doc["runs"].append(night(19, [green("case-red")]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[human]))
        [(_, path, body)] = gh.writes()
        self.assertEqual(path, f"{ISSUES}/3989/comments")
        self.assertIn("2026-10-19 nightly", body["body"])

    def test_unreadable_comments_are_reported_failed(self):
        _, out, err = self.run_once(self.recovered_data(), FakeGh(open_issues=[self.ISSUE], fail_comments=True))
        self.assertIn("1 failed", out)
        self.assertIn("failed: case-red", out)
        self.assertIn("could not read the comments on #3990", err)

    def test_no_comment_without_an_open_issue_or_while_still_partial(self):
        gh, _, _ = self.run_once(self.recovered_data())
        self.assertEqual(gh.writes(), [])
        partial = data(night(15, [red()]), night(16, [task("case-red", "pass", "fail", "pass")]))
        gh, _, _ = self.run_once(partial, FakeGh(open_issues=[self.ISSUE]))
        self.assertEqual(gh.writes(), [])

    def test_a_new_recovery_after_a_new_red_comments_again(self):
        first = self.recovered_data()
        gh, _, _ = self.run_once(first, FakeGh(open_issues=[self.ISSUE]))
        [(_, _, old)] = gh.writes()
        later = copy.deepcopy(first)
        later["runs"] += [night(19, [red()]), night(20, [green("case-red")])]
        gh, _, _ = self.run_once(later, FakeGh(open_issues=[self.ISSUE], comments={3990: [{"body": old["body"]}]}))
        [(_, _, new)] = gh.writes()
        self.assertIn("2026-10-20 nightly", new["body"])


class AssigneeTest(Harness):
    def test_any_login_that_owns_a_case_is_assigned(self):
        # case_owner.py's rule: any GitHub login, not only OWNERS members.
        self.assertEqual(nightly_bugs.resolve_assignee("alice", "x", {"x": "carol"}), ("alice", nightly_bugs.WHY_CASE_OWNER))
        self.assertEqual(nightly_bugs.resolve_assignee("zed", "x", {"x": "carol"})[0], "zed")

    def test_maintainers_or_no_owner_goes_to_the_domain_owner(self):
        login, why = nightly_bugs.resolve_assignee("maintainers", "x", {"x": "carol"})
        self.assertEqual(login, "carol")
        self.assertIn("`maintainers`", why)
        self.assertEqual(nightly_bugs.resolve_assignee(None, "x", {"x": "carol"})[0], "carol")
        self.assertEqual(nightly_bugs.resolve_assignee(None, "x", {})[0], None)

    def test_end_to_end_each_rule_lands_on_the_issue(self):
        cases = ("case-maint", "case-outsider", "case-noowner")
        gh, _, _ = self.run_once(data(*(night(d, [red(c) for c in cases]) for d in (15, 16, 17))))
        by_case = {b["title"].split()[1]: b for b in self.filed(gh)}
        self.assertEqual(by_case["case-maint"]["assignees"], ["carol"])
        self.assertEqual(by_case["case-outsider"]["assignees"], ["zed"], "any login, as case_owner.py resolves it")
        self.assertEqual(by_case["case-noowner"]["assignees"], ["sam"])
        self.assertIn("domain:security", by_case["case-outsider"]["labels"])

    def test_a_write_that_landed_before_failing_is_not_filed_twice(self):
        doc = data(*(night(d, [red()]) for d in (15, 16, 17)))
        gh = FakeGh(lands_then_fails=True)
        _, out, _ = self.run_once(doc, gh)
        self.assertEqual(len(self.filed(gh)), 1, "a timeout or 5xx is not a refusal: no retry this tick")
        self.assertIn("failed: case-red", out)
        # The next tick's list finds the issue the failed call created.
        gh.calls.clear()
        _, out, _ = self.run_once(doc, gh)
        self.assertEqual(self.filed(gh), [])
        self.assertIn("tracked: case-red by #4100", out)

    def test_a_transient_failure_is_not_retried_unassigned(self):
        gh, out, _ = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17))), FakeGh(fail_writes=True))
        [attempt] = self.filed(gh)
        self.assertEqual(attempt["assignees"], ["alice"])
        self.assertIn("failed: case-red", out)

    def test_a_failed_write_is_reported_failed_not_filed_and_not_called_a_refusal(self):
        _, out, _ = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17))), FakeGh(fail_writes=True))
        self.assertIn("(live): filed 0,", out)
        self.assertIn("1 failed", out)
        self.assertIn(f"  failed: case-red ({nightly_bugs.FAILED_NOTE})", out)
        self.assertNotIn("refused", out)

    def test_a_domain_with_no_owner_says_so_instead_of_mentioning_unknown(self):
        domains = self.root / "docs" / "designs" / "domains.yaml"
        domains.write_text(domains.read_text().replace("    owner: carol\n", ""))
        gh, out, _ = self.run_once(data(*(night(d, [red("case-maint")]) for d in (15, 16, 17))))
        [issue] = self.filed(gh)
        self.assertNotIn("assignees", issue)
        self.assertNotIn("@unknown", issue["body"])
        self.assertIn("No domain owner in `docs/designs/domains.yaml`.", issue["body"])
        self.assertIn("**Unassigned** (`docs/designs/domains.yaml` names no owner for `capacity`).", issue["body"])
        self.assertNotIn("mention @None", out)

    def test_a_refused_case_owner_falls_back_to_the_domain_owner(self):
        gh, out, err = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17))), FakeGh(refuse_assignees=("alice",)))
        first, second = self.filed(gh)
        self.assertEqual(first["assignees"], ["alice"])
        self.assertEqual(second["assignees"], ["carol"])
        self.assertIn("**Assigned to** @carol (domain owner; GitHub refused `alice` as assignee).", second["body"])
        self.assertNotIn("@alice", second["body"], "a refused login is not pinged")
        self.assertNotIn("Assigned to** @alice", second["body"])
        self.assertIn("GitHub refused @alice", err)
        self.assertIn("file #4000: case-red [nightly-red, domain:capacity, needs-triage] assign @carol", out)

    def test_an_unassignable_domain_owner_too_files_unassigned_and_says_so(self):
        gh, out, _ = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17))), FakeGh(refuse_assignees=True))
        attempts = self.filed(gh)
        self.assertEqual([a.get("assignees") for a in attempts], [["alice"], ["carol"], None])
        self.assertIn("**Unassigned** (GitHub refused `alice`, `carol` as assignee). Domain owner: @carol", attempts[2]["body"])
        self.assertNotIn("Assigned to", attempts[2]["body"])
        self.assertIn("unassigned (GitHub refused `alice`, `carol` as assignee)", out)


class DryRunTest(Harness):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        doc = data(*(night(d, [red()]) for d in (15, 16, 17)))
        gh, out, err = self.run_once(doc, live=False)
        self.assertEqual(gh.writes(), [])
        self.assertIn("(dry run): would file 1", out)
        self.assertIn("file: case-red [nightly-red, domain:capacity, needs-triage] assign @alice", out)
        self.assertIn("--dry-run: would POST", err)
        self.assertIn('"assignees"', err, "the printed request carries the assignee")

    def test_dry_run_and_live_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nightly_bugs.parse_args(["--data", "x", "--dry-run", "--live"])


class WorkflowSwitchTest(unittest.TestCase):
    """ci-health.yml's step is the only thing that arms the writes: run its
    shell with each switch setting and a `python3` that prints its argv."""

    def mode(self, live, mute):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        steps = workflow["jobs"]["refresh-and-adjudicate"]["steps"]
        [step] = [s for s in steps if s.get("name") == STEP_NAME]
        self.assertTrue(step.get("continue-on-error"), "never reds the tick")
        self.assertNotIn("if", step, "runs every tick, dry when unarmed")
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "python3"
            fake.write_text('#!/bin/sh\necho "$@"\n')
            fake.chmod(0o755)
            env = {"PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}", "NIGHTLY_BUGS_LIVE": live, "CI_HEALTH_MUTE": mute}
            proc = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()[-1]

    def test_only_an_armed_unmuted_switch_goes_live(self):
        self.assertEqual(self.mode("true", "false"), "--live")
        self.assertEqual(self.mode("false", "false"), "--dry-run")
        self.assertEqual(self.mode("True", "false"), "--dry-run")
        self.assertEqual(self.mode("true", "true"), "--dry-run", "the mute switch disarms it")

    def test_the_switch_defaults_off(self):
        self.assertIn("NIGHTLY_BUGS_LIVE: ${{ vars.NIGHTLY_BUGS_LIVE || 'false' }}", WORKFLOW.read_text())


def infra_task(name, reason=STORM_REASON):
    return task(name, "infra", "infra", "infra", reason=reason)


class LostJobTest(Harness):
    def test_two_lost_of_three_files_one_infra_issue_for_the_job(self):
        doc = data(night(15, [green()], truncated=True), night(16, [green()]), night(17, [], lost=True))
        gh, out, _ = self.run_once(doc)
        [issue] = self.filed(gh, infra=True)
        self.assertEqual(issue["labels"], ["nightly-red", "Infra", "needs-triage"])
        self.assertEqual(issue["assignees"], ["ivan"])
        self.assertIn(JOB, issue["title"])
        self.assertIn("lost 2 of the last 3 nightly dates", issue["title"], "what the rule counts")
        body = issue["body"]
        self.assertIn(f"<!-- kube-agents-bot:nightly-lost job={JOB} -->", body)
        self.assertIn("Infra backup: @iris", body)
        self.assertIn("2026-10-17: the pod was lost (node node-7, last event NodeNotReady)", body)
        self.assertIn("2026-10-16: ran to its verdict", body)
        self.assertIn("2026-10-15: ended before its verdict after 58m (result FAILURE); 5 cases recorded", body)
        self.assertIn(f"/logs/{JOB}/30000000000000150001", body)
        self.assertIn(f"file #4000: {JOB} [nightly-red, Infra, needs-triage] assign @ivan (infra owner); mention @iris", out)

    def test_one_lost_of_three_files_nothing(self):
        gh, _, _ = self.run_once(data(night(15, [green()]), night(16, [green()]), night(17, [green()], truncated=True)))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_each_part_is_judged_on_its_own(self):
        runs = [night(d, [green()]) for d in (15, 16, 17)] + [night(d, [green("case-red")], job=WRITERS_JOB, truncated=d != 16) for d in (15, 16, 17)]
        gh, _, _ = self.run_once(data(*sorted(runs, key=lambda r: r["build_id"])))
        [issue] = self.filed(gh, infra=True)
        self.assertIn(WRITERS_JOB, issue["title"])
        self.assertIn("writers part", issue["body"])

    def test_a_part_that_stopped_running_is_not_judged_on_its_stale_tail(self):
        # The writers job last ran on the 10th-12th, two of them cut off;
        # main ran clean through the 17th. A week-old tail files nothing.
        runs = [night(d, [green()]) for d in range(10, 18)] + [night(d, [green("case-red")], job=WRITERS_JOB, truncated=d != 10) for d in (10, 11, 12)]
        gh, _, _ = self.run_once(data(*sorted(runs, key=lambda r: r["build_id"])))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_job_paused_and_resumed_is_not_filed_from_losses_before_the_pause(self):
        # Writers lost the 10th and 11th, was paused, and ran clean on the
        # 17th; main ran clean throughout.
        runs = [night(d, [green()]) for d in range(10, 18)] + [night(d, [green("case-red")], job=WRITERS_JOB, truncated=d != 17) for d in (10, 11, 17)]
        gh, _, _ = self.run_once(data(*sorted(runs, key=lambda r: r["build_id"])))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_an_open_issue_gets_one_comment_per_later_lost_night_and_one_on_recovery(self):
        issue = {"number": 3980, "title": "t", "body": f"<!-- kube-agents-bot:nightly-lost job={JOB} -->", "created_at": "2026-10-16T12:00:00Z"}
        doc = data(night(15, [green()], truncated=True), night(16, [green()]), night(17, [green()], truncated=True))
        gh, out, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        self.assertEqual(self.filed(gh, infra=True), [], "no duplicate")
        [(path, comment)] = self.comments(gh)
        self.assertEqual(path, f"{ISSUES}/3980/comments")
        self.assertIn("Again on the 2026-10-17 nightly: ended before its verdict", comment)
        self.assertIn("new night: " + JOB, out)
        again, _, _ = self.run_once(doc, FakeGh(open_issues=[issue], comments={3980: [{"body": comment}]}))
        self.assertEqual(again.writes(), [], "once per night")
        # A night lost before the issue was opened is not news.
        older = dict(issue, created_at="2026-10-18T00:00:00Z")
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[older]))
        self.assertEqual(gh.writes(), [])
        # A later clean night: one recovery comment.
        doc["runs"].append(night(18, [green()]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn(f"The main part (`{JOB}`) ran to its verdict on the 2026-10-18 nightly", comment)
        again, _, _ = self.run_once(doc, FakeGh(open_issues=[issue], comments={3980: [{"body": comment}]}))
        self.assertEqual(again.writes(), [])


    def test_a_clean_night_the_new_issue_already_lists_is_not_a_recovery(self):
        doc = data(night(15, [], lost=True), night(16, [green()], truncated=True), night(17, [green()]))
        gh, _, _ = self.run_once(doc)
        [body] = self.filed(gh, infra=True)
        self.assertIn("2026-10-17: ran to its verdict", body["body"])
        issue = {"number": 4000, "title": body["title"], "body": body["body"], "created_at": "2026-10-17T12:00:00Z"}
        gh, out, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        self.assertEqual(gh.writes(), [])
        self.assertNotIn("recovered:", out)
        # The next clean night is news to the issue: one recovery comment.
        doc["runs"].append(night(18, [green()]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("ran to its verdict on the 2026-10-18 nightly", comment)

    def test_a_later_lost_night_is_noted_without_the_threshold_again(self):
        issue = {"number": 3981, "title": "t", "body": f"<!-- kube-agents-bot:nightly-lost job={JOB} -->", "created_at": "2026-10-14T12:00:00Z"}
        doc = data(night(15, [green()]), night(16, [green()]), night(17, [green()], truncated=True))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("Again on the 2026-10-17 nightly", comment)

    def test_a_clean_rerun_the_same_date_rescues_the_night(self):
        doc = data(night(15, [green()], truncated=True), night(16, [green()]), night(17, [green()], truncated=True), night(17, [green()], hour="09"))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])
        # A lost re-run after a clean build does not make the night lost.
        doc = data(night(15, [green()], truncated=True), night(16, [green()]), night(17, [green()]), night(17, [green()], hour="09", truncated=True))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_no_infra_owner_files_unassigned_without_a_stray_mention(self):
        domains = self.root / "docs" / "designs" / "domains.yaml"
        domains.write_text("\n".join(line for line in domains.read_text().splitlines() if not line.startswith("infra_")) + "\n")
        gh, _, _ = self.run_once(data(night(15, [green()], truncated=True), night(16, [green()]), night(17, [], lost=True)))
        [issue] = self.filed(gh, infra=True)
        self.assertNotIn("assignees", issue)
        self.assertNotIn("@unknown", issue["body"])
        self.assertIn("names no `infra_owner`", issue["body"])

    def test_an_infra_case_issue_in_a_domain_with_no_owner_mentions_no_one(self):
        domains = self.root / "docs" / "designs" / "domains.yaml"
        domains.write_text(domains.read_text().replace("    owner: carol\n", ""))
        filer = nightly_bugs.Filer(None, self.root)
        self.assertEqual(filer.infra_domain_owner("capacity"), "no owner in `docs/designs/domains.yaml`")
        self.assertEqual(filer.infra_domain_owner("security"), "owner @sam")


class InfraCaseTest(Harness):
    def test_three_all_infra_nights_file_one_infra_issue_with_the_reason(self):
        gh, _, _ = self.run_once(data(*(night(d, [infra_task("case-red")]) for d in (15, 16, 17))))
        self.assertEqual(self.filed(gh), [], "never a domain bug")
        [issue] = self.filed(gh, infra=True)
        self.assertEqual(issue["labels"], ["nightly-red", "Infra", "needs-triage"])
        self.assertEqual(issue["assignees"], ["ivan"])
        self.assertIn("case-red", issue["title"])
        body = issue["body"]
        self.assertIn("<!-- kube-agents-bot:nightly-infra case=case-red -->", body)
        self.assertIn("Infra backup: @iris", body)
        self.assertIn("owner @carol", body, "the domain owner is named for a hand-off")
        self.assertIn("`3 of 3 repetitions infrastructure: agent run failed: http 429 RESOURCE_EXHAUSTED`", body)
        self.assertIn("eval_case-red_rep1.log", body)

    def test_a_case_with_no_line_on_clean_nights_files(self):
        gh, _, _ = self.run_once(data(night(14, [green("case-red")]), *(night(d, []) for d in (15, 16, 17))))
        [issue] = self.filed(gh, infra=True)
        self.assertIn("no line for the case", issue["body"])
        self.assertNotIn("eval_case-red_rep1.log", issue["body"], "no transcript link for a run that recorded nothing")

    def test_a_case_the_inject_lane_dropped_is_not_kept_from_running(self):
        # case-red ran on the 14th (today mode), then the nightly moved to
        # next, whose inject lane drops it before the fan-out: not kept.
        nights = [night(d, []) for d in (15, 16, 17)]
        for run in nights:
            run["lane_excluded"] = ["case-red"]
        gh, _, _ = self.run_once(data(night(14, [green("case-red")]), *nights))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_lane_excluded_cases_do_not_count_toward_a_pool_wide_night(self):
        # The 14th ran four cases the next-mode lane drops from the 15th on.
        # Each later night has 2 of 5 cases infra: 40%, not pool-wide, though
        # it would read as 6 of 9 if the dropped four counted as no line.
        gone = ("gone-1", "gone-2", "gone-3", "gone-4")
        before = night(14, [green("case-red"), *(green(n) for n in gone)])
        after = []
        for d in (15, 16, 17):
            run = night(d, [infra_task("case-red"), infra_task("filler-1"), *(green(n) for n in FILLER[1:])], filler=False)
            run["lane_excluded"] = list(gone)
            after.append(run)
        gh, _, _ = self.run_once(data(before, *after))
        [issue] = self.filed(gh, infra=True)
        self.assertIn("case-red", issue["title"])

    def test_a_post_flip_matrix_larger_than_the_pre_flip_one_sizes_the_night(self):
        # The post-flip matrix (11) outgrew the pre-flip one less its dropped
        # four (5). A later night with 6 lines, 2 infra, misses 5 of 11:
        # 7 of 11 ungraded is pool-wide and skipped, never 3 of 7.
        gone = ("gone-1", "gone-2", "gone-3", "gone-4")
        new = tuple(f"new-{i}" for i in range(1, 7))
        before = night(14, [green("case-red"), *(green(n) for n in gone)])
        full = night(15, [green("case-red"), *(green(n) for n in new)])
        short = [night(d, [infra_task("case-red"), infra_task("new-1")]) for d in (16, 17, 18)]
        for run in (full, *short):
            run["lane_excluded"] = list(gone)
        gh, _, _ = self.run_once(data(before, full, *short))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_nights_before_a_case_first_ran_do_not_count(self):
        gh, _, _ = self.run_once(data(night(15, []), night(16, []), night(17, [infra_task("case-red")])))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_graded_night_breaks_and_a_lost_night_is_skipped(self):
        gh, _, _ = self.run_once(data(night(14, [infra_task("case-red")]), night(15, [infra_task("case-red")]), night(16, [red()]), night(17, [infra_task("case-red")])))
        self.assertEqual(self.filed(gh, infra=True), [])
        # The part cut short on the 16th: that night is skipped, not counted.
        gh, _, _ = self.run_once(data(night(14, [infra_task("case-red")]), night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")], truncated=True), night(17, [infra_task("case-red")])))
        [issue] = self.filed(gh, infra=True)
        self.assertNotIn("2026-10-16", issue["body"].split("**Nights**")[1].split("Nightly report")[0])

    def test_a_pool_wide_infra_night_is_skipped(self):
        everything = [infra_task(name) for name in ("case-red", *FILLER)]
        gh, _, _ = self.run_once(data(night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")]), night(17, everything, filler=False)))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_infra_and_missing_cases_together_make_a_pool_wide_night(self):
        # 5 cases on a full night; the 17th recorded 3, one of them infra:
        # 1 infra + 2 missing is 3 of 5 graded nothing, half or more.
        doc = data(night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")]),
                   night(17, [infra_task("case-red"), green("filler-1"), green("filler-2")], filler=False))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_graded_line_on_a_pool_wide_night_still_ends_the_streak(self):
        # The 17th is pool-wide, but case-red graded on it: kept on the
        # 15th, 16th and 18th is not three kept nights in a row.
        pool_wide = [green("case-red")] + [infra_task(name) for name in FILLER]
        doc = data(night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")]), night(17, pool_wide, filler=False), night(18, [infra_task("case-red")]))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_part_that_stopped_running_does_not_silence_the_absent_rule(self):
        # The writers job last ran on the 13th; main ran clean without
        # case-red on the 15th-17th after recording it on the 14th.
        doc = data(night(13, []), night(13, [green("filler-1")], job=WRITERS_JOB, filler=False), night(14, [green("case-red")]),
                   *(night(d, []) for d in (15, 16, 17)))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(len(self.filed(gh, infra=True)), 1)

    def test_expected_fail_and_off_matrix_cases_are_not_filed(self):
        doc = data(night(14, [green("case-xfail"), green("case-red")]), *(night(d, []) for d in (15, 16, 17)), inactive=("case-red",))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_an_open_issue_for_a_case_marked_expected_fail_still_gets_its_recovery(self):
        issue = {"number": 3976, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-xfail -->", "created_at": "2026-10-15T12:00:00Z"}
        doc = data(*(night(d, [infra_task("case-xfail")]) for d in (14, 15, 16)), night(17, [green("case-xfail")]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("`case-xfail` was graded on the 2026-10-17 nightly", comment)

    def test_recovery_comments_once_when_the_case_is_graded_again(self):
        issue = {"number": 3970, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->", "created_at": "2026-10-16T12:00:00Z"}
        doc = data(*(night(d, [infra_task("case-red")]) for d in (14, 15, 16)), night(17, [red()]), night(18, [green("case-red")]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(path, comment)] = self.comments(gh)
        self.assertEqual(path, f"{ISSUES}/3970/comments")
        self.assertIn("`case-red` was graded on the 2026-10-17 nightly", comment)
        self.assertEqual(self.filed(gh, infra=True), [])
        again, _, _ = self.run_once(doc, FakeGh(open_issues=[issue], comments={3970: [{"body": comment}]}))
        self.assertEqual(again.writes(), [])

    def test_a_graded_night_the_issue_already_saw_does_not_hide_a_later_one(self):
        # Opened after the 17th was graded: the 18th is the recovery.
        issue = {"number": 3973, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->", "created_at": "2026-10-17T12:00:00Z"}
        doc = data(*(night(d, [infra_task("case-red")]) for d in (14, 15, 16)), night(17, [red()]), night(18, [green("case-red")]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("`case-red` was graded on the 2026-10-18 nightly", comment)

    def test_a_later_kept_night_is_noted_without_a_new_streak(self):
        issue = {"number": 3971, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->", "created_at": "2026-10-14T12:00:00Z"}
        doc = data(night(15, [red()]), night(16, [green("case-red")]), night(17, [infra_task("case-red")]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("Again on the 2026-10-17 nightly: `3 of 3 repetitions infrastructure", comment)

    def test_a_human_issue_about_the_case_neither_blocks_nor_receives_infra(self):
        human = {"number": 3950, "title": "case-red fails on main", "body": "see the nightly", "created_at": "2026-10-01T00:00:00Z"}
        gh, _, _ = self.run_once(data(*(night(d, [infra_task("case-red")]) for d in (15, 16, 17))), FakeGh(open_issues=[human]))
        self.assertEqual(len(self.filed(gh, infra=True)), 1)
        gh, _, _ = self.run_once(data(night(15, [infra_task("case-red")]), night(16, [green("case-red")])), FakeGh(open_issues=[human]))
        self.assertEqual(self.comments(gh), [], "no infra recovery on a human's code issue")

    def test_a_night_that_recorded_almost_nothing_is_skipped(self):
        # The 17th printed a verdict and recorded no cases: not "absent" for
        # every case.
        gh, _, _ = self.run_once(data(night(14, [green("case-red")]), night(15, []), night(16, []), night(17, [], filler=False)))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_night_missing_three_fifths_of_its_cases_is_skipped_and_two_fifths_counts(self):
        # The fullest night (the 14th) recorded 5 cases: 3 missing is half
        # or more graded nothing (skipped), 2 missing is not (counted).
        base = [night(14, [green("case-red")]), night(15, []), night(16, [])]
        two = [green("filler-1"), green("filler-2")]
        gh, _, _ = self.run_once(data(*base, night(17, two, filler=False)))
        self.assertEqual(self.filed(gh, infra=True), [], "3 of 5 missing: skipped")
        gh, _, _ = self.run_once(data(*base, night(17, two + [green("filler-3")], filler=False)))
        self.assertEqual(len(self.filed(gh, infra=True)), 1, "2 of 5 missing: counted")

    def test_exactly_half_infra_is_a_pool_wide_night_and_a_quarter_is_not(self):
        # Four cases every night: the 17th's 2 infra of 4 is exactly INFRA_EVENT_FRACTION.
        base = [night(d, [infra_task("case-red"), green("filler-1"), green("filler-2"), green("filler-3")], filler=False) for d in (15, 16)]
        half = [infra_task("case-red"), infra_task("filler-1"), green("filler-2"), green("filler-3")]
        gh, _, _ = self.run_once(data(*base, night(17, half, filler=False)))
        self.assertEqual(self.filed(gh, infra=True), [], "2 of 4 infra: half or more, skipped")
        quarter = [infra_task("case-red"), green("filler-1"), green("filler-2"), green("filler-3")]
        gh, _, _ = self.run_once(data(*base, night(17, quarter, filler=False)))
        self.assertEqual(len(self.filed(gh, infra=True)), 1, "1 of 4 infra: counted")

    def test_two_builds_of_one_part_on_one_date_both_count(self):
        # Two clean main builds each date with different cases (the main
        # part split in halves): a case on the earlier build is not "absent"
        # because the later one has no line for it.
        runs = [r for d in (15, 16, 17) for r in (night(d, [green("case-red")], filler=False), night(d, [], hour="09"))]
        gh, _, _ = self.run_once(data(*runs))
        self.assertEqual(self.filed(gh, infra=True), [])
        # Kept from running on the earlier build: filed, with that build's links.
        runs = [r for d in (15, 16, 17) for r in (night(d, [infra_task("case-red")], filler=False), night(d, [], hour="09"))]
        gh, _, _ = self.run_once(data(*runs))
        [issue] = self.filed(gh, infra=True)
        self.assertIn(f"/logs/{JOB}/30000000000000170001/artifacts/eval_case-red_rep1.log", issue["body"])

    def test_a_handed_off_infra_issue_tracks_until_the_case_is_graded(self):
        closed = {"number": 3998, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->",
                  "closed_at": "2026-10-16T12:00:00Z", "state_reason": "not_planned"}
        doc = data(*(night(d, [infra_task("case-red")]) for d in (14, 15, 16, 17)))
        gh, _, _ = self.run_once(doc, FakeGh(closed_issues=[closed]))
        self.assertEqual(self.filed(gh, infra=True), [])
        # Closed as completed (fixed), the next kept night files again.
        gh, _, _ = self.run_once(doc, FakeGh(closed_issues=[dict(closed, state_reason="completed")]))
        self.assertEqual(len(self.filed(gh, infra=True)), 1)
        # Graded after the close, then kept again: a new problem, filed.
        doc = data(*(night(d, [infra_task("case-red")]) for d in (14, 15, 16)), night(17, [green("case-red")]),
                   *(night(d, [infra_task("case-red")]) for d in (18, 19, 20)))
        gh, _, _ = self.run_once(doc, FakeGh(closed_issues=[closed]))
        self.assertEqual(len(self.filed(gh, infra=True)), 1)

    def test_a_case_that_changed_parts_is_read_from_the_part_that_ran_it(self):
        # Graded by the writers part on the 11th-16th, then moved to main,
        # whose 17th was cut off after recording it: never kept from running.
        writers = [night(d, [green("case-red")], job=WRITERS_JOB, filler=False) for d in range(11, 17)]
        main = [night(d, []) for d in range(11, 17)] + [night(17, [green("case-red")], truncated=True)]
        gh, _, _ = self.run_once(data(*sorted(writers + main, key=lambda r: r["build_id"])))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_moved_case_is_not_absent_while_its_new_part_has_not_landed(self):
        # Kept on writers on the 14th and 15th, then moved to main: on the
        # 16th and 17th writers ran clean without it and main has no clean
        # build on record (not landed on the 16th, cut off before it on the
        # 17th). The writers part's "no line" is not a kept night.
        writers = [night(d, [infra_task("case-red")] if d < 16 else [], job=WRITERS_JOB) for d in (13, 14, 15, 16, 17)]
        main = [night(d, []) for d in (13, 14, 15)] + [night(17, [], truncated=True)]
        writers[0] = night(13, [green("case-red")], job=WRITERS_JOB)
        gh, _, _ = self.run_once(data(*sorted(writers + main, key=lambda r: r["build_id"])))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_an_open_issue_for_a_case_absent_all_window_keeps_its_notes_and_recovery(self):
        # Recorded on the 1st, absent since; its issue opened on the 4th.
        # The 14-night window no longer holds a line for it.
        issue = {"number": 3975, "title": "t", "body": f"<!-- kube-agents-bot:nightly-infra case=case-red --> (`{JOB}`)", "created_at": "2026-10-04T12:00:00Z"}
        runs = [night(1, [green("case-red")])] + [night(d, []) for d in range(2, 18)]
        gh, _, _ = self.run_once(data(*runs), FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("Again on the 2026-10-17 nightly", comment)
        gh, _, _ = self.run_once(data(*runs, night(18, [green("case-red")])), FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("`case-red` was graded on the 2026-10-18 nightly", comment)

    def test_a_lost_half_is_not_vouched_for_by_a_clean_sibling_job(self):
        # Two jobs of the main part each date: the first runs clean, the
        # second (which runs case-red) is cut off. case-red's job did not run
        # to its verdict, so the night is skipped, not counted as "absent".
        half = JOB + "-b"
        both_clean = [night(14, []), night(14, [green("case-red")], job=half, hour="09")]
        runs = [r for d in (15, 16, 17) for r in (night(d, []), night(d, [green("case-red")], job=half, hour="09", truncated=True))]
        gh, _, _ = self.run_once(data(*both_clean, *runs))
        self.assertEqual(self.filed(gh, infra=True), [])
        # A lost build re-run clean by the same job still counts.
        runs = [r for d in (15, 16, 17) for r in (night(d, [], lost=True), night(d, [infra_task("case-red")], hour="09"))]
        gh, _, _ = self.run_once(data(*runs))
        self.assertEqual(len(self.filed(gh, infra=True)), 1)

    def test_a_rerun_lost_to_infra_does_not_undo_a_graded_line(self):
        # The 00:00 build graded case-red on the 17th; a 09:00 re-run lost
        # it to infra. The night is graded, as the domain reader reads it.
        doc = data(night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")]), night(17, [green("case-red")]),
                   night(17, [infra_task("case-red")], hour="09"))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])
        # The mirror: kept on the cron build, graded on the re-run, is graded.
        doc = data(night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")]), night(17, [infra_task("case-red")]),
                   night(17, [green("case-red")], hour="09"))
        gh, _, _ = self.run_once(doc)
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_a_verdict_carries_the_clock_of_the_build_that_recorded_it(self):
        # case-red is kept on the 00:00 build (finished 00:59); the 09:00
        # build has no line for it. An issue opened at 05:00 already saw the
        # 17th's verdict, so the night is not news to it.
        issue = {"number": 3974, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->", "created_at": "2026-10-17T05:00:00Z"}
        runs = [r for d in (15, 16, 17) for r in (night(d, [infra_task("case-red")], filler=False), night(d, [], hour="09"))]
        gh, _, _ = self.run_once(data(*runs), FakeGh(open_issues=[issue]))
        self.assertEqual(self.comments(gh), [])

    def test_a_case_recorded_only_before_the_window_is_new_again(self):
        # Recorded on the 1st, then off the matrix for the 14 nights on
        # record and re-listed: not kept from running on nights it was not
        # on the list.
        gh, _, _ = self.run_once(data(night(1, [green("case-red")]), *(night(d, []) for d in range(2, 18))))
        self.assertEqual(self.filed(gh, infra=True), [])

    def test_only_the_newest_kept_night_is_noted(self):
        # The issue footer and docs promise a note on the newest night a tick
        # sees; an older kept night after the issue opened is not walked.
        issue = {"number": 3972, "title": "t", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->", "created_at": "2026-10-14T12:00:00Z"}
        doc = data(night(15, [infra_task("case-red")]), night(16, [infra_task("case-red")]), night(17, [infra_task("case-red")]))
        gh, _, _ = self.run_once(doc, FakeGh(open_issues=[issue]))
        [(_, comment)] = self.comments(gh)
        self.assertIn("2026-10-17", comment)

    def test_a_domain_bug_is_not_taken_for_the_infra_issue_or_the_reverse(self):
        infra_issue = {"number": 3960, "title": "Nightly infra: case-red", "body": "<!-- kube-agents-bot:nightly-infra case=case-red -->", "created_at": "2026-10-20T00:00:00Z"}
        gh, _, _ = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17))), FakeGh(open_issues=[infra_issue]))
        self.assertEqual(len(self.filed(gh)), 1, "the infra issue does not track the domain streak")
        red_issue = {"number": 3961, "title": "t", "body": nightly_bugs.MARKER.format(case="case-red"), "created_at": "2026-10-20T00:00:00Z"}
        gh, _, _ = self.run_once(data(*(night(d, [infra_task("case-red")]) for d in (15, 16, 17))), FakeGh(open_issues=[red_issue]))
        self.assertEqual(len(self.filed(gh, infra=True)), 1)


class RunAsAScript(Harness):
    """ci-health.yml runs nightly_bugs.py by path from the repository root,
    which takes the module's import fallback rather than the package
    import every other test here takes."""

    def test_the_workflow_invocation_runs_dry(self):
        (self.dir / "data.json").write_text(json.dumps(data(*(night(d, [red()]) for d in (15, 16, 17)))))
        script = pathlib.Path(nightly_bugs.__file__).resolve()
        # A `gh` that fails first on PATH: the one GET is a logged warning,
        # never the network, and with no open issues readable nothing is filed.
        fake_gh = self.dir / "gh"
        fake_gh.write_text("#!/bin/sh\necho 'offline test: no gh' >&2\nexit 1\n")
        fake_gh.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PATH"] = f"{self.dir}{os.pathsep}{env.get('PATH', '')}"
        argv = [sys.executable, str(script), "--data", str(self.dir / "data.json"), "--repo-root", str(self.root)]
        proc = subprocess.run(argv, cwd=script.parents[2], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("warning: gh api GET repos/gke-labs/kube-agents/issues?labels=nightly-red", proc.stderr)
        self.assertIn("nightly bugs (dry run): would file 0", proc.stdout)


if __name__ == "__main__":
    unittest.main()
