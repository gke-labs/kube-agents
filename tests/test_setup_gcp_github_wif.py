"""Tests for k8s-operator/scripts/dev/setup-gcp-github-wif.sh.

Asserts that required GCP APIs and IAM roles (for both standard CI and extended
--admin E2E pipelines) remain consistent and complete.
"""

import pathlib
import re
import subprocess
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WIF_SCRIPT = _REPO_ROOT / "k8s-operator" / "scripts" / "dev" / "setup-gcp-github-wif.sh"


_EXPECTED_APIS = [
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "container.googleapis.com",
    "storage.googleapis.com",
    "pubsub.googleapis.com",
    "gkebackup.googleapis.com",
    "logging.googleapis.com",
    "artifactregistry.googleapis.com",
]

_STANDARD_ROLES = [
    "roles/cloudkms.admin",
    "roles/container.admin",
    "roles/compute.viewer",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/serviceusage.serviceUsageConsumer",
]

_ADMIN_ROLES = [
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/pubsub.admin",
    "roles/gkebackup.admin",
    "roles/storage.admin",
    "roles/logging.configWriter",
    "roles/artifactregistry.admin",
]


class SetupGcpGithubWifTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_WIF_SCRIPT.is_file(), f"WIF script not found at {_WIF_SCRIPT}")
        self.script_content = _WIF_SCRIPT.read_text()

    def _get_enabled_services_invocation(self) -> str:
        match = re.search(r"gcloud services enable\s+([\s\S]*?)--project=", self.script_content)
        self.assertIsNotNone(match, "Could not find 'gcloud services enable ... --project=' invocation")
        return match.group(1)

    def _split_script_by_admin_boundary(self) -> tuple[str, str]:
        boundary = 'echo "Admin mode selected.'
        self.assertIn(boundary, self.script_content, "Admin branch boundary not found in script")
        base_part, admin_part = self.script_content.split(boundary, 1)
        return base_part, admin_part

    def test_missing_required_env_vars_fails(self):
        """Executing the script without required env vars should fail with an informative message."""
        env = get_isolated_test_env(
            overrides={
                "PROJECT_ID": "",
                "SA_NAME": "",
                "GITHUB_REPO": "",
            }
        )
        proc = subprocess.run(
            ["bash", str(_WIF_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Please set the required variables", proc.stdout)

    def test_required_services_enabled_in_invocation(self):
        """Ensures all required APIs are explicitly present in the gcloud services enable call."""
        invocation = self._get_enabled_services_invocation()
        active_tokens = [
            line.strip().rstrip("\\").strip()
            for line in invocation.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for api in _EXPECTED_APIS:
            self.assertIn(
                api,
                active_tokens,
                f"Expected API '{api}' missing from gcloud services enable invocation in {_WIF_SCRIPT}",
            )
        self.assertEqual(len(active_tokens), len(_EXPECTED_APIS))

    def test_standard_roles_defined_in_base_tier_only(self):
        """Verifies base standard CI roles are in base ROLES array and admin roles are excluded."""
        base_part, _ = self._split_script_by_admin_boundary()
        base_roles_match = re.search(r"^\s*ROLES=\(\s*\n([\s\S]*?)^\s*\)", base_part, re.MULTILINE)
        self.assertIsNotNone(base_roles_match, "Base ROLES array definition not found")
        base_block = base_roles_match.group(1)

        for role in _STANDARD_ROLES:
            self.assertIn(
                f'"{role}"',
                base_block,
                f"Standard role '{role}' missing in base ROLES array of {_WIF_SCRIPT}",
            )
        for role in _ADMIN_ROLES:
            self.assertNotIn(
                f'"{role}"',
                base_block,
                f"Admin role '{role}' unexpectedly found in base ROLES array of {_WIF_SCRIPT}",
            )

    def test_admin_extended_roles_defined_in_admin_tier_only(self):
        """Verifies extended admin roles are in admin ROLES+= array and base roles are excluded."""
        _, admin_part = self._split_script_by_admin_boundary()
        admin_roles_match = re.search(r"^\s*ROLES\+=\(\s*\n([\s\S]*?)^\s*\)", admin_part, re.MULTILINE)
        self.assertIsNotNone(admin_roles_match, "Admin ROLES+= array definition not found")
        admin_block = admin_roles_match.group(1)

        for role in _ADMIN_ROLES:
            self.assertIn(
                f'"{role}"',
                admin_block,
                f"Admin lifecycle role '{role}' missing in ROLES+= array of {_WIF_SCRIPT}",
            )
        for role in _STANDARD_ROLES:
            self.assertNotIn(
                f'"{role}"',
                admin_block,
                f"Base role '{role}' unexpectedly found in admin ROLES+= array of {_WIF_SCRIPT}",
            )


if __name__ == "__main__":
    unittest.main()
