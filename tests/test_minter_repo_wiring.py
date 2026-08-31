"""The repository the RC's GitHub token minter is scoped to.

Two workflows have to name the same repository, and it must not be the release
repository. `deploy-environment.yml` hands GITOPS_ORG/GITOPS_REPO to
install.sh as GITHUB_ORG/GITHUB_REPO, which is what installer_common.sh scopes
the minter's tokens to; `e2e-run.yml` hands the same pair to the E2E suites,
and `e2e-manual-runner.yml` to the ones it dispatches, because a token minted
for one repository does not authenticate against another.

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

# workflow -> the step `name:` carrying the pair. Anchored on the name rather
# than the `run:` because e2e-run.yml has two steps that run suites -- the
# blocking gate and the optional list, through different scripts -- and both are
# handed a repository.
_CONSUMERS = {
    "deploy-environment.yml": "Provision Environment in GCP",
    "e2e-run.yml": "Execute Blocking E2E Gate",
    "e2e-run.yml#optional": "Execute Optional E2E Suites",
    "e2e-manual-runner.yml": "Execute E2E Tests",
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
        for workflow, step in _CONSUMERS.items():
            with self.subTest(workflow=workflow):
                env = _step_env(workflow, step)
                self.assertIn("vars.GITOPS_ORG", env.get("GITHUB_ORG", ""))
                self.assertIn("vars.GITOPS_REPO", env.get("GITHUB_REPO", ""))

    def test_neither_consumer_falls_back_to_the_release_repository(self) -> None:
        for workflow, step in _CONSUMERS.items():
            env = _step_env(workflow, step)
            for key in ("GITHUB_ORG", "GITHUB_REPO"):
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
        as an authentication error against a repository nobody configured."""
        envs = [_step_env(w, s) for w, s in _CONSUMERS.items()]
        self.assertEqual(
            {e.get("GITHUB_ORG") for e in envs},
            {envs[0].get("GITHUB_ORG")},
            "the installer and the E2E suites disagree on GITHUB_ORG",
        )
        self.assertEqual(
            {e.get("GITHUB_REPO") for e in envs},
            {envs[0].get("GITHUB_REPO")},
            "the installer and the E2E suites disagree on GITHUB_REPO",
        )

    def test_the_app_id_is_a_secret_not_a_var(self) -> None:
        env = _step_env(
            "deploy-environment.yml", _CONSUMERS["deploy-environment.yml"]
        )
        self.assertIn("secrets.GH_APP_ID", env.get("GITHUB_APP_ID", ""))


if __name__ == "__main__":
    unittest.main()
