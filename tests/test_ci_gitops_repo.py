"""Tests for the GitOps-repository resolution in hack/ci-deploy.sh.

The presubmit eval's GitHub-writing scenarios read the `Git Repo:` line out of
/opt/data/SETTINGS.md, which the operator renders from the PlatformAgent CR's
spec.integration.github.gitRepo. CI has to supply that value, and the whole
point of how it supplies it is the *failure* behaviour:

* one GitOps repository per leasable project, never a shared default -- two
  Boskos leases must not write to the same ledger issue;
* an unmapped project stops the deploy, in Prow and on a laptop alike, rather
  than silently installing an agent with an empty gitRepo (every scenario then
  fails at step 0 for a reason no log explains) or, worse, one pointed at some
  other project's repository;
* under Prow the in-repo table is the only source, because the project is
  leased per run and a value pinned in the job environment would eventually
  outlive the lease it was written for.

None of that is observable from a successful run, which is exactly why it
regresses quietly. The block is extracted from the script by its section
markers and executed, so these assertions are against the code that ships
rather than a copy.
"""

import pathlib
import re
import subprocess
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

# The section this suite exercises, and the marker that ends it. Both are
# asserted below, so renaming a section fails here loudly instead of silently
# shrinking what is tested.
_SECTION_START = "# ─── 2b. GitOps Repository for This Run"
_SECTION_END_RE = re.compile(r"^# ─── (?!2b\.)", re.MULTILINE)

# Repeated here on purpose rather than parsed out of the script: a test that
# derives the expected mapping from the mapping under test asserts nothing.
# These pairs are also the table in
# docs/site/src/content/docs/deploy/ci-pool-projects.md.
_EXPECTED_MAPPING = {
    "kube-agents-evals": "gke-agentic/kube-agents-evals-infra",
    "kube-agents-evals-2": "gke-agentic/kube-agents-evals-2-infra",
    "kube-agents-evals-3": "gke-agentic/kube-agents-evals-3-infra",
    "kube-agents-evals-4": "gke-agentic/kube-agents-evals-4-infra",
    "kube-agents-evals-5": "gke-agentic/kube-agents-evals-5-infra",
    "kube-agents-evals-6": "gke-agentic/kube-agents-evals-6-infra",
    "kube-agents-evals-7": "gke-agentic/kube-agents-evals-7-infra",
    "kube-agents-evals-8": "gke-agentic/kube-agents-evals-8-infra",
    "kube-agents-evals-9": "gke-agentic/kube-agents-evals-9-infra",
    "kube-agents-evals-10": "gke-agentic/kube-agents-evals-10-infra",
}

# The fail-closed tests need a project the mapping will never contain, and for
# a while that was "kube-agents-evals-3" — the obvious next name in the
# sequence, picked because nothing could plausibly claim it. The pool claimed
# it: the project was added to Boskos on 2026-08-21 and every presubmit that
# leased it died on the unmapped-project refusal. Mapping it turned this suite
# red, which is the test doing its job, but it also showed the fixture was
# wrong to begin with. A placeholder that is a plausible future value of the
# thing it stands outside of is a placeholder with an expiry date on it, and
# the next name in the sequence has the same date on it.
_NEVER_MAPPED_PROJECT = "not-a-pool-project-fixture"


def _resolution_block():
    text = _CI_DEPLOY.read_text(encoding="utf-8")
    start = text.find(_SECTION_START)
    assert start != -1, f"{_SECTION_START!r} not found in hack/ci-deploy.sh"
    end_match = _SECTION_END_RE.search(text, start + len(_SECTION_START))
    assert end_match, "no section marker follows the GitOps-repository block"
    return text[start : end_match.start()]


