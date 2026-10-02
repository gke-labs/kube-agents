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
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LIFECYCLE_SH = _REPO_ROOT / "terraform" / "examples" / "full-install" / "lifecycle.sh"
_FULL_INSTALL = _LIFECYCLE_SH.parent

# The two files lifecycle.sh writes around a `terraform import` and removes
# afterwards: the helm provider placeholder beside the composition, and the
# scope resolver pin inside that module's own directory.
_PROVIDER_OVERRIDE = "providers_lifecycle_override.tf"
_SCOPE_OVERRIDE = "scope_resolver_lifecycle_override.tf"
_SCOPE_RESOLVER_SOURCE_RE = re.compile(r'module "scope_resolver" \{\s*source\s*=\s*"([^"]+)"')
_OVERRIDE_DATA_RE = re.compile(r'^data "(\w+)" "(\w+)"', re.MULTILINE)
_OVERRIDE_OUTPUT_RE = re.compile(r'^output "(\w+)"', re.MULTILINE)
# The README's hand-run import recipe writes the scope override itself: the
# path it writes to, and the heredoc body up to the terminator.
_README_SCOPE_OVERRIDE_RE = re.compile(
    r"^\s*cat > (\S+" + re.escape(_SCOPE_OVERRIDE) + r") <<'EOF'\n(.*?)^\s*EOF$",
    re.MULTILINE | re.DOTALL,
)


def _scope_resolver_source():
    """The path main.tf sources the scope resolver module from, relative to the composition."""
    match = _SCOPE_RESOLVER_SOURCE_RE.search((_FULL_INSTALL / "main.tf").read_text())
    assert match, "main.tf no longer sources a scope_resolver module"
    return match.group(1)


