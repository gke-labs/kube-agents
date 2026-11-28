"""Unit tests for scripts/release/resolve_artifact_registry_hosts.sh.

release-publish.yml logs docker in to the hosts this script writes, with a
Google access token as the password. The script writes a host only for a
registry variable whose host is an Artifact Registry Docker host, writes
nothing for an unset variable or another kind of registry, and fails for a
value that carries the Artifact Registry suffix anywhere but in a valid host.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import MOCK_DEFAULT_REGISTRY_PREFIX, get_isolated_test_env
from tests.testing.release import MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX, MOCK_AR_REGISTRY_PREFIX

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "resolve_artifact_registry_hosts.sh"
_AR_HOST = "us-docker.pkg.dev"


class ResolveArtifactRegistryHostsTest(unittest.TestCase):
    def _resolve(self, candidate=None, additional=None):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        output = pathlib.Path(temp_dir.name) / "github_output"
        output.touch()
        env = {"GITHUB_OUTPUT": str(output)}
        if candidate is not None:
            env["CANDIDATE_REGISTRY_PREFIX"] = candidate
        if additional is not None:
            env["ADDITIONAL_RELEASE_REGISTRY_PREFIX"] = additional
        proc = subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env),
            cwd=str(_REPO_ROOT),
        )
        return proc, output.read_text().splitlines()

    def test_each_artifact_registry_variable_writes_its_host(self):
        proc, lines = self._resolve(MOCK_AR_REGISTRY_PREFIX, MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines, [f"candidate={_AR_HOST}", f"additional={_AR_HOST}"])

    def test_a_regional_host_is_written_whole(self):
        proc, lines = self._resolve(additional="us-central1-docker.pkg.dev/project/repository")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines, ["additional=us-central1-docker.pkg.dev"])

    def test_only_the_variable_that_is_set_writes(self):
        proc, lines = self._resolve(candidate=MOCK_AR_REGISTRY_PREFIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines, [f"candidate={_AR_HOST}"])
        proc, lines = self._resolve(additional=MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(lines, [f"additional={_AR_HOST}"])

    def test_unset_empty_or_another_registry_writes_nothing(self):
        for candidate, additional in (
            (None, None),
            ("", ""),
            (MOCK_DEFAULT_REGISTRY_PREFIX, "registry.example.com/kube-agents"),
        ):
            with self.subTest(candidate=candidate, additional=additional):
                proc, lines = self._resolve(candidate, additional)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(lines, [])

    def test_a_value_that_only_looks_like_artifact_registry_fails_and_names_no_host(self):
        """The suffix in the path, behind another host, or a host with a
        character no host has: each would send the token somewhere else."""
        for value in (
            "registry.example.com/mirror/us-docker.pkg.dev/x",
            "ghcr.io/org/x-docker.pkg.dev/y",
            "evil.example?x-docker.pkg.dev/project/repository",
            "US-docker.pkg.dev/project/repository",
        ):
            for variable in ("candidate", "additional"):
                with self.subTest(value=value, variable=variable):
                    proc, lines = self._resolve(**{variable: value})
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("::error title=Not an Artifact Registry host::", proc.stdout)
                    self.assertEqual(lines, [])

    def test_a_bad_additional_registry_fails_after_a_good_candidate(self):
        proc, lines = self._resolve(MOCK_AR_REGISTRY_PREFIX, "ghcr.io/org/x-docker.pkg.dev/y")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ADDITIONAL_RELEASE_REGISTRY_PREFIX", proc.stdout)


if __name__ == "__main__":
    unittest.main()
