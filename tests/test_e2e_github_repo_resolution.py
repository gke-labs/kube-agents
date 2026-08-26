"""Unit tests for the owner/repo resolution in tests/e2e/conftest.py.

GH_REPO is bare by repository convention -- reusable-deploy-integrations.yml hands the
org and the repo to the GitHub Token Minter as separate values -- while the e2e suite
needs 'owner/repo': test_agent_fleet_audit.py asserts the shape and
agents/platform/scripts/github_token_refresh.py refuses anything else. The RC pipeline
passed the bare name through and both of those failed on every scheduled run.

Most of these rows are about when composition must not happen. Prefixing an owner onto a
value that is not a repository name produces one that parses: 'test-org-kube-agent/  '
and 'test-org-kube-agent/gke-labs' both satisfy the slash check and the non-empty-halves
check in test_github_target_repository_configuration, so the helper written to surface a
misconfiguration would bury it instead, and the run would fail later against a
repository nobody configured.
"""

import importlib.util
import os
import pathlib
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CONFTEST = _REPO_ROOT / "tests" / "e2e" / "conftest.py"


def _pytest_stub() -> types.ModuleType:
    """A stand-in for pytest, which requirements-test.txt does not install.

    conftest.py touches exactly two pytest attributes at import time: the `fixture`
    decorator and the `Config` annotation on pytest_configure. A third one added at
    module scope later fails the import with an AttributeError naming it.
    """
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *args, **kwargs: (lambda func: func)
    stub.Config = object
    return stub


