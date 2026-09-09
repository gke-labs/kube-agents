"""Unit tests for terraform/examples/full-install/lifecycle.sh.

Tests safety guards in lifecycle.sh before terraform apply:
- guard_gsa_identity: prevents accidental GSA destruction and replace-under-auto-approve
  when agent_service_account_id override goes missing or changes against existing state.
- guard_cluster_ownership: prevents cluster destruction when create_cluster is false
  against a state that manages the cluster.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LIFECYCLE_SH = _REPO_ROOT / "terraform" / "examples" / "full-install" / "lifecycle.sh"


class LifecycleScriptGuardTest(unittest.TestCase):
    def _run_guard(self, func_call, state_list="", state_show="", tfvar_agent_sa="null", tfvar_create_cluster="true"):
        """Run a lifecycle.sh function against stubbed terraform commands."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()

            # Stub terraform CLI to return configured state list, state show, and console outputs
            terraform_stub = bin_dir / "terraform"
            terraform_stub.write_text(f"""#!/usr/bin/env bash
set -e
cmd="${{1:-}}"
if [[ "$cmd" == "state" && "${{2:-}}" == "list" ]]; then
    cat << 'EOF'
{state_list}
EOF
    exit 0
elif [[ "$cmd" == "state" && "${{2:-}}" == "show" ]]; then
    cat << 'EOF'
{state_show}
EOF
    exit 0
elif [[ "$cmd" == "console" ]]; then
    read -r expr
    if [[ "$expr" == *"agent_service_account_id"* ]]; then
        echo '{tfvar_agent_sa}'
        exit 0
    elif [[ "$expr" == *"create_cluster"* ]]; then
        echo '{tfvar_create_cluster}'
        exit 0
    fi
    echo 'null'
    exit 0
fi
exit 0
""")
            terraform_stub.chmod(0o755)

            script = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_LIFECYCLE_SH}"
{func_call}
"""
            env = get_isolated_test_env(bin_dir=str(bin_dir))
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(_REPO_ROOT / "terraform" / "examples" / "full-install"),
            )

    def test_guard_gsa_identity_no_op_when_gsa_not_in_state(self):
        """When GSA is not in state (first apply), guard_gsa_identity is a silent no-op."""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list="",
            tfvar_agent_sa="null",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    def test_guard_gsa_identity_passes_when_override_matches_state(self):
        """When state has an override GSA and the run resolves the same override, apply proceeds."""
        state_list = "module.kube_agents_iam.google_service_account.agent"
        state_show = """# module.kube_agents_iam.google_service_account.agent:
resource "google_service_account" "agent" {
    account_id   = "kubeagents-platform-gsa-2"
    project      = "test-proj"
}
"""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list=state_list,
            state_show=state_show,
            tfvar_agent_sa='"kubeagents-platform-gsa-2"',
        )
        self.assertEqual(proc.returncode, 0, f"unexpected failure: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_guard_gsa_identity_passes_when_default_name_matches_state(self):
        """When state has the default GSA and variable is unset (null), apply proceeds."""
        state_list = "module.kube_agents_iam.google_service_account.agent"
        state_show = """# module.kube_agents_iam.google_service_account.agent:
