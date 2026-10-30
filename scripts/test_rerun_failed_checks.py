#!/usr/bin/env python3
"""Unit tests for the `/retest` re-run of failed GitHub Actions checks.

Run: cd scripts && python3 -m unittest test_rerun_failed_checks

Three things here fail green if they go wrong. The per-name state is the one Tide used:
one success clears a check name, one pending holds it, and only a name every
check run failed is red -- read it differently and the command re-runs
superseded runs that block nothing, or misses one that does. The plan decides
how: a run that concluded `failure` by its failed jobs, any other red run
whole, because GitHub's failed-jobs endpoint refuses a run with no failed job.
And the ceiling is what keeps the command a flake retry rather than a way to
grind a real break to green: a run already on its third attempt is left alone,
and the person is told so.
"""

import contextlib
import io
import os
import pathlib
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

import yaml

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import github_api
import rerun_failed_checks as rfc

REPO_ROOT = _HERE.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "retest-actions.yml"
HEAD = "3170d0726e585fb004209d32aeb330c0dc1d6e6e"


# The pull request's head as `GET /pulls/{n}` returns it: the fork branch every PR here comes from.
PR_HEAD = {"sha": HEAD, "ref": "fix-the-thing", "repo": {"full_name": "someone/kube-agents"}}


def run(run_id, conclusion, suite, status="completed", attempt=1, name="Python Unit Tests", event="pull_request", head_branch=PR_HEAD["ref"], head_repo=PR_HEAD["repo"]["full_name"]):
    """A workflow run as the runs list returns it; `suite` ties it to its check runs.
    By default it is the pull request's own: a pull-request event at its head branch and repository."""
    return {
        "id": run_id,
        "name": name,
        "check_suite_id": suite,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "head_sha": HEAD,
        "event": event,
        "head_branch": head_branch,
        "head_repository": {"full_name": head_repo},
    }


def check(check_id, name, conclusion, suite, status="completed", app=rfc.ACTIONS_APP_SLUG):
    """A check run as the commit's check-runs list returns it."""
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "check_suite": {"id": suite},
        "app": {"slug": app},
    }


class FakeAPI(rfc.GitHubAPI):
    """`GitHubAPI` with `get` and `post` faked, so the real pager runs over one pull request,
    paged lists and recorded posts."""

    def __init__(self, pull=None, runs=(), checks=(), per_page=github_api.PER_PAGE, refusing=(), refusal_body=b"", failing=()):
        super().__init__("gke-labs/kube-agents", "x")
        self.refusal_body = refusal_body
        self.failing = set(failing)  # paths that answer 502, as the API failing rather than refusing
        self.pull = pull or {"number": 3245, "state": "open", "head": PR_HEAD, "title": "ci: bump"}
        self.runs = list(runs)
        self.checks = list(checks)
        self.per_page = per_page
        self.refusing = set(refusing)
        self.posts = []

    def _page(self, items, key, query):
        page = int(query["page"])
        start = (page - 1) * self.per_page
        return {"total_count": len(items), key: items[start : start + self.per_page]}

    def get(self, path, tolerate=()):
        if re.search(r"/pulls/\d+$", path):
            return self.pull
        matched = re.search(r"/actions/runs\?(.*)$", path)
        if matched:
            query = dict(part.split("=") for part in matched.group(1).split("&"))
            assert query["head_sha"] == HEAD, path
            return self._page(self.runs, "workflow_runs", query)
        matched = re.search(rf"/commits/{HEAD}/check-runs\?(.*)$", path)
        if matched:
            query = dict(part.split("=") for part in matched.group(1).split("&"))
            assert query["filter"] == "latest", path  # the attempt set the rollup lists; `all` would hide a red re-run behind a green first attempt
            return self._page(self.checks, "check_runs", query)
        raise AssertionError(f"unexpected call: {path}")

    def post(self, path, payload=None, tolerate=()):
        if path in self.failing:
            raise urllib.error.HTTPError(path, 502, "Bad Gateway", {}, io.BytesIO(b""))
        if path in self.refusing:
            raise urllib.error.HTTPError(path, 403, "Forbidden", {}, io.BytesIO(self.refusal_body))
        self.posts.append((path, payload))
        return {}


