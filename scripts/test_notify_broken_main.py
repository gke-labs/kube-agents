#!/usr/bin/env python3
"""Unit tests for the broken-main issue notifier.

Run: cd scripts && python3 -m unittest test_notify_broken_main

The notifier cannot be exercised end to end before it is on main -- a
`workflow_run` workflow only runs from the default branch's copy of itself -- so
everything that can be decided without a runner is decided here. Three failure
modes are worth more than the rest: staying quiet when main is broken, which
reproduces the gap this exists to close; opening an issue on a green run, which
trains everyone to ignore the label; and leaving an issue open after main
recovers, which does the same thing more slowly.
"""

import json
import re
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import notify_broken_main as notifier


def run(number, conclusion, *, sha=None, subject="a commit", run_id=None, name="Operator Tests"):
    """A workflow run, carrying only the fields the notifier reads."""
    return {
        "id": run_id if run_id is not None else 1000 + number,
        "run_number": number,
        "conclusion": conclusion,
        "name": name,
        "event": "push",
        "head_branch": "main",
        "head_sha": sha or f"{number:040x}",
        "head_commit": {"message": f"{subject}\n\nbody", "author": {"name": "A Contributor"}},
        # What Tide leaves behind on every merge here, and the reason the
        # attribution is read off the commit rather than the run.
        "actor": {"login": "google-oss-prow[bot]"},
        "triggering_actor": {"login": "google-oss-prow[bot]"},
        "html_url": f"https://github.com/gke-labs/kube-agents/actions/runs/{1000 + number}",
        "workflow_id": 77,
    }


