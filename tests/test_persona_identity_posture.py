"""Regression tests for platform and cluster persona host/cluster identity posture (#3350).

Verifies that:
1. agents/platform/SOUL.md directs the platform worker to resolve host cluster
   queries immediately from environment variables without fleet enumeration,
   naming only variables the shell sandbox session receives (`GKE_PROJECT_ID`,
   not `GCP_PROJECT_ID`, which is set on the agent container only).
2. Both agents/platform/SOUL.md and agents/cluster/SOUL.md inline the canonical
   Google Cloud Console URL templates and name no path under
   `/opt/defaults/docs/`, which the sandbox file tool cannot open, so workers do
   not spend tool turns searching the filesystem for gcp-console-links.md.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_identity_posture.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SOUL = REPO_ROOT / "agents/platform/SOUL.md"
CLUSTER_SOUL = REPO_ROOT / "agents/cluster/SOUL.md"

# A SOUL.md shorter than this was truncated or replaced, not edited.
MIN_SOUL_CHARS = 1000
IDENTITY_BULLET = "- **Host and Cluster Identity Queries:**"
PROJECT_RESOLUTION_BULLET = "- **Dynamic Project Resolution:**"
SANDBOX_UNREACHABLE_DOCS = "/opt/defaults/docs/"


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < MIN_SOUL_CHARS:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


def _line_starting(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starts with {prefix!r}")


class PersonaIdentityPostureTests(unittest.TestCase):
    def test_platform_persona_resolves_cluster_identity_immediately(self):
        bullet = _line_starting(_read(PLATFORM_SOUL), IDENTITY_BULLET)
        for var in ("$GKE_CLUSTER_NAME", "$GKE_PROJECT_ID", "$GKE_LOCATION", "$KUBE_CONTEXT_NAME"):
            self.assertIn(var, bullet, f"identity bullet must cite {var} for immediate resolution")
        self.assertIn(
            "Do not enumerate the fleet",
            bullet,
            "identity bullet must forbid enumerating the fleet for basic host queries",
        )

    def test_platform_identity_bullet_names_only_sandbox_forwarded_vars(self):
        soul = _read(PLATFORM_SOUL)
        bullet = _line_starting(soul, IDENTITY_BULLET)
        entrypoint = _read(REPO_ROOT / "deploy/sandbox/entrypoint.sh")
        match = re.search(r'SANDBOX_FORWARDED_ENV_NAMES="([^"]+)"', entrypoint)
        self.assertIsNotNone(match, "deploy/sandbox/entrypoint.sh must define SANDBOX_FORWARDED_ENV_NAMES")
        forwarded = set(match.group(1).split())

        # Assert every environment variable cited in the identity bullet is forwarded to the sandbox
        vars_in_bullet = set(re.findall(r'\$([A-Z0-9_]+)', bullet))
        self.assertTrue(
            vars_in_bullet.issubset(forwarded),
            f"identity bullet names vars not in SANDBOX_FORWARDED_ENV_NAMES: {vars_in_bullet - forwarded}",
        )
        self.assertNotIn(
            "$GCP_PROJECT_ID",
            bullet,
            "GCP_PROJECT_ID is not forwarded to the shell sandbox session "
            "(deploy/sandbox/entrypoint.sh SANDBOX_FORWARDED_ENV_NAMES); name GKE_PROJECT_ID",
        )
        # That bullet quotes a literal `$GCP_PROJECT_ID` as prose to avoid, so
        # only the equivalence claim is checked, not every mention.
        self.assertNotIn(
            "equivalently `$GCP_PROJECT_ID`",
            _line_starting(soul, PROJECT_RESOLUTION_BULLET),
            "Dynamic Project Resolution must not call GCP_PROJECT_ID equivalent: "
            "the shell sandbox session does not receive it",
        )

    def test_platform_and_cluster_personas_inline_console_links(self):
        for path in (PLATFORM_SOUL, CLUSTER_SOUL):
            with self.subTest(persona=path.parent.name):
                soul = _read(path)
                self.assertIn(
                    "https://console.cloud.google.com/logs/query",
                    soul,
                    f"{path} must inline the Cloud Logging URL template",
                )
                self.assertIn(
                    "https://console.cloud.google.com/kubernetes/workload/overview",
                    soul,
                    f"{path} must inline the GKE Workloads Console URL template",
                )
                self.assertNotIn(
                    SANDBOX_UNREACHABLE_DOCS,
                    soul,
                    f"{path} names a {SANDBOX_UNREACHABLE_DOCS} path the sandbox file tool cannot open",
                )


if __name__ == "__main__":
    unittest.main()
