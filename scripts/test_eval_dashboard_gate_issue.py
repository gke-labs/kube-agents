"""gate_issue.Tracker across repositories: a carried issue is addressed
where its `url` says it lives, and the bot's own issue is known by its body,
not by the token that filed it."""

import json
import unittest
import unittest.mock

from eval_dashboard import gate_issue, ghcli

ISSUES_REPO = "gke-labs/kube-agents-eval-triage"
SOURCE_ISSUE = {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300", "condition": "shared_break"}
TRIAGE_ISSUE = {"number": 12, "url": f"https://github.com/{ISSUES_REPO}/issues/12", "condition": "shared_break"}
LEGACY_ISSUE = {"number": 1278, "condition": "shared_break"}
BOT_BODY = "<!-- kube-agents-bot:gate-outage cases=a-probe -->\nThe smoke gate is in OUTAGE.\n\nFiled automatically by the smoke health bot; edit freely."


class Proc:
    def __init__(self, stdout="{}", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class Runner:
    """`gh api`: issues by (repo, number), every call recorded as (method, path)."""

    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        self.calls.append((method, path))
        if method == "GET" and "/issues/" in path and not path.endswith("/comments"):
            repo = path.split("repos/")[1].split("/issues/")[0]
            number = int(path.rsplit("/", 1)[1])
            payload = self.issues.get((repo, number))
            return Proc(json.dumps(payload)) if payload else Proc("", returncode=1, stderr="HTTP 404")
        return Proc("{}")


def tracker(issues, repo=ISSUES_REPO):
    runner = Runner(issues)
    return gate_issue.Tracker(ghcli.Gh(repo, runner, dry_run=False), infra=(None, None)), runner


class CarriedIssueRepositoryTest(unittest.TestCase):
    def test_a_source_repository_issue_is_read_and_settled_there_while_the_filer_targets_the_issues_repository(self):
        """An episode that began before the variable was set carries this
        repository's issue; the tracker must not address <issues-repo>#1300."""
        t, runner = tracker({("gke-labs/kube-agents", 1300): {"number": 1300, "state": "open", "user": {"login": "github-actions[bot]"}, "body": BOT_BODY}})
        self.assertTrue(t.is_open(SOURCE_ISSUE))
        self.assertTrue(t.recovered(SOURCE_ISSUE, "2h"))
        self.assertEqual(runner.calls, [
            ("GET", "repos/gke-labs/kube-agents/issues/1300"),
            ("POST", "repos/gke-labs/kube-agents/issues/1300/comments"),
            ("PATCH", "repos/gke-labs/kube-agents/issues/1300"),
        ])
        self.assertEqual(t.gh.repo, ISSUES_REPO, "the filer still targets the issues repository")

    def test_an_issues_repository_issue_and_a_legacy_record_go_to_their_repositories(self):
        t, runner = tracker({(ISSUES_REPO, 12): {"number": 12, "state": "closed", "closed_at": "2026-11-10T10:00:00Z", "user": {"login": "x"}, "body": ""},
                             ("gke-labs/kube-agents", 1278): {"number": 1278, "state": "open", "user": {"login": "alice"}, "body": "a human's"}})
        self.assertFalse(t.is_open(TRIAGE_ISSUE))
        self.assertTrue(t.is_open(LEGACY_ISSUE), "a record without a URL is this repository's")
        self.assertEqual([p for _, p in runner.calls], [f"repos/{ISSUES_REPO}/issues/12", "repos/gke-labs/kube-agents/issues/1278"])
        self.assertEqual(len(t._repo_gh), 2)
        t.is_open(TRIAGE_ISSUE)
        self.assertEqual(len(runner.calls), 2, "one read per issue per tick, whichever repository")

    def test_a_human_filed_issue_gets_the_comment_but_not_the_close(self):
        t, runner = tracker({("gke-labs/kube-agents", 1278): {"number": 1278, "state": "open", "user": {"login": "alice"}, "body": "the crashloop trio is red everywhere"}})
        self.assertTrue(t.cleared(LEGACY_ISSUE, "9:00 AM ET"))
        self.assertEqual([m for m, _ in runner.calls], ["GET", "POST"], "comment only")


class PullRequestLinksTest(unittest.TestCase):
    def test_the_lost_pods_and_deadline_bodies_link_pull_requests_in_this_repository_in_full(self):
        """A bare #N in an issue filed in the issues repository would resolve
        there; the pull requests live in SOURCE_REPO."""
        incident = {"prs": [2853, 2827], "runs": 2, "nodes": {"node-1": 2}, "window_start": "2026-10-09T00:00:00+00:00", "window_end": "2026-10-09T00:10:00+00:00"}
        # health.py's evidence lines write the pull requests as _pr_list does: "#N".
        evidence = [
            f"{gate_issue.DEADLINE_EVIDENCE_PREFIX} 2 runs killed at the 360m job deadline 00:00–00:10 UTC (#2853, #2827)",
            f"{gate_issue.LOST_PODS_EVIDENCE_PREFIX}2 runs 00:00–00:10 UTC (nodes node-1; #2853, #2827)",
        ]
        lost_health = {"incident": incident, "evidence": evidence}
        killed_health = {"incident": incident, "evidence": evidence, "since": "2026-10-09T00:00:00+00:00"}
        with unittest.mock.patch.object(ghcli, "DEFAULT_REPO", "gke-labs/kube-agents-eval-triage"):
            lost = gate_issue.render_lost_pods_body(lost_health, "Thu 8:00 PM ET", "8:00–8:10 PM ET", "brief")
            killed = gate_issue.render_deadline_kill_body(killed_health, "Thu 8:00 PM ET", "8:00–8:10 PM ET", "brief")
            for body in (lost, killed):
                self.assertIn("**Affected PRs:** https://github.com/gke-labs/kube-agents/pull/2853, https://github.com/gke-labs/kube-agents/pull/2827.", body)
                self.assertIn("; https://github.com/gke-labs/kube-agents/pull/2853, https://github.com/gke-labs/kube-agents/pull/2827)" if body is lost else "(https://github.com/gke-labs/kube-agents/pull/2853, https://github.com/gke-labs/kube-agents/pull/2827)", body, "the quoted evidence line too")
                self.assertNotIn("#2853", body)
            self.assertEqual(gate_issue.public_evidence({"evidence": ["issue #12 noted; node-1 lost; #2853 and 3 more", "sha a1#2 kept"]}), ["issue https://github.com/gke-labs/kube-agents/pull/12 noted; node-1 lost; https://github.com/gke-labs/kube-agents/pull/2853 and 3 more", "sha a1#2 kept"])
        with unittest.mock.patch.object(ghcli, "DEFAULT_REPO", ghcli.SOURCE_REPO):
            body = gate_issue.render_lost_pods_body(lost_health, "Thu 8:00 PM ET", "8:00–8:10 PM ET", "brief")
            self.assertIn("(nodes node-1; #2853, #2827)", body, "no issues repository set: the evidence line reads as health.json wrote it, so the scrub's re-render of an existing issue is a no-op")
            self.assertIn("**Affected PRs:** https://github.com/gke-labs/kube-agents/pull/2853", body)
        self.assertIn("none recorded", gate_issue.render_lost_pods_body({"incident": {"prs": []}, "evidence": []}, "x", "y", "z"))


class BotFiledTest(unittest.TestCase):
    def test_the_bots_issue_is_known_by_its_body_whatever_token_filed_it(self):
        app_authored = {"number": 12, "state": "open", "user": {"login": "kube-agents-eval-triage[bot]"}, "body": BOT_BODY}
        t, _ = tracker({(ISSUES_REPO, 12): app_authored})
        self.assertTrue(t.bot_filed(TRIAGE_ISSUE), "filed with the issues repository's App token: the author is the App")
        lost_pods = dict(app_authored, body="The Prow build cluster lost the node(s) below.\n\nFiled automatically by the smoke health bot; the cluster owner should check the node events and autorepair; the bot closes it when the condition clears.\n")
        t, _ = tracker({(ISSUES_REPO, 12): lost_pods})
        self.assertTrue(t.bot_filed(TRIAGE_ISSUE), "the Filed automatically line, on a body with no marker")
        by_workflow = {"number": 1300, "state": "open", "user": {"login": "github-actions[bot]"}, "body": "edited away by a human"}
        t, _ = tracker({("gke-labs/kube-agents", 1300): by_workflow})
        self.assertTrue(t.bot_filed(SOURCE_ISSUE), "the workflow's login still counts")
        human = {"number": 1278, "state": "open", "user": {"login": "alice"}, "body": "we should file this automatically"}
        t, _ = tracker({("gke-labs/kube-agents", 1278): human})
        self.assertFalse(t.bot_filed(LEGACY_ISSUE))
        self.assertFalse(t.bot_filed({"number": 9999, "url": "https://github.com/gke-labs/kube-agents/issues/9999"}), "unreadable: not claimed as the bot's")


if __name__ == "__main__":
    unittest.main()
