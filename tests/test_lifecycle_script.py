"""Unit tests for terraform/examples/full-install/lifecycle.sh.

Tests safety guards in lifecycle.sh before terraform apply:
- guard_gsa_identity: prevents accidental GSA destruction and replace-under-auto-approve
  when agent_service_account_id override goes missing or changes against existing state.
- guard_cluster_ownership: prevents cluster destruction when create_cluster is false
  against a state that manages the cluster.
- guard_kms_identity: prevents the CMEK key ring or key being replaced (and the live
  key's versions scheduled for destruction) when GKE_DB_KMS_KEYRING / GKE_DB_KMS_KEY
  disagree with state.
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
    def _run_guard(self, func_call, state_list="", state_show="", tfvar_agent_sa="null",
                   tfvar_create_cluster="true", gcloud_stub="exit 1",
                   tfvar_namespace='"kubeagents-system"',
                   tfvar_kms_keyring='"platform-agent-keyring"',
                   tfvar_kms_key='"k8s-secret-encryption-key"',
                   tfvar_cluster_name="null",
                   tfvar_enable_minter="false",
                   gcloud_key_version="",
                   gcloud_kms_fail=False,
                   gcloud_kms_error="ERROR: permission denied",
                   gcloud_kms_notice="",
                   tfvar_enable_google_chat="true",
                   tfvar_chat_sub_name='"platform-agent-chat-events-sub"'):
        """Run a lifecycle.sh function against stubbed terraform and gcloud commands."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            if gcloud_kms_fail:
                kms_behavior = f"echo '{gcloud_kms_error}' >&2; exit 1"
            else:
                # gcloud_kms_notice models a warning written to stderr on a zero exit.
                notice = f"echo '{gcloud_kms_notice}' >&2; " if gcloud_kms_notice else ""
                kms_behavior = f"{notice}echo '{gcloud_key_version}'; exit 0"
            gcloud.write_text(f"""#!/usr/bin/env bash
set -e
if [[ "$*" == *"kms keys versions list"* ]]; then
    {kms_behavior}
fi
{gcloud_stub}
""")
            gcloud.chmod(0o755)

            # Stub terraform CLI to return configured state list, state show, and console outputs
            terraform_stub = bin_dir / "terraform"
            terraform_stub.write_text(f"""#!/usr/bin/env bash
set -e
cmd="${{1:-}}"
if [[ "$cmd" == "state" && "${{2:-}}" == "list" ]]; then
    # One line per read, for the test that counts them.
    echo "state list" >> "${{TF_STUB_LOG:-/dev/null}}"
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
    elif [[ "$expr" == *"namespace"* ]]; then
        echo '{tfvar_namespace}'
        exit 0
    elif [[ "$expr" == *"kms_keyring_name"* ]]; then
        echo '{tfvar_kms_keyring}'
        exit 0
    elif [[ "$expr" == *"kms_key_name"* ]]; then
        echo '{tfvar_kms_key}'
        exit 0
    elif [[ "$expr" == *"cluster_name"* ]]; then
        echo '{tfvar_cluster_name}'
        exit 0
    elif [[ "$expr" == *"enable_github_minter"* ]]; then
        echo '{tfvar_enable_minter}'
        exit 0
    elif [[ "$expr" == *"github_minter_kms_keyring"* ]]; then
        echo '"github-token-minter-keyring"'
        exit 0
    elif [[ "$expr" == *"github_minter_kms_key"* ]]; then
        echo '"github-token-minter-key"'
        exit 0
    elif [[ "$expr" == *"project_id"* ]]; then
        echo '"test-project"'
        exit 0
    elif [[ "$expr" == *"location"* ]]; then
        echo '"us-central1-c"'
        exit 0
    elif [[ "$expr" == *"enable_google_chat"* ]]; then
        echo '{tfvar_enable_google_chat}'
        exit 0
    elif [[ "$expr" == *"chat_subscription_name"* ]]; then
        echo '{tfvar_chat_sub_name}'
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

    def test_the_default_gsa_name_comes_from_the_defaults_file(self):
        """lifecycle.sh sources install.defaults.env rather than spelling the
        name a third time; a guard against the module default is only right
        while the two agree, which the defaults file is what keeps true."""
        proc = self._run_guard('printf "%s" "$DEFAULT_PLATFORM_AGENT_GSA_NAME"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "kubeagents-platform-gsa")

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
        self.assertIn('PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa-2"', proc.stderr)

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
        self.assertIn('PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa-1"', proc.stderr)

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

    def test_guard_cluster_ownership_names_a_shared_prefix_when_the_state_holds_another_cluster(self):
        """Under a shared custom KUBE_AGENTS_STATE_PREFIX the managed entry can be
        some other cluster; "set create_cluster = true" would then plan that
        one's replacement, so the remedy is the prefix, not the variable."""
        proc = self._run_guard(
            "guard_cluster_ownership",
            state_list="module.gke_cluster.google_container_cluster.autopilot[0]",
            state_show='resource "google_container_cluster" "autopilot" {\n    name = "other-cluster"\n}',
            tfvar_create_cluster='"false"',
            tfvar_cluster_name='"this-cluster"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("manages a DIFFERENT cluster, 'other-cluster'", proc.stderr)
        self.assertIn("KUBE_AGENTS_STATE_PREFIX", proc.stderr)
        self.assertNotIn("Set create_cluster = true", proc.stderr)

    def test_the_state_list_is_read_once_until_something_writes_state(self):
        with tempfile.NamedTemporaryFile(delete=False) as log:
            log_path = log.name
        try:
            proc = self._run_guard(
                f'export TF_STUB_LOG="{log_path}"; load_state; load_state; in_state x || true; '
                'state_changed; load_state'
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            reads = pathlib.Path(log_path).read_text().count("state list")
        finally:
            os.unlink(log_path)
        self.assertEqual(reads, 2, "two reads: the first, and the one after state_changed")

    def test_guard_cluster_ownership_passes_a_create_when_no_cluster_exists(self):
        proc = self._run_guard("guard_cluster_ownership", state_list="", tfvar_create_cluster="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_guard_cluster_ownership_passes_a_create_the_state_already_manages(self):
        proc = self._run_guard(
            "guard_cluster_ownership",
            state_list="module.gke_cluster.google_container_cluster.autopilot[0]",
            tfvar_create_cluster="true",
            gcloud_stub="exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_cluster_ownership_refuses_a_create_over_a_live_cluster_outside_state(self):
        """The 409 a retry after an interrupted install hits, refused before the apply (#1296)."""
        proc = self._run_guard(
            "guard_cluster_ownership",
            state_list="",
            tfvar_create_cluster="true",
            gcloud_stub="exit 0",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("create_cluster is true, but cluster", proc.stderr)
        self.assertIn("this state does not manage it", proc.stderr)
        self.assertIn("uninstall.sh", proc.stderr)

    def test_unmanaged_cluster_kms_is_forgotten_before_an_adoption_apply(self):
        """State an interrupted install leaves holds the adopted CMEK key; with
        create_cluster = false the module would destroy it (#1296)."""
        proc = self._run_guard(
            "forget_unmanaged_cluster_kms",
            state_list="module.gke_cluster.google_kms_crypto_key.gke_key[0]\nmodule.gke_cluster.google_kms_key_ring.gke_keyring[0]",
            tfvar_create_cluster='"false"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("forgetting module.gke_cluster.google_kms_crypto_key.gke_key[0]", proc.stdout)
        self.assertIn("forgetting module.gke_cluster.google_kms_key_ring.gke_keyring[0]", proc.stdout)

    def test_cluster_kms_is_kept_when_this_state_creates_the_cluster(self):
        proc = self._run_guard(
            "forget_unmanaged_cluster_kms",
            state_list="module.gke_cluster.google_kms_crypto_key.gke_key[0]",
            tfvar_create_cluster='"true"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("forgetting", proc.stdout)

    def test_guard_kms_identity_no_op_when_the_state_manages_no_kms(self):
        proc = self._run_guard("guard_kms_identity", state_list="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_guard_kms_identity_passes_when_configuration_matches_state(self):
        proc = self._run_guard(
            "guard_kms_identity",
            state_list="module.gke_cluster.google_kms_crypto_key.gke_key[0]",
            state_show='resource "google_kms_crypto_key" "gke_key" {\n    name = "k8s-secret-encryption-key"\n}',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_guard_kms_identity_refuses_a_renamed_key(self):
        """name is ForceNew on google_kms_crypto_key: a changed GKE_DB_KMS_KEY
        plans the live key's destruction, which schedules its versions for
        destruction and leaves etcd unreadable."""
        proc = self._run_guard(
            "guard_kms_identity",
            state_list="module.gke_cluster.google_kms_crypto_key.gke_key[0]",
            state_show='resource "google_kms_crypto_key" "gke_key" {\n    name = "k8s-secret-encryption-key"\n}',
            tfvar_kms_key='"k8s-secret-encryption-key-v2"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("kms_key_name resolved to 'k8s-secret-encryption-key-v2', but this state manages the CMEK resource 'k8s-secret-encryption-key'", proc.stderr)
        self.assertIn('GKE_DB_KMS_KEY="k8s-secret-encryption-key"', proc.stderr)

    def test_guard_kms_identity_refuses_a_renamed_key_ring(self):
        proc = self._run_guard(
            "guard_kms_identity",
            state_list="module.gke_cluster.google_kms_key_ring.gke_keyring[0]",
            state_show='resource "google_kms_key_ring" "gke_keyring" {\n    name = "platform-agent-keyring"\n}',
            tfvar_kms_keyring='"ring-two"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("kms_keyring_name resolved to 'ring-two'", proc.stderr)
        self.assertIn('GKE_DB_KMS_KEYRING="platform-agent-keyring"', proc.stderr)

    def test_guard_kms_identity_stands_down_on_an_adoption_apply(self):
        """create_cluster = false manages no CMEK: forget_unmanaged_cluster_kms
        owns that shape, and a name check there would refuse the forget."""
        proc = self._run_guard(
            "guard_kms_identity",
            state_list="module.gke_cluster.google_kms_crypto_key.gke_key[0]",
            state_show='resource "google_kms_crypto_key" "gke_key" {\n    name = "k8s-secret-encryption-key"\n}',
            tfvar_kms_key='"k8s-secret-encryption-key-v2"',
            tfvar_create_cluster='"false"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_release_namespace_no_op_when_release_not_in_state(self):
        proc = self._run_guard("guard_release_namespace", state_list="")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_release_namespace_passes_when_configuration_matches_state(self):
        proc = self._run_guard(
            "guard_release_namespace",
            state_list="helm_release.kube_agents",
            state_show='resource "helm_release" "kube_agents" {\n    name      = "kube-agents"\n    namespace = "kubeagents-system"\n}',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_release_namespace_refuses_a_release_move(self):
        """helm_release.namespace is ForceNew: a changed NAMESPACE plans destroy-and-recreate."""
        proc = self._run_guard(
            "guard_release_namespace",
            state_list="helm_release.kube_agents",
            state_show='resource "helm_release" "kube_agents" {\n    name      = "kube-agents"\n    namespace = "kubeagents-system"\n}',
            tfvar_namespace='"agents-two"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("namespace resolved to 'agents-two', but this state's release runs in 'kubeagents-system'", proc.stderr)
        self.assertIn('NAMESPACE="kubeagents-system"', proc.stderr)

    def test_guard_pubsub_subscription_no_op_when_chat_disabled(self):
        proc = self._run_guard("guard_pubsub_subscription", tfvar_enable_google_chat="false")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_no_op_when_subscription_not_in_state(self):
        proc = self._run_guard("guard_pubsub_subscription", state_list="")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_passes_when_matches_state(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list="module.chat_pubsub[0].google_pubsub_subscription.chat_events",
            state_show='resource "google_pubsub_subscription" "chat_events" {\n    name = "platform-agent-chat-events-sub"\n}',
            tfvar_chat_sub_name='"platform-agent-chat-events-sub"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_refuses_when_differs_from_state(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list="module.chat_pubsub[0].google_pubsub_subscription.chat_events",
            state_show='resource "google_pubsub_subscription" "chat_events" {\n    name = "platform-agent-chat-events-sub"\n}',
            tfvar_chat_sub_name='"custom-chat-events-sub"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("chat_subscription_name resolved to 'custom-chat-events-sub', but this state manages Pub/Sub subscription 'platform-agent-chat-events-sub'", proc.stderr)
        self.assertIn('CHAT_SUB_NAME="platform-agent-chat-events-sub"', proc.stderr)

    def test_guard_minter_key_no_op_when_minter_disabled(self):
        """When enable_github_minter is false, guard_minter_key passes cleanly."""
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"false"',
        )
        self.assertEqual(proc.returncode, 0, f"unexpected failure: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_guard_minter_key_passes_when_key_version_enabled(self):
        """When enable_github_minter is true and key version exists in ENABLED state, apply proceeds."""
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"true"',
            gcloud_key_version="projects/test-project/locations/us-central1/keyRings/github-token-minter-keyring/cryptoKeys/github-token-minter-key/cryptoKeyVersions/1",
        )
        self.assertEqual(proc.returncode, 0, f"unexpected failure: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_guard_minter_key_refuses_when_no_enabled_key_version(self):
        """When enable_github_minter is true but key has no ENABLED version, apply refuses to prevent wedged helm wait."""
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"true"',
            gcloud_key_version="",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("enable_github_minter is true, but KMS signing key 'us-central1/github-token-minter-keyring/github-token-minter-key' has no ENABLED version.", proc.stderr)
        self.assertIn("Applying now would deploy the minter and wedge waiting on its readiness probe.", proc.stderr)

    def test_guard_minter_key_warns_and_proceeds_when_gcloud_fails(self):
        """When enable_github_minter is true but gcloud command fails, guard_minter_key logs warning and allows apply."""
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"true"',
            gcloud_kms_fail=True,
        )
        self.assertEqual(proc.returncode, 0, f"unexpected failure: {proc.stderr}")
        self.assertIn("could not verify Cloud KMS signing key 'us-central1/github-token-minter-keyring/github-token-minter-key' for GitHub minter", proc.stderr)
        self.assertIn("Proceeding with apply", proc.stderr)

    def test_guard_minter_key_refuses_when_key_does_not_exist_yet(self):
        """A NOT_FOUND keyring or key is the first-apply wedge itself, so the guard refuses rather than proceeding.

        Terraform creates the keyring and key import-only, so before the first apply
        neither exists and `gcloud kms keys versions list` exits non-zero with
        NOT_FOUND. Treating that like an unreachable API would let the apply build
        the cluster and then hang forever on the minter's readiness probe.
        """
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"true"',
            gcloud_kms_fail=True,
            gcloud_kms_error="ERROR: (gcloud.kms.keys.versions.list) NOT_FOUND: KeyRing projects/test-project/locations/us-central1/keyRings/github-token-minter-keyring not found.",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("KMS signing key 'us-central1/github-token-minter-keyring/github-token-minter-key' does not exist yet.", proc.stderr)
        self.assertIn("Applying now would deploy the minter and wedge waiting on its readiness probe.", proc.stderr)
        self.assertNotIn("Proceeding with apply", proc.stderr)

    def test_guard_minter_key_refuses_when_cloud_kms_is_not_enabled_yet(self):
        """A disabled Cloud KMS API is the same first-apply state as an absent key.

        main.tf enables cloudkms.googleapis.com as part of the very apply this
        guard runs ahead of, so on a genuinely fresh project the probe comes back
        SERVICE_DISABLED rather than NOT_FOUND. Reading only NOT_FOUND let the
        first apply -- the wedge the guard exists for -- fall into warn-and-proceed.
        """
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"true"',
            gcloud_kms_fail=True,
            gcloud_kms_error="ERROR: (gcloud.kms.keys.versions.list) FAILED_PRECONDITION: Cloud Key Management Service (KMS) API has not been used in project 123 before or it is disabled.",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("does not exist yet.", proc.stderr)
        self.assertNotIn("Proceeding with apply", proc.stderr)

    def test_guard_minter_key_ignores_a_gcloud_notice_on_stderr(self):
        """A warning gcloud writes to stderr on a zero exit must not be read back as a key version.

        The version list and stderr are captured separately for this reason: merged,
        `head -1` takes the notice, the guard sees a non-empty "version" and passes
        against a key that has none -- the exact wedge it exists to prevent.
        """
        proc = self._run_guard(
            "guard_minter_key",
            tfvar_enable_minter='"true"',
            gcloud_key_version="",
            gcloud_kms_notice="WARNING: Your active project does not match the quota project.",
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("has no ENABLED version.", proc.stderr)


if __name__ == "__main__":
    unittest.main()
