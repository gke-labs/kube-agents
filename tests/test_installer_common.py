"""Unit tests for k8s-operator/scripts/installer_common.sh helpers.

Covers the Terraform-state cluster probe (a managed-mode cluster entry reads
as "ours", a data-mode entry from an existing-cluster install does not, and
unparseable or unreadable state fails safe), the comma-or-space splitting
behind --custom-roles, and the API_SERVER_KEY guard in the tfvars generator.
"""

import json
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALLER_COMMON = _REPO_ROOT / "k8s-operator" / "scripts" / "installer_common.sh"

# installer_common.sh's contract: the caller defines the print helpers.
_PRINT_STUBS = """
print_info() { :; }
print_success() { :; }
print_warning() { :; }
print_error() { echo "ERROR: $*" >&2; }
"""


def _state_doc(resources):
    return json.dumps({"version": 4, "resources": resources})


MANAGED_CLUSTER_STATE = _state_doc(
    [{"mode": "managed", "type": "google_container_cluster", "name": "standard"}]
)
DATA_MODE_STATE = _state_doc(
    [{"mode": "data", "type": "google_container_cluster", "name": "existing"}]
)


def _autopilot_describe_stub(version="1.31.5-gke.1023000"):
    """A `clusters describe` stub for an Autopilot cluster.

    The generator asks twice on this path — autopilot.enabled first, then
    currentMasterVersion for the gVisor floor — so the stub answers on the
    --format it is given. An empty `version` stands for a version that
    could not be read.
    """
    return (
        'case "$*" in\n'
        f"  *currentMasterVersion*) printf '{version}\\n' ;;\n"
        "  *) printf 'True\\n' ;;\n"
        "esac\n"
        "exit 0"
    )