class StateTest(unittest.TestCase):
    """The reading, kept from Tide, of a check run and of a name with several."""

    def test_a_check_run_reads_as_tide_read_it(self):
        for conclusion in ("success", "neutral", "skipped"):
            self.assertEqual(rfc.STATE_SUCCESS, rfc.check_state(check(1, "x", conclusion, 1)))
        for conclusion in ("failure", "cancelled", "timed_out", "action_required", "stale"):
            self.assertEqual(rfc.STATE_FAILURE, rfc.check_state(check(1, "x", conclusion, 1)))
        self.assertEqual(rfc.STATE_PENDING, rfc.check_state(check(1, "x", None, 1, status="in_progress")))
        self.assertEqual(rfc.STATE_PENDING, rfc.check_state(check(1, "x", None, 1, status="queued")))

    def test_one_success_clears_a_name_and_one_pending_holds_it(self):
        self.assertEqual(rfc.STATE_SUCCESS, rfc.best_state([rfc.STATE_FAILURE, rfc.STATE_PENDING, rfc.STATE_SUCCESS]))
        self.assertEqual(rfc.STATE_PENDING, rfc.best_state([rfc.STATE_FAILURE, rfc.STATE_PENDING]))
        self.assertEqual(rfc.STATE_FAILURE, rfc.best_state([rfc.STATE_FAILURE, rfc.STATE_FAILURE]))

    def test_the_newest_red_check_run_of_a_red_name_is_the_one_kept(self):
        red, pending = rfc.red_check_runs([check(5, "Tests", "cancelled", 50), check(9, "Tests", "failure", 90), check(7, "Tests", "failure", 70)])
        self.assertEqual({"Tests": 9}, {name: entry["id"] for name, entry in red.items()})
        self.assertEqual(0, pending)

    def test_a_superseded_red_check_run_blocks_nothing(self):
        """Concurrency cancelled the older run's job; the newer run's job of the same name passed."""
        red, pending = rfc.red_check_runs([check(1, "Tests", "cancelled", 10), check(2, "Tests", "success", 20), check(3, "Docs", "failure", 30)])
        self.assertEqual(["Docs"], sorted(red))
        self.assertEqual(0, pending)

    def test_a_red_name_with_a_newer_run_still_going_is_pending(self):
        red, pending = rfc.red_check_runs([check(1, "Tests", "failure", 10), check(2, "Tests", None, 20, status="in_progress")])
        self.assertEqual({}, red)
        self.assertEqual(1, pending)


