"""ghcli.py: the issues repository setting and the issue URL form."""

import importlib
import os
import unittest
import unittest.mock

from eval_dashboard import ghcli


class IssuesRepoTest(unittest.TestCase):
    def test_the_default_is_this_repository_unless_the_variable_names_another(self):
        self.addCleanup(importlib.reload, ghcli)
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ghcli.ISSUES_REPO_ENV, None)
            unset = importlib.reload(ghcli)
            self.assertEqual((unset.DEFAULT_REPO, unset.SOURCE_REPO), ("gke-labs/kube-agents", "gke-labs/kube-agents"))
        with unittest.mock.patch.dict(os.environ, {ghcli.ISSUES_REPO_ENV: "gke-labs/kube-agents-eval-triage"}):
            other = importlib.reload(ghcli)
            self.assertEqual(other.DEFAULT_REPO, "gke-labs/kube-agents-eval-triage")
            self.assertEqual(other.SOURCE_REPO, "gke-labs/kube-agents", "the source repository does not move")
        with unittest.mock.patch.dict(os.environ, {ghcli.ISSUES_REPO_ENV: ""}):
            self.assertEqual(importlib.reload(ghcli).DEFAULT_REPO, "gke-labs/kube-agents", "an empty variable is unset")

    def test_issue_key_reads_both_forms_and_has_issue_matches_across_them(self):
        self.assertEqual(ghcli.issue_key("#1300"), ("gke-labs/kube-agents", 1300))
        self.assertEqual(ghcli.issue_key("https://github.com/gke-labs/kube-agents/issues/1300"), ("gke-labs/kube-agents", 1300))
        self.assertEqual(ghcli.issue_key("https://github.com/gke-labs/kube-agents-eval-triage/issues/12"), ("gke-labs/kube-agents-eval-triage", 12))
        self.assertIsNone(ghcli.issue_key("issue 1300"))
        self.assertIsNone(ghcli.issue_key(None))
        for not_an_issue in ("https://github.com/gke-labs/kube-agents/pull/1300", "https://github.com/gke-labs/kube-agents/issues/1300/",
                             "https://github.com/gke-labs/kube-agents/issues/1300#issuecomment-5642245071", "https://GitHub.com/gke-labs/kube-agents/issues/1300",
                             "#", "#12a"):
            self.assertIsNone(ghcli.issue_key(not_an_issue), not_an_issue)
        self.assertTrue(ghcli.has_issue(["#1300"], "https://github.com/gke-labs/kube-agents/issues/1300"), "a case-notes #N and the bot's URL name one issue")
        self.assertFalse(ghcli.has_issue(["#1300"], "https://github.com/gke-labs/kube-agents-eval-triage/issues/1300"), "the same number in another repository is another issue")
        self.assertFalse(ghcli.has_issue([], "#1"))

    def test_issue_tag_is_the_number_here_and_the_url_elsewhere(self):
        here = {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300"}
        there = {"number": 12, "url": "https://github.com/gke-labs/kube-agents-eval-triage/issues/12"}
        legacy = {"number": 1278}
        self.assertEqual(ghcli.issue_tag(here), "#1300")
        self.assertEqual(ghcli.issue_tag(there), "https://github.com/gke-labs/kube-agents-eval-triage/issues/12")
        self.assertEqual(ghcli.issue_tag(legacy), "#1278", "a record without a URL is this repository's")
        self.assertEqual((ghcli.issue_repo(here), ghcli.issue_repo(there), ghcli.issue_repo(legacy), ghcli.issue_repo(None)),
                         ("gke-labs/kube-agents", "gke-labs/kube-agents-eval-triage", "gke-labs/kube-agents", "gke-labs/kube-agents"))
        self.assertIsNone(ghcli.issue_tag({"url": "x"}))
        self.assertIsNone(ghcli.issue_tag("#1300"))

    def test_issue_url_is_the_issues_page(self):
        self.assertEqual(ghcli.issue_url("gke-labs/kube-agents", 1278), "https://github.com/gke-labs/kube-agents/issues/1278")
        self.assertEqual(ghcli.issue_url("gke-labs/kube-agents-eval-triage", "12"), "https://github.com/gke-labs/kube-agents-eval-triage/issues/12")


if __name__ == "__main__":
    unittest.main()