resource "google_service_account" "agent" {
    account_id   = "kubeagents-platform-gsa"
    project      = "test-proj"
}
"""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list=state_list,
            state_show=state_show,
            tfvar_agent_sa="null",
        )
        self.assertEqual(proc.returncode, 0, f"unexpected failure: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_guard_gsa_identity_reads_a_typed_null_as_the_default(self):
        """terraform console prints an unset nullable variable as tostring(null).
        Read as a name, it disagreed with every state and refused every apply
        whose tfvars left the variable alone -- the autopush deploys after #1309."""
        state_list = "module.kube_agents_iam.google_service_account.agent"
        state_show = """# module.kube_agents_iam.google_service_account.agent:
resource "google_service_account" "agent" {
    account_id   = "kubeagents-platform-gsa"
    project      = "test-proj"
}
"""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list=state_list,
            state_show=state_show,
            tfvar_agent_sa="tostring(null)",
        )
        self.assertEqual(proc.returncode, 0, f"unexpected failure: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_a_typed_null_still_refuses_a_lost_override(self):
        """When state has an override GSA but variable resolves to a typed null,
        the fallback default name still disagrees with state and refuses destruction."""
        state_list = "module.kube_agents_iam.google_service_account.agent"
        state_show = """resource "google_service_account" "agent" {
    account_id   = "kubeagents-platform-gsa-2"
}
"""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list=state_list,
            state_show=state_show,
            tfvar_agent_sa="tostring(null)",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent_service_account_id resolved to 'kubeagents-platform-gsa', but this state manages GSA 'kubeagents-platform-gsa-2'", proc.stderr)
        self.assertIn("Applying now would plan the service account's DESTRUCTION and recreation under -auto-approve.", proc.stderr)

    def test_tfvar_reads_a_typed_null_as_empty(self):
        """tfvar should normalize typed nulls (tostring(null)) to empty string."""
        proc = self._run_guard(
            'printf "[%s]" "$(tfvar agent_service_account_id)"',
            tfvar_agent_sa="tostring(null)",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]")

    def test_tfvar_reads_bare_null_as_empty(self):
        """tfvar should normalize bare null to empty string."""
        proc = self._run_guard(
            'printf "[%s]" "$(tfvar agent_service_account_id)"',
            tfvar_agent_sa="null",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[]")

    def test_guard_gsa_identity_refuses_when_override_lost_and_resolves_to_default(self):
        """When state has override GSA but variable resolves to default, apply refuses before terraform runs."""
        state_list = "module.kube_agents_iam.google_service_account.agent"
        state_show = """# module.kube_agents_iam.google_service_account.agent:
resource "google_service_account" "agent" {
    account_id   = "kubeagents-platform-gsa-2"
    project      = "test-proj"
}
"""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list=state_list,
            state_show=state_show,
            tfvar_agent_sa="null",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent_service_account_id resolved to 'kubeagents-platform-gsa', but this state manages GSA 'kubeagents-platform-gsa-2'", proc.stderr)
        self.assertIn("Applying now would plan the service account's DESTRUCTION and recreation under -auto-approve.", proc.stderr)
        self.assertIn('TF_VAR_agent_service_account_id="kubeagents-platform-gsa-2"', proc.stderr)

    def test_guard_gsa_identity_refuses_when_override_differs_from_state(self):
        """When state has one override GSA and variable resolves to a different override, apply refuses."""
        state_list = "module.kube_agents_iam.google_service_account.agent"
        state_show = """# module.kube_agents_iam.google_service_account.agent:
resource "google_service_account" "agent" {
    account_id   = "kubeagents-platform-gsa-1"
    project      = "test-proj"
}
"""
        proc = self._run_guard(
            "guard_gsa_identity",
            state_list=state_list,
            state_show=state_show,
            tfvar_agent_sa='"kubeagents-platform-gsa-2"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent_service_account_id resolved to 'kubeagents-platform-gsa-2', but this state manages GSA 'kubeagents-platform-gsa-1'", proc.stderr)
        self.assertIn('TF_VAR_agent_service_account_id="kubeagents-platform-gsa-1"', proc.stderr)

    def test_guard_cluster_ownership_refuses_when_create_cluster_false_against_managed_cluster(self):
        """When create_cluster is false but state manages cluster, apply refuses destruction."""
        state_list = "module.gke_cluster.google_container_cluster.standard[0]"
        proc = self._run_guard(
            "guard_cluster_ownership",
            state_list=state_list,
            tfvar_create_cluster='"false"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("create_cluster is false, but this state already manages the cluster", proc.stderr)
        self.assertIn("Applying now would plan the cluster's DESTRUCTION", proc.stderr)


if __name__ == "__main__":
    unittest.main()