class DecideTest(unittest.TestCase):
    """Which of the four cases a run falls into. This is the whole design."""

    def test_first_failure_announces_a_break(self):
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertEqual(decision["kind"], "broken")
        self.assertEqual(decision["streak_length"], 1)
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_green_after_green_is_still_a_green_to_reconcile(self):
        """Not None. Whether a green run has anything to do depends on whether
        an issue is open, which the run history cannot see -- it cannot see a
        notify run that was dropped, or a red run re-run into a green. `decide`
        reports the state; `reconcile` decides whether it matters."""
        decision = notifier.decide(run(10, "success"), [run(9, "success")])
        self.assertEqual(decision["kind"], "green")
        self.assertEqual(decision["streak_length"], 0)

    def test_a_conclusion_that_says_nothing_says_nothing(self):
        """`neutral`, `stale` and `action_required` all reach the script -- the
        workflow's `if:` filters only `cancelled` and `skipped`. Read as green,
        any of them closes the issue on a main that is still broken."""
        for conclusion in ("neutral", "stale", "action_required", "cancelled", "skipped", None):
            with self.subTest(conclusion=conclusion):
                self.assertIsNone(notifier.decide(run(10, conclusion), [run(9, "failure")]))

    def test_a_second_failure_is_a_follow_up_not_a_new_break(self):
        decision = notifier.decide(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        self.assertEqual(decision["kind"], "still-broken")
        self.assertEqual(decision["streak_length"], 2)
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_green_after_a_failure_carries_the_streak_it_ended(self):
        decision = notifier.decide(run(12, "success"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        self.assertEqual(decision["kind"], "green")
        self.assertEqual(decision["streak_length"], 2)
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_the_recovery_is_not_counted_into_the_streak_it_ends(self):
        """`broke_at` is the issue's identity, so a green run joining its own
        streak would look for an issue that was never opened -- the reader would
        see a break with no end and an end with no break."""
        history = [run(11, "failure"), run(10, "failure"), run(9, "success")]
        decision = notifier.decide(run(12, "success"), history)
        self.assertEqual(decision["broke_at"]["run_number"], 10)
        self.assertNotIn(12, [r["run_number"] for r in decision["streak"]])

    def test_the_streak_reads_oldest_first(self):
        """The order the issue's table is in: the commit that broke main at the
        top, the ones that landed on top of it below."""
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        self.assertEqual([r["run_number"] for r in decision["streak"]], [10, 11, 12])

    def test_a_timeout_is_a_failure(self):
        self.assertEqual(notifier.decide(run(10, "timed_out"), [run(9, "success")])["kind"], "broken")

    def test_an_unparseable_workflow_is_a_failure(self):
        """`startup_failure` reaches main exactly the way a failing test does,
        and reads as 'nothing ran' if it is filtered out."""
        self.assertEqual(notifier.decide(run(10, "startup_failure"), [run(9, "success")])["kind"], "broken")

    def test_the_first_run_of_a_workflow_can_still_break_main(self):
        """No history at all. `broke_at` has to fall back to the current run
        rather than raising, or a newly added required check could never
        report its first failure."""
        decision = notifier.decide(run(1, "failure"), [])
        self.assertEqual(decision["kind"], "broken")
        self.assertEqual(decision["broke_at"]["run_number"], 1)


class HistoryFilterTest(unittest.TestCase):
    def test_the_triggering_run_is_removed_from_its_own_history(self):
        """The API list includes it, and left in it would compare against
        itself -- every failure would read as 'still broken'."""
        current = run(10, "failure")
        history = notifier.reporting_history([current, run(9, "success")], current)
        self.assertEqual([r["run_number"] for r in history], [9])

    def test_a_newer_run_is_not_treated_as_history(self):
        """Merges land close enough together that the newest run in the list is
        regularly not the one being handled."""
        current = run(10, "failure")
        history = notifier.reporting_history([run(11, "success"), current, run(9, "success")], current)
        self.assertEqual([r["run_number"] for r in history], [9])

    def test_a_cancelled_run_does_not_end_a_streak(self):
        """A concurrency-superseded run says nothing about the tree. Counted as
        a non-failure it would make the next failure look like a fresh break and
        open a second issue."""
        current = run(12, "failure")
        history = notifier.reporting_history([run(11, "cancelled"), run(10, "failure")], current)
        decision = notifier.decide(current, history)
        self.assertEqual(decision["kind"], "still-broken")
        self.assertEqual(decision["broke_at"]["run_number"], 10)

    def test_a_skipped_run_does_not_end_a_streak_either(self):
        current = run(12, "failure")
        history = notifier.reporting_history([run(11, "skipped"), run(10, "failure")], current)
        self.assertEqual(notifier.decide(current, history)["kind"], "still-broken")

    def test_a_run_that_reports_success_without_testing_would_fool_this(self):
        """The #812 shape, and the one thing this script cannot defend itself
        against -- recorded as the contract it depends on rather than left for
        someone to rediscover in production.

        `k8s-operator-test.yml` used to skip its steps on a docs commit and
        report `success` at run level anyway: run 2664 on main, `1d68f09`, every
        real step `skipped` and the conclusion `success`. Replayed through
        `decide` that green closes the issue -- and on the real history it does
        so twice, at 2655 and again at 2664, naming a docs commit as the fix and
        saying nothing when 2700 genuinely repaired main. The fix is in the
        workflow: its push trigger filters with `paths:`, so no run is recorded
        at all. This states why a watched workflow may not self-skip into a
        green, and it is why `Prettier Check`, which scopes itself to the files
        a push touched, is not on the watch list.
        """
        docs_commit_that_tested_nothing = run(11, "success")
        decision = notifier.decide(docs_commit_that_tested_nothing, [run(10, "failure")])
        self.assertEqual(
            decision["kind"],
            "green",
            "if this stops reading as a green the guard has moved into the script, and "
            "the paths: filter in k8s-operator-test.yml can be reconsidered",
        )


class EpisodeMarkerTest(unittest.TestCase):
    """The hidden marker is how an update finds the issue it belongs to. Get it
    wrong in either direction and the issue either duplicates or never closes."""

    def test_one_breakage_is_one_issue(self):
        break_decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        follow_up = notifier.decide(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        recovery = notifier.decide(run(12, "success"), [run(11, "failure"), run(10, "failure")])
        markers = {notifier.episode_marker(d, 77) for d in (break_decision, follow_up, recovery)}
        self.assertEqual(len(markers), 1, f"expected one issue, got {markers}")

    def test_the_next_breakage_opens_a_new_issue(self):
        first = notifier.decide(run(10, "failure"), [run(9, "success")])
        second = notifier.decide(run(20, "failure"), [run(19, "success")])
        self.assertNotEqual(notifier.episode_marker(first, 77), notifier.episode_marker(second, 77))

    def test_two_workflows_breaking_at_once_do_not_share_an_issue(self):
        """Run numbers are per workflow, so the workflow id has to be in the
        marker or a coincidence of numbering merges two unrelated breakages."""
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertNotEqual(notifier.episode_marker(decision, 77), notifier.episode_marker(decision, 88))

    def test_the_workflow_prefix_matches_that_workflows_markers_only(self):
        """`reconcile` uses the prefix to find stale issues to close. Matching
        another workflow's issue would close a real breakage."""
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertIn(notifier.workflow_marker(77), notifier.episode_marker(decision, 77))
        self.assertNotIn(notifier.workflow_marker(88), notifier.episode_marker(decision, 77))

    def test_the_prefix_does_not_match_a_workflow_whose_id_extends_it(self):
        """Workflow 7 and workflow 77 are both plausible ids, and a prefix that
        ended at the digits would confuse them."""
        self.assertNotIn(notifier.workflow_marker(7), notifier.workflow_marker(77))


class RenderTest(unittest.TestCase):
    REPO = "gke-labs/kube-agents"

    def _body(self, decision):
        return notifier.render_body(decision, self.REPO, notifier.episode_marker(decision, 77))

    def test_a_break_names_the_workflow_commit_and_run(self):
        decision = notifier.decide(
            run(10, "failure", sha="277de10c43e3b7311bfb159a4016eee2831ca6f7", subject="feat: add PDBs (#733)"),
            [run(9, "success")],
        )
        body = self._body(decision)
        self.assertIn("Operator Tests", body)
        self.assertIn("277de10", body)
        self.assertIn("actions/runs/1010", body)

    def test_the_title_names_the_workflow_so_five_issues_are_distinguishable(self):
        decision = notifier.decide(run(10, "failure", name="Prettier Check"), [run(9, "success")])
        self.assertEqual(notifier.render_title(decision), "🔴 main is broken: Prettier Check")

    def test_the_title_is_stable_across_an_episode(self):
        """It is rewritten on every update, so a changing title would rename the
        issue under anyone reading it."""
        first = notifier.decide(run(10, "failure"), [run(9, "success")])
        later = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")])
        self.assertEqual(notifier.render_title(first), notifier.render_title(later))

    def test_the_marker_is_in_the_body(self):
        """Without it the next update cannot find this issue and opens another."""
        decision = notifier.decide(run(10, "failure"), [run(9, "success")])
        self.assertIn(notifier.episode_marker(decision, 77), self._body(decision))

    def test_the_change_is_attributed_to_its_author_not_to_tide(self):
        """`actor` is `google-oss-prow[bot]` on every merge here, so a body
        built from it names the robot on every breakage and nobody else."""
        body = self._body(notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertIn("A Contributor", body)
        self.assertNotIn("prow", body)

    def test_the_pull_request_is_named_so_github_autolinks_it(self):
        """Bare `#733` rather than a URL: it renders as a link and leaves a
        back-reference on the pull request that broke main."""
        body = self._body(notifier.decide(run(10, "failure", subject="feat: add PDBs (#733)"), [run(9, "success")]))
        self.assertIn("| #733 |", body)

    def test_a_commit_with_no_pull_request_number_still_renders(self):
        """A direct push, or a merge commit that does not carry the suffix. The
        cell is empty rather than the row being dropped."""
        body = self._body(notifier.decide(run(10, "failure", subject="hotfix, no PR"), [run(9, "success")]))
        self.assertIn("A Contributor", body)
        self.assertNotIn("#", body.split("| run |")[1].split("\n")[2])

    def test_every_commit_in_the_streak_gets_a_row(self):
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure"), run(9, "success")])
        body = self._body(decision)
        rows = [line for line in body.splitlines() if line.startswith("| [")]
        self.assertEqual(len(rows), 3)
        self.assertIn("Broken since", body)
        self.assertIn("3 consecutive failures", body)

    def test_the_table_reads_oldest_first(self):
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")])
        rows = [line for line in self._body(decision).splitlines() if line.startswith("| [")]
        self.assertEqual([row.split("]")[0] for row in rows], ["| [10", "| [11", "| [12"])

    def test_a_first_failure_does_not_claim_a_streak(self):
        """"Broken since X -- 1 consecutive failures" reads as a bug in the
        counter. One row is its own explanation."""
        body = self._body(notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertNotIn("Broken since", body)
        self.assertNotIn("consecutive", body)

    def test_rebuilding_the_body_for_the_same_run_is_idempotent(self):
        """The body is rewritten, not appended to, so a redelivered event or a
        re-run of the workflow cannot double a row."""
        decision = notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")])
        self.assertEqual(self._body(decision), self._body(decision))

    def test_a_pipe_in_an_author_name_does_not_break_the_table(self):
        """An unescaped `|` in a cell silently splits the row into five columns,
        and Markdown drops the overflow -- the PR link vanishes."""
        odd = run(10, "failure")
        odd["head_commit"]["author"]["name"] = "A | Contributor"
        body = self._body(notifier.decide(odd, [run(9, "success")]))
        row = [line for line in body.splitlines() if line.startswith("| [")][0]
        self.assertIn(r"A \| Contributor", row)
        separators = len(re.findall(r"(?<!\\)\|", row))
        self.assertEqual(separators, 5, f"expected four cells, got {row}")

    def test_a_commit_with_no_message_still_renders(self):
        """`head_commit` is absent on some run payloads. A KeyError here would
        take down the notification rather than degrade it."""
        bare = run(10, "failure")
        del bare["head_commit"]
        body = self._body(notifier.decide(bare, [run(9, "success")]))
        self.assertIn(bare["head_sha"][:7], body)

    def test_a_new_issue_needs_no_comment(self):
        """Opening the issue is the notification."""
        self.assertIsNone(notifier.render_comment(notifier.decide(run(10, "failure"), [run(9, "success")]), self.REPO))

    def test_a_follow_up_comments_because_a_body_edit_notifies_nobody(self):
        comment = notifier.render_comment(
            notifier.decide(run(12, "failure"), [run(11, "failure"), run(10, "failure")]), self.REPO
        )
        self.assertIn("Still failing", comment)
        self.assertIn("3 consecutive failures", comment)

    def test_the_recovery_comment_is_recognisably_not_another_break(self):
        comment = notifier.render_comment(notifier.decide(run(12, "success"), [run(11, "failure")]), self.REPO)
        self.assertIn("✅", comment)
        self.assertNotIn("🔴", comment)
        self.assertIn("Fixed by", comment)

    def test_a_one_commit_breakage_is_not_pluralised(self):
        comment = notifier.render_comment(notifier.decide(run(12, "success"), [run(11, "failure")]), self.REPO)
        self.assertIn("1 consecutive failure before it.", comment)


def _response(raw):
    """What `urlopen` returns: a context manager yielding something with `read`.

    `__exit__` returns False on purpose. A `MagicMock` there is truthy, which
    swallows any exception raised inside the `with` -- including the ones these
    tests exist to catch -- and surfaces it much later as an unrelated
    `UnboundLocalError`.
    """
    return mock.MagicMock(
        __enter__=mock.Mock(return_value=mock.Mock(read=lambda: raw)),
        __exit__=mock.Mock(return_value=False),
    )


class RequestTest(unittest.TestCase):
    def _api(self, responses):
        """A `GitHubAPI` whose opener raises or returns per call."""
        calls = []

        def opener(request):
            calls.append(request)
            outcome = responses[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return _response(b"{}")

        return notifier.GitHubAPI("o/r", "t", opener=opener, sleep=lambda _: None), calls

    def test_a_5xx_is_retried(self):
        """A dropped write is the exact failure this script exists to prevent --
        an issue that never opened, or one that never closed."""
        api, calls = self._api([urllib.error.HTTPError("u", 503, "busy", {}, None), None])
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_a_4xx_is_not_retried(self):
        """A permissions failure will not start working on the second attempt,
        and the job should fail now rather than in fifteen seconds."""
        api, calls = self._api([urllib.error.HTTPError("u", 403, "no", {}, None), None])
        with self.assertRaises(urllib.error.HTTPError):
            api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 1)

    def test_giving_up_raises_rather_than_returning(self):
        api, calls = self._api([urllib.error.HTTPError("u", 503, "busy", {}, None)] * notifier.REQUEST_ATTEMPTS)
        with self.assertRaises(urllib.error.HTTPError):
            api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), notifier.REQUEST_ATTEMPTS)

    def test_a_tolerated_status_is_not_an_error(self):
        """`ensure_label` runs on every first failure and 422 means the label is
        already there, which is the normal case."""
        api, calls = self._api([urllib.error.HTTPError("u", 422, "exists", {}, None)])
        self.assertIsNone(api.request("POST", "/labels", {"name": "x"}, tolerate=(422,)))
        self.assertEqual(len(calls), 1)

    def test_a_secondary_rate_limit_is_retried_even_though_it_is_a_403(self):
        """GitHub throttles writes with 403, not 429. Treating every 403 as
        fatal drops the write; treating every 403 as retryable turns a missing
        `issues: write` into fifteen seconds of pointless retries."""
        throttled = urllib.error.HTTPError("u", 403, "slow down", {"Retry-After": "1"}, None)
        api, calls = self._api([throttled, None])
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_an_exhausted_quota_is_retried_too(self):
        exhausted = urllib.error.HTTPError("u", 403, "limit", {"x-ratelimit-remaining": "0"}, None)
        api, calls = self._api([exhausted, None])
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(len(calls), 2)

    def test_retry_after_is_honoured_and_capped(self):
        slept = []
        api, _ = self._api([urllib.error.HTTPError("u", 429, "wait", {"Retry-After": "9"}, None), None])
        api.sleep = slept.append
        api.request("POST", "/x", {"a": 1})
        self.assertEqual(slept, [9])
        self.assertEqual(notifier._retry_delay(urllib.error.HTTPError("u", 429, "w", {"Retry-After": "9999"}, None)),
                         notifier.REQUEST_RETRY_CEILING)


class QueryTest(unittest.TestCase):
    """The URLs the API layer builds. Nothing else covers them: `ReconcileTest`
    fakes the API wholesale, so dropping `state=open` from the issue query or
    `event=push` from the history query breaks the notifier while every other
    test still passes."""

    def _api(self, payload):
        calls = []

        def opener(request):
            calls.append(request)
            return _response(json.dumps(payload).encode())

        return notifier.GitHubAPI("gke-labs/kube-agents", "t", opener=opener, sleep=lambda _: None), calls

    def test_the_history_query_asks_only_for_completed_pushes_on_the_branch(self):
        """A pull-request run of the same workflow says nothing about main, and
        they outnumber the push runs by an order of magnitude."""
        api, calls = self._api({"workflow_runs": []})
        api.history(77, "main")
        url = calls[0].full_url
        self.assertIn("/actions/workflows/77/runs?", url)
        for expected in ("branch=main", "event=push", "status=completed", f"per_page={notifier.HISTORY_DEPTH}"):
            self.assertIn(expected, url)

    def test_the_issue_query_is_scoped_to_the_open_labelled_issues(self):
        api, calls = self._api([])
        api.open_issues_for_workflow(77)
        url = calls[0].full_url
        self.assertIn("state=open", url)
        self.assertIn(urllib.parse.quote(notifier.LABEL, safe=""), url)

    def test_pull_requests_are_excluded_from_the_issue_list(self):
        """`/issues` returns pull requests too. One carrying the marker -- this
        change's own pull request quotes it -- would be commented on and closed
        as though it were the tracking issue."""
        marker = notifier.workflow_marker(77) + "episode=10 -->"
        api, _ = self._api(
            [
                {"number": 1, "body": marker, "pull_request": {"url": "..."}},
                {"number": 2, "body": marker},
                {"number": 3, "body": "unrelated issue"},
                {"number": 4, "body": None},
            ]
        )
        self.assertEqual([i["number"] for i in api.open_issues_for_workflow(77)], [2])

    def test_the_token_and_api_version_are_sent(self):
        api, calls = self._api({"workflow_runs": []})
        api.history(77)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer t")
        self.assertEqual(calls[0].get_header("X-github-api-version"), "2022-11-28")

    def test_closing_an_issue_sends_a_patch_with_a_state_reason(self):
        """`state_reason` is what makes the issue read as completed rather than
        as abandoned in the issue list."""
        api, calls = self._api({})
        api.close_issue(901)
        self.assertEqual(calls[0].method, "PATCH")
        self.assertTrue(calls[0].full_url.endswith("/repos/gke-labs/kube-agents/issues/901"))
        self.assertEqual(json.loads(calls[0].data), {"state": "closed", "state_reason": "completed"})

    def test_creating_an_issue_carries_the_label(self):
        """Without it `open_issues_for_workflow` never finds the issue again."""
        api, calls = self._api({"number": 901})
        api.create_issue("t", "b")
        self.assertEqual(json.loads(calls[0].data)["labels"], [notifier.LABEL])


class FakeAPI:
    """Records what `reconcile` asks of the API and answers from a list of open
    issues, which is the whole of the state it reads."""

    def __init__(self, open_issues=()):
        self.open_issues = list(open_issues)
        self.actions = []
        self.next_number = 900

    def open_issues_for_workflow(self, workflow_id):
        prefix = notifier.workflow_marker(workflow_id)
        return [issue for issue in self.open_issues if prefix in (issue.get("body") or "")]

    def ensure_label(self):
        self.actions.append(("label",))

    def create_issue(self, title, body):
        self.next_number += 1
        self.actions.append(("create", self.next_number, title, body))
        return {"number": self.next_number}

    def update_issue(self, number, **fields):
        self.actions.append(("update", number, fields))

    def comment(self, number, body):
        self.actions.append(("comment", number, body))

    def close_issue(self, number):
        self.actions.append(("close", number))

    def kinds(self):
        return [action[0] for action in self.actions]


def issue(number, workflow_id, episode, state="open"):
    return {
        "number": number,
        "state": state,
        "body": f"whatever\n\n<!-- main-broken workflow={workflow_id} episode={episode} -->",
    }


class ReconcileTest(unittest.TestCase):
    REPO = "gke-labs/kube-agents"

    def _reconcile(self, api, decision, workflow_id=77):
        return notifier.reconcile(api, decision, self.REPO, workflow_id)

    def test_a_first_failure_opens_an_issue(self):
        api = FakeAPI()
        self._reconcile(api, notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertEqual(api.kinds(), ["label", "create"])

    def test_a_follow_up_updates_the_existing_issue_rather_than_opening_another(self):
        """The failure that would flood the label with one issue per red run."""
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, notifier.decide(run(11, "failure"), [run(10, "failure"), run(9, "success")]))
        self.assertEqual(api.kinds(), ["update", "comment"])
        self.assertEqual(api.actions[0][1], 901)

    def test_a_recovery_closes_the_issue(self):
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, notifier.decide(run(12, "success"), [run(11, "failure"), run(10, "failure")]))
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertIn("Fixed by", api.actions[0][2])

    def test_a_recovery_with_nothing_open_is_quiet(self):
        """Main was already red when this workflow was added, or someone closed
        the issue by hand. Opening one just to close it would be noise."""
        api = FakeAPI()
        result = self._reconcile(api, notifier.decide(run(12, "success"), [run(11, "failure")]))
        self.assertEqual(api.actions, [])
        self.assertIn("no issue is open", result)

    def test_an_ordinary_green_run_writes_nothing(self):
        """Nearly every run. It costs one list request to be sure, and that is
        the price of never leaving an issue open on a green main."""
        api = FakeAPI()
        self._reconcile(api, notifier.decide(run(12, "success"), [run(11, "success")]))
        self.assertEqual(api.actions, [])

    def test_a_green_run_closes_an_open_issue_even_with_no_failure_behind_it(self):
        """The self-heal, and the reason `decide` no longer infers "recovered"
        from the history. Three ways to get here, all real: the notify run that
        would have closed the issue was cancelled out of its concurrency group;
        a red run was re-run and passed, so its own history reads green after
        green; or two runs were handled out of order."""
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, notifier.decide(run(12, "success"), [run(11, "success")]))
        self.assertEqual(api.kinds(), ["comment", "close"])
        self.assertIn("still open", api.actions[0][2])
        self.assertNotIn("Fixed by", api.actions[0][2], "this run is not the fix and must not claim to be")

    def test_another_workflows_issue_is_left_alone(self):
        """Several workflows are watched and each breaks independently. Closing
        another one's issue would hide a real breakage."""
        api = FakeAPI([issue(901, 88, 10)])
        self._reconcile(api, notifier.decide(run(12, "success"), [run(11, "failure")]))
        self.assertEqual(api.actions, [])

    def test_redelivering_the_break_that_opened_the_issue_does_not_duplicate_it(self):
        """GitHub redelivers `workflow_run` events, and a re-run of the notifier
        replays one deliberately. For a `broken` this is free -- there is no
        comment to repeat -- so the guarantee tested here is only that a second
        issue is not opened."""
        api = FakeAPI([issue(901, 77, 10)])
        self._reconcile(api, notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertEqual(api.kinds(), ["update"], "a new break on an existing issue should not comment")

    def test_redelivering_a_follow_up_repeats_its_comment(self):
        """Known and accepted, recorded so it is a decision rather than a
        surprise. The body is rebuilt so no row doubles; the "Still failing"
        comment does. Deduplicating it means listing every comment on the issue
        on every failure, which costs more than the duplicate does -- and a
        redelivered `workflow_run` is rare, where a broken main is not."""
        api = FakeAPI([issue(901, 77, 10)])
        decision = notifier.decide(run(11, "failure"), [run(10, "failure"), run(9, "success")])
        self._reconcile(api, decision)
        self._reconcile(api, decision)
        self.assertEqual(api.kinds(), ["update", "comment", "update", "comment"])

    def test_a_stale_issue_from_an_earlier_breakage_is_closed(self):
        """Only reachable if a close failed, but two open issues both claiming
        main is broken is worse than the missed close that caused it."""
        api = FakeAPI([issue(901, 77, 5)])
        self._reconcile(api, notifier.decide(run(10, "failure"), [run(9, "success")]))
        self.assertEqual(api.kinds(), ["label", "create", "comment", "close"])
        self.assertIn("Superseded by #901", api.actions[2][2])
        self.assertEqual(api.actions[3][1], 901)

    def test_a_recovery_closes_every_open_issue_for_the_workflow(self):
        """Same repair: main is green, so nothing about this workflow should
        still be claiming otherwise."""
        api = FakeAPI([issue(901, 77, 10), issue(902, 77, 5)])
        self._reconcile(api, notifier.decide(run(12, "success"), [run(11, "failure"), run(10, "failure")]))
        self.assertEqual(api.kinds(), ["comment", "close", "comment", "close"])


class MainTest(unittest.TestCase):
    """The wiring, with the API and the reconciliation stubbed."""

    def _main(self, current, history, argv, env):
        api = mock.Mock()
        api.run.return_value = current
        api.history.return_value = history
        with mock.patch.object(notifier, "GitHubAPI", return_value=api), mock.patch.dict(
            "os.environ", env, clear=True
        ), mock.patch.object(notifier, "reconcile", return_value="done") as reconcile:
            return notifier.main(argv), reconcile

    def test_a_break_is_reconciled(self):
        status, reconcile = self._main(
            run(10, "failure"), [run(9, "success")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args[0][1]["kind"], "broken")

    def test_a_green_run_still_reaches_reconcile(self):
        """It has to: whether there is an issue to close is a question only the
        API can answer. `reconcile` is what makes it cheap when there is not."""
        status, reconcile = self._main(
            run(10, "success"), [run(9, "success")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_args[0][1]["kind"], "green")

    def test_a_conclusion_that_says_nothing_reaches_nothing(self):
        status, reconcile = self._main(
            run(10, "neutral"), [run(9, "failure")], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"}
        )
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_run_a_later_one_has_overtaken_is_left_to_that_one(self):
        """Out of order, a red run handled after the green that fixed it would
        open a "main is broken" issue against a green main."""
        status, reconcile = self._main(
            run(11, "failure"),
            [run(12, "success"), run(11, "failure"), run(10, "failure")],
            ["--run-id", "1011"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_later_run_that_said_nothing_does_not_silence_this_one(self):
        """A `cancelled` run is filtered out by the workflow's `if:` and will
        never reconcile anything, so deferring to it drops the notification
        altogether -- the one outcome this script exists to prevent."""
        status, reconcile = self._main(
            run(11, "failure"),
            [run(12, "cancelled"), run(11, "failure"), run(10, "success")],
            ["--run-id", "1011"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        self.assertEqual(reconcile.call_args[0][1]["kind"], "broken")

    def test_a_pull_request_run_is_refused(self):
        """Defence in depth behind the workflow's `if:`: a hand-run against the
        wrong run id must not file a pull request as broken main."""
        pr_run = run(10, "failure")
        pr_run["event"] = "pull_request"
        status, reconcile = self._main(pr_run, [], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_run_on_another_branch_is_refused(self):
        other = run(10, "failure")
        other["head_branch"] = "release-1.2"
        status, reconcile = self._main(other, [], ["--run-id", "1010"], {"GITHUB_TOKEN": "t"})
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_dry_run_never_writes(self):
        status, reconcile = self._main(
            run(10, "failure"),
            [run(9, "success")],
            ["--run-id", "1010", "--dry-run"],
            {"GITHUB_TOKEN": "t"},
        )
        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_no_token_is_an_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(notifier.main(["--run-id", "1"]), 1)


if __name__ == "__main__":
    unittest.main()
