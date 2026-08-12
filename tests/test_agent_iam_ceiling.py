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
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "k8s-operator" / "scripts"
COMMON_SH = SCRIPTS / "common.sh"
PROVISION_IAM_SH = SCRIPTS / "provision_04_gcp_iam.sh"
IAM_MODULE = REPO_ROOT / "terraform" / "modules" / "kube-agents-iam"

# The exact set provision_04_gcp_iam.sh grants for `read-only`. Written out
# rather than derived so that widening it is a visible diff here too.
READ_ONLY_ROLES = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
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


def _hcl_string_list(source: str, name: str) -> list[str]:
    """The string entries of a named HCL list, in order.

    Deliberately crude — a regex over the source rather than an HCL parse — but
    anchored hard enough to fail rather than to quietly return nothing: the
    block must be found, and it must be non-empty. A test that silently compares
    two empty lists is the failure this whole file exists to prevent.
    """
    block = re.search(
        rf"^\s*{re.escape(name)}\s*=\s*\[(.*?)^\s*\]",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if block is None:
        raise AssertionError(f"no list named {name} found; it moved or was renamed")
    roles = re.findall(r'"([^"]+)"', block.group(1))
    if not roles:
        raise AssertionError(f"the list named {name} parsed as empty")
    return roles


class TerraformAndScriptAgreeTest(unittest.TestCase):
    """The two provisioning paths grant the same thing, and that is checked.

    `terraform/modules/kube-agents-iam` and `provision_04_gcp_iam.sh` each grant
    the agent's service account a read-only role set, and until now the only
    thing asserting they matched was a sentence in the Terraform variable's
    description saying it mirrored the script. A description does not fail.
    Somebody widening one path would have left the other alone and nothing would
    have said so.

    The lists are compared as *sets against the base set*, not as one list
    against the other, because they legitimately differ by one entry once a
    scoped service account pool is in play — see the scoped-pool test below.
    """

    def test_the_terraform_base_set_is_the_script_s_read_only_set(self):
        source = (IAM_MODULE / "main.tf").read_text(encoding="utf-8")
        self.assertEqual(
            READ_ONLY_ROLES,
            _hcl_string_list(source, "agent_read_only_roles"),
            "the Terraform module and provision_04_gcp_iam.sh no longer grant the "
            "same read-only set; widening one path and not the other is how an "
            "install ends up with a ceiling nobody chose",
        )

    def test_the_terraform_base_set_grants_no_forbidden_role(self):
        source = (IAM_MODULE / "main.tf").read_text(encoding="utf-8")
        self.assertEqual(
            set(),
            set(_hcl_string_list(source, "agent_read_only_roles")) & FORBIDDEN_ROLES,
        )


class ScopedPoolCeilingTest(unittest.TestCase):
    """What the agent's own identity keeps once the pool carries the reads.

    The pool exists because impersonation constrains only the RBAC half of GKE's
    IAM-or-RBAC union: an identity holding roles/container.viewer reads objects
    in every cluster in the project no matter how narrow its Kubernetes RBAC is.
    Moving that role onto per-cluster accounts is only worth anything if it also
    comes *off* the agent, and this is what says so.

    It matters more than an ordinary least-privilege tidy-up because the agent
    container can reach the metadata server in a default install and mint a token
    for this identity without going near the broker. Everything the broker
    enforces is bypassable that way; the size of this role set is not.
    """

    def roles(self) -> list[str]:
        source = (IAM_MODULE / "main.tf").read_text(encoding="utf-8")
        base = _hcl_string_list(source, "agent_read_only_roles")
        expression = re.search(
            r"agent_project_roles\s*=\s*\((.*?)\n  \)", source, re.DOTALL
        )
        self.assertIsNotNone(
            expression, "the computed agent role set moved or was renamed"
        )
        self.assertIn(
            'role != "roles/container.viewer"',
            expression.group(1),
            "the pool no longer removes roles/container.viewer from the agent's own "
            "grant, so the per-cluster accounts are an addition rather than a "
            "replacement and the wide ceiling is still there",
        )
        return [role for role in base if role != "roles/container.viewer"]

    def test_with_a_pool_the_agent_keeps_nothing_in_container_but_cluster_viewer(self):
        container_roles = [
            role for role in self.roles() if role.startswith("roles/container.")
        ]
        self.assertEqual(["roles/container.clusterViewer"], container_roles)

    def test_the_role_it_keeps_is_the_one_get_credentials_needs(self):
        """Narrowing to nothing would be a different bug.

        `container.clusterViewer` carries container.clusters.get and .list, which
        is what `gcloud container clusters get-credentials` and the fleet
        reconcile loop run on. Dropping it too would break the broker's own
        kubeconfig materialisation, and the failure would look like a pool
        problem rather than a ceiling problem.
        """
        self.assertIn("roles/container.clusterViewer", self.roles())

    def test_the_pool_grants_token_creator_per_account_and_never_project_wide(self):
        """The one line that decides whether the pool is a boundary.

        roles/iam.serviceAccountTokenCreator at project scope would let the agent
        mint a token for any service account in the project — a general
        escalation primitive that makes the per-cluster accounts decorative,
        since the agent could just become something wider. Bound on each pool
        member as a resource, the set of identities it can become is exactly the
        pool, and every member is narrower than the agent already is.

        Asserted by resource type, because the difference between safe and
        catastrophic here is `google_service_account_iam_member` versus
        `google_project_iam_member` and nothing else in the block would look
        wrong.
        """
        source = (IAM_MODULE / "scoped_pool.tf").read_text(encoding="utf-8")
        grants = re.findall(
            r'resource\s+"(google_\w+_iam_member)"\s+"[^"]*"\s*\{(.*?)\n\}',
            source,
            re.DOTALL,
        )
        self.assertTrue(grants, "no IAM member resources found in scoped_pool.tf")
        token_creator = [
            resource_type
            for resource_type, body in grants
            if "roles/iam.serviceAccountTokenCreator" in body
        ]
        self.assertEqual(
            ["google_service_account_iam_member"],
            token_creator,
            "the token-creator grant is not bound on the service account as a "
            "resource; at project scope it lets the agent impersonate anything",
        )

    def test_the_condition_names_the_cluster_the_account_claims_to_scope(self):
        """The checker and the enforcer must spell the cluster identically.

        Terraform writes the IAM Condition; the broker builds the lookup key.
        This asserts the Terraform half renders the same string the broker's
        `scope_key` does, by importing the broker's own function rather than
        restating the format — a second copy of the format here would make the
        test agree with itself.
        """
        sys.path.insert(0, str(REPO_ROOT / "agents" / "platform" / "scripts"))
        try:
            import scoped_sa_pool
        finally:
            sys.path.pop(0)

        source = (IAM_MODULE / "scoped_pool.tf").read_text(encoding="utf-8")
        template = re.search(r'expression\s*=\s*"(.*?)"\n', source)
        self.assertIsNotNone(template, "the IAM Condition expression moved")
        key_template = re.search(
            r'for cluster in var\.scoped_clusters :\s*\n\s*"([^"]+)"', source
        )
        self.assertIsNotNone(key_template, "the scope key template moved")

        rendered_key = (
            key_template.group(1)
            .replace("${cluster.project_id}", "kagents-dev")
            .replace("${cluster.location}", "us-east4")
            .replace("${cluster.cluster_name}", "ka-test")
        )
        rendered_expression = template.group(1).replace(
            '${each.key}', rendered_key
        ).replace('\\"', '"')

        self.assertEqual(
            scoped_sa_pool.scope_key("kagents-dev", "us-east4", "ka-test"),
            rendered_key,
        )
        self.assertEqual(
            scoped_sa_pool.iam_condition_expression(rendered_key),
            rendered_expression,
        )


if __name__ == "__main__":
    unittest.main()
