"""The IAM ceiling the agent GSA is provisioned with.

GKE authorizes a request if EITHER IAM or Kubernetes RBAC allows it. The agent's
Kubernetes RBAC is read-only, but that constrains only one half of the union: a
GSA holding `roles/container.admin` is authorized by IAM no matter how narrow the
KSA is. `roles/container.admin` also carries `container.clusters.impersonate`,
and IAM has no `resourceNames` equivalent, so the grant cannot be scoped to a
cluster or a principal.

The provisioner used to offer that as one word, `PLATFORM_AGENT_PERMISSION_SET=gke-admin`.
These tests are what keeps it gone. They run the real bash — `common.sh` sourced,
`get_platform_agent_roles` lifted out of `provision_04_gcp_iam.sh` — rather than
grepping for a string, so re-adding the bundle under a different arm name is
caught by the accepted-value test even if the role list is spelled differently.

`custom` remains, so a deployment that needs broad roles still has a path; it just
has to name each role. That is deliberately not tested as "safe" — a `custom` set
can grant anything. What is tested is that no *built-in* set does.

Run:
  python3 -m unittest discover -s tests -p 'test_agent_iam_ceiling.py' -v
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "k8s-operator" / "scripts"
COMMON_SH = SCRIPTS / "common.sh"
PROVISION_IAM_SH = SCRIPTS / "provision_04_gcp_iam.sh"

# The exact set provision_04_gcp_iam.sh grants for `read-only`. Written out
# rather than derived so that widening it is a visible diff here too.
READ_ONLY_ROLES = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
]

# Roles no built-in permission set may grant the agent GSA. The first two are the
# structural ones (IAM-side authorization that outranks RBAC, plus unscopable
# impersonation); the rest were in the removed bundle and would come back with it.
FORBIDDEN_ROLES = {
    "roles/container.admin",
    "roles/container.clusterAdmin",
    "roles/container.developer",
    "roles/container.hostServiceAgentUser",
    "roles/monitoring.admin",
    "roles/logging.admin",
    "roles/owner",
    "roles/editor",
    "roles/iam.serviceAccountTokenCreator",
}

# Values a human or a stale vars.sh might plausibly carry. Everything here that
# is not `read-only` or `custom` must be rejected outright.
REJECTED_VALUES = [
    "gke-admin",
    "GKE-ADMIN",
    "  gke-admin  ",
    "gke_admin",
    "admin",
    "cluster-admin",
    "gke-owner",
    "readonly",
]


def _run_bash(script: str, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Run `script` with common.sh already sourced, in a throwaway state dir.

    CI=1 makes `init_var` take defaults instead of blocking on a prompt, and
    VARS_FILE points at a temp file so nothing touches the developer's real
    (git-ignored) k8s-operator/scripts/vars.sh. TERM=dumb keeps common.sh's
    EXIT trap (`tput cnorm`) from writing cursor escapes into the stdout the
    role list is read from.
    """
    with tempfile.TemporaryDirectory() as state_dir:
        env = dict(os.environ)
        env.pop("PLATFORM_AGENT_PERMISSION_SET", None)
        env.pop("PLATFORM_AGENT_CUSTOM_ROLES", None)
        env.update(
            {
                "CI": "1",
                "TERM": "dumb",
                "SCRIPT_DIR": str(SCRIPTS),
                "VARS_FILE": str(Path(state_dir) / "vars.sh"),
            }
        )
        env.update(env_overrides)
        return subprocess.run(
            ["bash", "-c", f'source "$SCRIPT_DIR/common.sh"\n{script}'],
            capture_output=True,
            text=True,
            env=env,
        )