def _load_conftest():
    """Imports the e2e conftest by path -- tests/e2e is not an importable package.

    The stub replaces pytest even when the real package is installed. `make test-python`
    runs on an interpreter without pytest, so the stub is what makes the import possible
    at all there; forcing it everywhere keeps the module the same shape in both places.
    Under the real pytest the fixtures come back as FixtureFunctionDefinition objects
    that raise when called directly, so the fixture tests below would pass on a checkout
    with tests/e2e/requirements.txt installed and fail without it.

    os.environ is snapshotted because the module calls load_dotenv on a repo-root .env
    at import time, and sys.modules is restored so nothing that runs afterwards picks
    the stub up as pytest.
    """
    with mock.patch.dict(sys.modules, {"pytest": _pytest_stub()}), mock.patch.dict(
        os.environ, {}, clear=False
    ):
        spec = importlib.util.spec_from_file_location("e2e_conftest", _CONFTEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _ConftestTestCase(unittest.TestCase):
    """Loads the module under test once and pins everything it reads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.conftest = _load_conftest()

    def _pinned(self, env=None, config_env_vars=None):
        """Both of the helpers' inputs: the process environment and e2e_config.yaml."""
        return (
            mock.patch.dict(os.environ, env or {}, clear=True),
            mock.patch.object(
                self.conftest,
                "_get_default_config_env",
                return_value={"env_vars": config_env_vars or {}},
            ),
        )


class QualifyRepoTest(_ConftestTestCase):
    """_qualify_repo prefixes an owner onto a bare name, and onto nothing else."""

    def _qualify(self, repo, env=None, config_env_vars=None):
        env_patch, config_patch = self._pinned(env, config_env_vars)
        with env_patch, config_patch:
            return self.conftest._qualify_repo(repo)

    def test_bare_repo_composed_with_env_org(self):
        """The RC pipeline's case: GH_REPO='kube-agents', GH_ORG='gke-labs'."""
        self.assertEqual(
            self._qualify("kube-agents", env={"GITHUB_ORG": "gke-labs"}),
            "gke-labs/kube-agents",
        )

    def test_bare_repo_composed_with_config_org(self):
        """Nothing in the environment: the owner comes from e2e_config.yaml."""
        self.assertEqual(
            self._qualify("agents-repo", config_env_vars={"GITHUB_ORG": "test-org-kube-agent"}),
            "test-org-kube-agent/agents-repo",
        )

    def test_environment_org_beats_config_org(self):
        self.assertEqual(
            self._qualify(
                "kube-agents",
                env={"GITHUB_ORG": "gke-labs"},
                config_env_vars={"GITHUB_ORG": "test-org-kube-agent"},
            ),
            "gke-labs/kube-agents",
        )

    def test_qualified_repo_is_not_prefixed_twice(self):
        """An already-qualified value passes through, whatever the org says."""
        self.assertEqual(
            self._qualify("test-org-kube-agent/agents-repo", env={"GITHUB_ORG": "gke-labs"}),
            "test-org-kube-agent/agents-repo",
        )

    def test_surrounding_whitespace_is_trimmed_before_composing(self):
        self.assertEqual(
            self._qualify(" agents-repo ", env={"GITHUB_ORG": "myorg"}),
            "myorg/agents-repo",
        )

    def test_blank_repo_is_never_composed(self):
        """A value with no name in it must not acquire an owner.

        'test-org-kube-agent/  ' satisfies both the slash check and the
        non-empty-halves check in test_github_target_repository_configuration.
        """
        for blank in ("  ", "\t", "\n", ""):
            with self.subTest(repo=blank):
                self.assertIsNone(self._qualify(blank, env={"GITHUB_ORG": "test-org-kube-agent"}))

    def test_half_written_repo_is_passed_through_not_composed(self):
        """A missing half must reach the caller as the missing half.

        Stripping the stray slash first would turn 'gke-labs/' into the bare name
        'gke-labs' and compose 'test-org-kube-agent/gke-labs' -- a repository that
        parses, belongs to the wrong owner, and first surfaces as a 404. Passed through,
        'gke-labs/' fails the caller's structure check quoting the value.
        """
        for value, expected in (
            ("/", "/"),
            ("gke-labs/", "gke-labs/"),
            ("gke-labs//", "gke-labs//"),
            ("/kube-agents", "/kube-agents"),
            ("  gke-labs/  ", "gke-labs/"),
        ):
            with self.subTest(repo=value):
                result = self._qualify(value, env={"GITHUB_ORG": "test-org-kube-agent"})
                self.assertEqual(result, expected)
                self.assertNotIn("test-org-kube-agent", result)

    def test_blank_org_is_not_an_org(self):
        """An exported-but-empty GITHUB_ORG is the unset case, not a 'None/repo' prefix."""
        self.assertEqual(self._qualify("kube-agents", env={"GITHUB_ORG": "  "}), "kube-agents")

    def test_bare_repo_without_any_org_stays_bare(self):
        """No owner anywhere -- the caller's shape assertion has to stay reachable.

        Not a configuration CI can produce, since every e2e environment hard-codes
        GITHUB_ORG.
        """
        result = self._qualify("kube-agents")
        self.assertEqual(result, "kube-agents")
        self.assertNotIn("/", result)

    def test_missing_repo_passes_through(self):
        self.assertIsNone(self._qualify(None, env={"GITHUB_ORG": "gke-labs"}))


class ResolveGithubOrgTest(_ConftestTestCase):
    """_resolve_github_org reads the owner without consulting GITHUB_REPO."""

    def _resolve(self, env=None, config_env_vars=None):
        env_patch, config_patch = self._pinned(env, config_env_vars)
        with env_patch, config_patch:
            return self.conftest._resolve_github_org()

    def test_environment_wins_over_config(self):
        self.assertEqual(
            self._resolve(env={"GITHUB_ORG": "gke-labs"}, config_env_vars={"GITHUB_ORG": "other"}),
            "gke-labs",
        )

    def test_falls_back_to_config(self):
        self.assertEqual(self._resolve(config_env_vars={"GITHUB_ORG": "gke-labs"}), "gke-labs")

    def test_whitespace_and_slashes_trimmed(self):
        """An owner has no slash of its own, so a stray one is noise here."""
        self.assertEqual(self._resolve(env={"GITHUB_ORG": " gke-labs/ "}), "gke-labs")

    def test_blank_falls_through_to_config(self):
        """A blank environment value must not shadow a real one in the config."""
        self.assertEqual(
            self._resolve(env={"GITHUB_ORG": "   "}, config_env_vars={"GITHUB_ORG": "gke-labs"}),
            "gke-labs",
        )
        self.assertEqual(
            self._resolve(env={"GITHUB_ORG": "/"}, config_env_vars={"GITHUB_ORG": "gke-labs"}),
            "gke-labs",
        )

    def test_absent_everywhere_is_none(self):
        self.assertIsNone(self._resolve())

    def test_does_not_read_github_repo(self):
        """The recursion guard: github_repo may still be mid-resolution."""
        self.assertIsNone(self._resolve(env={"GITHUB_REPO": "gke-labs/kube-agents"}))


class FixtureWiringTest(_ConftestTestCase):
    """The fixtures apply the helpers -- deleting the calls has to fail something.

    Only the environment branch of github_repo is exercised, which returns before the
    kubectl lookup, so nothing here shells out.
    """

    def _repo(self, env=None, config_env_vars=None):
        env_patch, config_patch = self._pinned(env, config_env_vars)
        with env_patch, config_patch:
            return self.conftest.github_repo("kubeagents-system")

    def _org(self, repo, env=None, config_env_vars=None):
        env_patch, config_patch = self._pinned(env, config_env_vars)
        with env_patch, config_patch:
            return self.conftest.github_org(repo)

    def test_github_repo_qualifies_a_bare_environment_value(self):
        self.assertEqual(
            self._repo(env={"GITHUB_REPO": "kube-agents", "GITHUB_ORG": "gke-labs"}),
            "gke-labs/kube-agents",
        )

    def test_github_repo_qualifies_gitops_repo_too(self):
        self.assertEqual(
            self._repo(env={"GITOPS_REPO": "kube-agents", "GITHUB_ORG": "gke-labs"}),
            "gke-labs/kube-agents",
        )

    def test_github_repo_qualifies_the_config_value(self):
        self.assertEqual(
            self._repo(config_env_vars={"GITHUB_REPO": "agents-repo", "GITHUB_ORG": "test-org"}),
            "test-org/agents-repo",
        )

    def test_github_repo_leaves_a_qualified_value_alone(self):
        self.assertEqual(
            self._repo(env={"GITHUB_REPO": "myorg/myrepo", "GITHUB_ORG": "gke-labs"}),
            "myorg/myrepo",
        )

    def test_the_two_fixtures_agree_on_the_owner(self):
        """What test_github_target_repository_configuration's last assertion compares."""
        env = {"GITHUB_REPO": " kube-agents ", "GITHUB_ORG": " gke-labs "}
        repo = self._repo(env=env)
        self.assertEqual(repo, "gke-labs/kube-agents")
        self.assertEqual(self._org(repo, env=env), repo.split("/", 1)[0])

    def test_github_org_falls_back_to_the_repository_owner(self):
        """Precedence: a qualified GITHUB_REPO outranks the config's GITHUB_ORG."""
        self.assertEqual(
            self._org("myorg/myrepo", config_env_vars={"GITHUB_ORG": "test-org-kube-agent"}),
            "myorg",
        )


if __name__ == "__main__":
    unittest.main()
