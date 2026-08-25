"""Unit tests for scripts/release/execute_e2e_tests.py.

Covers the environment the runner hands its pytest child. The suite itself needs a live
GKE cluster, so the child is stubbed and only the environment is asserted.
"""

import importlib.util
import os
import pathlib
import re
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "scripts" / "release" / "execute_e2e_tests.py"
_CONFTEST = _REPO_ROOT / "tests" / "e2e" / "conftest.py"
_CONFIG = _REPO_ROOT / "tests" / "e2e" / "e2e_config.yaml"

# Read by the runner from the ambient environment, so a shell that exports one would
# shadow the YAML block these tests assert about. Cleared per call rather than trusted.
_SHADOWING_KEYS = ("FLEET_AUDIT_STREAMS", "STOCKOUT_SCENARIOS", "GITHUB_ORG", "GITHUB_REPO")


def _load_runner():
    """Imports the runner by path -- scripts/release is not a package.

    GOOGLE_APPLICATION_CREDENTIALS is cleared first: the module shells out to
    `gcloud auth activate-service-account` at import time when it names a real file.
    """
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        spec = importlib.util.spec_from_file_location("execute_e2e_tests", _RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ChildEnvironmentTest(unittest.TestCase):
    """run_environment_tests must name the environment it selected in the child's env."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def _run(self, env, ambient=None):
        """Calls run_environment_tests with the child stubbed, returning its environment."""
        captured = {}
        ambient = ambient or {}

        def fake_run(cmd, env=None, cwd=None):
            captured.update(env or {})
            return mock.Mock(returncode=0)

        base = {
            "GCP_PROJECT_ID": "test-project",
            "GKE_CLUSTER_NAME": "test-cluster",
            "GCP_REGION": "us-east4",
        }
        base.update(ambient)
        with mock.patch.dict(os.environ, base, clear=False), \
                mock.patch.object(self.runner, "subprocess") as sub, \
                mock.patch.object(self.runner, "connect_gke_credentials"), \
                mock.patch.object(self.runner, "find_pytest_executable", return_value="pytest"):
            # The runner builds {**custom_env_vars, **os.environ, ...}, so an exported
            # value deliberately shadows the YAML block's. Without this, a developer who
            # has FLEET_AUDIT_STREAMS set reds `make test-python` on a change they did
            # not make. patch.dict restores whatever we pop here on exit.
            for key in _SHADOWING_KEYS:
                if key not in ambient:
                    os.environ.pop(key, None)
            sub.run.side_effect = fake_run
            self.runner.run_environment_tests(env, {"region": "us-east4"}, [])
        return captured

    def test_selected_environment_is_named_for_the_child(self) -> None:
        captured = self._run({"name": "audit-e2e", "tests": ["tests/e2e/x.py"]})
        self.assertEqual(captured.get("E2E_ENV"), "audit-e2e")

    def test_selection_overrides_a_conflicting_ambient_value(self) -> None:
        """The regression: E2E_ENV must be set after **os.environ, not before.

        e2e-nightly-matrix.yml exports E2E_ENV from a dispatch input whose choices
        include "all". The runner expands that into one child per environment, but the
        ambient "all" rode through to each of them, and conftest matches environment
        names exactly -- so the lookup found nothing. Reorder the dict so **os.environ
        lands last and the leak comes back.
        """
        captured = self._run(
            {"name": "audit-e2e", "tests": ["tests/e2e/x.py"]},
            ambient={"E2E_ENV": "all"},
        )
        self.assertEqual(captured.get("E2E_ENV"), "audit-e2e")

    def test_environment_specific_env_vars_still_reach_the_child(self) -> None:
        captured = self._run(
            {
                "name": "audit-e2e",
                "tests": ["tests/e2e/x.py"],
                "env_vars": {"FLEET_AUDIT_STREAMS": "compliance-audit"},
            }
        )
        self.assertEqual(captured.get("FLEET_AUDIT_STREAMS"), "compliance-audit")
        self.assertEqual(captured.get("E2E_ENV"), "audit-e2e")


class ConfigDefaultTest(unittest.TestCase):
    """The fallback environment name has to be one e2e_config.yaml actually defines."""

    def _environment_names(self):
        import yaml

        config = yaml.safe_load(_CONFIG.read_text())
        return config, {e["name"] for e in config["environments"]}

    def test_configured_default_environment_exists(self) -> None:
        config, names = self._environment_names()
        self.assertIn("default_environment", config["defaults"])
        self.assertIn(config["defaults"]["default_environment"], names)

    def test_hardcoded_fallbacks_name_a_real_environment(self) -> None:
        """Both copies are unreachable while the YAML key exists, and a hard error after.

        Asserted as membership rather than as a literal so that renaming the default --
        updating the YAML and both fallbacks together -- keeps this green.
        """
        _, names = self._environment_names()
        pattern = re.compile(r'default_environment"\s*,\s*"([^"]+)"')
        for path in (_RUNNER, _CONFTEST):
            found = pattern.findall(path.read_text())
            self.assertTrue(found, f"no default_environment fallback found in {path.name}")
            for fallback in found:
                self.assertIn(fallback, names, f"{path.name} falls back to an unknown environment")


if __name__ == "__main__":
    unittest.main()