class CiDeployGitopsRepoTest(unittest.TestCase):
    maxDiff = None

    def _resolve(self, project_id, **env):
        """Run the resolution block and report what it decided.

        Returns (returncode, stdout, stderr). On success stdout's last line is
        `RESOLVED <gitRepo>|<helm minter args>`, with an empty gitRepo meaning
        the GitHub integration is deliberately off.
        """
        script = _resolution_block() + (
            '\nprintf "RESOLVED %s|%s\\n" "${GITOPS_REPO}" "${GITHUB_MINTER_ARGS[*]}"\n'
        )
        # set -euo pipefail mirrors the script's own header: a resolution that
        # only "fails" by leaving a variable unset must show up as a failure.
        proc = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PROJECT_ID": project_id,
                    # Cleared unless a case sets them: the ambient shell must
                    # not decide whether this looks like a Prow run.
                    "PULL_NUMBER": "",
                    "JOB_NAME": "",
                    "EVAL_GITOPS_REPO": "",
                    "EVAL_GITHUB_APP_ID": "",
                    **env,
                }
            ),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _resolved_repo(self, stdout):
        line = [ln for ln in stdout.splitlines() if ln.startswith("RESOLVED ")][-1]
        return line[len("RESOLVED ") :].split("|", 1)[0]

    def _minter_args(self, stdout):
        line = [ln for ln in stdout.splitlines() if ln.startswith("RESOLVED ")][-1]
        return line.split("|", 1)[1]

    # --- the mapping ------------------------------------------------------

    def test_each_pool_project_maps_to_its_own_repository(self):
        for project, repo in _EXPECTED_MAPPING.items():
            with self.subTest(project=project):
                rc, out, err = self._resolve(project, PULL_NUMBER="123", JOB_NAME="pull-eval")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._resolved_repo(out), repo)

    def test_no_two_projects_share_a_repository(self):
        repos = list(_EXPECTED_MAPPING.values())
        self.assertEqual(len(repos), len(set(repos)))

    def test_the_fail_closed_fixture_is_not_a_mapped_project(self):
        """Keeps the fail-closed tests honest about what they prove.

        If the fixture ever becomes a real mapping, `test_unmapped_project_*`
        starts asserting that a *mapped* project is refused — the opposite of
        its name. It would fail loudly here rather than quietly inverting its
        own meaning, which is the failure the kube-agents-evals-3 fixture was
        one onboarding away from.
        """
        self.assertNotIn(_NEVER_MAPPED_PROJECT, _EXPECTED_MAPPING)
        self.assertNotIn(_NEVER_MAPPED_PROJECT, _resolution_block())

    # --- fail-closed ------------------------------------------------------

    def test_unmapped_project_fails_the_prow_deploy(self):
        rc, out, err = self._resolve(
            _NEVER_MAPPED_PROJECT, PULL_NUMBER="123", JOB_NAME="pull-eval"
        )
        self.assertNotEqual(rc, 0)
        self.assertNotIn("RESOLVED", out)
        # The message has to name the edit, or the next person onboarding a
        # Boskos project has a red job and no lead.
        self.assertIn("gitops_repo_for_project", err)

    def test_unmapped_project_fails_a_local_run_with_the_escape_hatches(self):
        rc, out, err = self._resolve("some-developer-project")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("RESOLVED", out)
        self.assertIn("EVAL_GITOPS_REPO=owner/repo", err)
        self.assertIn("EVAL_GITOPS_REPO=none", err)

    def test_prow_run_refuses_a_pinned_override(self):
        rc, _, err = self._resolve(
            "kube-agents-evals",
            PULL_NUMBER="123",
            JOB_NAME="pull-eval",
            EVAL_GITOPS_REPO="gke-agentic/some-other-infra",
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("EVAL_GITOPS_REPO", err)

    def test_malformed_override_is_rejected(self):
        for value in (
            "https://github.com/acme/fleet",
            "git@github.com:acme/fleet.git",
            "acme",
            "acme/fleet/extra",
            "acme/fleet; rm -rf /",
        ):
            with self.subTest(value=value):
                rc, out, _ = self._resolve("dev-project", EVAL_GITOPS_REPO=value)
                self.assertNotEqual(rc, 0)
                self.assertNotIn("RESOLVED", out)

    # --- the explicit opt-outs -------------------------------------------

    def test_local_override_is_honoured(self):
        rc, out, err = self._resolve("dev-project", EVAL_GITOPS_REPO="gke-agentic/scratch-infra")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resolved_repo(out), "gke-agentic/scratch-infra")

    def test_none_is_the_only_route_to_an_empty_gitrepo(self):
        rc, out, err = self._resolve("dev-project", EVAL_GITOPS_REPO="none")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._resolved_repo(out), "")

    # --- the minter half --------------------------------------------------

    def test_minter_stays_off_until_the_app_id_is_supplied(self):
        # The minter Deployment is in the release `helm --wait` gates on, and
        # it cannot pass readiness before a human has imported the GitHub App
        # key into the project's KMS key. Defaulting it on would fail every
        # presubmit in a project that has not been through that step.
        rc, out, err = self._resolve("kube-agents-evals", PULL_NUMBER="123")
        self.assertEqual(rc, 0, err)
        self.assertIn("githubMinter.enabled=false", self._minter_args(out))

    def test_minter_is_scoped_to_the_leased_project_repository(self):
        rc, out, err = self._resolve(
            "kube-agents-evals-2", PULL_NUMBER="123", EVAL_GITHUB_APP_ID="123456"
        )
        self.assertEqual(rc, 0, err)
        args = self._minter_args(out)
        self.assertIn("githubMinter.enabled=true", args)
        self.assertIn("githubMinter.org=gke-agentic", args)
        self.assertIn("githubMinter.repo=kube-agents-evals-2-infra", args)
        self.assertIn("githubMinter.appId=123456", args)

    def test_minter_stays_off_when_the_github_integration_is_off(self):
        rc, out, err = self._resolve(
            "dev-project", EVAL_GITOPS_REPO="none", EVAL_GITHUB_APP_ID="123456"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("githubMinter.enabled=false", self._minter_args(out))


class CiDeployWiringTest(unittest.TestCase):
    """The resolved value has to reach helm, and reach it early enough."""

    def setUp(self):
        self.text = _CI_DEPLOY.read_text(encoding="utf-8")

    def test_helm_receives_the_resolved_repository(self):
        self.assertIn(
            '--set-string "platformAgent.integration.github.gitRepo=${GITOPS_REPO}"',
            self.text,
        )
        self.assertIn('"${GITHUB_MINTER_ARGS[@]}"', self.text)

    def test_resolution_runs_before_the_image_build(self):
        # A ~20-minute Cloud Build submit sits between the two. Resolving
        # after it would burn the whole build to report a one-line
        # configuration error.
        resolution = self.text.find(_SECTION_START)
        build = self.text.find("gcloud builds submit")
        self.assertNotEqual(resolution, -1)
        self.assertNotEqual(build, -1)
        self.assertLess(resolution, build)

    def test_ci_deploy_parses(self):
        subprocess.run(["bash", "-n", str(_CI_DEPLOY)], check=True)


if __name__ == "__main__":
    unittest.main()