class PlanTest(unittest.TestCase):
    def test_a_failed_run_reruns_its_failed_jobs(self):
        plan = rfc.plan_reruns([check(1, "Tests", "failure", 10)], [run(100, "failure", 10)], PR_HEAD)
        self.assertEqual([(100, rfc.RERUN_FAILED_JOBS, ["Tests"])], [(e.run["id"], e.action, e.check_names) for e in plan.rerun])
        self.assertEqual([], plan.skipped)

    def test_a_cancelled_or_timed_out_run_reruns_whole(self):
        plan = rfc.plan_reruns(
            [check(1, "Tests", "cancelled", 10), check(2, "A2A", "timed_out", 20)],
            [run(100, "cancelled", 10), run(200, "timed_out", 20, name="A2A Module Tests")],
            PR_HEAD,
        )
        self.assertEqual([rfc.RERUN_WHOLE, rfc.RERUN_WHOLE], [entry.action for entry in plan.rerun])

    def test_two_red_jobs_of_one_run_make_one_rerun_naming_both(self):
        plan = rfc.plan_reruns([check(1, "Lint", "failure", 10), check(2, "Tests", "failure", 10)], [run(100, "failure", 10)], PR_HEAD)
        self.assertEqual(1, len(plan.rerun))
        self.assertEqual(["Lint", "Tests"], plan.rerun[0].check_names)

    def test_a_red_check_from_another_app_is_named_and_left_alone(self):
        plan = rfc.plan_reruns([check(1, "AI Review", "failure", 10, app="kube-agents-bot")], [], PR_HEAD)
        self.assertEqual([], plan.rerun)
        self.assertEqual("AI Review", plan.skipped[0].name)
        self.assertIn("kube-agents-bot", plan.skipped[0].reason)

    def test_a_red_check_whose_run_is_not_at_the_head_is_left_alone(self):
        plan = rfc.plan_reruns([check(1, "Tests", "failure", 10)], [run(100, "failure", 99)], PR_HEAD)
        self.assertEqual([], plan.rerun)
        self.assertIn("no workflow run", plan.skipped[0].reason)

    def test_a_red_check_whose_run_is_already_rerunning_is_left_alone(self):
        plan = rfc.plan_reruns([check(1, "Tests", "failure", 10)], [run(100, None, 10, status="in_progress")], PR_HEAD)
        self.assertEqual([], plan.rerun)
        self.assertIn("still running", plan.skipped[0].reason)

    def test_a_run_awaiting_approval_or_an_environment_says_so(self):
        for status, phrase in (("action_required", "awaits approval from someone with Write access"), ("waiting", "waiting on an environment"), ("pending", "waiting on an environment"), ("queued", "still running")):
            with self.subTest(status=status):
                plan = rfc.plan_reruns([check(1, "Tests", "failure", 10)], [run(100, None, 10, status=status)], PR_HEAD)
                self.assertEqual([], plan.rerun)
                self.assertIn(phrase, plan.skipped[0].reason)

    def test_a_run_that_is_not_the_pull_requests_is_named_and_left_alone(self):
        """A head commit that is also a commit on main lists main's push runs; a red one of those is
        not this command's to re-run, whatever its name."""
        others = (
            run(100, "failure", 10, name="Docker Publish", event="push", head_branch="main", head_repo="gke-labs/kube-agents"),
            run(100, "failure", 10, name="Autopush: Deploy", event="workflow_run", head_branch="main", head_repo="gke-labs/kube-agents"),
            run(100, "failure", 10, event="pull_request", head_branch="other-branch"),
            run(100, "failure", 10, event="pull_request", head_repo="else/kube-agents"),
        )
        for other in others:
            with self.subTest(run=f"{other['event']} {other['head_branch']} {other['head_repository']['full_name']}"):
                plan = rfc.plan_reruns([check(1, "Publish", "failure", 10)], [other], PR_HEAD)
                self.assertEqual([], plan.rerun)
                self.assertIn("not this pull request's", plan.skipped[0].reason)
                self.assertIn(other["event"], plan.skipped[0].reason)

    def test_a_pull_request_target_run_at_the_head_is_the_pull_requests(self):
        plan = rfc.plan_reruns([check(1, "Risk", "failure", 10)], [run(100, "failure", 10, event="pull_request_target")], PR_HEAD)
        self.assertEqual(1, len(plan.rerun))

    def test_a_run_at_the_attempt_ceiling_is_reported_not_rerun(self):
        plan = rfc.plan_reruns([check(1, "Tests", "failure", 10)], [run(100, "failure", 10, attempt=rfc.RERUN_ATTEMPT_CEILING)], PR_HEAD)
        self.assertEqual([], plan.rerun)
        self.assertIn(f"attempt {rfc.RERUN_ATTEMPT_CEILING}", plan.skipped[0].reason)

    def test_a_run_one_below_the_ceiling_is_still_rerun(self):
        plan = rfc.plan_reruns([check(1, "Tests", "failure", 10)], [run(100, "failure", 10, attempt=rfc.RERUN_ATTEMPT_CEILING - 1)], PR_HEAD)
        self.assertEqual(1, len(plan.rerun))

    def test_a_green_head_plans_nothing(self):
        plan = rfc.plan_reruns([check(1, "Tests", "success", 10), check(2, "Docs", "skipped", 20)], [run(100, "success", 10), run(200, "success", 20)], PR_HEAD)
        self.assertEqual([], plan.rerun)
        self.assertEqual([], plan.skipped)
        self.assertEqual(0, plan.pending)


