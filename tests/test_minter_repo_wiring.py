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

The second half pins the coupling between the Helm chart / Kustomize policy keys
and volume mount paths: Minty evaluates `<org>/<repo>` under `<CONFIGS_DIR>/<org>/<repo>.yaml`,
so the ConfigMap must carry key `<bareRepo>.yaml` and the Deployment must mount at
`/etc/minty/<org>`.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_KUSTOMIZE_GITHUB = (
    _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "github"
)

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


class MinterChartMountCouplingTest(unittest.TestCase):
    """The Helm chart and Kustomize templates couple the policy key to the mount path.

    Minty evaluates requests for `<org>/<repo>` by loading `<CONFIGS_DIR>/<org>/<repo>.yaml`.
    With `CONFIGS_DIR=/etc/minty`, the Deployment mounts the ConfigMap at `/etc/minty/<org>`,
    and the ConfigMap must carry key `<bareRepo>.yaml`.

    If the ConfigMap key does not match `<bareRepo>.yaml`, or the volume mount path does
    not match `/etc/minty/<org>`, Minty returns HTTP 403 during the window before the
    operator's first reconcile adopts the keys.
    """

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_helm_template_renders_repo_key_and_org_mount_path(self) -> None:
        test_cases = [
            ("bare repo", "test-repo", "test-repo.yaml"),
            ("repo slug", "test-org/test-repo", "test-repo.yaml"),
            ("full url", "https://github.com/test-org/test-repo.git", "test-repo.yaml"),
        ]
        for desc, repo_val, expected_key in test_cases:
            with self.subTest(desc=desc, repo=repo_val):
                cmd = [
                    "helm",
                    "template",
                    "test-release",
                    str(_CHART),
                    "--set-string",
                    "platformAgent.harness.clusterName=test-cluster",
                    "--set-string",
                    "platformAgent.harness.location=us-central1",
                    "--set-string",
                    "platformAgent.harness.projectId=test-project",
                    "--set",
                    "githubMinter.enabled=true",
                    "--set-string",
                    "githubMinter.org=test-org",
                    "--set-string",
                    f"githubMinter.repo={repo_val}",
                    "-s",
                    "templates/github-minter.yaml",
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
                docs = list(yaml.safe_load_all(proc.stdout))

                # Find the minter ConfigMap
                cm = next(
                    (
                        d
                        for d in docs
                        if d
                        and d.get("kind") == "ConfigMap"
                        and d.get("metadata", {}).get("name")
                        == "github-token-minter-config"
                    ),
                    None,
                )
                self.assertIsNotNone(
                    cm,
                    "github-token-minter-config ConfigMap not found in rendered manifests",
                )
                self.assertIn(
                    expected_key,
                    cm.get("data", {}),
                    f"ConfigMap data must carry key {expected_key!r} matching the bare repository name",
                )

                # Find the minter Deployment
                deploy = next(
                    (
                        d
                        for d in docs
                        if d
                        and d.get("kind") == "Deployment"
                        and d.get("metadata", {}).get("name")
                        == "github-token-minter"
                    ),
                    None,
                )
                self.assertIsNotNone(
                    deploy,
                    "github-token-minter Deployment not found in rendered manifests",
                )
                container = deploy["spec"]["template"]["spec"]["containers"][0]

                # Verify CONFIGS_DIR env var
                env_vars = {
                    e["name"]: e.get("value")
                    for e in container.get("env", [])
                    if "name" in e
                }
                self.assertEqual(env_vars.get("CONFIGS_DIR"), "/etc/minty")

                # Verify volumeMount matches /etc/minty/<org>
                mounts = {
                    m["name"]: m.get("mountPath")
                    for m in container.get("volumeMounts", [])
                }
                self.assertEqual(
                    mounts.get("config-volume"),
                    "/etc/minty/test-org",
                    "Deployment must mount config-volume at /etc/minty/<org>",
                )

    def test_kustomize_templates_couple_repo_key_and_org_mount_path(self) -> None:
        """k8s-operator/config/integrations/github templates couple ${GITHUB_REPO}.yaml and /etc/minty/${GITHUB_ORG}."""
        cm_template = (_KUSTOMIZE_GITHUB / "configmap.yaml.template").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "${GITHUB_REPO}.yaml:",
            cm_template,
            "configmap.yaml.template must carry `${GITHUB_REPO}.yaml:` key",
        )

        deploy_template = (
            _KUSTOMIZE_GITHUB / "deployment.yaml.template"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "mountPath: /etc/minty/${GITHUB_ORG}",
            deploy_template,
            "deployment.yaml.template must mount config-volume at `/etc/minty/${GITHUB_ORG}`",
        )
        self.assertIn(
            'value: "/etc/minty"',
            deploy_template,
            "deployment.yaml.template must set CONFIGS_DIR to `/etc/minty`",
        )


if __name__ == "__main__":
    unittest.main()