def _scratch_composition(root):
    """A copy of lifecycle.sh in a tree shaped like the checkout's.

    What the script resolves relative to its own directory is there: the
    install defaults and the endpoint helper three levels up, and the scope
    resolver module's directory where main.tf sources it, so an import
    override lands in the scratch tree and never in the checkout's module.
    """
    comp = root / "terraform" / "examples" / "full-install"
    comp.mkdir(parents=True)
    shutil.copy(_LIFECYCLE_SH, comp / "lifecycle.sh")
    shutil.copy(_REPO_ROOT / "install.defaults.env", root / "install.defaults.env")
    helpers = root / "scripts" / "installer"
    helpers.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "installer" / "gke_dns_endpoint.sh", helpers)
    (comp / _scope_resolver_source()).resolve().mkdir(parents=True)
    return comp


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
                   tfvar_chat_sub_name='"platform-agent-chat-events-sub"',
                   tfvar_chat_topic_name='"platform-agent-chat-events"',
                   tfvar_enable_drift_pubsub="false",
                   tfvar_drift_topic='"platform-agent-drift-audit"',
                   tfvar_drift_sub='"platform-agent-drift-audit-sub"',
                   tfvar_drift_sink='"platform-agent-drift-audit-sink"',
                   tfvar_enable_stockout="false",
                   tfvar_stockout_topic='"gke-stockout-alerts-topic"',
                   tfvar_stockout_sub='"gke-stockout-alerts-sub"',
                   console_fail_var=""):
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
    # console_fail_var models a terraform console that cannot answer -- a state
    # lock, a missing provider, a syntax error in the configuration -- for the
    # one variable named. Checked before every other branch so it wins.
    if [[ -n "{console_fail_var}" && "$expr" == *"{console_fail_var}"* ]]; then
        echo 'Error: could not load the configuration' >&2
        exit 1
    fi
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
    elif [[ "$expr" == *"chat_topic_name"* ]]; then
        echo '{tfvar_chat_topic_name}'
        exit 0
    elif [[ "$expr" == *"enable_drift_pubsub"* ]]; then
        echo '{tfvar_enable_drift_pubsub}'
        exit 0
    elif [[ "$expr" == *"drift_pubsub_topic"* ]]; then
        echo '{tfvar_drift_topic}'
        exit 0
    elif [[ "$expr" == *"drift_pubsub_subscription"* ]]; then
        echo '{tfvar_drift_sub}'
        exit 0
    elif [[ "$expr" == *"drift_pubsub_sink"* ]]; then
        echo '{tfvar_drift_sink}'
        exit 0
    elif [[ "$expr" == *"enable_stockout_investigator"* ]]; then
        echo '{tfvar_enable_stockout}'
        exit 0
    elif [[ "$expr" == *"stockout_pubsub_topic"* ]]; then
        echo '{tfvar_stockout_topic}'
        exit 0
    elif [[ "$expr" == *"stockout_pubsub_subscription"* ]]; then
        echo '{tfvar_stockout_sub}'
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

    def test_tfvar_ignores_state_lock_messages(self):
        """tfvar should ignore acquiring and releasing state lock messages."""
        lock_output = (
            "Acquiring state lock. This may take a few moments...\n"
            '"custom-sa"\n'
            "Releasing state lock. This may take a few moments..."
        )
        proc = self._run_guard(
            'printf "[%s]" "$(tfvar agent_service_account_id)"',
            tfvar_agent_sa=lock_output,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "[custom-sa]")

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
        """A subscription in state whose feature is switched off is a teardown, not a rename."""
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list="module.chat_pubsub[0].google_pubsub_subscription.chat_events",
            state_show='resource "google_pubsub_subscription" "chat_events" {\n    name = "platform-agent-chat-events-sub"\n}',
            tfvar_enable_google_chat="false",
            tfvar_chat_sub_name='"custom-chat-events-sub"',
        )
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

    def test_guard_pubsub_subscription_refuses_when_topic_differs_from_state(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list="module.chat_pubsub[0].google_pubsub_subscription.chat_events",
            state_show='resource "google_pubsub_subscription" "chat_events" {\n    name = "platform-agent-chat-events-sub"\n    topic = "projects/test-proj/topics/platform-agent-chat-events"\n}',
            tfvar_chat_sub_name='"platform-agent-chat-events-sub"',
            tfvar_chat_topic_name='"renamed-chat-topic"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("chat_topic_name resolved to 'renamed-chat-topic', but this state's Pub/Sub subscription is attached to topic 'platform-agent-chat-events'", proc.stderr)
        self.assertIn('CHAT_TOPIC_NAME="platform-agent-chat-events"', proc.stderr)

    # The drift and stockout subscriptions are the same resource type with the
    # same ForceNew name and topic, so the guard covers them too. A drift
    # recreate drops the audit records the detector exists to report, and the
    # detector stays Ready either way.
    _DRIFT_SUB_ADDRESS = "module.drift_pubsub[0].google_pubsub_subscription.drift_audit"
    _DRIFT_SUB_STATE = (
        'resource "google_pubsub_subscription" "drift_audit" {\n'
        '    name = "platform-agent-drift-audit-sub"\n'
        '    topic = "projects/test-proj/topics/platform-agent-drift-audit"\n}'
    )
    _STOCKOUT_SUB_ADDRESS = "google_pubsub_subscription.stockout_alerts[0]"
    _STOCKOUT_SUB_STATE = (
        'resource "google_pubsub_subscription" "stockout_alerts" {\n'
        '    name = "gke-stockout-alerts-sub"\n'
        '    topic = "projects/test-proj/topics/gke-stockout-alerts-topic"\n}'
    )

    def test_guard_pubsub_subscription_passes_when_drift_matches_state(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="true",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_no_op_when_drift_not_in_state(self):
        """Chat in state and clean, drift renamed but absent: only what state manages is checked."""
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list="module.chat_pubsub[0].google_pubsub_subscription.chat_events",
            state_show='resource "google_pubsub_subscription" "chat_events" {\n    name = "platform-agent-chat-events-sub"\n}',
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_sub='"renamed-drift-audit-sub"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_refuses_when_drift_subscription_differs(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_sub='"renamed-drift-audit-sub"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("drift_pubsub_subscription resolved to 'renamed-drift-audit-sub', but this state manages Pub/Sub subscription 'platform-agent-drift-audit-sub'", proc.stderr)
        self.assertIn("unacknowledged GKE audit records", proc.stderr)
        # No install.env key carries this name, so the advice names the passthrough
        # the front doors do read rather than inventing a key.
        self.assertIn("No install.env key carries this subscription name", proc.stderr)
        self.assertIn('TF_VAR_drift_pubsub_subscription="platform-agent-drift-audit-sub"', proc.stderr)
        self.assertNotIn("record it in install.env", proc.stderr)

    def test_guard_pubsub_subscription_refuses_when_drift_topic_differs(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_topic='"renamed-drift-audit"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("drift_pubsub_topic resolved to 'renamed-drift-audit', but this state's Pub/Sub subscription is attached to topic 'platform-agent-drift-audit'", proc.stderr)
        self.assertIn("No install.env key carries this topic name", proc.stderr)
        self.assertIn('TF_VAR_drift_pubsub_topic="platform-agent-drift-audit"', proc.stderr)

    def test_guard_pubsub_subscription_refuses_a_blanked_name(self):
        """`TF_VAR_drift_pubsub_subscription=` in install.env exports "", which beats the default."""
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_sub='""',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("An empty resolution is a blanked variable rather than a rename", proc.stderr)
        self.assertIn('TF_VAR_drift_pubsub_subscription="platform-agent-drift-audit-sub"', proc.stderr)

    def test_guard_pubsub_subscription_refuses_a_blanked_topic(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_topic='""',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("drift_pubsub_topic resolved to ''", proc.stderr)
        self.assertIn("An empty resolution is a blanked variable rather than a rename", proc.stderr)

    def test_guard_pubsub_subscription_no_op_when_drift_disabled(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="false",
            tfvar_drift_sub='"renamed-drift-audit-sub"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_refuses_when_the_flag_cannot_be_read(self):
        """A terraform console that cannot answer stops the apply rather than
        reading as "feature disabled".

        The flag is assigned to a local before it is compared for this reason:
        tfvar ends in `exit 1`, which inside $( ) kills only the subshell, so
        `[[ "$(tfvar "$flag")" == "true" ]] || continue` would skip the row and
        let the rename through. Every other flag read in lifecycle.sh still has
        that inline shape, so this pins the one that does not.
        """
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_sub='"renamed-drift-audit-sub"',
            console_fail_var="enable_drift_pubsub",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("could not evaluate var.enable_drift_pubsub", proc.stderr)

    def test_guard_pubsub_subscription_refuses_when_stockout_subscription_differs(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._STOCKOUT_SUB_ADDRESS,
            state_show=self._STOCKOUT_SUB_STATE,
            tfvar_enable_stockout="true",
            tfvar_stockout_sub='"renamed-stockout-sub"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stockout_pubsub_subscription resolved to 'renamed-stockout-sub', but this state manages Pub/Sub subscription 'gke-stockout-alerts-sub'", proc.stderr)
        self.assertIn("unacknowledged stockout alerts", proc.stderr)

    def test_guard_pubsub_subscription_refuses_when_stockout_topic_differs(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._STOCKOUT_SUB_ADDRESS,
            state_show=self._STOCKOUT_SUB_STATE,
            tfvar_enable_stockout="true",
            tfvar_stockout_topic='"renamed-stockout-topic"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stockout_pubsub_topic resolved to 'renamed-stockout-topic', but this state's Pub/Sub subscription is attached to topic 'gke-stockout-alerts-topic'", proc.stderr)

    def test_guard_pubsub_subscription_no_op_when_stockout_disabled(self):
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list=self._STOCKOUT_SUB_ADDRESS,
            state_show=self._STOCKOUT_SUB_STATE,
            tfvar_enable_stockout="false",
            tfvar_stockout_sub='"renamed-stockout-sub"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_guard_pubsub_subscription_checks_every_subscription_in_state(self):
        """Chat clean and drift renamed in one state: the loop must not stop at the first row.

        The terraform stub answers `state show` with the same body whatever address it
        is given, so the chat variables are pointed at that body's names to make the
        chat row compare equal and the loop reach the drift one.
        """
        proc = self._run_guard(
            "guard_pubsub_subscription",
            state_list="module.chat_pubsub[0].google_pubsub_subscription.chat_events\n" + self._DRIFT_SUB_ADDRESS,
            state_show=self._DRIFT_SUB_STATE,
            tfvar_chat_sub_name='"platform-agent-drift-audit-sub"',
            tfvar_chat_topic_name='"platform-agent-drift-audit"',
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_sub='"renamed-drift-audit-sub"',
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("drift_pubsub_subscription resolved to 'renamed-drift-audit-sub'", proc.stderr)

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

    # adopt_kms's drift-pubsub block. create_cluster is false in both so the
    # cluster CMEK half adds no targets, and the minter and stockout flags stay
    # off, so what adopt_kms imports is exactly what the drift flag adds.
    # gcloud exits 0, so every describe reports its resource present.

    def test_adopt_kms_imports_the_drift_pubsub_trio_when_the_flag_is_on(self):
        """The composition's default names under the module's addresses, so a
        re-install after a partial teardown adopts rather than 409s."""
        proc = self._run_guard(
            "adopt_kms",
            state_list="",
            tfvar_create_cluster='"false"',
            tfvar_enable_drift_pubsub="true",
            gcloud_stub="exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("adopting pre-existing resource: projects/test-project/topics/platform-agent-drift-audit", proc.stdout)
        self.assertIn("adopting pre-existing resource: projects/test-project/subscriptions/platform-agent-drift-audit-sub", proc.stdout)
        self.assertIn("adopting pre-existing resource: projects/test-project/sinks/platform-agent-drift-audit-sink", proc.stdout)
        self.assertIn("resource adoption complete: 3 imported", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_adopt_kms_adopts_the_drift_pubsub_trio_under_the_names_this_state_would_create(self):
        """A second install in the project names its own trio through the
        drift_pubsub_* variables; adopt_kms reads those, never the module's
        defaults, so the names it imports are the ones this state owns and
        the first install's default-named trio is left alone."""
        proc = self._run_guard(
            "adopt_kms",
            state_list="",
            tfvar_create_cluster='"false"',
            tfvar_enable_drift_pubsub="true",
            tfvar_drift_topic='"second-drift-audit"',
            tfvar_drift_sub='"second-drift-audit-sub"',
            tfvar_drift_sink='"second-drift-audit-sink"',
            gcloud_stub="exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("adopting pre-existing resource: projects/test-project/topics/second-drift-audit", proc.stdout)
        self.assertIn("adopting pre-existing resource: projects/test-project/subscriptions/second-drift-audit-sub", proc.stdout)
        self.assertIn("adopting pre-existing resource: projects/test-project/sinks/second-drift-audit-sink", proc.stdout)
        self.assertNotIn("platform-agent-drift-audit", proc.stdout)
        self.assertIn("resource adoption complete: 3 imported", proc.stdout)

    def test_adopt_kms_skips_the_drift_pubsub_trio_already_in_state(self):
        proc = self._run_guard(
            "adopt_kms",
            state_list="module.drift_pubsub[0].google_pubsub_topic.drift_audit\n"
                       "module.drift_pubsub[0].google_pubsub_subscription.drift_audit\n"
                       "module.drift_pubsub[0].google_logging_project_sink.drift_audit",
            tfvar_create_cluster='"false"',
            tfvar_enable_drift_pubsub="true",
            gcloud_stub="exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("adopting", proc.stdout)
        self.assertIn("resource adoption complete: 0 imported", proc.stdout)

    def test_adopt_kms_never_names_the_drift_pubsub_trio_when_the_flag_is_off(self):
        """Off is the default; an install that never set the flag must not
        import a topic, subscription or sink that happens to share the name."""
        proc = self._run_guard(
            "adopt_kms",
            state_list="",
            tfvar_create_cluster='"false"',
            tfvar_enable_drift_pubsub="false",
            gcloud_stub="exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("drift-audit", proc.stdout)
        self.assertNotIn("drift_pubsub", proc.stdout)
        self.assertIn("resource adoption complete: 0 imported", proc.stdout)


class DeleteAgentCrEndpointTest(unittest.TestCase):
    """delete_agent_cr has to reach the cluster over the endpoint that answers.

    Its own guard cannot catch a wrong one. `get-credentials` is a describe plus
    a file write, neither of which touches the control plane, so it exits 0
    having written a kubeconfig naming an unroutable IP. The kubectl after it
    then reports an unreachable cluster and a namespace holding no
    PlatformAgent identically, and teardown returns success over the
    cluster-scoped RBAC the finalizer would have removed.
    """

    def _run_delete(self, dns_endpoint="gke-abc.us-central1.gke.goog",
                    allow_external="True", supports_flag=True):
        """Run delete_agent_cr against stubbed gcloud, kubectl and terraform.

        Returns (completed process, recorded get-credentials invocation).
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            record = pathlib.Path(tmp) / "fetch.args"
            help_text = "--dns-endpoint" if supports_flag else "--internal-ip"
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(f"""#!/usr/bin/env bash
case "$*" in
  *"get-credentials --help"*) printf -- '{help_text}\\n'; exit 0 ;;
  *"clusters describe"*) printf '{dns_endpoint}\\t{allow_external}\\n'; exit 0 ;;
  *get-credentials*) printf '%s\\n' "$*" >> '{record}'; exit 0 ;;
esac
exit 0
""")
            gcloud.chmod(0o755)

            # No PlatformAgent in the namespace: the function logs and returns,
            # which is all these assertions need. What is under test is the
            # command that ran before it, not the deletion itself.
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("#!/usr/bin/env bash\nexit 0\n")
            kubectl.chmod(0o755)

            terraform = bin_dir / "terraform"
            terraform.write_text("""#!/usr/bin/env bash
if [[ "${1:-}" == "console" ]]; then
    read -r expr
    case "$expr" in
        *cluster_name*) echo '"test-cluster"' ;;
        *project_id*)   echo '"test-project"' ;;
        *location*)     echo '"us-central1"' ;;
        *namespace*)    echo '"kubeagents-system"' ;;
        *)              echo 'null' ;;
    esac
fi
exit 0
""")
            terraform.chmod(0o755)

            script = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_LIFECYCLE_SH}"\ndelete_agent_cr\n'
            proc = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT / "terraform" / "examples" / "full-install"),
            )
            return proc, (record.read_text() if record.exists() else "")

    def test_it_uses_the_dns_endpoint_when_one_accepts_external_traffic(self):
        proc, args = self._run_delete()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--dns-endpoint", args)

    def test_it_leaves_an_ordinary_cluster_on_its_ip_endpoint(self):
        # gcloud rejects the flag on a cluster with no externally reachable DNS
        # endpoint, so passing it blind would break the teardowns that work.
        for dns_endpoint, allow_external, supports_flag, why in (
            ("gke-abc.us-central1.gke.goog", "False", True, "external traffic is off"),
            ("", "True", True, "no DNS endpoint is published"),
            ("gke-abc.us-central1.gke.goog", "True", False, "this gcloud has no such flag"),
        ):
            with self.subTest(why=why):
                proc, args = self._run_delete(dns_endpoint, allow_external, supports_flag)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("--dns-endpoint", args)
                self.assertIn("get-credentials test-cluster", args)


class MissingEndpointHelperTest(unittest.TestCase):
    """The fallback for a checkout with no scripts/installer/gke_dns_endpoint.sh.

    Nothing else reaches it. Every other test sources the checkout's own
    lifecycle.sh, and the script cd's to its own directory before resolving the
    helper three levels up, so the file is always there and the arm below never
    runs. It matters because it runs under `set -euo pipefail` at load time: a
    slip in its syntax, or a rename of `warn`, fails the whole teardown on
    exactly the incomplete checkout the arm exists to keep working.
    """

    def _source_without_helper(self, probe):
        """Source a copy of lifecycle.sh from a tree that has no helper."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            composition = root / "terraform" / "examples" / "full-install"
            composition.mkdir(parents=True)
            copied = composition / "lifecycle.sh"
            shutil.copy(_LIFECYCLE_SH, copied)
            # Three levels up, where the script looks. Copied because their
            # absence is a hard failure by design; this is about the helper.
            shutil.copy(_REPO_ROOT / "install.defaults.env", root / "install.defaults.env")
            # scripts/installer/gke_dns_endpoint.sh is deliberately not created.
            bin_dir = root / "bin"
            bin_dir.mkdir()
            script = f'KUBE_AGENTS_SOURCE_ONLY=true source "{copied}"\n{probe}'
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(composition),
            )

    def test_a_checkout_without_the_helper_still_loads(self):
        # The arm runs at load time under `set -euo pipefail`. Broken, it takes
        # the teardown with it -- and only on the tree it was written for.
        proc = self._source_without_helper('echo "kind=$(type -t gke_dns_endpoint_flag)"')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("kind=function", proc.stdout, proc.stderr)

    def test_the_stub_leaves_the_flag_empty_so_teardown_dials_the_ip_endpoint(self):
        # delete_agent_cr splices the flag unquoted, so the stub's one job is to
        # leave nothing behind to splice.
        proc = self._source_without_helper(
            'GKE_DNS_ENDPOINT_FLAG=--stale\n'
            'gke_dns_endpoint_flag some-cluster us-central1 some-project\n'
            'echo "flag=[${GKE_DNS_ENDPOINT_FLAG}]"'
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("flag=[]", proc.stdout, proc.stderr)

    def test_it_warns_rather_than_falling_back_in_silence(self):
        proc = self._source_without_helper("true")
        self.assertIn("gke_dns_endpoint.sh", proc.stderr)
        self.assertIn("IP endpoint", proc.stderr)


# Terraform's own colouring, so the fixtures exercise the escape stripping the
# filter needs on a real CI stream.
_ESC = "\x1b"
_BOLD, _RESET, _YELLOW, _RED = f"{_ESC}[1m", f"{_ESC}[0m", f"{_ESC}[33m", f"{_ESC}[31m"

# Shaped like the helm_release diff CI printed, with fake values. The inner
# `}` lines sit deeper than metadata's own closing brace, which is what the
# filter must not stop at.
_FAKE_KEY = "fake-api-server-key-0123456789"
_FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----"


def _helm_metadata_block(marker, colour, closing_suffix):
    # Terraform right-aligns action symbols in three columns ("  ~", "-/+"),
    # so an attribute name, and the brace that closes it, never move.
    sym = " " * (3 - len(marker)) + f"{colour}{marker}{_RESET}{_RESET}"
    return [
        f"    {sym} metadata                   = {{",
        f"        {sym} notes          = <<-EOT",
        "                NOTE: uninstalling needs the finalizer cleared first",
        f"            EOT{closing_suffix}",
        f"          {sym} values         = jsonencode(",
        "              {",
        "                  credentials = {",
        "                      data = {",
        f'                          API_SERVER_KEY          = "{_FAKE_KEY}"',
        "                          SANDBOX_SSH_PRIVATE_KEY = <<-EOT",
        f"                              {_FAKE_PEM}",
        "                          EOT",
        "                      }",
        "                  }",
        "              }",
        "          )",
        f"        }}{closing_suffix}",
    ]


_UPDATE_PLAN = "\n".join([
    "Terraform will perform the following actions:",
    "",
    f"{_BOLD}  # helm_release.kube_agents{_RESET} will be updated in-place",
    f'{_RESET}  {_YELLOW}~{_RESET}{_RESET} resource "helm_release" "kube_agents" {{',
    f'      {_YELLOW}~{_RESET}{_RESET} id                         = "kube-agents" -> (known after apply)',
    *_helm_metadata_block("~", _YELLOW, " -> (known after apply)"),
    f"      {_YELLOW}~{_RESET}{_RESET} values                     = (sensitive value)",
    "        # (26 unchanged attributes hidden)",
    "    }",
    "",
    f"{_BOLD}Plan:{_RESET} 0 to add, 1 to change, 0 to destroy.",
]) + "\n"

_DESTROY_PLAN = "\n".join([
    f"{_BOLD}  # helm_release.kube_agents{_RESET} will be {_BOLD}{_RED}destroyed{_RESET}",
    f'{_RESET}  {_RED}-{_RESET}{_RESET} resource "helm_release" "kube_agents" {{',
    *_helm_metadata_block("-", _RED, " -> null"),
    f'      {_RED}-{_RESET}{_RESET} name                       = "kube-agents" -> null',
    "    }",
    "",
    f"{_BOLD}Plan:{_RESET} 0 to add, 0 to change, 1 to destroy.",
]) + "\n"

# Real terraform puts the replace symbol on the resource line only, at column
# 0; the attributes inside carry their own action.
_REPLACE_PLAN = "\n".join([
    f'{_RED}-{_RESET}/{_YELLOW}+{_RESET} resource "helm_release" "kube_agents" {{',
    *_helm_metadata_block("~", _YELLOW, " -> (known after apply)"),
    '      ~ namespace = "old" -> "new" # forces replacement',
    "    }",
]) + "\n"

# google_compute_instance has a metadata map of its own. It is not chart
# values, and hiding it would hide a real diff from the operator.
_OTHER_RESOURCE_PLAN = "\n".join([
    f'{_RESET}  {_YELLOW}~{_RESET}{_RESET} resource "google_compute_instance" "bastion" {{',
    f"      {_YELLOW}~{_RESET}{_RESET} metadata = {{",
    '          ~ "enable-oslogin" = "FALSE" -> "TRUE"',
    "        }",
    "    }",
]) + "\n"

# `terraform show` prints attributes with no action symbol at all. This is
# Terraform 1.9.5 with helm provider 3.3.0 rendering a state that holds these
# fake values, byte for byte.
_SHOW_HELM_BODY = [
    '    chart     = "kube-agents"',
    '    id        = "kube-agents"',
    "    metadata  = {",
    '        app_version    = "0.7.0"',
    '        chart          = "kube-agents"',
    "        first_deployed = 1",
    "        last_deployed  = 2",
    '        name           = "kube-agents"',
    '        namespace      = "kubeagents-system"',
    "        notes          = <<-EOT",
    "            NOTE: uninstalling needs the finalizer cleared first",
    "        EOT",
    "        revision       = 7",
    "        values         = jsonencode(",
    "            {",
    "                credentials = {",
    "                    data = {",
    f'                        API_SERVER_KEY          = "{_FAKE_KEY}"',
    "                        SANDBOX_SSH_PRIVATE_KEY = <<-EOT",
    f"                            {_FAKE_PEM}",
    "                            abc",
    "                        EOT",
    "                    }",
    "                }",
    "            }",
    "        )",
    '        version        = "0.7.0"',
    "    }",
    '    name      = "kube-agents"',
    '    namespace = "kubeagents-system"',
    "    values    = (sensitive value)",
]
_SHOW_OUTPUT = "\n".join([
    "# helm_release.kube_agents:",
    'resource "helm_release" "kube_agents" {',
    *_SHOW_HELM_BODY,
    "}",
]) + "\n"

# A plan that imports the release has no action to show either, so its body
# carries no symbol; only the indent differs from `terraform show`.
_IMPORT_PLAN = "\n".join([
    "  # helm_release.kube_agents will be imported",
    '    resource "helm_release" "kube_agents" {',
    *("    " + line for line in _SHOW_HELM_BODY),
    "    }",
    "",
    "Plan: 1 to import, 0 to add, 0 to change, 0 to destroy.",
]) + "\n"

_OTHER_RESOURCE_SHOW = "\n".join([
    "# google_compute_instance.bastion:",
    'resource "google_compute_instance" "bastion" {',
    "    metadata  = {",
    '        "enable-oslogin" = "TRUE"',
    "    }",
    "}",
]) + "\n"


def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _source(call):
    return f'KUBE_AGENTS_SOURCE_ONLY=true source "{_LIFECYCLE_SH}"\n{call}\n'


def _read_until(fd, needle, timeout):
    """Bytes read from fd until needle shows up or timeout passes."""
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            # A terminal whose other side has closed reads as EIO on Linux.
            break
        if not chunk:
            break
        buf += chunk
    return buf


class RedactHelmReleaseMetadataTest(unittest.TestCase):
    """helm_release's metadata repeats every chart value, which may include secrets.

    The helm provider does not mark it sensitive, so a plan that touches the
    release -- and every destroy -- prints the old values in full. The filter
    keeps that block out of terraform's output, and so out of any log of it.
    """

    def _filter(self, text):
        proc = subprocess.run(
            ["bash", "-c", _source("redact_helm_release_metadata")],
            input=text,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_LIFECYCLE_SH.parent),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _assert_hidden(self, out, marker):
        plain = _plain(out)
        self.assertNotIn(_FAKE_KEY, plain)
        self.assertNotIn(_FAKE_PEM, plain)
        self.assertNotIn("NOTE: uninstalling", plain)
        self.assertEqual(plain.count("hidden by lifecycle.sh"), 1, plain)
        self.assertIn(f"    {marker:>3} metadata = (hidden by lifecycle.sh", plain)

    def test_an_update_hides_the_old_values_and_keeps_the_rest_of_the_diff(self):
        out = self._filter(_UPDATE_PLAN)
        self._assert_hidden(out, "~")
        plain = _plain(out)
        # Everything past metadata's own closing brace is still there, so the
        # skip ended at that brace and not at one of the nested ones.
        self.assertIn('id                         = "kube-agents"', plain)
        self.assertIn("values                     = (sensitive value)", plain)
        self.assertIn("# (26 unchanged attributes hidden)", plain)
        self.assertIn("Plan: 0 to add, 1 to change, 0 to destroy.", plain)
        self.assertNotIn("} -> (known after apply)", plain)

    def test_a_destroy_hides_the_values_it_is_about_to_delete(self):
        out = self._filter(_DESTROY_PLAN)
        self._assert_hidden(out, "-")
        plain = _plain(out)
        self.assertIn('name                       = "kube-agents" -> null', plain)
        self.assertIn("Plan: 0 to add, 0 to change, 1 to destroy.", plain)

    def test_a_replacement_hides_them_too(self):
        out = self._filter(_REPLACE_PLAN)
        self._assert_hidden(out, "~")
        self.assertIn("# forces replacement", _plain(out))

    def test_a_replace_symbol_on_the_attribute_itself_is_hidden(self):
        # Not what terraform prints today; a renderer change must not reopen it.
        for marker in ("-/+", "+/-"):
            with self.subTest(marker=marker):
                header = '  ~ resource "helm_release" "kube_agents" {'
                block = "\n".join([header, *_helm_metadata_block(marker, _YELLOW, ""), "    }"])
                self._assert_hidden(self._filter(block + "\n"), marker)

    def test_the_list_form_of_older_providers_is_hidden(self):
        # helm provider v2 rendered metadata as a list of one object.
        lines = [
            '  - resource "helm_release" "kube_agents" {',
            "      - metadata = [",
            "          - {",
            f'              - values = "{_FAKE_KEY}"',
            "            },",
            "        ] -> null",
            '      - name = "kube-agents" -> null',
            "    }",
        ]
        plain = _plain(self._filter("\n".join(lines) + "\n"))
        self.assertNotIn(_FAKE_KEY, plain)
        self.assertIn("- metadata = (hidden by lifecycle.sh", plain)
        self.assertIn('- name = "kube-agents" -> null', plain)

    def test_crlf_line_endings_are_hidden_as_well(self):
        out = self._filter(_UPDATE_PLAN.replace("\n", "\r\n"))
        self._assert_hidden(out, "~")
        self.assertIn("Plan: 0 to add, 1 to change, 0 to destroy.", _plain(out))

    def test_lines_outside_the_block_pass_through_byte_for_byte(self):
        # Colour included: the filter matches on a stripped copy and prints the
        # original, so CI keeps its highlighting.
        out = self._filter(_UPDATE_PLAN)
        kept = [line for line in _UPDATE_PLAN.splitlines()
                if line.startswith(f"{_BOLD}  # helm_release") or "(sensitive value)" in line]
        self.assertEqual(len(kept), 2)
        for line in kept:
            self.assertIn(line, out.splitlines())

    def test_another_resource_s_metadata_is_left_alone(self):
        for text in (_OTHER_RESOURCE_PLAN, _OTHER_RESOURCE_SHOW):
            with self.subTest(text=text.splitlines()[0]):
                self.assertEqual(self._filter(text), text)

    def test_terraform_show_prints_the_block_without_a_symbol_and_it_is_hidden(self):
        # `terraform show` and `terraform state show` render state, where no
        # attribute carries an action; the block has to be found without one.
        plain = _plain(self._filter(_SHOW_OUTPUT))
        self.assertNotIn(_FAKE_KEY, plain)
        self.assertNotIn(_FAKE_PEM, plain)
        self.assertNotIn("NOTE: uninstalling", plain)
        self.assertEqual(plain.count("hidden by lifecycle.sh"), 1, plain)
        self.assertIn("\n    metadata = (hidden by lifecycle.sh", plain)
        # The skip ended at metadata's own brace: what follows it is intact.
        self.assertIn('    name      = "kube-agents"\n', plain)
        self.assertIn("    values    = (sensitive value)\n}\n", plain)

    def test_an_import_plan_prints_it_without_a_symbol_too(self):
        plain = _plain(self._filter(_IMPORT_PLAN))
        self.assertNotIn(_FAKE_KEY, plain)
        self.assertNotIn(_FAKE_PEM, plain)
        self.assertEqual(plain.count("hidden by lifecycle.sh"), 1, plain)
        self.assertIn("\n        metadata = (hidden by lifecycle.sh", plain)
        self.assertIn('        name      = "kube-agents"\n', plain)
        self.assertIn("Plan: 1 to import, 0 to add, 0 to change, 0 to destroy.", plain)

    def test_the_helm_state_ends_at_the_next_resource(self):
        # Only the helm block is hidden; the instance after it keeps its diff.
        out = _plain(self._filter(_UPDATE_PLAN + _OTHER_RESOURCE_PLAN))
        self.assertEqual(out.count("hidden by lifecycle.sh"), 1, out)
        self.assertIn('"enable-oslogin" = "FALSE" -> "TRUE"', out)

    def test_the_helm_state_ends_at_a_data_source_too(self):
        data = "\n".join([
            ' <= data "google_compute_instance" "bastion" {',
            "      + metadata = {",
            '          + "enable-oslogin" = "TRUE"',
            "        }",
            "    }",
        ]) + "\n"
        out = _plain(self._filter(_UPDATE_PLAN + data))
        self.assertEqual(out.count("hidden by lifecycle.sh"), 1, out)
        self.assertIn('+ "enable-oslogin" = "TRUE"', out)

    def test_a_block_that_never_closes_hides_the_rest_and_says_so(self):
        # Fails closed: a missing closer must not let the values through.
        closer = "        } -> (known after apply)"
        self.assertIn(closer, _UPDATE_PLAN.splitlines())
        unclosed = _UPDATE_PLAN.replace(closer + "\n", "")
        plain = _plain(self._filter(unclosed))
        self.assertNotIn(_FAKE_KEY, plain)
        self.assertNotIn("Plan: 0 to add", plain)
        self.assertIn("metadata block never closed", plain.splitlines()[-1])

    def test_the_filter_outlives_ctrl_c_so_terraform_can_shut_down(self):
        # Ctrl-C reaches the whole process group. Were the filter to die of it,
        # terraform's graceful-shutdown output would hit a closed pipe and
        # SIGPIPE would kill it before it saved state and released the lock.
        proc = subprocess.Popen(
            ["bash", "-c", _source("redact_helm_release_metadata")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=get_isolated_test_env(),
            cwd=str(_LIFECYCLE_SH.parent),
            start_new_session=True,
        )
        try:
            proc.stdin.write(b"applying\n")
            proc.stdin.flush()
            self.assertIn(b"applying", _read_until(proc.stdout.fileno(), b"applying", 10))
            os.killpg(proc.pid, signal.SIGINT)
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "the filter died of SIGINT")
            proc.stdin.write(b"Interrupt received. Gracefully shutting down...\n")
            proc.stdin.close()
            rest = proc.stdout.read()
            self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read())
            self.assertIn(b"Gracefully shutting down", rest)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def test_each_line_streams_before_the_next_arrives_on_every_awk_present(self):
        # A helm wait runs ten minutes; a filter that buffers makes CI look
        # hung. mawk buffers its input despite fflush() and needs -W interactive.
        variants = {
            "gawk": ["gawk"],
            "mawk": ["mawk"],
            "original-awk": ["original-awk"],
            "busybox": ["busybox", "awk"],
        }
        tested = 0
        for name, cmd in variants.items():
            if not shutil.which(cmd[0]):
                continue
            tested += 1
            with self.subTest(awk=name), tempfile.TemporaryDirectory() as tmp:
                awk = pathlib.Path(tmp) / "awk"
                awk.write_text(f'#!/bin/sh\nexec {" ".join(cmd)} "$@"\n')
                awk.chmod(0o755)
                proc = subprocess.Popen(
                    ["bash", "-c", _source("redact_helm_release_metadata")],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=get_isolated_test_env(bin_dir=tmp),
                    cwd=str(_LIFECYCLE_SH.parent),
                )
                try:
                    proc.stdin.write(b"Still creating... [10m0s elapsed]\n")
                    proc.stdin.flush()
                    # The pipe stays open: the line has to come out on its own.
                    got = _read_until(proc.stdout.fileno(), b"elapsed]", 5)
                    self.assertIn(b"elapsed]", got, f"{name} held the line back")
                finally:
                    proc.stdin.close()
                    proc.wait(timeout=10)
                    proc.stdout.close()
                    proc.stderr.close()
        self.assertGreater(tested, 0, "no awk found to test")


class LifecycleSubcommandFilterTest(unittest.TestCase):
    """Each subcommand, run as a command, gets the filter it claims to.

    The filter tests above prove the function; these prove the wiring -- that
    plan, apply and destroy actually pipe through it and keep terraform's
    exit code. Each run uses a copy of the script in a scratch tree, a PATH of
    basic tools plus stubs, and an environment with nothing from the host.
    Nothing in the checkout is read or written beyond that copy.
    """

    def _sandbox(self, root, output, tf_rc):
        """A scratch tree with lifecycle.sh, and a PATH of basic tools and stubs.

        terraform prints `output` for plan/apply/destroy and exits `tf_rc`; an
        apply without -auto-approve first asks, as terraform does, and applies
        only on "yes".
        Returns the composition directory, the environment, and the file
        every stub call is logged to.
        """
        comp = _scratch_composition(root)
        bin_dir = create_minimal_tools_bin(root)
        fixture = root / "terraform-output.txt"
        fixture.write_text(output)
        calls = root / "calls"
        bash = shutil.which("bash")
        # An empty state, and a console that answers null: every guard and
        # adoption step stands down, and the run reaches terraform itself.
        (bin_dir / "terraform").write_text(
            f"#!{bash}\n"
            f'echo "terraform $*" >> "{calls}"\n'
            'case "$1" in\n'
            "  state) exit 0 ;;\n"
            "  console) read -r _; echo null; exit 0 ;;\n"
            f'  plan|destroy) cat "{fixture}"; exit {tf_rc} ;;\n'
            f'  apply) cat "{fixture}"\n'
            f'    case " $* ${{TF_CLI_ARGS_apply:-}} " in *" -auto-approve "*) exit {tf_rc} ;; esac\n'
            "    printf '  Enter a value: '; read -r answer\n"
            f'    [ "$answer" = yes ] && exit {tf_rc}; exit 1 ;;\n'
            "esac\n"
            "exit 0\n"
        )
        # gcloud and kubectl answer "not found": the cluster is unreachable,
        # there is no backup plan, no key ring to adopt.
        for tool in ("gcloud", "kubectl"):
            (bin_dir / tool).write_text(f'#!{bash}\necho "{tool} $*" >> "{calls}"\nexit 1\n')
        for stub in ("terraform", "gcloud", "kubectl"):
            (bin_dir / stub).chmod(0o755)
        return comp, {"PATH": str(bin_dir), "HOME": str(root)}, calls

    def _run_lifecycle(self, args, output="", tf_rc=0):
        """Run lifecycle.sh with terraform printing `output` for plan/apply/destroy.

        Returns the completed process and every stub call, one per line.
        """
        with tempfile.TemporaryDirectory() as tmp:
            comp, env, calls = self._sandbox(pathlib.Path(tmp), output, tf_rc)
            proc = subprocess.run(
                [shutil.which("bash"), str(comp / "lifecycle.sh"), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(comp),
                timeout=60,
            )
            return proc, calls.read_text() if calls.exists() else ""

    def _terraform_call(self, calls, subcommand):
        calls_of = [line.split() for line in calls.splitlines()]
        matches = [call for call in calls_of if call[:2] == ["terraform", subcommand]]
        self.assertEqual(len(matches), 1, calls)
        return matches[0]

    def _assert_hidden(self, out, marker):
        RedactHelmReleaseMetadataTest._assert_hidden(self, out, marker)

    def test_plan_keeps_the_detailed_exit_code_through_the_filter(self):
        # upgrade.sh reads exit 2 as "changes pending". A pipe without pipefail
        # would report the filter's 0 instead, and the plan would read as clean.
        proc, calls = self._run_lifecycle(["plan", "-detailed-exitcode"], _UPDATE_PLAN, tf_rc=2)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("-detailed-exitcode", self._terraform_call(calls, "plan"))
        self._assert_hidden(proc.stdout, "~")

    def test_apply_is_filtered(self):
        proc, calls = self._run_lifecycle(["apply", "-auto-approve"], _UPDATE_PLAN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self._assert_hidden(proc.stdout, "~")
        self.assertIn("-auto-approve", self._terraform_call(calls, "apply"))

    def test_plan_apply_and_destroy_remove_a_stale_import_override_first(self):
        """A lifecycle.sh killed mid-import leaves both override files behind.

        Merged into a plan or apply, the scope one resolves every declared
        selector to nothing and retires its bindings, so each subcommand
        removes the pair before terraform reads the configuration.
        """
        for args in (["plan"], ["apply", "-auto-approve"], ["destroy", "-auto-approve"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                comp, env, calls = self._sandbox(pathlib.Path(tmp), _UPDATE_PLAN, 0)
                stale = [
                    comp / _PROVIDER_OVERRIDE,
                    comp / _scope_resolver_source() / _SCOPE_OVERRIDE,
                ]
                for path in stale:
                    path.write_text("# left behind by an interrupted import\n")
                proc = subprocess.run(
                    [shutil.which("bash"), str(comp / "lifecycle.sh"), *args],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=str(comp),
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(f"terraform {args[0]}", calls.read_text())
                for path in stale:
                    self.assertFalse(path.exists(), f"{path.name} survived {args[0]}")

    def _run_with_a_terminal(self, args, stdin_is_terminal=True, answer=None, extra_env=None):
        """Run lifecycle.sh with stdout and stderr on a terminal.

        With `answer`, returns what printed before it was typed; without one,
        everything printed. Also returns the exit code.
        """
        with tempfile.TemporaryDirectory() as tmp:
            comp, env, _ = self._sandbox(pathlib.Path(tmp), _UPDATE_PLAN, 0)
            env.update(extra_env or {})
            master, slave = pty.openpty()
            proc = None
            try:
                proc = subprocess.Popen(
                    [shutil.which("bash"), str(comp / "lifecycle.sh"), *args],
                    stdin=slave if stdin_is_terminal else subprocess.DEVNULL,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    cwd=str(comp),
                )
                os.close(slave)
                slave = None
                if answer is None:
                    out = _read_until(master, b"\0", 30)
                else:
                    out = _read_until(master, b"Enter a value: ", 10)
                    os.write(master, answer.encode() + b"\n")
                rc = proc.wait(timeout=30)
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
                for fd in (master, slave):
                    if fd is not None:
                        os.close(fd)
        return out.decode(errors="replace"), rc

    def test_an_apply_that_will_ask_at_a_terminal_is_left_as_is_so_its_prompt_shows(self):
        # Terraform's closing "Enter a value: " has no newline: through the
        # line-based filter it would wait unseen until the answer was typed.
        before, rc = self._run_with_a_terminal(["apply"], answer="yes")
        self.assertIn("Enter a value: ", before)
        self.assertEqual(rc, 0, before)

    def test_an_approved_apply_at_a_terminal_is_still_filtered(self):
        # upgrade.sh passes -auto-approve: nothing will be asked, and the whole
        # diff prints, so there is nothing to leave unfiltered for.
        for args, extra_env in (
            (["apply", "-auto-approve"], {}),
            (["apply"], {"TF_CLI_ARGS_apply": "-auto-approve"}),
        ):
            with self.subTest(args=args, extra_env=extra_env):
                out, rc = self._run_with_a_terminal(args, extra_env=extra_env)
                self.assertEqual(rc, 0, out)
                self._assert_hidden(out, "~")

    def test_an_apply_with_no_terminal_to_answer_from_is_filtered(self):
        # stdout on a terminal but stdin not (a pty in CI): no one can answer,
        # so terraform's question ends the run, and the diff before it is a log.
        out, rc = self._run_with_a_terminal(["apply"], stdin_is_terminal=False)
        self.assertEqual(rc, 1, out)
        self._assert_hidden(out, "~")

    def test_an_apply_piped_from_a_terminal_is_still_filtered(self):
        # `./lifecycle.sh apply | tee apply.log`: a terminal on stdin says
        # nothing about stdout, which here is a log.
        with tempfile.TemporaryDirectory() as tmp:
            comp, env, _ = self._sandbox(pathlib.Path(tmp), _UPDATE_PLAN, 0)
            master, slave = pty.openpty()
            try:
                # Typed ahead: the terminal holds the answer until it is read.
                os.write(master, b"yes\n")
                proc = subprocess.run(
                    [shutil.which("bash"), str(comp / "lifecycle.sh"), "apply"],
                    stdin=slave,
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=str(comp),
                    timeout=60,
                )
            finally:
                os.close(master)
                os.close(slave)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self._assert_hidden(proc.stdout, "~")

    def test_a_failed_apply_fails_the_script(self):
        proc, _ = self._run_lifecycle(["apply", "-auto-approve"], _UPDATE_PLAN, tf_rc=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn(_FAKE_KEY, proc.stdout)

    def test_destroy_is_filtered(self):
        proc, calls = self._run_lifecycle(["destroy", "-auto-approve"], _DESTROY_PLAN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self._assert_hidden(proc.stdout, "-")
        call = self._terraform_call(calls, "destroy")
        self.assertIn("-var=deletion_protection=false", call)
        self.assertIn("-auto-approve", call)
        self.assertIn("done. The KMS key rings remain", proc.stdout)

    def test_a_failed_destroy_stops_before_reporting_done(self):
        proc, _ = self._run_lifecycle(["destroy", "-auto-approve"], _DESTROY_PLAN, tf_rc=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn(_FAKE_KEY, proc.stdout)
        self.assertNotIn("done. The KMS key rings remain", proc.stdout)

    def test_usage_prints_the_whole_header_and_no_code(self):
        # The usage arm prints a fixed line range of this file; the header grew
        # with the redaction paragraph, so the range has to follow it.
        proc, _ = self._run_lifecycle([])
        self.assertEqual(proc.returncode, 1)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(lines[0].startswith("Makes `apply` and `destroy` repeatable"), lines[:1])
        self.assertIn("`plan`, `apply` and `destroy` hide helm_release's `metadata` block", proc.stdout)
        self.assertEqual(lines[-1], "default kube-agents/<cluster_name>>. Unset, state stays local as before.")
        self.assertNotIn("set -euo pipefail", proc.stdout)


class ImportOverrideTest(unittest.TestCase):
    """What adopt_kms and adopt_pubsub write around each `terraform import`.

    `terraform import` evaluates the whole configuration with every resource
    not yet in state unknown, and two things in it refuse that walk: the helm
    provider built from the cluster's endpoint, and the scope resolver module's
    monitored-project lookup, whose for_each is keyed on a read the walk never
    makes. lifecycle.sh writes an override for each, the second into the
    module's own directory, for the duration of the import, and removes both
    afterwards whether the import succeeded or not. Every run here uses a
    scratch copy of the tree, so the module override is written beside a
    scratch module directory and never into the checkout's.
    """

    _IMPORT_ERROR = "Error: Invalid for_each argument (stub)"

    def _run(self, func_call, import_rc=0, enable_drift="true", enable_chat="false"):
        """Run one adoption function against stubs that record each import.

        Returns the process, one line per `terraform import` saying whether
        each override file existed at that moment, the scope override's text
        as the last import saw it, and the two paths the files were at.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            comp = _scratch_composition(root)
            bin_dir = create_minimal_tools_bin(root)
            bash = shutil.which("bash")
            imports = root / "imports"
            captured = root / "captured-scope-override.tf"
            provider_override = comp / _PROVIDER_OVERRIDE
            scope_override = (comp / _scope_resolver_source() / _SCOPE_OVERRIDE).resolve()
            # An empty state; a console that enables the drift trio (three
            # adopt_kms imports) or Chat (an adopt_pubsub import) and leaves
            # every other flag at null; an import that records what it saw
            # and exits as told.
            (bin_dir / "terraform").write_text(
                f"#!{bash}\n"
                'case "$1" in\n'
                "  state) exit 0 ;;\n"
                "  console) read -r expr\n"
                '    case "${expr#var.}" in\n'
                "      project_id) echo '\"test-project\"' ;;\n"
                "      location) echo '\"us-central1\"' ;;\n"
                "      create_cluster) echo '\"false\"' ;;\n"
                f"      enable_drift_pubsub) echo '{enable_drift}' ;;\n"
                "      drift_pubsub_topic) echo '\"platform-agent-drift-audit\"' ;;\n"
                "      drift_pubsub_subscription) echo '\"platform-agent-drift-audit-sub\"' ;;\n"
                "      drift_pubsub_sink) echo '\"platform-agent-drift-audit-sink\"' ;;\n"
                f"      enable_google_chat) echo '{enable_chat}' ;;\n"
                "      chat_topic_name) echo '\"platform-agent-chat-events\"' ;;\n"
                "      chat_subscription_name) echo '\"platform-agent-chat-events-sub\"' ;;\n"
                "      *) echo null ;;\n"
                "    esac ;;\n"
                "  import)\n"
                f'    provider=no; [ -f "{provider_override}" ] && provider=yes\n'
                f'    scope=no; [ -f "{scope_override}" ] && scope=yes\n'
                f'    echo "import $3 provider=$provider scope=$scope" >> "{imports}"\n'
                f'    [ -f "{scope_override}" ] && cp "{scope_override}" "{captured}"\n'
                f'    [ {import_rc} -eq 0 ] || echo "{self._IMPORT_ERROR}" >&2\n'
                f"    exit {import_rc} ;;\n"
                "esac\n"
                "exit 0\n"
            )
            # Every describe succeeds: each candidate exists and is adopted.
            (bin_dir / "gcloud").write_text(f"#!{bash}\nexit 0\n")
            for stub in ("terraform", "gcloud"):
                (bin_dir / stub).chmod(0o755)
            proc = subprocess.run(
                [bash, "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source ./lifecycle.sh\n{func_call}\n'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env={"PATH": str(bin_dir), "HOME": str(root)},
                cwd=str(comp),
                timeout=60,
            )
            seen = imports.read_text().splitlines() if imports.exists() else []
            text = captured.read_text() if captured.exists() else ""
            left = [path.name for path in (provider_override, scope_override) if path.exists()]
            return proc, seen, text, left

    def test_both_overrides_exist_for_every_import_and_neither_survives_it(self):
        for func_call, expected_imports, summary in (
            ("adopt_kms", 3, "resource adoption complete: 3 imported"),
            ("adopt_pubsub", 1, "Pub/Sub adoption complete: 1 imported"),
        ):
            with self.subTest(func_call=func_call):
                proc, seen, _, left = self._run(
                    func_call, enable_drift="true", enable_chat="true",
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(len(seen), expected_imports, seen)
                for line in seen:
                    self.assertIn("provider=yes scope=yes", line)
                self.assertEqual(left, [])
                self.assertIn(summary, proc.stdout)
                self.assertEqual(proc.stderr, "")

    def test_a_failed_import_prints_terraforms_error_and_still_removes_both_overrides(self):
        """The warning alone cannot say why; before this, the error went to /dev/null
        and the first sign of trouble was the apply's 409."""
        for func_call in ("adopt_kms", "adopt_pubsub"):
            with self.subTest(func_call=func_call):
                proc, seen, _, left = self._run(
                    func_call, import_rc=1, enable_drift="true", enable_chat="true",
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertGreater(len(seen), 0)
                for line in seen:
                    self.assertIn("provider=yes scope=yes", line)
                self.assertEqual(left, [])
                self.assertIn(self._IMPORT_ERROR, proc.stderr)
                self.assertIn("could not import module.", proc.stderr)
                self.assertIn("the apply will fail with a 409", proc.stderr)
                self.assertNotIn("imported", proc.stdout.replace("0 imported", ""))

    def test_the_scope_override_pins_blocks_the_module_defines(self):
        """An override of a block the module does not define fails every import
        with "Missing data resource to override", so a rename in the module
        has to reach here; this is where it fails first."""
        _, seen, text, _ = self._run("adopt_kms")
        self.assertGreater(len(seen), 0)
        module = (_FULL_INSTALL / _scope_resolver_source()).resolve()
        module_text = "".join(path.read_text() for path in sorted(module.glob("*.tf")))
        data_blocks = _OVERRIDE_DATA_RE.findall(text)
        outputs = _OVERRIDE_OUTPUT_RE.findall(text)
        self.assertEqual(len(data_blocks), 1, text)
        self.assertEqual(len(outputs), 1, text)
        for provider, name in data_blocks:
            self.assertIn(f'data "{provider}" "{name}"', module_text)
            self.assertRegex(text, rf'data "{provider}" "{name}" \{{\s*for_each\s*=\s*toset\(\[\]\)')
        for name in outputs:
            self.assertIn(f'output "{name}"', module_text)
            self.assertRegex(text, rf'output "{name}" \{{\s*value\s*=\s*\{{\}}')

    def test_the_readme_recipe_writes_the_scope_override_the_script_writes(self):
        """The README's BackupPlan import recipe carries its own copy of the
        override, because lifecycle.sh exposes no import subcommand to borrow.
        A rename that reaches the script's heredoc has to reach the recipe too,
        or the next operator who follows it gets "Missing data resource to
        override" from a document that was correct when written."""
        _, seen, text, _ = self._run("adopt_kms")
        self.assertGreater(len(seen), 0)
        readme = (_FULL_INSTALL / "README.md").read_text()
        recipes = _README_SCOPE_OVERRIDE_RE.findall(readme)
        self.assertEqual(len(recipes), 1, "the README writes the scope override once, in the BackupPlan recipe")
        path, body = recipes[0]
        self.assertEqual(
            pathlib.PurePosixPath(path),
            pathlib.PurePosixPath(_scope_resolver_source()) / _SCOPE_OVERRIDE,
        )
        body = "\n".join(line.strip() for line in body.splitlines())
        self.assertEqual(_OVERRIDE_DATA_RE.findall(body), _OVERRIDE_DATA_RE.findall(text))
        self.assertEqual(_OVERRIDE_OUTPUT_RE.findall(body), _OVERRIDE_OUTPUT_RE.findall(text))
        self.assertIn("for_each = toset([])", body)
        self.assertIn("value = {}", body)


if __name__ == "__main__":
    unittest.main()