class ListTest(unittest.TestCase):
    def test_every_page_of_both_object_endpoints_is_read(self):
        api = FakeAPI(runs=[run(index, "success", index) for index in range(5)], checks=[check(index, "T", "success", index) for index in range(3)], per_page=2)
        with mock.patch.object(github_api, "PER_PAGE", 2):
            self.assertEqual([0, 1, 2, 3, 4], [entry["id"] for entry in rfc.list_runs(api, api.repo, HEAD)])
            self.assertEqual([0, 1, 2], [entry["id"] for entry in rfc.list_check_runs(api, api.repo, HEAD)])

    def test_an_empty_page_ends_the_walk_even_when_the_total_overstates(self):
        api = FakeAPI(runs=[run(0, "success", 0)], per_page=1)
        original = api.get

        def get(path, tolerate=()):
            result = original(path, tolerate)
            if isinstance(result, dict) and "workflow_runs" in result:
                result["total_count"] = 5
            return result

        api.get = get
        with mock.patch.object(github_api, "PER_PAGE", 1):
            self.assertEqual([0], [entry["id"] for entry in rfc.list_runs(api, api.repo, HEAD)])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.environ = mock.patch.dict(os.environ, {"GITHUB_TOKEN": "x", "GITHUB_ACTIONS": "true"}, clear=False)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.summary = tempfile.NamedTemporaryFile("r", suffix=".md", delete=False)
        self.addCleanup(lambda: os.unlink(self.summary.name))
        os.environ[rfc.STEP_SUMMARY_ENV] = self.summary.name

    def run_main(self, api, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rfc, "GitHubAPI", return_value=api):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rfc.main(["--pr", "3245", "--react-to", "77", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_red_runs_are_rerun_and_the_comment_gets_eyes(self):
        api = FakeAPI(
            runs=[run(1, "failure", 10), run(2, "success", 20, name="Docs Build"), run(3, "cancelled", 30, name="Merge Gate")],
            checks=[check(1, "Python Unit Tests", "failure", 10), check(2, "Docs Build", "success", 20), check(3, "matrix.gate", "cancelled", 30)],
        )
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual(
            [
                ("/repos/gke-labs/kube-agents/actions/runs/1/rerun-failed-jobs", None),
                ("/repos/gke-labs/kube-agents/actions/runs/3/rerun", None),
                ("/repos/gke-labs/kube-agents/issues/comments/77/reactions", {"content": rfc.REACTION_ACKNOWLEDGED}),
            ],
            api.posts,
        )
        self.assertIn("Python Unit Tests", pathlib.Path(self.summary.name).read_text())

    def test_nothing_red_declines_with_a_warning_and_a_confused_face(self):
        api = FakeAPI(
            runs=[run(1, "success", 10), run(2, None, 20, status="queued", name="Docs Build")],
            checks=[check(1, "Tests", "success", 10), check(2, "Docs Build", None, 20, status="queued")],
        )
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual(
            [("/repos/gke-labs/kube-agents/issues/comments/77/reactions", {"content": rfc.REACTION_DECLINED})],
            api.posts,
        )
        self.assertIn(rfc.WORKFLOW_WARNING_PREFIX, out)
        self.assertIn("no red GitHub Actions check", out)
        self.assertIn("1 still running", out)

    def test_a_cancelled_run_that_never_made_a_check_run_is_not_rerun(self):
        """Displaced from its concurrency group while queued: no job ran, so the rollup lists nothing."""
        api = FakeAPI(runs=[run(1, "cancelled", 10), run(2, "success", 20)], checks=[check(2, "Tests", "success", 20)])
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual(rfc.REACTION_DECLINED, api.posts[-1][1]["content"])
        self.assertEqual(1, len(api.posts))

    def test_only_red_checks_this_command_cannot_rerun_declines_and_names_them(self):
        api = FakeAPI(
            runs=[run(1, "failure", 10, attempt=rfc.RERUN_ATTEMPT_CEILING)],
            checks=[check(1, "Python Unit Tests", "failure", 10), check(2, "AI Review", "failure", 99, app="kube-agents-bot")],
        )
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual(rfc.REACTION_DECLINED, api.posts[-1][1]["content"])
        self.assertEqual(1, len(api.posts))
        self.assertIn("Python Unit Tests", out)
        self.assertIn("AI Review", out)

    def test_a_closed_pull_request_declines(self):
        api = FakeAPI(pull={"number": 3245, "state": "closed", "head": PR_HEAD, "title": "t"})
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual(rfc.REACTION_DECLINED, api.posts[-1][1]["content"])
        self.assertEqual(1, len(api.posts))

    def test_a_run_github_refuses_to_rerun_does_not_stop_the_others(self):
        api = FakeAPI(
            runs=[run(1, "failure", 10), run(2, "failure", 20, name="A2A Module Tests")],
            checks=[check(1, "Python Unit Tests", "failure", 10), check(2, "A2A", "failure", 20)],
            refusing={"/repos/gke-labs/kube-agents/actions/runs/1/rerun-failed-jobs"},
            refusal_body=b'{"message": "This workflow is already running"}',
        )
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual(
            [
                ("/repos/gke-labs/kube-agents/actions/runs/2/rerun-failed-jobs", None),
                ("/repos/gke-labs/kube-agents/issues/comments/77/reactions", {"content": rfc.REACTION_ACKNOWLEDGED}),
            ],
            api.posts,
        )
        self.assertIn("403: This workflow is already running", err)
        self.assertIn("refused to re-run Python Unit Tests", out)

    def test_every_rerun_refused_gets_a_confused_face_and_a_red_run(self):
        """A token that cannot re-run at all refuses everything: the person sees 😕 and the job fails
        with GitHub's message, instead of passing for a quiet run."""
        api = FakeAPI(
            runs=[run(1, "failure", 10)],
            checks=[check(1, "Tests", "failure", 10)],
            refusing={"/repos/gke-labs/kube-agents/actions/runs/1/rerun-failed-jobs"},
            refusal_body=b'{"message": "Resource not accessible by integration"}',
        )
        code, out, err = self.run_main(api)
        self.assertEqual(1, code, err)
        self.assertEqual([("/repos/gke-labs/kube-agents/issues/comments/77/reactions", {"content": rfc.REACTION_DECLINED})], api.posts)
        self.assertIn("Resource not accessible by integration", out)

    def test_a_refusal_with_no_json_body_still_names_something(self):
        error = urllib.error.HTTPError("x", 403, "Forbidden", {}, io.BytesIO(b"nope"))
        self.assertEqual("nope", rfc.refusal_message(error))
        error = urllib.error.HTTPError("x", 403, "Forbidden", {}, io.BytesIO(b""))
        self.assertEqual("Forbidden", rfc.refusal_message(error))

    def test_a_refused_reaction_is_a_warning_not_a_failure(self):
        """The first live runs declined correctly, then exited red because the reaction got 403."""
        reaction = "/repos/gke-labs/kube-agents/issues/comments/77/reactions"
        api = FakeAPI(runs=[run(1, "success", 10)], checks=[check(1, "Tests", "success", 10)], refusing={reaction}, refusal_body=b'{"message": "Resource not accessible by integration"}')
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual([], api.posts)
        self.assertIn("Could not react", out)
        self.assertIn("Resource not accessible by integration", out)

        self.assertIn("Could not react", pathlib.Path(self.summary.name).read_text())

        api = FakeAPI(runs=[run(1, "failure", 10)], checks=[check(1, "Tests", "failure", 10)], refusing={reaction})
        code, out, err = self.run_main(api)
        self.assertEqual(0, code, err)
        self.assertEqual([("/repos/gke-labs/kube-agents/actions/runs/1/rerun-failed-jobs", None)], api.posts)

    def test_the_api_failing_on_the_reaction_still_fails_the_run(self):
        """Only a refusal is tolerated: a 5xx is the API failing, and the retries behind it exhausted."""
        api = FakeAPI(runs=[run(1, "success", 10)], checks=[check(1, "Tests", "success", 10)], failing={"/repos/gke-labs/kube-agents/issues/comments/77/reactions"})
        with self.assertRaises(urllib.error.HTTPError):
            self.run_main(api)

    def test_dry_run_posts_nothing(self):
        api = FakeAPI(runs=[run(1, "failure", 10)], checks=[check(1, "Tests", "failure", 10)])
        code, out, err = self.run_main(api, "--dry-run")
        self.assertEqual(0, code, err)
        self.assertEqual([], api.posts)
        self.assertIn("would re-run", err)

    def test_no_token_is_an_error(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "", "GH_TOKEN": ""}):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, rfc.main(["--pr", "1"]))


