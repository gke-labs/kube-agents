"""The repository the RC's GitHub token minter is scoped to.

Two workflows have to name the same repository, and it must not be the release
repository. `deploy-environment.yml` hands GITOPS_ORG/GITOPS_REPO to install.sh,
which is what installer_common.sh scopes the minter's tokens to; `e2e-run.yml`
hands the same pair to the E2E suites, and `e2e-manual-runner.yml` to the ones
it dispatches, because a token minted for one repository does not authenticate
against another.

The two sides spell the keys differently on purpose. Since #1026 the installer's
inputs are GITOPS_ORG/GITOPS_REPO; the E2E suite's own variables for "the
repository a test acts on" are still GITHUB_ORG/GITHUB_REPO. What has to match
is the value, not the key, so that is what these assertions compare.

The hazard is that every other workflow in this repository uses vars.GH_ORG /
vars.GH_REPO for "the repository", and on the `rc` environment that pair names
gke-labs/kube-agents -- what common.sh's get_target_repo resolves for tag and
release operations. "Tidying" either side onto it is the natural-looking edit,
it scopes a live GitHub App token at the release repository, and nothing else
in the suite would go red. Hence these assertions.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

# workflow -> (step `name:` carrying the pair, org key, repo key). Anchored on
# the step name rather than the `run:` because e2e-run.yml has two steps that
# run suites -- the blocking gate and the optional list, through different
# scripts -- and both are handed a repository.
#
# The installer takes GITOPS_*; the E2E suites take GITHUB_*. Both must resolve
# to the same GitHub environment variables.
_CONSUMERS = {
    "deploy-environment.yml": (
        "Provision Environment in GCP", "GITOPS_ORG", "GITOPS_REPO",
    ),
    "e2e-run.yml": ("Execute Blocking E2E Gate", "GITHUB_ORG", "GITHUB_REPO"),
    "e2e-run.yml#optional": (
        "Execute Optional E2E Suites", "GITHUB_ORG", "GITHUB_REPO",
    ),
    "e2e-manual-runner.yml": ("Execute E2E Tests", "GITHUB_ORG", "GITHUB_REPO"),
}

# The pair that must never appear on these keys: on `rc` it is the release repo.
_FORBIDDEN = ("vars.GH_ORG", "vars.GH_REPO")


def _step_env(workflow_name: str, step_name: str) -> dict:
    # A "file.yml#suffix" key lets one workflow appear twice with two steps.
    doc = yaml.safe_load((_WORKFLOWS / workflow_name.split("#", 1)[0]).read_text())
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("name") == step_name:
                return step.get("env") or {}
    raise AssertionError(f"{workflow_name} has no step named {step_name!r}")


class MinterRepositoryWiringTest(unittest.TestCase):
    def test_both_consumers_read_the_gitops_pair(self) -> None:
        for workflow, (step, org_key, repo_key) in _CONSUMERS.items():
            with self.subTest(workflow=workflow):
                env = _step_env(workflow, step)
                self.assertIn("vars.GITOPS_ORG", env.get(org_key, ""))
                self.assertIn("vars.GITOPS_REPO", env.get(repo_key, ""))

    def test_the_installer_takes_the_pair_under_its_own_names(self) -> None:
        """The workflow passes the installer's GITOPS_* names straight through. (see #1026)

        `GITHUB_ORG: ${{ vars.GITOPS_ORG }}` invites a "fix" to vars.GH_ORG,
        which is exactly how a live App token gets scoped at the release
        repository. The installer's inputs are GITOPS_*, so the workflow
        passes them straight through — and, for as long as validated
        candidates predating the rename can be deployed, it passes the old
        names as well, because those trees read only GITHUB_* and their
        half-configured-minter guard refuses the deploy with them empty.
        The transition pair must map to vars.GITOPS_*, never vars.GH_*;
        drop it together with the provision_rc_environment.sh fallback.
        """
        env = _step_env(
            "deploy-environment.yml", _CONSUMERS["deploy-environment.yml"][0]
        )
        self.assertIn("vars.GITOPS_ORG", env.get("GITOPS_ORG", ""))
        self.assertIn("vars.GITOPS_REPO", env.get("GITOPS_REPO", ""))
        for legacy, expected in (
            ("GITHUB_ORG", "vars.GITOPS_ORG"),
            ("GITHUB_REPO", "vars.GITOPS_REPO"),
        ):
            self.assertIn(
                expected,
                env.get(legacy, expected),
                f"{legacy} is the transition spelling for pre-rename candidates; "
                f"it may only carry {expected}",
            )

    def test_neither_consumer_falls_back_to_the_release_repository(self) -> None:
        for workflow, (step, org_key, repo_key) in _CONSUMERS.items():
            env = _step_env(workflow, step)
            for key in (org_key, repo_key):
                for forbidden in _FORBIDDEN:
                    with self.subTest(workflow=workflow, key=key, var=forbidden):
                        self.assertNotIn(
                            forbidden,
                            env.get(key, ""),
                            f"{workflow} scopes {key} to {forbidden}; on the `rc` "
                            "environment that is the release repository, so a live "
                            "GitHub App token would be minted against it",
                        )

    def test_the_two_consumers_agree(self) -> None:
        """A minter scoped to one repository and a suite probing another fails
        as an authentication error against a repository nobody configured.

        Compared by VALUE across both key spellings: the installer takes
        GITOPS_*, the E2E suites GITHUB_*, and what matters is that they resolve
        to the same GitHub environment variables.
        """
        orgs, repos = set(), set()
        for workflow, (step, org_key, repo_key) in _CONSUMERS.items():
            env = _step_env(workflow, step)
            orgs.add(env.get(org_key))
            repos.add(env.get(repo_key))
        self.assertEqual(
            len(orgs), 1, f"the installer and the E2E suites disagree on the org: {orgs}"
        )
        self.assertEqual(
            len(repos), 1,
            f"the installer and the E2E suites disagree on the repo: {repos}",
        )

    def test_the_app_id_is_a_secret_not_a_var(self) -> None:
        env = _step_env(
            "deploy-environment.yml", _CONSUMERS["deploy-environment.yml"][0]
        )
        self.assertIn("secrets.GH_APP_ID", env.get("GITHUB_APP_ID", ""))


if __name__ == "__main__":
    unittest.main()
