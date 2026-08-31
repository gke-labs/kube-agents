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
    """run_suite_tests must name the suite it selected in the child's env."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def _run(self, env, ambient=None):
        """Calls run_suite_tests with the child stubbed, returning its environment."""
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
            self.runner.run_suite_tests(env, {"region": "us-east4"}, [])
        return captured

    def test_selected_suite_is_named_for_the_child(self) -> None:
        captured = self._run({"name": "audit", "tests": ["tests/e2e/x.py"]})
        self.assertEqual(captured.get("E2E_SUITE"), "audit")

    def test_the_deprecated_alias_is_exported_alongside_the_real_name(self) -> None:
        """E2E_ENV keeps working for one release, so both carry the same value.

        Deleting the alias export is a deliberate act; this fails when it happens
        by accident, and is deleted with it.
        """
        captured = self._run({"name": "audit", "tests": ["tests/e2e/x.py"]})
        self.assertEqual(captured.get("E2E_ENV"), captured.get("E2E_SUITE"))

    def test_selection_overrides_a_conflicting_ambient_value(self) -> None:
        """The regression: the suite name must be set after **os.environ, not before.

        A caller can select every environment at once, and e2e-manual-runner.yml
        dispatches exactly that. No workflow exports an ambient E2E_ENV=all today --
        e2e-run.yml is the only writer and writes one suite name -- so this ordering
        is the only thing standing between here and the regression returning. The runner expands that into one child per environment, but the
        ambient "all" rode through to each of them, and conftest matches environment
        names exactly -- so the lookup found nothing. Reorder the dict so **os.environ
        lands last and the leak comes back.
        """
        captured = self._run(
            {"name": "audit", "tests": ["tests/e2e/x.py"]},
            ambient={"E2E_SUITE": "all", "E2E_ENV": "all"},
        )
        self.assertEqual(captured.get("E2E_SUITE"), "audit")
        self.assertEqual(captured.get("E2E_ENV"), "audit")

    def test_suite_specific_env_vars_still_reach_the_child(self) -> None:
        captured = self._run(
            {
                "name": "audit",
                "tests": ["tests/e2e/x.py"],
                "env_vars": {"FLEET_AUDIT_STREAMS": "compliance-audit"},
            }
        )
        self.assertEqual(captured.get("FLEET_AUDIT_STREAMS"), "compliance-audit")
        self.assertEqual(captured.get("E2E_SUITE"), "audit")


class ConfigDefaultTest(unittest.TestCase):
    """The fallback suite name has to be one e2e_config.yaml actually defines."""

    def _suite_names(self):
        import yaml

        config = yaml.safe_load(_CONFIG.read_text())
        return config, {s["name"] for s in config["suites"]}

    def test_configured_default_suite_exists(self) -> None:
        config, names = self._suite_names()
        self.assertIn("default_suite", config["defaults"])
        self.assertIn(config["defaults"]["default_suite"], names)

    def test_no_suite_name_still_carries_the_e2e_suffix(self) -> None:
        """The suffix was redundant on every value and is what the rename dropped."""
        _, names = self._suite_names()
        offenders = sorted(n for n in names if n.endswith("-e2e"))
        self.assertEqual(offenders, [], f"suite names still suffixed: {offenders}")

    def test_hardcoded_fallbacks_name_a_real_suite(self) -> None:
        """Both copies are unreachable while the YAML key exists, and a hard error after.

        Asserted as membership rather than as a literal so that renaming the default --
        updating the YAML and both fallbacks together -- keeps this green. Both
        spellings are matched: the legacy `default_environment` fallback is still
        present for one release and must name a real suite too.
        """
        _, names = self._suite_names()
        # Matches the literal key and the module constant the runner reads it
        # through, so moving the legacy spelling into a named constant does not
        # silently empty this test.
        pattern = re.compile(
            r'(?:default_(?:suite|environment)"|_LEGACY_DEFAULT_KEY)\s*,\s*"([^"]+)"'
        )
        for path in (_RUNNER, _CONFTEST):
            found = pattern.findall(path.read_text())
            self.assertTrue(found, f"no default-suite fallback found in {path.name}")
            for fallback in found:
                self.assertIn(fallback, names, f"{path.name} falls back to an unknown suite")


class LegacySuiteSuffixTest(unittest.TestCase):
    """The values are the half of the rename no name-level alias covers.

    `E2E_ENV` -> `E2E_SUITE`, `--env` -> `--suite` and `environments:` ->
    `suites:` are all aliased, so a reader is entitled to believe the old
    spellings keep working. `E2E_ENV=rc-e2e` is an old spelling, and until the
    suffix is stripped it reaches the lookup and exits 1 as an unknown suite.
    """

    def setUp(self) -> None:
        self.runner = _load_runner()

    def test_the_suffix_is_stripped(self) -> None:
        for legacy, canonical in (
            ("rc-e2e", "rc"),
            ("nightly-e2e", "nightly"),
            ("gchat-e2e", "gchat"),
        ):
            self.assertEqual(self.runner.canonical_suite_name(legacy), canonical)

    def test_a_current_name_is_left_alone(self) -> None:
        for name in ("rc", "nightly", "gchat", "agent-plugin"):
            self.assertEqual(self.runner.canonical_suite_name(name), name)

    def test_a_bare_suffix_is_not_stripped_to_nothing(self) -> None:
        """Guards the slice: an empty suite name would match every entry's absence."""
        self.assertEqual(self.runner.canonical_suite_name("-e2e"), "-e2e")

    def test_the_chat_suite_needs_no_cluster_under_its_legacy_name(self) -> None:
        """A pre-rename config names the suite `gchat-e2e`, and that name reaches
        the cluster gate directly rather than through the selector — so the
        selector-side strip does not cover it.

        Without the strip the one suite that deliberately needs no cluster exits 1
        demanding GCP_PROJECT_ID, which is the opposite of what the deprecation
        note in the runner promises.
        """
        self.assertEqual(
            self.runner.canonical_suite_name("gchat-e2e"),
            self.runner._CHAT_ONLY_SUITE,
        )


if __name__ == "__main__":
    unittest.main()