class WorkflowTest(unittest.TestCase):
    """The workflow's gate and token are what make the script safe to expose to a comment."""

    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.doc = yaml.safe_load(self.text)
        (self.job,) = self.doc["jobs"].values()

    def test_the_gate_is_the_command_from_a_collaborator_on_a_pull_request(self):
        condition = " ".join(self.job["if"].split())
        self.assertIn("github.event.issue.pull_request != null", condition)
        self.assertIn("startsWith(github.event.comment.body, '/retest')", condition)
        self.assertIn('["OWNER", "MEMBER", "COLLABORATOR"]', condition)

    def test_the_token_can_rerun_and_react_and_nothing_more(self):
        """`pull-requests` is write, not read: a reaction on a pull request's comment is checked against
        that scope too, and with `read` the first two live `/retest` comments got 403 on the reaction."""
        self.assertEqual({}, self.doc["permissions"])
        self.assertEqual({"contents": "read", "actions": "write", "pull-requests": "write", "issues": "write"}, self.job["permissions"])

    def test_the_checkout_is_the_default_branch_without_credentials(self):
        checkouts = [step for step in self.job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(1, len(checkouts))
        self.assertNotIn("ref", checkouts[0].get("with", {}))
        self.assertIs(False, checkouts[0]["with"]["persist-credentials"])

    def test_event_fields_reach_the_script_through_the_environment(self):
        step = next(step for step in self.job["steps"] if "rerun_failed_checks.py" in step.get("run", ""))
        self.assertNotIn("${{", step["run"])
        self.assertEqual("${{ github.event.issue.number }}", step["env"]["PR_NUMBER"])
        self.assertEqual("${{ github.event.comment.id }}", step["env"]["COMMENT_ID"])


if __name__ == "__main__":
    unittest.main()
