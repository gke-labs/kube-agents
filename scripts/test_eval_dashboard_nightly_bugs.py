"""nightly_bugs.py: a case failing every graded repetition on three graded
nights in a row gets one `nightly-red` issue, assigned per the hybrid rule,
and one comment when it passes again.

The nights are synthetic, built like test_eval_dashboard_nightly.py's; the
repository facts (task.yaml owners and markers, domains.yaml, the root
OWNERS) come from testdata_nightly_bugs/, copied into a temporary checkout
because a file named OWNERS anywhere in this tree is one Prow reads.
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

from eval_dashboard import nightly_bugs

TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_nightly_bugs"
JOB = "ci-kube-agents-eval-nightly"
WRITERS_JOB = "ci-kube-agents-eval-nightly-writers"
REPO = "gke-labs/kube-agents"
ISSUES = f"repos/{REPO}/issues"
FAIL_REASON = "VerificationCorrectness=0.0 (floor 1.0) -- names-the-pool: required phrases absent"
STORM_REASON = "agent run failed: http 429 RESOURCE_EXHAUSTED"
CASES = ["case-red", "case-red-extended", "case-maint", "case-outsider", "case-reviewer", "case-noowner", "case-xfail", "case-green"]


def rep(n, result, reason=None):
    return {"n": n, "result": result, "reason": reason}


def task(name, *results, reason=FAIL_REASON):
    reps = [rep(i + 1, r, None if r == "pass" else (STORM_REASON if r == "infra" else reason)) for i, r in enumerate(results)]
    return {"name": name, "result": "pass" if all(r == "pass" for r in results) else "fail", "reps": reps}


# Passing cases every night carries beside the ones under test, so a night
# with a red or two is not a run-level event (classify.is_run_event).
FILLER = ("filler-1", "filler-2", "filler-3", "filler-4")


def night(day, tasks, build=None, job=JOB, truncated=False, hour="00", filler=True):
    """One nightly build started at <hour>:00 UTC on 2026-10-<day>."""
    tasks = list(tasks) + ([green(name) for name in FILLER] if filler else [])
    build = build or f"30000000000000{day:02d}{hour}0{1 if job == JOB else 2}"
    run = {"build_id": build, "tier": "nightly", "job": job, "pr": None, "started": f"2026-10-{day:02d}T{hour}:00:30+00:00",
           "finished": f"2026-10-{day:02d}T{hour}:59:00+00:00", "result": "FAILURE" if truncated else "SUCCESS",
           "eval_verdict": None if truncated else "GREEN", "duration_s": 3500, "tasks": tasks}
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

    def __init__(self, open_issues=(), comments=None, fail_list=False, refuse_assignees=False):
        self.calls = []
        self.open_issues = list(open_issues)
        self.comments = comments or {}
        self.fail_list = fail_list
        self.refuse_assignees = refuse_assignees
        self.next_number = 4000

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        body = json.loads(input) if input else None
        self.calls.append((method, path, body))
        if method == "GET" and "/comments" in path:
            number = int(path.split("/issues/")[1].split("/")[0])
            return GhResult(stdout="\n".join(json.dumps(c) for c in self.comments.get(number, [])))
        if method == "GET":
            if self.fail_list:
                return GhResult(rc=1, stderr="HTTP 502")
            return GhResult(stdout="\n".join(json.dumps(i) for i in self.open_issues))
        if self.refuse_assignees and body and body.get("assignees"):
            return GhResult(rc=1, stderr="HTTP 422: Validation Failed (assignees)")
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
        (self.root / "OWNERS.fixture").rename(self.root / "OWNERS")
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

    def filed(self, gh):
        return [b for m, p, b in gh.writes() if m == "POST" and p == ISSUES]


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
        gh, out, _ = self.run_once(data(*(night(d, [red("case-xfail")]) for d in (15, 16, 17))))
        self.assertEqual(gh.writes(), [])
        self.assertIn("skipped: case-xfail (expected_fail: true)", out)

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
        # Without the real reds, the infra nights alone file nothing.
        gh, _, _ = self.run_once(data(night(15, [all_infra]), night(16, [all_infra]), night(17, [all_infra])))
        self.assertEqual(gh.writes(), [])

    def test_run_level_event_night_is_skipped(self):
        # Every graded case failed on the 17th: a run-level event, which says
        # nothing about case-red.
        gh, _, _ = self.run_once(data(night(15, [red(), green()]), night(16, [red(), green()]), night(17, [red(), red("case-green")], filler=False)))
        self.assertEqual(self.filed(gh), [])

    def test_a_same_date_rerun_is_one_night(self):
        gh, _, _ = self.run_once(data(night(16, [red()]), night(17, [red()]), night(17, [red()], hour="09")))
        self.assertEqual(self.filed(gh), [])


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

    def test_an_issue_naming_a_longer_case_name_is_not_a_match(self):
        issue = {"number": 3997, "title": "case-red-extended flakes", "body": nightly_bugs.MARKER.format(case="case-red-extended")}
        gh, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[issue]))
        self.assertEqual(len(self.filed(gh)), 1)

    def test_a_second_pass_after_filing_does_not_file_again(self):
        gh, _, _ = self.run_once(self.THREE_RED())
        [body] = self.filed(gh)
        again, _, _ = self.run_once(self.THREE_RED(), FakeGh(open_issues=[{"number": 4000, "title": body["title"], "body": body["body"]}]))
        self.assertEqual(again.writes(), [])

    def test_unreadable_open_issues_write_nothing(self):
        gh, _, err = self.run_once(self.THREE_RED(), FakeGh(fail_list=True))
        self.assertEqual(gh.writes(), [])
        self.assertIn("could not list the open nightly-red issues", err)


class RecoveryTest(Harness):
    ISSUE: typing.ClassVar[dict] = {"number": 3990, "title": "Nightly: case-red", "body": nightly_bugs.MARKER.format(case="case-red")}

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
    def setUp(self):
        super().setUp()
        self.owners = nightly_bugs.load_owners(self.root)

    def test_a_case_owner_in_owners_is_assigned(self):
        self.assertEqual(nightly_bugs.resolve_assignee("alice", "carol", self.owners)[0], "alice")
        self.assertEqual(nightly_bugs.resolve_assignee("bob", "carol", self.owners)[0], "bob", "reviewers count, case-insensitively")

    def test_maintainers_or_no_owner_goes_to_the_domain_owner(self):
        login, why = nightly_bugs.resolve_assignee("maintainers", "carol", self.owners)
        self.assertEqual(login, "carol")
        self.assertIn("`maintainers`", why)
        self.assertEqual(nightly_bugs.resolve_assignee(None, "carol", self.owners)[0], "carol")

    def test_an_owner_not_in_owners_goes_to_the_domain_owner(self):
        login, why = nightly_bugs.resolve_assignee("zed", "sam", self.owners)
        self.assertEqual(login, "sam")
        self.assertIn("not in the root OWNERS file", why)

    def test_end_to_end_each_rule_lands_on_the_issue(self):
        cases = ("case-maint", "case-outsider", "case-noowner")
        gh, _, _ = self.run_once(data(*(night(d, [red(c) for c in cases]) for d in (15, 16, 17))))
        by_case = {b["title"].split()[1]: b for b in self.filed(gh)}
        self.assertEqual(by_case["case-maint"]["assignees"], ["carol"])
        self.assertEqual(by_case["case-outsider"]["assignees"], ["sam"])
        self.assertEqual(by_case["case-noowner"]["assignees"], ["sam"])
        self.assertIn("domain:security", by_case["case-outsider"]["labels"])

    def test_an_unassignable_login_files_unassigned(self):
        gh, _, err = self.run_once(data(*(night(d, [red()]) for d in (15, 16, 17))), FakeGh(refuse_assignees=True))
        attempts = self.filed(gh)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("assignees", attempts[1])
        self.assertIn("@carol", attempts[1]["body"])
        self.assertIn("retrying unassigned", err)


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