def _extract_function(script: Path, name: str) -> str:
    """The bash source of one top-level function, for evaluation in isolation.

    provision_04_gcp_iam.sh runs its pipeline at import, so it cannot be sourced;
    the function is lifted out instead. The extraction is anchored to a
    column-zero `}` — the repository's shell style — and asserts it found
    something, so a reformat turns into a failure rather than a silent pass.
    """
    body = re.search(
        rf"^{re.escape(name)}\(\) \{{$.*?^\}}$",
        script.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    if body is None:
        raise AssertionError(f"{script} no longer defines a top-level {name}()")
    return body.group(0)


class PermissionSetValidatorTest(unittest.TestCase):
    """`init_var_platform_agent_permission_set` is the only entry point."""

    def _validate(self, value: str) -> subprocess.CompletedProcess:
        return _run_bash(
            "init_var_platform_agent_permission_set",
            {"PLATFORM_AGENT_PERMISSION_SET": value},
        )

    def test_gke_admin_is_rejected(self):
        result = self._validate("gke-admin")
        self.assertNotEqual(
            0,
            result.returncode,
            "PLATFORM_AGENT_PERMISSION_SET=gke-admin must fail provisioning; it grants "
            "roles/container.admin, which authorizes the agent through IAM regardless "
            "of its Kubernetes RBAC.\n" + result.stdout + result.stderr,
        )

    def test_gke_admin_says_why_rather_than_just_invalid(self):
        """A cached vars.sh from before the removal has to be diagnosable."""
        combined = self._validate("gke-admin")
        self.assertIn("has been removed", combined.stdout + combined.stderr)

    def test_only_read_only_and_custom_are_accepted(self):
        for value in REJECTED_VALUES:
            with self.subTest(value=value):
                self.assertNotEqual(
                    0,
                    self._validate(value).returncode,
                    f"{value!r} must not be an accepted permission set",
                )

    def test_read_only_is_accepted(self):
        self.assertEqual(0, self._validate("read-only").returncode)

    def test_custom_is_accepted_when_roles_are_named(self):
        result = _run_bash(
            "init_var_platform_agent_permission_set",
            {
                "PLATFORM_AGENT_PERMISSION_SET": "custom",
                "PLATFORM_AGENT_CUSTOM_ROLES": "roles/container.viewer",
            },
        )
        self.assertEqual(
            0,
            result.returncode,
            "`custom` is the documented path for a deployment that needs broader "
            "roles; removing it would leave no supported alternative.\n"
            + result.stdout
            + result.stderr,
        )


class PlatformAgentRolesTest(unittest.TestCase):
    """`get_platform_agent_roles` is what the grant is actually built from."""

    def _roles(self, env_overrides: dict[str, str]) -> list[str]:
        result = _run_bash(
            _extract_function(PROVISION_IAM_SH, "get_platform_agent_roles")
            + "\nget_platform_agent_roles",
            env_overrides,
        )
        self.assertEqual(
            0, result.returncode, result.stdout + result.stderr
        )
        return result.stdout.split()

    def test_default_is_the_read_only_set(self):
        self.assertEqual(READ_ONLY_ROLES, self._roles({}))

    def test_read_only_grants_no_forbidden_role(self):
        self.assertEqual(
            set(),
            set(self._roles({"PLATFORM_AGENT_PERMISSION_SET": "read-only"}))
            & FORBIDDEN_ROLES,
        )

    def test_gke_admin_falls_back_to_read_only_instead_of_granting_admin(self):
        """Defence in depth for a caller that skipped the validator.

        The validator rejects `gke-admin` before this function is reached, but
        `get_platform_agent_roles` is also called directly by the verify/execute
        pair. Reaching it with a removed value must produce the least-privilege
        set, not an empty one (which would read as "grant nothing" and pass a
        weaker assertion) and certainly not the admin bundle.
        """
        self.assertEqual(
            READ_ONLY_ROLES, self._roles({"PLATFORM_AGENT_PERMISSION_SET": "gke-admin"})
        )

    def test_no_builtin_set_can_produce_a_forbidden_role(self):
        for value in ["", "read-only", "gke-admin", "GKE-ADMIN", "bogus"]:
            with self.subTest(value=value):
                granted = set(self._roles({"PLATFORM_AGENT_PERMISSION_SET": value}))
                self.assertEqual(
                    set(),
                    granted & FORBIDDEN_ROLES,
                    f"permission set {value!r} grants a role that authorizes the "
                    "agent through IAM independently of its Kubernetes RBAC",
                )

    def test_the_prompt_does_not_offer_a_set_the_validator_rejects(self):
        """The prompt string is the operator-facing list; it has to match."""
        prompt = re.search(
            r'init_var "PLATFORM_AGENT_PERMISSION_SET" [^\n]*"([^"]*)"',
            COMMON_SH.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(prompt, "the permission-set prompt moved or was renamed")
        self.assertNotIn("gke-admin", prompt.group(1))


if __name__ == "__main__":
    unittest.main()
