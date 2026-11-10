"""ci-health.yml: the issue filers and their readers follow the issues
repository setting and its token; the pull-request comment and the collect
do not. docs/ci-health.md, "The tracking issue"."""

import pathlib
import re
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-health.yml"
JOB = "refresh-and-adjudicate"
VARIABLE = "${{ vars.KUBE_AGENTS_ISSUES_REPO }}"
GUARD = "vars.KUBE_AGENTS_ISSUES_REPO != ''"
TOKEN = "${{ steps.issues_token.outputs.token || github.token }}"
OWN_TOKEN = "${{ github.token }}"
ISSUE_STEPS = (
    "Rank each domain's understudies for the scorecard",
    "File bugs for persistent nightly failures (dry run unless armed)",
    "File an issue per domain with no presubmit seat (dry run unless armed)",
    "Post on state change or digest",
    "Render the weekly health email",
)
OWN_TOKEN_STEPS = ("Collect (incremental, onto the published data.json)", "Comment on pull requests whose smoke run went red or lost its node")
PINNED_ACTION = re.compile(r"^actions/create-github-app-token@[0-9a-f]{40}$")


def steps():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return {s.get("name"): s for s in workflow["jobs"][JOB]["steps"]}


class IssuesRepoWiringTest(unittest.TestCase):
    def test_the_issue_steps_read_the_variable_and_the_minted_token_with_a_fallback(self):
        by_name = steps()
        for name in ISSUE_STEPS:
            env = by_name[name].get("env") or {}
            self.assertEqual(env.get("KUBE_AGENTS_ISSUES_REPO"), VARIABLE, name)
            self.assertEqual(env.get("GH_TOKEN"), TOKEN, name)
        self.assertEqual((by_name["Adjudicate"].get("env") or {}).get("KUBE_AGENTS_ISSUES_REPO"), VARIABLE, "health.json records the repository")

    def test_the_pull_request_comment_and_the_collect_keep_the_jobs_own_token(self):
        by_name = steps()
        for name in OWN_TOKEN_STEPS:
            env = by_name[name].get("env") or {}
            self.assertEqual(env.get("GH_TOKEN"), OWN_TOKEN, name)
            self.assertNotIn("KUBE_AGENTS_ISSUES_REPO", env, name)

    def test_the_token_step_is_guarded_pinned_and_scoped_to_the_named_repository(self):
        by_name = steps()
        split, mint = by_name["Split the issues repository into owner and name"], by_name["Mint a token for the issues repository"]
        self.assertEqual((split.get("if"), mint.get("if")), (GUARD, GUARD))
        self.assertEqual(mint.get("id"), "issues_token")
        self.assertRegex(mint["uses"], PINNED_ACTION)
        with_ = mint["with"]
        self.assertEqual(with_["client-id"], "${{ secrets.ISSUES_APP_ID }}")
        self.assertEqual(with_["private-key"], "${{ secrets.ISSUES_APP_PRIVATE_KEY }}")
        self.assertEqual((with_["owner"], with_["repositories"]), ("${{ steps.issues_repo.outputs.owner }}", "${{ steps.issues_repo.outputs.name }}"))
        self.assertEqual(with_.get("permission-issues"), "write")
        self.assertIs(mint.get("continue-on-error"), True, "a mint that fails must not cancel the dashboard, Chat and PR-comment steps")
        self.assertIn("must be owner/name", split["run"], "the split refuses a value with no slash")
        names = list(steps())
        self.assertLess(names.index("Split the issues repository into owner and name"), names.index("Mint a token for the issues repository"))
        self.assertLess(names.index("Mint a token for the issues repository"), min(names.index(n) for n in ISSUE_STEPS), "minted before any step that uses it")


if __name__ == "__main__":
    unittest.main()
