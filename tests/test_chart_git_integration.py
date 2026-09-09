"""The chart's forge declaration: one spelling reaches the CR, never two.

`spec.integration.git` is the declaration; `spec.integration.github` is a
deprecated alias for it with `provider: github`. The operator refuses a
PlatformAgent that sets both, because there is no precedence rule that would not
surprise somebody -- so the chart has to refuse it too, at `helm install`, where
the administrator can still see which values file set which field. A chart that
rendered both would produce a CR the API server rejects with an error naming
neither values key.

The minter guard is the same failure one layer down. minty issues GitHub App
installation tokens and nothing else, so `githubMinter.enabled` alongside a
non-GitHub provider is a contradiction. Rendered anyway, it surfaces as the
credential proxy handing the agent a token the forge it talks to does not
accept -- a runtime authentication error a long way from the values file that
caused it.

See docs/designs/multi-forge-support.md §6.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"

# The three harness fields every render needs, whatever it is testing.
_HARNESS = (
    "platformAgent.harness.projectId=p",
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-central1",
)

# githubMinter's own required fields, so a minter render fails on the guard
# under test rather than on a missing value.
_MINTER = (
    "githubMinter.enabled=true",
    "githubMinter.org=gke-labs",
    "githubMinter.repo=gke-labs/kube-agents",
)

_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_MINTER_TEMPLATE = "templates/github-minter.yaml"


def _render(template: str, *sets: str) -> subprocess.CompletedProcess:
    args = ["helm", "template", "t", str(_CHART), "--show-only", template]
    for value in _HARNESS + sets:
        args += ["--set", value]
    return subprocess.run(args, capture_output=True, text=True)


def _integration(*sets: str) -> dict:
    result = _render(_CR_TEMPLATE, *sets)
    if result.returncode != 0:
        raise AssertionError(f"helm template failed: {result.stderr}")
    return (yaml.safe_load(result.stdout)["spec"]).get("integration") or {}


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ChartGitIntegrationTest(unittest.TestCase):
    def test_git_block_reaches_the_cr_with_a_defaulted_provider(self):
        integration = _integration(
            "platformAgent.integration.git.repository=gke-labs/kube-agents"
        )
        self.assertEqual(
            integration.get("git"),
            {"provider": "github", "repository": "gke-labs/kube-agents"},
        )
        # The alias must not be synthesised alongside it: the operator refuses
        # a CR carrying both, so rendering both makes every git-only install
        # un-appliable.
        self.assertNotIn("github", integration)

    def test_the_deprecated_alias_still_renders_unchanged(self):
        """Existing values files are the reason the alias exists at all."""
        integration = _integration(
            "platformAgent.integration.github.org=gke-labs",
            "platformAgent.integration.github.gitRepo=gke-labs/kube-agents",
        )
        self.assertEqual(
            integration.get("github"),
            {"org": "gke-labs", "gitRepo": "gke-labs/kube-agents"},
        )
        self.assertNotIn("git", integration)

    def test_an_explicit_provider_and_host_are_carried_through(self):
        integration = _integration(
            "platformAgent.integration.git.provider=github",
            "platformAgent.integration.git.host=github.com",
            "platformAgent.integration.git.namespace=gke-labs",
            "platformAgent.integration.git.repository=kube-agents",
        )
        self.assertEqual(
            integration.get("git"),
            {
                "provider": "github",
                "host": "github.com",
                "namespace": "gke-labs",
                "repository": "kube-agents",
            },
        )

    def test_declaring_both_spellings_fails_the_render(self):
        result = _render(
            _CR_TEMPLATE,
            "platformAgent.integration.git.repository=gke-labs/kube-agents",
            "platformAgent.integration.github.org=gke-labs",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("not both", result.stderr)

    def test_no_forge_declaration_renders_no_integration_key(self):
        """`integration: {}` is not the same as an absent integration: the
        operator reads a present-but-empty block as a declaration."""
        self.assertEqual(_integration(), {})

    def test_an_unregistered_provider_fails_the_render(self):
        """The CRD's enum would reject it at apply; the chart names the values
        key while the administrator is still looking at their values file."""
        result = _render(
            _CR_TEMPLATE, "platformAgent.integration.git.provider=gitlab"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("platformAgent.integration.git.provider", result.stderr)

    def test_the_minter_never_renders_for_a_non_github_provider(self):
        """Asserts the outcome, not which guard produced it.

        With `github` the only registered provider, `kube-agents.gitProvider`
        refuses `gitlab` before `github-minter.yaml`'s own check can fire. Both
        guards must hold: the minter one is what keeps a GitLab install from
        provisioning a GitHub App token minter once the registry widens.
        """
        result = _render(
            _MINTER_TEMPLATE,
            *_MINTER,
            "platformAgent.integration.git.provider=gitlab",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("kind: Deployment", result.stdout)

    def test_the_no_repository_sentinel_does_not_collide_with_the_git_block(self):
        """`None` means no repository, so it is not a second declaration.

        Reading it as one would make the deprecated key collide with the `git`
        block that replaces it — blocking the migration for exactly the installs
        that opted out of a GitOps repository.
        """
        integration = _integration(
            "platformAgent.integration.github.gitRepo=None",
            "platformAgent.integration.git.repository=gke-labs/kube-agents",
        )
        self.assertEqual(
            integration.get("git"),
            {"provider": "github", "repository": "gke-labs/kube-agents"},
        )

    def test_the_minter_renders_for_github_and_for_no_declaration(self):
        for label, extra in (
            ("no declaration", ()),
            ("git provider github", ("platformAgent.integration.git.provider=github",)),
            ("deprecated alias", ("platformAgent.integration.github.org=gke-labs",)),
        ):
            with self.subTest(declaration=label):
                result = _render(_MINTER_TEMPLATE, *_MINTER, *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("name: github-token-minter", result.stdout)


if __name__ == "__main__":
    unittest.main()