class InstallerCommonTest(unittest.TestCase):
    def _run(
        self,
        script,
        gcloud_stdout=None,
        gcloud_exit=0,
        env=None,
        kubectl_script=None,
        describe_stub='echo "ERROR: (gcloud.container.clusters.describe) NOT_FOUND" >&2; exit 1',
        kms_versions="",
    ):
        """Source installer_common.sh with print stubs and run `script`.

        A stub `gcloud` on PATH prints `gcloud_stdout` (when given) and exits
        `gcloud_exit` for `storage cat` calls on the state object;
        `clusters describe` runs `describe_stub` (default: exit 1, meaning
        the cluster does not exist).
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            state_file = pathlib.Path(tmp) / "default.tfstate"
            if gcloud_stdout is not None:
                state_file.write_text(gcloud_stdout)
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *\"clusters describe\"*) {describe_stub} ;;\n"
                f"  *\"keys versions list\"*) printf '%s' '{kms_versions}'; exit 0 ;;\n"
                "esac\n"
                f"[ -f '{state_file}' ] && cat '{state_file}'\n"
                f"exit {gcloud_exit}\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            # Hermetic kubectl: the generator recovers credentials from the
            # live Secret when it can, and a developer's real kube context
            # must never answer a unit test.
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(kubectl_script or "#!/usr/bin/env bash\nexit 1\n")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            full_env = get_isolated_test_env(
                overrides={
                    "PROJECT_ID": "test-project",
                    "CLUSTER_NAME": "test-cluster",
                    "REGION": "us-central1",
                    **(env or {}),
                },
                bin_dir=str(bin_dir),
            )
            body = f'set -u\n{_PRINT_STUBS}\nsource "{_INSTALLER_COMMON}"\n{script}'
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    # ── tf_state_has_cluster: the create_cluster re-run probe ────────────────

    def test_managed_cluster_entry_reads_as_ours(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=MANAGED_CLUSTER_STATE,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    def test_data_mode_entry_is_not_ours(self):
        # An existing-cluster install records a data-mode entry in the same
        # state; reading it as "ours" would flip create_cluster back to true
        # on re-run and plan a second cluster over the real one.
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=DATA_MODE_STATE,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)

    def test_unparseable_state_fails_safe(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout="this is not JSON {",
        )
        self.assertNotIn("rc=0", proc.stdout, proc.stderr)

    def test_unreadable_state_fails_safe(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=None,
            gcloud_exit=1,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)

    # ── hcl_csv_list: --custom-roles documents "space- or comma-separated" ──

    def test_csv_list_splits_on_commas(self):
        proc = self._run('hcl_csv_list "roles/viewer,roles/monitoring.viewer"')
        self.assertEqual(
            proc.stdout, '["roles/viewer", "roles/monitoring.viewer"]', proc.stderr
        )

    def test_csv_list_splits_on_spaces(self):
        proc = self._run('hcl_csv_list "roles/viewer roles/monitoring.viewer"')
        self.assertEqual(
            proc.stdout, '["roles/viewer", "roles/monitoring.viewer"]', proc.stderr
        )

    def test_csv_list_splits_mixed_and_trims(self):
        proc = self._run('hcl_csv_list " roles/a , roles/b  roles/c "')
        self.assertEqual(proc.stdout, '["roles/a", "roles/b", "roles/c"]', proc.stderr)

    def test_csv_list_empty_input_is_empty_list(self):
        proc = self._run('hcl_csv_list ""')
        self.assertEqual(proc.stdout, "[]", proc.stderr)

    # ── write_tfvars_from_state: the API_SERVER_KEY guard ────────────────────

    def test_tfvars_generation_without_api_server_key_fails_with_guidance(self):
        # vars.sh omits API_SERVER_KEY when PERSIST_SECRETS_ON_DISK=false
        # stripped it; under the front doors' `set -u` an unguarded read would
        # abort on an opaque unbound-variable error mid-run.
        proc = self._run(
            "set -Eeo pipefail\n"
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"'
        )
        self.assertNotIn("rc=0", proc.stdout)
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertIn("API_SERVER_KEY", proc.stderr)

    # ── cluster_mode follows the live cluster ────────────────────────────────

    def test_tfvars_autopilot_cluster_keeps_autopilot_mode(self):
        # Hardcoding "standard" against a live Autopilot install planned the
        # cluster's destruction on the next uninstall/upgrade regeneration.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub="printf 'True\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('cluster_mode               = "autopilot"', content)
            # Exists but not in state (the stub serves no state object).
            self.assertIn("create_cluster             = false", content)

    def test_tfvars_standard_cluster_and_unasked_fresh_create_stay_standard(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            # An existing Standard cluster: describe succeeds, empty output.
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn('cluster_mode               = "standard"', dest.read_text())
            # No cluster at all and no CLUSTER_MODE: the installer's default
            # shape, unchanged by --cluster-mode existing.
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('cluster_mode               = "standard"', content)
            self.assertIn("create_cluster             = true", content)

    def test_tfvars_fresh_create_honours_cluster_mode(self):
        # --cluster-mode reaches the generator through vars.sh. The probe found
        # nothing, so the interview's choice is the only shape on offer.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "autopilot"},
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('cluster_mode               = "autopilot"', content)
            self.assertIn("create_cluster             = true", content)

    def test_tfvars_fresh_create_rejects_an_unknown_cluster_mode(self):
        # vars.sh is hand-editable, and an unknown shape reaching Terraform
        # fails at validate with the whole interview already paid for.
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "autopiloot"},
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("autopiloot", proc.stderr)

    def test_tfvars_live_cluster_outranks_a_conflicting_cluster_mode(self):
        # The teardown path: uninstall.sh and upgrade.sh regenerate through
        # this generator from vars.sh alone and have no flag to correct a wrong
        # CLUSTER_MODE with. A persisted value that disagrees with the live
        # cluster must lose in BOTH directions — either way round, the losing
        # answer takes the cluster's count to 0 and turns the next apply into a
        # replacement.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            # Live Autopilot, vars.sh says standard.
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "standard"},
                describe_stub="printf 'True\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn('cluster_mode               = "autopilot"', dest.read_text())
            # Live Standard, vars.sh says autopilot.
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "autopilot"},
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn('cluster_mode               = "standard"', dest.read_text())

    # ── ENABLE_GVISOR splits into a pool and a RuntimeClass by cluster shape ──

    def test_tfvars_gvisor_on_standard_asks_for_pool_and_runtime_class(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "true"},
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_gvisor_node_pool    = true", content)
            self.assertIn('agent_runtime_class        = "gvisor"', content)

    def test_tfvars_gvisor_on_autopilot_asks_for_runtime_class_only(self):
        # enable_gvisor_node_pool fails the plan on Autopilot, which ships the
        # gvisor RuntimeClass natively. Passing ENABLE_GVISOR straight through
        # made --gvisor=true unusable there rather than sandboxing the agent.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "true"},
                describe_stub=_autopilot_describe_stub(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_gvisor_node_pool    = false", content)
            self.assertIn('agent_runtime_class        = "gvisor"', content)

    def test_tfvars_gvisor_on_a_fresh_autopilot_create_skips_the_version_probe(self):
        # There is no cluster to describe yet, so the floor check would only
        # ever produce its "could not read the version" warning. A cluster
        # created now comes up on its release channel's current version.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                'print_warning() { echo "WARN: $*" >&2; }; '
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "ENABLE_GVISOR": "true",
                    "CLUSTER_MODE": "autopilot",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertNotIn("Could not read the GKE version", proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_gvisor_node_pool    = false", content)
            self.assertIn('agent_runtime_class        = "gvisor"', content)

    def test_tfvars_gvisor_on_autopilot_below_the_version_floor_aborts(self):
        # Autopilot's gvisor RuntimeClass has a version floor, and a cluster
        # under it takes the whole apply before failing on a missing agent
        # Deployment. Abort while nothing has been applied.
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "true"},
            describe_stub=_autopilot_describe_stub("1.26.9-gke.9999"),
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("1.26.9-gke.9999", proc.stderr)
        self.assertIn("1.27.4-gke.800", proc.stderr)

    def test_tfvars_gvisor_on_autopilot_warns_when_the_version_is_unreadable(self):
        # An unparseable version is "unknown", not "too old": say so and carry
        # on rather than blocking an install on a gcloud output change.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                'print_warning() { echo "WARN: $*" >&2; }; '
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "true"},
                describe_stub=_autopilot_describe_stub(""),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("Could not read the GKE version", proc.stderr)
            self.assertIn('agent_runtime_class        = "gvisor"', dest.read_text())

    def test_tfvars_gvisor_on_standard_does_not_check_the_autopilot_floor(self):
        # The floor is Autopilot's. On Standard the node pool carries the
        # RuntimeClass, so an old cluster there must not be rejected by it.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "true"},
                describe_stub=(
                    'case "$*" in\n'
                    "  *currentMasterVersion*) printf '1.24.0-gke.100\\n' ;;\n"
                    "  *) printf '\\n' ;;\n"
                    "esac\n"
                    "exit 0"
                ),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("enable_gvisor_node_pool    = true", dest.read_text())

    def test_tfvars_without_gvisor_sets_neither(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_gvisor_node_pool    = false", content)
            self.assertIn('agent_runtime_class        = ""', content)

    def test_gke_version_at_least_orders_the_gke_suffix_numerically(self):
        # gke.800 is older than gke.1500, which a lexical compare gets backwards.
        cases = {
            "1.27.4-gke.800 1.27.4-gke.800": "0",
            "1.27.4-gke.1500 1.27.4-gke.800": "0",
            "1.30.11-gke.1131000 1.27.4-gke.800": "0",
            "1.28.1-gke.100 1.27.4-gke.800": "0",
            "1.27.4-gke.700 1.27.4-gke.800": "1",
            "1.27.3-gke.1700 1.27.4-gke.800": "1",
            "1.26.9-gke.9999 1.27.4-gke.800": "1",
        }
        for pair, want in cases.items():
            with self.subTest(pair=pair):
                proc = self._run(f"gke_version_at_least {pair}; echo \"rc=$?\"")
                self.assertIn(f"rc={want}", proc.stdout, proc.stderr)

    def test_tfvars_refuses_to_guess_on_a_transient_describe_failure(self):
        # Anything other than NOT_FOUND must abort: reading an auth expiry or
        # network blip as "cluster absent" regenerates standard/create=true
        # against a live Autopilot install and plans its replacement.
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k"},
            describe_stub='echo "ERROR: (gcloud) PERMISSION_DENIED: token expired" >&2; exit 1',
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("Could not probe cluster", proc.stderr)

    def test_tfvars_generation_recovers_credentials_from_live_secret(self):
        # PERSIST_SECRETS_ON_DISK=false leaves vars.sh without the keys; the
        # live Secret is their home, so the generator reads them back from it.
        recovered_b64 = "cmVjb3ZlcmVkLWtleQ=="  # base64("recovered-key")
        kubectl_stub = (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            # Recovery is gated on the current context being this install's
            # cluster; the stub answers with the expected gke_<p>_<r>_<c> name.
            '  *"config current-context"*) printf "gke_test-project_us-central1_test-cluster" ;;\n'
            f'  *"get secret platform-agent-secrets"*) printf "%s" "{recovered_b64}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                kubectl_script=kubectl_stub,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('api_server_key    = "recovered-key"', content)
            # SESSION_KV_* recover too: an adoption re-install must keep the
            # live salt or every chat identity re-pseudonymises.
            self.assertIn('session_kv_salt    = "recovered-key"', content)

    def test_tfvars_omits_credentials_when_persist_secrets_off(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$? tfvar=$TF_VAR_api_server_key"',
                env={
                    "PERSIST_SECRETS_ON_DISK": "false",
                    "API_SERVER_KEY": "k1",
                    "GEMINI_API_KEY": "g1",
                    "SLACK_ENABLED": "true",
                    "SLACK_BOT_TOKEN": "xoxb-1",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            for leaked in ("k1", "g1", "xoxb-1", "api_server_key", "slack_bot_token"):
                self.assertNotIn(leaked, content)
            self.assertIn("Credentials omitted", content)
            # The TF_VAR_* channel carries them instead.
            self.assertIn("tfvar=k1", proc.stdout)

    def test_minter_deferred_without_an_enabled_key_version(self):
        # A minter whose KMS key holds no ENABLED version never passes
        # readiness, and the apply waits on it — the generator defers.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            env = {
                "API_SERVER_KEY": "k",
                "GITHUB_ORG": "org",
                "GITHUB_REPO": "repo",
                "GITHUB_APP_ID": "42",
            }
            proc = self._run(f'write_tfvars_from_state "{dest}"', env=env, kms_versions="")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("enable_github_minter = false", dest.read_text())
            proc = self._run(f'write_tfvars_from_state "{dest}"', env=env, kms_versions="1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("enable_github_minter = true", dest.read_text())

    def test_tfvars_recovery_refuses_a_foreign_kube_context(self):
        # A stale context pointing at some other install must not donate that
        # environment's credentials: recovery skips, and the generator fails
        # on the missing key instead.
        recovered_b64 = "cmVjb3ZlcmVkLWtleQ=="
        kubectl_stub = (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '  *"config current-context"*) printf "gke_other-project_us-east1_other-cluster" ;;\n'
            f'  *"get secret platform-agent-secrets"*) printf "%s" "{recovered_b64}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            kubectl_script=kubectl_stub,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("API_SERVER_KEY", proc.stderr)

    def test_default_vertex_location_is_global(self):
        proc = self._run('printf "%s" "$DEFAULT_VERTEX_LOCATION"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "global")

    def test_default_vertex_location_is_a_separate_knob_from_the_region(self):
        # The whole point of the constant: a Vertex model is only callable from
        # a location that serves it, and DEFAULT_REGION is not one of those for
        # the vertex_ai default model. Tying the two together is the bug, so
        # neither the constant nor its expansion may be derived from the other.
        proc = self._run(
            'printf "%s %s" "$DEFAULT_REGION" "$DEFAULT_VERTEX_LOCATION"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        region, vertex_location = proc.stdout.split()
        self.assertEqual(vertex_location, "global")
        self.assertNotEqual(region, vertex_location)


if __name__ == "__main__":
    unittest.main()
