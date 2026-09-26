"""Unit tests for scripts/installer/installer_common.sh helpers.

Covers the Terraform-state cluster probe (a managed-mode cluster entry reads
as "ours", a data-mode entry from an existing-cluster install does not, and
unparseable or unreadable state fails safe), the comma-or-space splitting
behind --custom-roles, and the API_SERVER_KEY guard in the tfvars generator.
"""

import datetime
import json
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_GKE_DNS_ENDPOINT = _REPO_ROOT / "scripts" / "installer" / "gke_dns_endpoint.sh"

# The two ERR-trap tests below are real only on a bash that runs an inherited
# ERR trap inside a `$(...)` whose failure the caller handles: bash 3.2 does,
# bash 4.4 and 5.x do not (measured), so on the latter they pass with or
# without the guard. They skip there rather than read as coverage; the
# source-shape test in ToleratedProbesClearErrTrapTest is the regression
# guard on every bash.
# The trap writes to stderr: the substitution's stdout is the value being
# captured, so a trap that echoed there would be swallowed with it.
_INHERITED_TRAP_PROBE = (
    "set -E; trap 'echo FIRED >&2' ERR; "
    "probe() { if ! x=$(false); then :; fi; }; probe"
)


def _bash_runs_inherited_err_trap_in_substitution():
    proc = subprocess.run(["bash", "-c", _INHERITED_TRAP_PROBE], capture_output=True, text=True)
    return "FIRED" in proc.stderr


_SKIP_UNLESS_TRAP_FIRES = (
    "this bash does not run an inherited ERR trap inside a $(...) the caller "
    "handles, so the guard cannot be seen missing here; "
    "ToleratedProbesClearErrTrapTest pins it by shape"
)

# installer_common.sh's contract: the caller defines the print helpers.
_PRINT_STUBS = """
print_info() { :; }
print_success() { :; }
print_warning() { :; }
print_error() { echo "ERROR: $*" >&2; }
"""


def _state_doc(resources):
    return json.dumps({"version": 4, "resources": resources})


def _cluster_instance(name="test-cluster", location="us-central1", project="test-project"):
    return {"attributes": {
        "id": f"projects/{project}/locations/{location}/clusters/{name}",
        "name": name, "location": location, "project": project,
    }}


# The coordinates _run exports: PROJECT_ID=test-project, REGION=us-central1,
# CLUSTER_NAME=test-cluster.
MANAGED_CLUSTER_STATE = _state_doc(
    [{"mode": "managed", "type": "google_container_cluster", "name": "standard",
      "instances": [_cluster_instance()]}]
)
DATA_MODE_STATE = _state_doc(
    [{"mode": "data", "type": "google_container_cluster", "name": "existing",
      "instances": [_cluster_instance()]}]
)
# What an apply that died before the create finished leaves behind (#1296).
EMPTY_INSTANCES_STATE = _state_doc(
    [{"mode": "managed", "type": "google_container_cluster", "name": "standard",
      "instances": []}]
)
OTHER_CLUSTER_STATE = _state_doc(
    [{"mode": "managed", "type": "google_container_cluster", "name": "standard",
      "instances": [_cluster_instance(name="some-other-cluster")]}]
)


# The composition's own cert-manager release, as an apply that got past it
# records it: a root-module managed entry with an instance.
CERT_MANAGER_RELEASE_STATE = _state_doc(
    [{"mode": "managed", "type": "helm_release", "name": "cert_manager",
      "instances": [{"index_key": 0, "attributes": {"id": "cert-manager", "name": "cert-manager"}}]}]
)

# A kubectl that finds a cert-manager Deployment on this install's cluster.
_CERT_MANAGER_PRESENT_KUBECTL = (
    "#!/usr/bin/env bash\n"
    'case "$*" in\n'
    '  *"get deployment cert-manager"*) exit 0 ;;\n'
    '  *"current-context"*) echo "gke_test-project_us-central1_test-cluster"; exit 0 ;;\n'
    "esac\n"
    "exit 1\n"
)

# A kubectl whose current-context points at some other cluster; the cert-manager
# probe and credential recovery must not touch it.
_CERT_MANAGER_OTHER_CONTEXT_KUBECTL = (
    "#!/usr/bin/env bash\n"
    'case "$*" in\n'
    '  *"get deployment cert-manager"*) exit 0 ;;\n'
    '  *"current-context"*) echo "some-other-context"; exit 0 ;;\n'
    "esac\n"
    "exit 1\n"
)


def _service_account_state(*account_ids):
    return _state_doc([
        {"mode": "managed", "type": "google_service_account", "name": "agent",
         "instances": [{"attributes": {"account_id": account_id}}]}
        for account_id in account_ids
    ])


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
        sa_describe_stub="exit 1",
        gcloud_stderr=None,
        get_credentials_stub=None,
    ):
        """Source installer_common.sh with print stubs and run `script`.

        A stub `gcloud` on PATH prints `gcloud_stdout` (when given) and exits
        `gcloud_exit` for `storage cat` calls on the state object;
        `clusters describe` runs `describe_stub` (default: exit 1, meaning
        the cluster does not exist).
        """
        # A failing `storage cat` with no stderr of its own reads as "absent":
        # that is what every pre-existing caller meant by gcloud_exit=1, and
        # the one test about an unreadable state passes a 5xx message instead.
        if gcloud_stderr is None:
            gcloud_stderr = (
                "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files"
                if gcloud_exit else ""
            )
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            state_file = pathlib.Path(tmp) / "default.tfstate"
            if gcloud_stdout is not None:
                state_file.write_text(gcloud_stdout)
            get_cred_case = (
                f"  *\"clusters get-credentials\"*) {get_credentials_stub} ;;\n"
                if get_credentials_stub
                else ""
            )
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *\"clusters describe\"*) {describe_stub} ;;\n"
                f"{get_cred_case}"
                f"  *\"keys versions list\"*) printf '%s' '{kms_versions}'; exit 0 ;;\n"
                f"  *\"service-accounts describe\"*) {sa_describe_stub} ;;\n"
                "esac\n"
                f"printf '%s' '{gcloud_stderr}' >&2\n"
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

    def test_managed_entry_with_no_instances_is_not_ours(self):
        # An apply that died before the create finished leaves a managed entry
        # that manages nothing; reading it as ours planned a create over the
        # live cluster on the retry (#1296).
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=EMPTY_INSTANCES_STATE,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)

    def test_managed_entry_for_another_cluster_is_not_ours(self):
        proc = self._run(
            'tf_state_has_cluster; echo "rc=$?"',
            gcloud_stdout=OTHER_CLUSTER_STATE,
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

    def test_missing_state_does_not_fire_err_trap(self):
        # Under `set -E` (errtrace) with an ERR trap installed (as in install.sh / upgrade.sh),
        # an absent state object must not trigger the ERR trap inside the $(...) subshell.
        # On Bash 3.2 (macOS default), the subshell inherits the ERR trap and fires unless
        # explicitly cleared with `trap - ERR`.
        script = (
            "set -E\n"
            "trap 'echo \"ERR_TRAP_FIRED\" >&2' ERR\n"
            "tf_state_has_cluster || true\n"
            'tag="$(tf_state_image_tag)"\n'
            'echo "done"\n'
        )
        proc = self._run(
            script,
            gcloud_stdout=None,
            gcloud_exit=1,
        )
        self.assertIn("done", proc.stdout, proc.stderr)
        self.assertNotIn("ERR_TRAP_FIRED", proc.stderr)

    def test_missing_deployment_does_not_fire_err_trap(self):
        # running_image_tag's kubectl probe: no Deployment to read (a first
        # install, or a context that cannot reach the cluster) is an empty
        # answer the caller handles, not an abort. Same mechanism as above:
        # inside the $(...) the probe is a bare failing command, so without
        # `trap - ERR` in the substitution bash 3.2 fires the inherited trap
        # there (#1798). The default kubectl stub exits 1.
        if not _bash_runs_inherited_err_trap_in_substitution():
            self.skipTest(_SKIP_UNLESS_TRAP_FIRES)
        script = (
            "set -E\n"
            "trap 'echo \"ERR_TRAP_FIRED\" >&2' ERR\n"
            'tag="$(running_image_tag kubeagents-system)"\n'
            'echo "tag=[$tag] done"\n'
        )
        proc = self._run(script)
        self.assertIn("tag=[] done", proc.stdout, proc.stderr)
        self.assertNotIn("ERR_TRAP_FIRED", proc.stderr)

    # ── tf_state_manages_resource: whose release is this? ────────────────────

    def test_managed_root_resource_with_an_instance_is_ours(self):
        proc = self._run(
            'tf_state_manages_resource helm_release cert_manager; echo "rc=$?"',
            gcloud_stdout=CERT_MANAGER_RELEASE_STATE,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    def test_managed_resource_without_instances_is_not_ours(self):
        proc = self._run(
            'tf_state_manages_resource helm_release cert_manager; echo "rc=$?"',
            gcloud_stdout=_state_doc([{"mode": "managed", "type": "helm_release",
                                       "name": "cert_manager", "instances": []}]),
        )
        self.assertIn("rc=1\n", proc.stdout, proc.stderr)

    def test_a_module_resource_of_the_same_name_is_not_the_root_one(self):
        proc = self._run(
            'tf_state_manages_resource helm_release cert_manager; echo "rc=$?"',
            gcloud_stdout=_state_doc([{"module": "module.other", "mode": "managed",
                                       "type": "helm_release", "name": "cert_manager",
                                       "instances": [{"attributes": {"id": "cert-manager"}}]}]),
        )
        self.assertIn("rc=1\n", proc.stdout, proc.stderr)

    def test_a_different_resource_name_is_not_ours(self):
        proc = self._run(
            'tf_state_manages_resource helm_release kube_agents; echo "rc=$?"',
            gcloud_stdout=CERT_MANAGER_RELEASE_STATE,
        )
        self.assertIn("rc=1\n", proc.stdout, proc.stderr)

    def test_absent_state_manages_nothing(self):
        proc = self._run(
            'tf_state_manages_resource helm_release cert_manager; echo "rc=$?"',
            gcloud_exit=1,
        )
        self.assertIn("rc=1\n", proc.stdout, proc.stderr)

    def test_unreadable_state_is_reported_as_unreadable_not_as_not_ours(self):
        # "Not ours" is the destructive direction for both callers, so a
        # state that could not be read must not read as it.
        proc = self._run(
            'tf_state_manages_resource helm_release cert_manager; echo "rc=$?"',
            gcloud_exit=1,
            gcloud_stderr="ERROR: (gcloud.storage.cat) HTTPError 503: Service Unavailable",
        )
        self.assertIn("rc=2\n", proc.stdout, proc.stderr)

    def test_unparseable_state_is_reported_as_unreadable(self):
        proc = self._run(
            'tf_state_manages_resource helm_release cert_manager; echo "rc=$?"',
            gcloud_stdout="this is not JSON {",
        )
        self.assertIn("rc=2\n", proc.stdout, proc.stderr)

    # ── the cert-manager probe: a Deployment alone cannot say whose it is ────

    def test_tfvars_keeps_cert_manager_when_the_state_manages_the_release(self):
        # A retry after an apply that died past the cert-manager release, or
        # an upgrade.sh regeneration of an existing-cluster install: the
        # Deployment the probe finds is the composition's own. Turning the
        # flag off had Terraform destroy it, webhooks and all.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub=_autopilot_describe_stub(),
                kubectl_script=_CERT_MANAGER_PRESENT_KUBECTL,
                gcloud_stdout=CERT_MANAGER_RELEASE_STATE,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn("create_cluster             = false", content)
            self.assertIn("enable_cert_manager        = true", content)

    def test_tfvars_skips_cert_manager_when_the_state_does_not_manage_it(self):
        # The existing behaviour, kept: somebody else's cert-manager makes the
        # composition's own release fail on the existing CRDs.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub=_autopilot_describe_stub(),
                kubectl_script=_CERT_MANAGER_PRESENT_KUBECTL,
                gcloud_exit=1,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("enable_cert_manager        = false", dest.read_text())

    def test_tfvars_keeps_cert_manager_when_the_state_cannot_be_read(self):
        # The two wrong answers are not symmetric: a wrong true fails the
        # apply on the existing CRDs, a wrong false destroys the install's
        # own cert-manager under -auto-approve.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub=_autopilot_describe_stub(),
                kubectl_script=_CERT_MANAGER_PRESENT_KUBECTL,
                gcloud_exit=1,
                gcloud_stderr="ERROR: (gcloud.storage.cat) HTTPError 503: Service Unavailable",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("enable_cert_manager        = true", dest.read_text())

    def test_tfvars_keeps_cert_manager_when_kubectl_context_is_not_this_cluster(self):
        # A stale or different kubectl context must not probe the wrong cluster
        # and wrongly disable cert-manager on the target cluster.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub=_autopilot_describe_stub(),
                kubectl_script=_CERT_MANAGER_OTHER_CONTEXT_KUBECTL,
                gcloud_exit=1,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("enable_cert_manager        = true", dest.read_text())

    def test_tfvars_adoption_path_resolves_dns_endpoint_flag(self):
        # When adopting an existing cluster (create_cluster=false), get-credentials
        # must resolve --dns-endpoint via gke_dns_endpoint_flag so clusters publishing
        # only a DNS endpoint can be reached.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            cred_log = pathlib.Path(out_dir) / "get_credentials.log"
            describe_stub = (
                'case "$*" in\n'
                '  *controlPlaneEndpointsConfig*) printf "cluster-dns.gke.goog\\tTrue\\n"; exit 0 ;;\n'
                '  *currentMasterVersion*) printf "1.31.5-gke.1023000\\n"; exit 0 ;;\n'
                '  *) printf "True\\n"; exit 0 ;;\n'
                'esac'
            )
            get_cred_stub = (
                'case "$*" in\n'
                '  *--help*) echo "--dns-endpoint"; exit 0 ;;\n'
                f'  *) echo "$*" >> "{cred_log}"; exit 0 ;;\n'
                'esac'
            )
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub=describe_stub,
                get_credentials_stub=get_cred_stub,
                kubectl_script=_CERT_MANAGER_PRESENT_KUBECTL,
                gcloud_exit=1,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertTrue(cred_log.exists(), "get-credentials was not called")
            logged_args = cred_log.read_text()
            self.assertIn("--dns-endpoint", logged_args)
            self.assertIn("test-cluster", logged_args)

    # ── check_service_account_ownership: the 409 a second install hits (#1294) ─

    def test_service_account_ownership_passes_when_nothing_exists(self):
        proc = self._run('check_service_account_ownership; echo "rc=$?"', gcloud_exit=1)
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    def test_service_account_ownership_passes_when_this_state_owns_it(self):
        proc = self._run(
            'check_service_account_ownership; echo "rc=$?"',
            gcloud_stdout=_service_account_state("kubeagents-platform-gsa"),
            sa_describe_stub="exit 0",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    _SHOW_REMEDY = 'print_info() { echo "INFO: $*" >&2; }; check_service_account_ownership; echo "rc=$?"'

    def test_service_account_ownership_refuses_an_account_this_state_does_not_own(self):
        proc = self._run(
            self._SHOW_REMEDY,
            gcloud_exit=1,
            sa_describe_stub="exit 0",
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("kubeagents-platform-gsa", proc.stderr)
        self.assertIn("PLATFORM_AGENT_GSA_NAME", proc.stderr)

    def test_service_account_ownership_stands_down_when_state_is_unreadable(self):
        # A transient GCS failure is not "no state": refusing on it would tell a
        # healthy install to delete its own account.
        proc = self._run(
            'print_warning() { echo "WARN: $*" >&2; }; check_service_account_ownership; echo "rc=$?"',
            gcloud_exit=1, gcloud_stderr="ERROR: HTTPError 503: backend unavailable",
            sa_describe_stub="exit 0",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("Skipping the service-account ownership check", proc.stderr)
        self.assertNotIn("ERROR: Service account", proc.stderr)

    def test_service_account_ownership_stands_down_on_an_unparseable_state(self):
        # A state that downloaded but does not parse says nothing about which
        # accounts it owns, so the delete-it remedy must not be reachable.
        proc = self._run(
            'print_warning() { echo "WARN: $*" >&2; }; check_service_account_ownership; echo "rc=$?"',
            gcloud_stdout="this is not JSON {",
            sa_describe_stub="exit 0",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("Skipping the service-account ownership check", proc.stderr)

    def test_the_one_release_alias_for_the_agent_gsa_key_still_works(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'print_warning() {{ echo "WARN: $*" >&2; }}; write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "TF_VAR_agent_service_account_id": "agent-two-gsa"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('agent_service_account_id         = "agent-two-gsa"', dest.read_text())
            self.assertIn("TF_VAR_agent_service_account_id is deprecated", proc.stderr)

    def test_load_install_env_drops_a_shell_exported_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("PROJECT_ID=p\n")
            proc = self._run(
                f'load_install_env "{env_file}"; echo "NS=${{NAMESPACE:-unset}}"',
                env={"NAMESPACE": "stray-from-kubectl-tooling"},
            )
            self.assertIn("NS=unset", proc.stdout, proc.stderr)
            env_file.write_text("PROJECT_ID=p\nNAMESPACE=from-the-file\n")
            proc = self._run(
                f'load_install_env "{env_file}"; echo "NS=${{NAMESPACE:-unset}}"',
                env={"NAMESPACE": "stray-from-kubectl-tooling"},
            )
            self.assertIn("NS=from-the-file", proc.stdout, proc.stderr)

    def test_load_install_env_drops_a_shell_exported_scope(self):
        # The keys render into the PlatformAgent and only install.sh's first
        # install records them, so on upgrade.sh, uninstall.sh and the menu the
        # file is the only way in; an inherited value would declare a project
        # the next clean-shell run drops again.
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("PROJECT_ID=p\n")
            stray = {"SCOPE_PROJECTS": "stray-project", "SCOPE_EXCLUDE_PROJECTS": "*-stray",
                     "SCOPE_EXCLUDE_CLUSTERS": "s/l/c"}
            probe = 'echo "P=${SCOPE_PROJECTS:-unset} X=${SCOPE_EXCLUDE_PROJECTS:-unset} C=${SCOPE_EXCLUDE_CLUSTERS:-unset}"'
            proc = self._run(f'load_install_env "{env_file}"; {probe}', env=stray)
            self.assertIn("P=unset X=unset C=unset", proc.stdout, proc.stderr)
            env_file.write_text("PROJECT_ID=p\nSCOPE_PROJECTS=from-the-file\n")
            proc = self._run(f'load_install_env "{env_file}"; {probe}', env=stray)
            self.assertIn("P=from-the-file X=unset C=unset", proc.stdout, proc.stderr)

    def test_service_account_ownership_still_refuses_on_a_clean_absence(self):
        proc = self._run(
            self._SHOW_REMEDY,
            gcloud_exit=1, gcloud_stderr="ERROR: (gcloud.storage.cat) The following URLs matched no objects or files",
            sa_describe_stub="exit 0",
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)

    def test_service_account_ownership_checks_the_configured_name(self):
        proc = self._run(
            self._SHOW_REMEDY,
            gcloud_exit=1,
            sa_describe_stub='[[ "$*" == *"my-own-agent-gsa@"* ]] && exit 0; exit 1',
            env={"PLATFORM_AGENT_GSA_NAME": "my-own-agent-gsa"},
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("my-own-agent-gsa", proc.stderr)

    def test_service_account_ownership_covers_the_minter_only_when_enabled(self):
        stub = '[[ "$*" == *"kubeagents-github-minter-gsa@"* ]] && exit 0; exit 1'
        proc = self._run(
            'check_service_account_ownership; echo "rc=$?"',
            gcloud_exit=1, sa_describe_stub=stub,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        proc = self._run(
            self._SHOW_REMEDY,
            gcloud_exit=1, sa_describe_stub=stub,
            env={"TFVARS_ENABLE_GITHUB_MINTER": "true"},
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("GITHUB_MINTER_GSA_NAME", proc.stderr)

    def test_service_account_ownership_covers_the_gateway_only_on_vertex(self):
        stub = '[[ "$*" == *"kubeagents-litellm-gsa@"* ]] && exit 0; exit 1'
        proc = self._run(
            'check_service_account_ownership; echo "rc=$?"',
            gcloud_exit=1, sa_describe_stub=stub,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        proc = self._run(
            self._SHOW_REMEDY,
            gcloud_exit=1, sa_describe_stub=stub,
            env={"MODEL_PROVIDER": "vertex_ai"},
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("LITELLM_GSA_NAME", proc.stderr)

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

    # ── expand_tilde_path: a ~ the operator's shell never resolved ───────────

    def _run_expand_tilde_path(self, path, home):
        """Run expand_tilde_path with HOME set to `home`, or unset when None.

        `_run` cannot express this: its overrides only add variables, and an
        unset HOME is the whole point. An empty HOME would not have caught the
        bug -- `${path/#\\~/$HOME}` aborted under `set -u` only when HOME was
        missing outright, which is what a systemd system unit or a container
        with no passwd entry gives you.
        """
        env = get_isolated_test_env(overrides={} if home is None else {"HOME": home})
        if home is None:
            env.pop("HOME", None)
        body = (
            f"set -u\n{_PRINT_STUBS}\n"
            f'source "{_INSTALLER_COMMON}"\n'
            f'expand_tilde_path "{path}"'
        )
        return subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO_ROOT),
        )

    def test_expand_tilde_path_resolves_a_leading_tilde(self):
        for path, expected in (("~/app.pem", "/home/me/app.pem"), ("~", "/home/me")):
            with self.subTest(path=path):
                proc = self._run_expand_tilde_path(path, "/home/me")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, expected, proc.stderr)

    def test_expand_tilde_path_leaves_a_path_without_a_tilde_alone(self):
        # The regression: HOME was read whether or not the pattern matched, so
        # an ordinary absolute --github-pem-path aborted the caller with
        # `HOME: unbound variable` wherever HOME was not set.
        for path in ("/tmp/app.pem", "/tmp/a~b.pem"):
            with self.subTest(path=path):
                proc = self._run_expand_tilde_path(path, None)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, path, proc.stderr)
                self.assertNotIn("unbound variable", proc.stderr)

    def test_expand_tilde_path_names_the_path_it_cannot_expand(self):
        # HOME is genuinely needed here and genuinely missing, so this must
        # fail -- but by the path the operator passed, not by the variable
        # they never set.
        proc = self._run_expand_tilde_path("~/app.pem", None)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(proc.stdout, "", "no half-expanded path may reach the caller")
        self.assertIn("HOME is unset", proc.stderr)
        self.assertIn("~/app.pem", proc.stderr)

    # ── write_tfvars_from_state: the API_SERVER_KEY guard ────────────────────

    def test_tfvars_generation_without_api_server_key_fails_with_guidance(self):
        # install.env omits API_SERVER_KEY when PERSIST_SECRETS_ON_DISK=false
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

    def test_tfvars_live_standard_survives_the_autopilot_default(self):
        # The two halves are the whole point of the default flip: a cluster that
        # exists keeps its own shape, and only a fresh create takes the default.
        # If the first half ever reported "autopilot", regenerating tfvars
        # against a live Standard install would plan its replacement.
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
            # No cluster at all and no CLUSTER_MODE: DEFAULT_CLUSTER_MODE.
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('cluster_mode               = "autopilot"', content)
            self.assertIn("create_cluster             = true", content)

    def test_tfvars_fresh_create_honours_cluster_mode(self):
        # --gke-cluster-mode reaches the generator through the exported environment. The probe found
        # nothing, so the interview's choice is the only shape on offer.
        #
        # Asks for "standard" specifically: autopilot is now DEFAULT_CLUSTER_MODE,
        # so requesting it would pass whether or not CLUSTER_MODE were read at all.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "standard"},
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('cluster_mode               = "standard"', content)
            self.assertIn("create_cluster             = true", content)

    def test_tfvars_fresh_create_rejects_an_unknown_cluster_mode(self):
        # install.env is hand-editable, and an unknown shape reaching Terraform
        # fails at validate with the whole interview already paid for.
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "autopiloot"},
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("autopiloot", proc.stderr)

    def test_tfvars_live_cluster_outranks_a_conflicting_cluster_mode(self):
        # The teardown path: uninstall.sh and upgrade.sh regenerate through
        # this generator from install.env alone and have no flag to correct a wrong
        # CLUSTER_MODE with. A persisted value that disagrees with the live
        # cluster must lose in BOTH directions — either way round, the losing
        # answer takes the cluster's count to 0 and turns the next apply into a
        # replacement.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            # Live Autopilot, install.env says standard.
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "CLUSTER_MODE": "standard"},
                describe_stub="printf 'True\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn('cluster_mode               = "autopilot"', dest.read_text())
            # Live Standard, install.env says autopilot.
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

    def test_tfvars_carry_accept_no_network_policy(self):
        # The module's postcondition reads the variable, not install.sh's flag,
        # so the generator has to emit it -- false by default, true when the
        # install accepted a cluster without enforcement (#1682).
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("accept_no_network_policy   = false", dest.read_text())

            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "ACCEPT_NO_NETWORK_POLICY": "true"},
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("accept_no_network_policy   = true", dest.read_text())

    def test_tfvars_carry_model_max_tokens(self):
        # Empty and unset both take DEFAULT_MODEL_MAX_TOKENS (0), which renders
        # nothing; a value is emitted as a bare HCL number, not a string.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for env, expected in (
                ({}, "model_max_tokens   = 0"),
                ({"MODEL_MAX_TOKENS": ""}, "model_max_tokens   = 0"),
                ({"MODEL_MAX_TOKENS": "4096"}, "model_max_tokens   = 4096"),
            ):
                with self.subTest(env=env):
                    proc = self._run(
                        f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                        env={"API_SERVER_KEY": "k", **env},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=0", proc.stdout, proc.stderr)
                    self.assertIn(expected, dest.read_text())

    def test_tfvars_refuse_a_model_max_tokens_that_is_not_a_whole_number(self):
        # upgrade.sh regenerates from install.env without install.sh's
        # interview, so the generator is the check that reaches it; a bare
        # word would otherwise fail at terraform's parser.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for value in ("4k", "-1", "4096.5"):
                with self.subTest(value=value):
                    proc = self._run(
                        f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                        env={"API_SERVER_KEY": "k", "MODEL_MAX_TOKENS": value},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=1", proc.stdout, proc.stderr)
                    self.assertIn("MODEL_MAX_TOKENS", proc.stderr + proc.stdout)
                    self.assertFalse(dest.exists(), "no tfvars is written for a value Terraform would refuse")

    def test_tfvars_gvisor_on_autopilot_asks_for_runtime_class_only(self):
        # enable_gvisor_node_pool fails the plan on Autopilot, which ships the
        # gvisor RuntimeClass natively. Passing ENABLE_GVISOR straight through
        # made --enable-gvisor=true unusable there rather than sandboxing the agent.
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

    def test_tfvars_leaves_the_agent_unsandboxed_when_gvisor_is_unset(self):
        # install.sh owns the default-on policy and always exports the result
        # before calling this, so an unset ENABLE_GVISOR here is not a
        # fresh install -- it is a caller reading an install that already
        # exists, and such an install is not sandboxed. Deciding otherwise
        # would make the generator disagree with the running cluster.
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

    def test_tfvars_unset_gvisor_skips_the_autopilot_floor(self):
        # uninstall.sh treats vars.sh as optional -- the documented
        # `curl ... | bash` teardown runs from a fresh clone that has none --
        # and calls this bare under `set -e` before lifecycle.sh destroy. If an
        # unset ENABLE_GVISOR defaulted on, the floor check would abort the
        # teardown of an old Autopilot cluster and leave the install with no
        # working way to remove itself.
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k"},
            describe_stub=_autopilot_describe_stub("1.26.9-gke.9999"),
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("1.27.4-gke.800", proc.stderr)

    def _tfvars(self, env):
        """Generate a terraform.tfvars and return its text.

        The generator writes `<dest>.tmp` and renames it into place, so the
        destination has to be a real path in a writable directory.
        """
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(f'write_tfvars_from_state "{dest}"; echo "rc=$?"', env=env)
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            return dest.read_text()

    def test_memory_provider_is_derived_from_the_recorded_mode(self):
        """install.env records MEMORY; the tfvars carry memory_provider.

        upgrade.sh and the Day-2 menu load the file and never pass through
        install.sh's parameter block, so with only MEMORY set the generator
        used to fall through to multiuser_memory and the apply deleted a
        Hindsight install's API server and Postgres.
        """
        for mode, provider in (
            ("hindsight", "kube_agents_memory"),
            ("off", "none"),
            ("file", "multiuser_memory"),
        ):
            with self.subTest(mode=mode):
                self.assertIn(
                    f'memory_provider          = "{provider}"',
                    self._tfvars(env={"API_SERVER_KEY": "k", "MEMORY": mode}),
                )

    def test_an_explicit_memory_provider_still_wins_over_the_mode(self):
        """install.sh exports MEMORY_PROVIDER on its own run; that is the
        more specific answer and the mode must not override it."""
        self.assertIn(
            'memory_provider          = "kube_agents_memory"',
            self._tfvars(
                env={
                    "API_SERVER_KEY": "k",
                    "MEMORY": "file",
                    "MEMORY_PROVIDER": "kube_agents_memory",
                }
            ),
        )

    def test_memory_provider_falls_back_when_nothing_is_recorded(self):
        """Neither name set — the project default, not an empty string."""
        self.assertIn(
            'memory_provider          = "multiuser_memory"',
            self._tfvars(env={"API_SERVER_KEY": "k"}),
        )

    def test_tfvars_autopilot_floor_names_a_way_out_for_every_caller(self):
        # The abort's remedy has to work for whoever hit it. --enable-gvisor=false is
        # install.sh's; upgrade.sh rejects that flag and reads install.env
        # instead, so naming only the flag sends its callers to a dead end.
        proc = self._run(
            # _PRINT_STUBS swallows print_info, and the way out is printed
            # there rather than beside the error.
            'print_info() { echo "INFO: $*" >&2; }; '
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "true"},
            describe_stub=_autopilot_describe_stub("1.26.9-gke.9999"),
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("--enable-gvisor=false", proc.stderr)
        self.assertIn("install.env", proc.stderr)

    def test_tfvars_gvisor_off_clears_the_floor_on_a_sub_floor_autopilot(self):
        # The composition uninstall.sh relies on: an explicit false must skip
        # the floor check, not merely the tfvars values. The unset case above
        # only covers a teardown from a fresh clone with no install.env; the
        # ordinary teardown loads one saying "true" and uninstall.sh exports
        # false over it, which is this row.
        proc = self._run(
            'rc=0; write_tfvars_from_state /dev/null || rc=$?; echo "rc=$rc"',
            env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "false"},
            describe_stub=_autopilot_describe_stub("1.26.9-gke.9999"),
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("1.27.4-gke.800", proc.stderr)

    def test_tfvars_with_gvisor_off_sets_neither(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "ENABLE_GVISOR": "false"},
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
        # PERSIST_SECRETS_ON_DISK=false leaves install.env without the keys; the
        # live Secret is their home, so the generator reads them back from it.
        recovered_b64 = "cmVjb3ZlcmVkLWtleQ=="  # base64("recovered-key")
        kubectl_stub = (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            # Recovery is gated on the current context being this install's
            # cluster; the stub answers with the expected gke_<p>_<r>_<c> name
            # and asserts that secret reads explicitly pass --context.
            '  *"config current-context"*) printf "gke_test-project_us-central1_test-cluster" ;;\n'
            f'  *"get secret platform-agent-secrets"*--context\\ gke_test-project_us-central1_test-cluster*) printf "%s" "{recovered_b64}" ;;\n'
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

    def test_google_chat_home_channel_written_to_tfvars(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "GOOGLE_CHAT_HOME_CHANNEL": "spaces/TEST12345",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('google_chat_home_channel  = "spaces/TEST12345"', content)

    def test_google_chat_derived_subscription_written_to_tfvars(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            # 1. Custom topic with unset subscription and no state derives <topic>-sub
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('chat_topic_name           = "custom-chat-events"', content)
            self.assertIn('chat_subscription_name    = "custom-chat-events-sub"', content)

            # 2. Custom topic with state managing legacy default subscription recovers state value
            legacy_state = _state_doc([{
                "module": "module.chat_pubsub[0]",
                "mode": "managed",
                "type": "google_pubsub_subscription",
                "name": "chat_events",
                "instances": [{"attributes": {"name": "platform-agent-chat-events-sub"}}],
            }])
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                gcloud_stdout=legacy_state,
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('chat_topic_name           = "custom-chat-events"', content)
            self.assertIn('chat_subscription_name    = "platform-agent-chat-events-sub"', content)

            # 3. Custom topic with explicit custom subscription retains explicit value
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                    "CHAT_SUB_NAME": "my-explicit-sub",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('chat_subscription_name    = "my-explicit-sub"', content)

            # 4. Custom topic with derived subscription (e.g. exported by install.sh) writes derived value
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                    "CHAT_SUB_NAME": "custom-chat-events-sub",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('chat_subscription_name    = "custom-chat-events-sub"', content)

            # 5. Default topic retains default subscription
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('chat_topic_name           = "platform-agent-chat-events"', content)
            self.assertIn('chat_subscription_name    = "platform-agent-chat-events-sub"', content)

            # 6. Custom topic with recorded default subscription re-derives when state has no subscription (#1397)
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                    "CHAT_SUB_NAME": "platform-agent-chat-events-sub",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn('chat_topic_name           = "custom-chat-events"', content)
            self.assertIn('chat_subscription_name    = "custom-chat-events-sub"', content)

            # 7. Unreadable state emits a warning to stderr (not into tfvars stdout) and proceeds
            proc = self._run(
                f'print_warning() {{ echo "WARN: $*"; }}; write_tfvars_from_state "{dest}"; echo "rc=$?"',
                gcloud_stderr="ERROR: 403 Forbidden",
                gcloud_exit=1,
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "true",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("Could not determine if Google Chat Pub/Sub subscription is in Terraform state", proc.stderr)
            content = dest.read_text()
            self.assertNotIn("Could not determine", content)
            self.assertNotIn("WARN:", content)
            self.assertIn('chat_topic_name           = "custom-chat-events"', content)
            self.assertIn('chat_subscription_name    = "custom-chat-events-sub"', content)

            # 8. Chat disabled never probes state even if state is unreadable
            proc = self._run(
                f'print_warning() {{ echo "WARN: $*"; }}; write_tfvars_from_state "{dest}"; echo "rc=$?"',
                gcloud_stderr="ERROR: 403 Forbidden",
                gcloud_exit=1,
                env={
                    "API_SERVER_KEY": "k",
                    "GOOGLE_CHAT_ENABLED": "false",
                    "CHAT_TOPIC_NAME": "custom-chat-events",
                },
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertNotIn("Could not determine", proc.stderr)
            self.assertNotIn("WARN:", proc.stderr)

    def test_tf_state_chat_subscription_name_returns_name(self):
        state = _state_doc([{
            "module": "module.chat_pubsub[0]",
            "mode": "managed",
            "type": "google_pubsub_subscription",
            "name": "chat_events",
            "instances": [{"attributes": {"name": "test-chat-sub"}}],
        }])
        proc = self._run(
            'tf_state_chat_subscription_name; echo "rc=$?"',
            gcloud_stdout=state,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("test-chat-sub", proc.stdout)

    def test_tf_state_chat_subscription_name_empty_when_absent(self):
        state = _state_doc([])
        proc = self._run(
            'sub="$(tf_state_chat_subscription_name)"; echo "sub=$sub rc=$?"',
            gcloud_stdout=state,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("sub= rc=0", proc.stdout)

    def test_minter_deferred_without_an_enabled_key_version(self):
        # A minter whose KMS key holds no ENABLED version never passes
        # readiness, and the apply waits on it — the generator defers.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            # GITOPS_*, the installer's names as of #1026. The generator reads
            # them directly; normalize_gitops_repo_vars folds the deprecated
            # GITHUB_* pair in before it runs, and is covered separately.
            env = {
                "API_SERVER_KEY": "k",
                "GITOPS_ORG": "org",
                "GITOPS_REPO": "repo",
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

    def test_tfvars_generation_includes_plugin_enablement(self):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            # Unset defaults to false
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_pubsub_platform       = false", content)
            self.assertIn("enable_stockout_investigator = false", content)

            # Explicit true
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "ENABLE_PUBSUB_PLATFORM": "true",
                    "ENABLE_STOCKOUT_INVESTIGATOR": "true",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_pubsub_platform       = true", content)
            self.assertIn("enable_stockout_investigator = true", content)

    def test_tfvars_carries_namespace_identity_and_cmek_names(self):
        # Every one of these used to be a fixed name the generator never wrote,
        # so install.env's NAMESPACE reached nothing and a second install in a
        # project had no way to name its own service accounts (#1294).
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = dest.read_text()
            self.assertIn('namespace    = "kubeagents-system"', content)
            self.assertIn('agent_service_account_id         = "kubeagents-platform-gsa"', content)
            self.assertIn('github_minter_service_account_id = "kubeagents-github-minter-gsa"', content)
            self.assertIn('litellm_service_account_id       = "kubeagents-litellm-gsa"', content)
            self.assertIn('kms_keyring_name = "platform-agent-keyring"', content)
            self.assertIn('kms_key_name     = "k8s-secret-encryption-key"', content)

            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "NAMESPACE": "agents-two",
                    "PLATFORM_AGENT_GSA_NAME": "agent-two-gsa",
                    "GITHUB_MINTER_GSA_NAME": "minter-two-gsa",
                    "LITELLM_GSA_NAME": "litellm-two-gsa",
                    "GKE_DB_KMS_KEYRING": "ring-two",
                    "GKE_DB_KMS_KEY": "key-two",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = dest.read_text()
            self.assertIn('namespace    = "agents-two"', content)
            self.assertIn('agent_service_account_id         = "agent-two-gsa"', content)
            self.assertIn('github_minter_service_account_id = "minter-two-gsa"', content)
            self.assertIn('litellm_service_account_id       = "litellm-two-gsa"', content)
            self.assertIn('kms_keyring_name = "ring-two"', content)
            self.assertIn('kms_key_name     = "key-two"', content)

    def test_tfvars_generation_carries_vertex_manage_serving_project(self):
        # Default true: the composition keeps enabling the API and granting the
        # gateway's role in the serving project. False is the opt-out for a
        # serving project the installing identity cannot administer, and it
        # has to reach Terraform as the bare boolean, not a quoted string.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("vertex_manage_serving_project = true", dest.read_text())

            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "VERTEX_MANAGE_SERVING_PROJECT": "false"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("vertex_manage_serving_project = false", dest.read_text())


class InstallDefaultsFileTest(unittest.TestCase):
    """install.defaults.env holds every default, and only defaults.

    One file, one job. The alternative -- a `${VAR:-value}` at each point of use
    -- is a second copy of the default living next to the code that reads it,
    and copies drift: that is how the installer's permission-set default once
    disagreed with the provisioner's, and how the chart sat on LiteLLM v1.92.0
    for a release after the kustomize base had moved on.
    """

    _DEFAULTS = _REPO_ROOT / "install.defaults.env"
    _INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

    def test_the_file_ships_with_the_repository(self):
        """Not git-ignored, unlike install.env. Every front door needs it to
        decide anything at all, including on a fresh clone."""
        self.assertTrue(self._DEFAULTS.is_file())
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "install.defaults.env"],
            cwd=str(_REPO_ROOT), capture_output=True, text=True,
        )
        self.assertEqual(tracked.returncode, 0, "install.defaults.env must be committed")

    def test_it_holds_nothing_but_defaults(self):
        """A configuration key here would apply to every install rather than
        one, which is the opposite of what install.env is for."""
        assignments = [
            line.split("=", 1)[0].strip()
            for line in self._DEFAULTS.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        ]
        self.assertTrue(assignments, "the defaults file declares nothing")
        for name in assignments:
            with self.subTest(name=name):
                self.assertTrue(
                    name.startswith("DEFAULT_"),
                    f"{name} is not a default; install configuration belongs in install.env",
                )

    def test_the_defaults_are_not_inlined_anywhere_else(self):
        """installer_common.sh must source them, not restate them."""
        source = self._INSTALLER_COMMON.read_text()
        self.assertIn("install.defaults.env", source)
        # re.MULTILINE, or `^` anchors at offset 0 only and a DEFAULT_* added
        # anywhere below the first line passes this guard unnoticed.
        self.assertNotRegex(
            source,
            re.compile(r"^DEFAULT_\w+=", re.MULTILINE),
            "installer_common.sh must not declare a default; they live in "
            "install.defaults.env so there is exactly one copy",
        )

    def test_sourcing_the_helpers_puts_them_in_scope(self):
        """The half that can break silently: whether the source actually
        resolves. Under `set -u` a missing constant aborts rather than
        expanding empty, so this is what a broken path would look like."""
        proc = subprocess.run(
            ["bash", "-c",
             f'set -u; source "{self._INSTALLER_COMMON}"; '
             'echo "$DEFAULT_CLUSTER_NAME|$DEFAULT_CLUSTER_MODE|$DEFAULT_MEMORY|'
             '$DEFAULT_PERMISSION_SET|$DEFAULT_REGISTRY_PREFIX"'],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "platform-agent-host|autopilot|file|read-only|ghcr.io/gke-labs/kube-agents",
        )

    def test_they_are_not_exported(self):
        """Shell variables, not environment. install.env is sourced with
        `set -a` because its values must reach Terraform; these must not --
        DEFAULT_* in the environment the agent and Terraform see would be noise
        at best and an accidental override at worst.
        """
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{self._INSTALLER_COMMON}" >/dev/null 2>&1; '
             'env | grep -c "^DEFAULT_" || true'],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.stdout.strip(), "0", "DEFAULT_* leaked into the environment")

    def test_it_is_found_from_any_working_directory(self):
        """upgrade.sh and uninstall.sh source the helpers from a fresh clone,
        so the path is resolved relative to installer_common.sh rather than to
        the caller's cwd."""
        proc = subprocess.run(
            ["bash", "-c",
             f'set -u; source "{self._INSTALLER_COMMON}"; echo "$DEFAULT_CLUSTER_MODE"'],
            capture_output=True, text=True, cwd=tempfile.gettempdir(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "autopilot")

    def test_the_chart_carries_the_same_per_provider_models(self):
        """charts/kube-agents/templates/litellm.yaml keeps its own copy of the
        per-provider default models for a hand-driven Helm install, because a
        chart cannot source this file. The copy is allowed only while it is
        equal, and this is what makes that true."""
        proc = subprocess.run(
            ["bash", "-c",
             f'set -u; source "{self._INSTALLER_COMMON}"; '
             'for p in gemini openai anthropic vertex_ai; do '
             'echo "$p=$(default_model_for_provider "$p")"; done'],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        defaults = dict(line.split("=", 1) for line in proc.stdout.split())
        chart = (_REPO_ROOT / "charts" / "kube-agents" / "templates" / "litellm.yaml").read_text()
        table = re.search(r'\$defaultModels := dict (.*?) \}\}', chart)
        self.assertIsNotNone(table, "litellm.yaml no longer declares $defaultModels")
        chart_models = dict(re.findall(r'"(\w+)" "([^"]+)"', table.group(1)))
        self.assertEqual(chart_models, defaults)

    def test_the_state_location_derives_from_the_defaults(self):
        """installer_common.sh and lifecycle.sh both derive the bucket and the
        prefix; both read these values, so the two cannot name different
        objects. The literal here is the contract every existing install's
        state already sits under."""
        proc = subprocess.run(
            ["bash", "-c",
             f'set -u; source "{self._INSTALLER_COMMON}"; '
             'PROJECT_ID=p CLUSTER_NAME=c; echo "$(tf_state_bucket) $(tf_state_prefix)"; '
             'KUBE_AGENTS_STATE_BUCKET=named; echo "$(tf_state_bucket)"'],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split("\n")[:2],
                         ["p-kube-agents-tfstate kube-agents/c", "named"])


class NormalizeMemoryVarsTest(unittest.TestCase):
    """install.env's MEMORY must beat a migrated vars.sh's MEMORY_PROVIDER.

    The two files spell the setting differently, so the load order that gives
    install.env the last word on every other key cannot do it for this one. The
    pre-install.env installer wrote `export MEMORY_PROVIDER=...` into vars.sh
    and every migrated install still has it; install.sh's migration writes only
    MEMORY. write_tfvars_from_state prefers MEMORY_PROVIDER, so without the
    normalizer the stale provider won and an upgrade regenerated the tfvars
    against the old store -- the apply then deleting the Hindsight API and its
    Postgres. #1060 item 5, on the front doors install.sh does not cover.
    """

    _INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

    def _normalize(self, assignments):
        proc = subprocess.run(
            ["bash", "-c",
             f'set -u; {_PRINT_STUBS}\nsource "{self._INSTALLER_COMMON}"\n'
             f'{assignments}\nnormalize_memory_vars\n'
             'echo "P=${MEMORY_PROVIDER:-}"'],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_the_install_env_mode_overrides_a_legacy_provider(self):
        """A legacy vars.sh says the file store, the operator's install.env says
        Hindsight, and the generated provider must be Hindsight's."""
        self.assertEqual(
            "P=kube_agents_memory",
            self._normalize('MEMORY_PROVIDER=multiuser_memory\nMEMORY=hindsight'),
        )

    def test_every_mode_translates(self):
        for mode, provider in (
            ("hindsight", "kube_agents_memory"),
            ("file", "multiuser_memory"),
            ("off", "none"),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(
                    f"P={provider}",
                    self._normalize(f'MEMORY_PROVIDER=multiuser_memory\nMEMORY={mode}'),
                )

    def test_nothing_recorded_leaves_the_provider_alone(self):
        """An install that never carried MEMORY -- a vars.sh-only install that
        has not been migrated yet -- must keep the provider it has."""
        self.assertEqual(
            "P=kube_agents_memory",
            self._normalize('MEMORY_PROVIDER=kube_agents_memory'),
        )

    def test_an_unrecognised_mode_leaves_the_provider_alone(self):
        """A typo in install.env must not silently retarget the store: blanking
        the provider here would fall through to the project default and plan
        the same deletion the normalizer exists to prevent."""
        self.assertEqual(
            "P=kube_agents_memory",
            self._normalize('MEMORY_PROVIDER=kube_agents_memory\nMEMORY=hindsigt'),
        )

    def test_the_front_doors_that_load_both_files_call_it(self):
        """upgrade.sh, uninstall.sh and install.sh's Day-2 menu each source a
        legacy vars.sh and then load install.env over it, and each generates
        tfvars without passing through install.sh's parameter block. A caller
        that loads both and skips the normalizer has the defect back."""
        for name in ("upgrade.sh", "uninstall.sh", "install.sh"):
            with self.subTest(name=name):
                self.assertIn(
                    "normalize_memory_vars",
                    (_REPO_ROOT / name).read_text(),
                    f"{name} loads vars.sh and install.env; it must normalize the pair",
                )


class HelmReleaseSelfHealingTest(unittest.TestCase):
    def _run_helm_test(self, script, helm_script, env_overrides=None, extra_bins=None):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            helm = bin_dir / "helm"
            helm.write_text(helm_script)
            helm.chmod(helm.stat().st_mode | stat.S_IEXEC)
            if extra_bins:
                for name, content in extra_bins.items():
                    target = bin_dir / name
                    target.write_text(content)
                    target.chmod(target.stat().st_mode | stat.S_IEXEC)
            full_env = get_isolated_test_env(overrides=env_overrides or {}, bin_dir=str(bin_dir))
            body = (
                f'set -u\n'
                f'{_PRINT_STUBS}\n'
                f'print_warning() {{ echo "WARNING: $*" >&2; }}\n'
                f'print_success() {{ echo "SUCCESS: $*" >&2; }}\n'
                f'source "{_INSTALLER_COMMON}"\n'
                f'{script}'
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    def test_clean_deployed_release_is_noop(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'if [[ "$*" == *"status kube-agents"* ]]; then\n'
            '  echo \'{"name": "kube-agents", "info": {"status": "deployed"}}\'\n'
            '  exit 0\n'
            'fi\n'
            'echo "unexpected helm call: $*" >&2\n'
            'exit 1\n'
        )
        proc = self._run_helm_test('ensure_clean_helm_release kube-agents kubeagents-system', helm_script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Rolling back", proc.stderr)

    def test_missing_release_does_not_fire_err_trap(self):
        # A first install onto an existing cluster: `helm status` exits 1
        # because no release exists, helm_release_status answers empty and the
        # caller carries on. Under the front doors' `set -E` the $(...) around
        # the probe inherits their ERR trap, and on bash 3.2 (macOS's default)
        # the trap fires inside the subshell unless `trap - ERR` clears it
        # there: an abort banner and a FAILED report from a successful run
        # (#1798).
        if not _bash_runs_inherited_err_trap_in_substitution():
            self.skipTest(_SKIP_UNLESS_TRAP_FIRES)
        helm_script = (
            '#!/usr/bin/env bash\n'
            'echo "Error: release: not found" >&2\n'
            'exit 1\n'
        )
        script = (
            "set -E\n"
            "trap 'echo \"ERR_TRAP_FIRED\" >&2' ERR\n"
            'status="$(helm_release_status kube-agents kubeagents-system)"\n'
            'echo "status=[$status] done"\n'
        )
        proc = self._run_helm_test(script, helm_script)
        self.assertIn("status=[] done", proc.stdout, proc.stderr)
        self.assertNotIn("ERR_TRAP_FIRED", proc.stderr)

    # ── clear_failed_initial_helm_release: the retry after a first apply died ─

    # The state coordinates the function reads through tf_state_read; the
    # gcloud stub decides what the state says, and the kubectl stub which
    # cluster the current context names.
    _STATE_ENV = {"PROJECT_ID": "test-project", "CLUSTER_NAME": "test-cluster", "REGION": "us-central1"}
    _NO_STATE_GCLOUD = (
        "#!/usr/bin/env bash\n"
        "echo 'ERROR: (gcloud.storage.cat) The following URLs matched no objects or files' >&2\n"
        "exit 1\n"
    )
    _UNREADABLE_STATE_GCLOUD = (
        "#!/usr/bin/env bash\n"
        "echo 'ERROR: (gcloud.storage.cat) HTTPError 503: Service Unavailable' >&2\n"
        "exit 1\n"
    )
    _THIS_CLUSTER_KUBECTL = (
        "#!/usr/bin/env bash\n"
        "echo gke_test-project_us-central1_test-cluster\n"
    )
    _OTHER_CLUSTER_KUBECTL = (
        "#!/usr/bin/env bash\n"
        "echo gke_someone-else_europe-west1_their-cluster\n"
    )

    @staticmethod
    def _failed_release_helm(history, uninstall='echo "UNINSTALL EXECUTED" >&2; exit 0'):
        return (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "failed"}}\'; exit 0 ;;\n'
            f'  *"history kube-agents"*) echo \'{history}\'; exit 0 ;;\n'
            f'  *"uninstall kube-agents"*) {uninstall} ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )

    def _run_clear(self, helm_script, gcloud=None, kubectl=None):
        return self._run_helm_test(
            'clear_failed_initial_helm_release kube-agents kubeagents-system; echo "rc=$?"',
            helm_script,
            env_overrides=self._STATE_ENV,
            extra_bins={"gcloud": gcloud or self._NO_STATE_GCLOUD,
                        "kubectl": kubectl or self._THIS_CLUSTER_KUBECTL},
        )

    def test_failed_release_that_never_deployed_is_uninstalled(self):
        proc = self._run_clear(self._failed_release_helm('[{"revision": 1, "status": "failed"}]'))
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertIn("UNINSTALL EXECUTED", proc.stderr)
        self.assertIn("no revision of it ever deployed", proc.stderr)

    def test_failed_release_that_served_before_is_left_alone(self):
        proc = self._run_clear(self._failed_release_helm(
            '[{"revision": 1, "status": "superseded"}, {"revision": 2, "status": "failed"}]'))
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)
        self.assertIn("served before", proc.stderr)

    def test_failed_release_the_state_manages_is_left_to_terraform(self):
        kube_agents_state = _state_doc([
            {"mode": "managed", "type": "helm_release", "name": "kube_agents",
             "instances": [{"attributes": {"id": "kube-agents"}}]},
        ])
        state_gcloud = (
            "#!/usr/bin/env bash\n"
            f"printf '%s' '{kube_agents_state}'\n"
            "exit 0\n"
        )
        proc = self._run_clear(self._failed_release_helm('[{"revision": 1, "status": "failed"}]'),
                               gcloud=state_gcloud)
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)

    def test_failed_release_is_left_alone_when_the_state_cannot_be_read(self):
        proc = self._run_clear(self._failed_release_helm('[{"revision": 1, "status": "failed"}]'),
                               gcloud=self._UNREADABLE_STATE_GCLOUD)
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)
        self.assertIn("could not be read", proc.stderr)

    def test_another_clusters_context_is_never_inspected(self):
        # The one destructive step here must not run against whatever
        # cluster the operator's kubeconfig last pointed at.
        proc = self._run_clear(self._failed_release_helm('[{"revision": 1, "status": "failed"}]'),
                               kubectl=self._OTHER_CLUSTER_KUBECTL)
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)
        self.assertNotIn("status kube-agents", proc.stderr)

    def test_deployed_release_is_not_touched(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "deployed"}}\'; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_clear(helm_script)
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertNotIn("unexpected helm call", proc.stderr)

    def test_pending_install_first_release_is_reported_not_uninstalled(self):
        # An interrupted first apply leaves pending-install, which Helm refuses
        # the name for too -- but so does an install running right now, and
        # the two cannot be told apart here.
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-install"}}\'; exit 0 ;;\n'
            '  *"uninstall kube-agents"*) echo "UNINSTALL EXECUTED" >&2; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_clear(helm_script)
        self.assertIn("rc=0\n", proc.stdout, proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)
        self.assertIn("helm uninstall kube-agents -n kubeagents-system", proc.stderr)

    def test_a_failed_uninstall_stops_the_run(self):
        proc = self._run_clear(self._failed_release_helm('[{"revision": 1, "status": "failed"}]',
                                                         uninstall='echo "boom" >&2; exit 1'))
        self.assertIn("rc=1\n", proc.stdout, proc.stderr)
        self.assertIn("Could not uninstall", proc.stderr)

    def test_pending_install_refuses_uninstall_by_default(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-install"}}\'; exit 0 ;;\n'
            '  *"uninstall kube-agents"*) echo "UNINSTALL EXECUTED" >&2; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test('ensure_clean_helm_release kube-agents kubeagents-system', helm_script)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Automatic uninstall is blocked", proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)

    def test_pending_install_uninstalls_when_opted_in(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-install"}}\'; exit 0 ;;\n'
            '  *"uninstall kube-agents"*) echo "Uninstall successful"; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system',
            helm_script,
            env_overrides={"ALLOW_UNINSTALL_PENDING_RELEASE": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Successfully cleaned up stuck pending-install release", proc.stderr)

    def test_pending_upgrade_recovers_to_last_good_revision(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-upgrade"}}\'; exit 0 ;;\n'
            '  *"history kube-agents"*) echo \'[{"revision": 1, "status": "superseded"}, {"revision": 2, "status": "superseded"}, {"revision": 3, "status": "pending-upgrade"}]\'; exit 0 ;;\n'
            '  *"rollback kube-agents 2"*) echo "Rollback successful"; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test('ensure_clean_helm_release kube-agents kubeagents-system', helm_script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Rolling back 'kube-agents' to revision 2", proc.stderr)

    def test_pending_upgrade_without_prior_good_revision_refuses_uninstall_by_default(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-upgrade"}}\'; exit 0 ;;\n'
            '  *"history kube-agents"*) echo \'[{"revision": 1, "status": "failed"}]\'; exit 0 ;;\n'
            '  *"uninstall kube-agents"*) echo "UNINSTALL EXECUTED" >&2; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test('ensure_clean_helm_release kube-agents kubeagents-system', helm_script)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Automatic uninstall is blocked", proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)

    def test_pending_upgrade_without_prior_good_revision_uninstalls_when_opted_in(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-upgrade"}}\'; exit 0 ;;\n'
            '  *"history kube-agents"*) echo \'[{"revision": 1, "status": "failed"}]\'; exit 0 ;;\n'
            '  *"uninstall kube-agents"*) echo "Uninstall successful"; exit 0 ;;\n'
            '  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system',
            helm_script,
            env_overrides={"ALLOW_UNINSTALL_PENDING_RELEASE": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_pending_upgrade_in_flight_waits_and_succeeds_when_deployed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = pathlib.Path(tmp) / "pending_stage"
            helm_script = (
                f'#!/usr/bin/env bash\n'
                f'case "$*" in\n'
                f'  *"status kube-agents"*)\n'
                f'    if [ ! -f "{marker}" ]; then\n'
                f'      touch "{marker}"\n'
                f'      echo \'{{"name": "kube-agents", "info": {{"status": "pending-upgrade"}}}}\'\n'
                f'    else\n'
                f'      echo \'{{"name": "kube-agents", "info": {{"status": "deployed"}}}}\'\n'
                f'    fi\n'
                f'    exit 0 ;;\n'
                f'  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
                f'esac\n'
            )
            proc = self._run_helm_test(
                'ensure_clean_helm_release kube-agents kubeagents-system',
                helm_script,
                env_overrides={
                    "HELM_LOCK_WAIT_TIMEOUT": "5",
                    "HELM_LOCK_POLL_INTERVAL": "1",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("In-flight Helm operation completed successfully", proc.stderr)



    def test_helm_release_status_reports_correctly(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'if [[ "$*" == *"status kube-agents"* ]]; then\n'
            '  echo \'{"name": "kube-agents", "info": {"status": "pending-upgrade"}}\'\n'
            '  exit 0\n'
            'fi\n'
            'exit 1\n'
        )
        proc = self._run_helm_test('helm_release_status kube-agents kubeagents-system', helm_script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "pending-upgrade")

    def test_missing_release_is_noop(self):
        helm_script = '#!/usr/bin/env bash\nexit 1\n'
        proc = self._run_helm_test('ensure_clean_helm_release kube-agents kubeagents-system', helm_script)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_parse_rfc3339_epoch_formats(self):
        proc = self._run_helm_test(
            'parse_rfc3339_epoch "2026-09-04T12:00:00Z"',
            '#!/usr/bin/env bash\nexit 0\n',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "1788523200")

        proc_inv = self._run_helm_test(
            'parse_rfc3339_epoch "not-a-valid-timestamp"',
            '#!/usr/bin/env bash\nexit 0\n',
        )
        self.assertNotEqual(proc_inv.returncode, 0)

    def test_parse_rfc3339_epoch_bsd_fallback(self):
        bsd_date_mock = (
            '#!/usr/bin/env bash\n'
            'if [ "${1:-}" = "-d" ]; then\n'
            '  echo "date: illegal option -- d" >&2\n'
            '  exit 1\n'
            'elif [ "${1:-}" = "-u" ] && [ "${2:-}" = "-j" ] && [ "${3:-}" = "-f" ]; then\n'
            '  echo "1788523200"\n'
            '  exit 0\n'
            'fi\n'
            'exec /bin/date "$@"\n'
        )
        proc = self._run_helm_test(
            'parse_rfc3339_epoch "2026-09-04T12:00:00Z"',
            '#!/usr/bin/env bash\nexit 0\n',
            extra_bins={"date": bsd_date_mock},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "1788523200")

    def test_pending_upgrade_computes_age_and_waits_with_kubectl_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = pathlib.Path(tmp) / "pending_stage"
            helm_script = (
                f'#!/usr/bin/env bash\n'
                f'case "$*" in\n'
                f'  *"status kube-agents"*)\n'
                f'    if [ ! -f "{marker}" ]; then\n'
                f'      touch "{marker}"\n'
                f'      echo \'{{"name": "kube-agents", "info": {{"status": "pending-upgrade"}}}}\'\n'
                f'    else\n'
                f'      echo \'{{"name": "kube-agents", "info": {{"status": "deployed"}}}}\'\n'
                f'    fi\n'
                f'    exit 0 ;;\n'
                f'  *) echo "unexpected helm call: $*" >&2; exit 1 ;;\n'
                f'esac\n'
            )
            recent_ts = (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            kubectl_script = (
                '#!/usr/bin/env bash\n'
                'case "$*" in\n'
                f'  *"get secret"*) echo "{recent_ts}" ; exit 0 ;;\n'
                '  *) echo "unexpected kubectl call: $*" >&2; exit 1 ;;\n'
                'esac\n'
            )
            proc = self._run_helm_test(
                'ensure_clean_helm_release kube-agents kubeagents-system',
                helm_script,
                env_overrides={
                    "HELM_LOCK_POLL_INTERVAL": "1",
                },
                extra_bins={"kubectl": kubectl_script},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Waiting up to", proc.stderr)
            self.assertIn("In-flight Helm operation completed successfully", proc.stderr)

    def test_pending_upgrade_fails_when_creation_timestamp_unparseable(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'if [[ "$*" == *"status kube-agents"* ]]; then\n'
            '  echo \'{"name": "kube-agents", "info": {"status": "pending-upgrade"}}\'\n'
            '  exit 0\n'
            'fi\n'
            'exit 0\n'
        )
        kubectl_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"get secret"*) echo "corrupted-unparseable-timestamp" ; exit 0 ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system',
            helm_script,
            extra_bins={"kubectl": kubectl_script},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Failed to parse creation timestamp 'corrupted-unparseable-timestamp'", proc.stderr)

    def test_pending_upgrade_refuses_recovery_if_wait_times_out_within_operation_window(self):
        helm_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"status kube-agents"*) echo \'{"name": "kube-agents", "info": {"status": "pending-upgrade"}}\' ; exit 0 ;;\n'
            '  *"rollback kube-agents"*) echo "ROLLBACK CALLED" >&2; exit 0 ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n'
        )
        recent_ts = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        kubectl_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            f'  *"get secret"*) echo "{recent_ts}" ; exit 0 ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system',
            helm_script,
            env_overrides={
                "HELM_PENDING_WAIT_MAX": "1",
                "HELM_LOCK_POLL_INTERVAL": "1",
            },
            extra_bins={"kubectl": kubectl_script},
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Refusing to recover active operation", proc.stderr)
        self.assertNotIn("ROLLBACK CALLED", proc.stderr)

    def test_running_image_tag_extracts_container_tag(self):
        kubectl_script = (
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            '  *"get deployment platform-agent-gateway"*) echo "ghcr.io/gke-labs/kube-agents/platform-agent:0.2.0" ; exit 0 ;;\n'
            '  *) exit 1 ;;\n'
            'esac\n'
        )
        proc = self._run_helm_test(
            'running_image_tag kubeagents-system',
            '#!/usr/bin/env bash\nexit 0\n',
            extra_bins={"kubectl": kubectl_script},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.2.0")

    def test_running_image_tag_handles_missing_deployment(self):
        kubectl_script = (
            '#!/usr/bin/env bash\n'
            'exit 1\n'
        )
        proc = self._run_helm_test(
            'running_image_tag kubeagents-system',
            '#!/usr/bin/env bash\nexit 0\n',
            extra_bins={"kubectl": kubectl_script},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class ToleratedProbesClearErrTrapTest(unittest.TestCase):
    """The library's tolerated probes clear the inherited ERR trap inside their $(...).

    The front doors' own handlers exit a subshell silently, so a probe there
    needs no guard; this library cannot know its caller's trap, so its probes
    guard themselves. The behavioural tests above cannot see the guard missing
    on the bash CI runs, so this pins the shape: each probe whose non-zero exit
    the caller handles begins its substitution with `trap - ERR;`. Dropping the
    prefix at any of them brings back the bash 3.2 abort banner and FAILED
    report under a caller whose trap is not subshell-aware (#1798), and this is
    the test that goes red for it.
    """

    # (file, guarded substring, how many times it appears). The unguarded form
    # is the same text without the prefix, and must not appear at all.
    GUARDED_PROBES = (
        (_INSTALLER_COMMON, 'response=$(trap - ERR; curl ', 1),
        (_INSTALLER_COMMON, 'image="$(trap - ERR; kubectl get deployment ', 1),
        (_INSTALLER_COMMON, 'status_json="$(trap - ERR; helm status ', 1),
        (_INSTALLER_COMMON, 'history_json="$(trap - ERR; helm history ', 2),
        (_INSTALLER_COMMON, 'last_good_rev="$(trap - ERR; printf ', 1),
        (_GKE_DNS_ENDPOINT, 'described=$(trap - ERR; gcloud container clusters describe ', 1),
        (_INSTALLER_COMMON, 'cr_json="$(trap - ERR; kubectl --context ', 1),
        (_INSTALLER_COMMON, 'record_json="$(trap - ERR; helm get values ', 1),
        (_INSTALLER_COMMON, 'served_rev="$(trap - ERR; helm history ', 1),
        (_INSTALLER_COMMON, 'served_json="$(trap - ERR; helm get values ', 1),
        (_INSTALLER_COMMON, 'verdict="$(trap - ERR; printf ', 1),
    )

    def test_each_tolerated_probe_clears_the_trap_inside_its_substitution(self):
        sources = {}
        for path, guarded, count in self.GUARDED_PROBES:
            source = sources.setdefault(path, path.read_text())
            unguarded = guarded.replace("trap - ERR; ", "")
            with self.subTest(file=path.name, probe=unguarded.strip()):
                self.assertEqual(source.count(guarded), count, f"{path.name}: {guarded!r}")
                self.assertNotIn(unguarded, source, f"{path.name}: a probe lost its `trap - ERR`")


class ScopeKeysReachTheTfvarsTest(unittest.TestCase):
    """The three SCOPE_* keys become the composition's `scope` object.

    Always a full block, empty lists included: the reconcile reads an emptied
    projects list as the declaration that drops projects and a missing block as
    no declaration (docs/designs/multi-project-scope.md §7), so the generator
    never omits it. Only the shape of an excluded cluster is checked here; the
    CRD's patterns, caps and repeats are the module variable's validations.
    """

    # The generator harness, borrowed rather than inherited: subclassing the
    # concrete test class would run its whole suite a second time.
    _run = InstallerCommonTest._run
    _tfvars = InstallerCommonTest._tfvars

    EMPTY_BLOCK = (
        "scope = {\n"
        "  projects = []\n"
        "  exclude = {\n"
        "    projects = []\n"
        "    clusters = []\n"
        "  }\n"
        "}\n"
    )

    def _scope_env(self, **keys):
        env = {"API_SERVER_KEY": "k", "SCOPE_PROJECTS": "", "SCOPE_EXCLUDE_PROJECTS": "",
               "SCOPE_EXCLUDE_CLUSTERS": ""}
        env.update(keys)
        return env

    def test_an_install_with_no_scope_key_renders_an_empty_present_block(self):
        content = self._tfvars(self._scope_env())
        self.assertIn(self.EMPTY_BLOCK, content)

    def test_the_keys_are_carried_verbatim_into_the_block(self):
        content = self._tfvars(self._scope_env(
            SCOPE_PROJECTS="payments-prod, payments-staging",
            SCOPE_EXCLUDE_PROJECTS="*-sandbox kube-agents-demo-0[2-9]",
            SCOPE_EXCLUDE_CLUSTERS="payments-staging/us-central1/scratch-cluster,p2/us-east1-b/c2",
        ))
        self.assertIn(
            "scope = {\n"
            '  projects = ["payments-prod", "payments-staging"]\n'
            "  exclude = {\n"
            '    projects = ["*-sandbox", "kube-agents-demo-0[2-9]"]\n'
            '    clusters = [{ project_id = "payments-staging", location = "us-central1", cluster_name = "scratch-cluster" }, '
            '{ project_id = "p2", location = "us-east1-b", cluster_name = "c2" }]\n'
            "  }\n"
            "}\n",
            content,
        )

    def test_a_glob_is_never_expanded_against_the_working_directory(self):
        # hcl_csv_list splits an unquoted string; with globbing on, *-sandbox
        # beside a file named team-a-sandbox renders the file name.
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as out_dir:
            for name in ("team-a-sandbox", "team-b-sandbox"):
                (pathlib.Path(cwd) / name).write_text("")
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'cd "{cwd}" && write_tfvars_from_state "{dest}"; echo "rc=$?"; '
                'case "$-" in *f*) echo "noglob-left-on" ;; *) echo "noglob-restored" ;; esac',
                env=self._scope_env(SCOPE_EXCLUDE_PROJECTS="*-sandbox", SCOPE_PROJECTS="*-sandbox"),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("noglob-restored", proc.stdout)
            content = dest.read_text()
            self.assertIn('projects = ["*-sandbox"]', content)
            self.assertNotIn("team-a-sandbox", content)

    def test_a_malformed_cluster_entry_is_refused_before_the_file_is_written(self):
        for bad in ("payments-staging/us-central1", "a/b/c/", "a//c", "/b/c", "a/b/c/d"):
            with self.subTest(entry=bad), tempfile.TemporaryDirectory() as out_dir:
                dest = pathlib.Path(out_dir) / "terraform.tfvars"
                proc = self._run(
                    f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                    env=self._scope_env(SCOPE_EXCLUDE_CLUSTERS=f"a-good/us-central1/one, {bad}"),
                )
                self.assertIn("rc=1", proc.stdout, proc.stderr)
                self.assertIn(f"SCOPE_EXCLUDE_CLUSTERS entry '{bad}' is not project/location/cluster",
                              proc.stderr + proc.stdout)
                self.assertFalse(dest.exists(), "no tfvars is written for an entry the block cannot render")
                self.assertFalse((pathlib.Path(out_dir) / "terraform.tfvars.tmp").exists())


_LIVE_SCOPE_CR = (
    '{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"projects":["p3-project","p2-project"],'
    '"exclude":{"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}]}'
)
_LIVE_SCOPE_LINES = (
    'SCOPE_PROJECTS="p2-project p3-project"',
    'SCOPE_EXCLUDE_PROJECTS=""',
    'SCOPE_EXCLUDE_CLUSTERS="p2-project/us-central1/c1"',
)


class PreApplyScopeCheckTest(unittest.TestCase):
    """refuse_apply_over_undeclared_scope: L (live CR), R (release record),
    K (the keys). Refused only when L is present and non-empty and matches
    neither R nor K; every read that cannot decide fails closed, except under
    "warn", where a plan applies nothing.
    """

    def _run(self, cr, record, keys=None, mode="", context_present=True, served=None, latest_failed=True,
             python_noise=False):
        """cr / record: a JSON string the stub prints, or one of the failure
        spellings: 'notype', 'norelease', 'err'. served: the values of the
        previous revision; with latest_failed the history reads deployed then
        failed, otherwise superseded then deployed."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            ctx_rc = 0 if context_present else 1
            cr_case = {
                "notype": 'echo "error: the server doesn\x27t have a resource type \\"platformagents\\"" >&2; exit 1',
                "err": 'echo "Unable to connect to the server: dial tcp: i/o timeout" >&2; exit 1',
            }.get(cr, f"printf '%s\\n' '{cr}'; exit 0")
            record_case = {
                "norelease": 'echo "Error: release: not found" >&2; exit 1',
                "err": 'echo "Error: Kubernetes cluster unreachable" >&2; exit 1',
            }.get(record, f"printf '%s\\n' '{record}'; exit 0")
            (bin_dir / "kubectl").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"config get-contexts"*) exit {ctx_rc} ;;\n'
                f'  *"get platformagents"*) {cr_case} ;;\n'
                "esac\nexit 1\n"
            )
            if served is None:
                history = '[{"revision": 3, "status": "deployed"}]'
                served_case = "exit 1"
            else:
                history = ('[{"revision": 3, "status": "deployed"}, {"revision": 4, "status": "failed"}]'
                           if latest_failed else
                           '[{"revision": 3, "status": "superseded"}, {"revision": 4, "status": "deployed"}]')
                served_case = f"printf '%s\\n' '{served}'; exit 0"
            (bin_dir / "helm").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"get values"*"--revision 3"*) {served_case} ;;\n'
                f'  *"get values"*) {record_case} ;;\n'
                f"  *\"history\"*) printf '%s\\n' '{history}'; exit 0 ;;\n"
                "esac\nexit 1\n"
            )
            if python_noise:
                # An interpreter that writes to stderr and exits 0: PYTHONWARNINGS,
                # -X dev, a half-installed prefix. The verdict must not read it.
                real = shutil.which("python3")
                (bin_dir / "python3").write_text(
                    "#!/usr/bin/env bash\n"
                    'echo "warning: stderr noise from the interpreter" >&2\n'
                    f'exec "{real}" "$@"\n'
                )
            for stub in ("kubectl", "helm") + (("python3",) if python_noise else ()):
                path = bin_dir / stub
                path.chmod(path.stat().st_mode | stat.S_IEXEC)
            env = {"PROJECT_ID": "test-project", "CLUSTER_NAME": "test-cluster", "REGION": "us-central1",
                   "SCOPE_PROJECTS": "", "SCOPE_EXCLUDE_PROJECTS": "", "SCOPE_EXCLUDE_CLUSTERS": ""}
            env.update(keys or {})
            body = (
                "set -u\n"
                'print_error() { echo "ERROR: $*"; }; print_info() { echo "INFO: $*"; }\n'
                'print_warning() { echo "WARN: $*"; }; print_success() { :; }\n'
                f'source "{_INSTALLER_COMMON}"\n'
                f'refuse_apply_over_undeclared_scope kubeagents-system {mode}; echo "rc=$?"\n'
            )
            return subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                                  env=get_isolated_test_env(overrides=env, bin_dir=str(bin_dir)),
                                  cwd=str(_REPO_ROOT))

    def _assert_rc(self, proc, rc):
        self.assertIn(f"rc={rc}", proc.stdout, proc.stdout + proc.stderr)

    def test_nothing_live_to_protect_passes_silently(self):
        for label, cr in (
            ("no PlatformAgent type served", "notype"),
            ("no PlatformAgent", '{"items":[]}'),
            ("a CR without a scope block", '{"items":[{"metadata":{"name":"platform-agent"},"spec":{}}]}'),
            ("a present but empty block", '{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{}}}]}'),
        ):
            with self.subTest(case=label):
                proc = self._run(cr, "norelease")
                self._assert_rc(proc, 0)
                self.assertNotIn("WARN", proc.stdout)
                self.assertNotIn("ERROR", proc.stdout)

    def test_a_hand_declared_scope_with_no_record_and_no_keys_is_refused_with_the_lines(self):
        proc = self._run(_LIVE_SCOPE_CR, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn("declares a scope this install did not write and install.env does not carry", proc.stdout)
        self.assertIn("retires the projects it drops", proc.stdout)
        for line in _LIVE_SCOPE_LINES:
            with self.subTest(line=line):
                self.assertIn(f"INFO:   {line}", proc.stdout)
        self.assertIn("edit the PlatformAgent", proc.stdout)

    def test_keys_that_carry_the_live_scope_pass_whatever_their_spelling(self):
        # Order, separators and repeats are spelling: every list is compared
        # as a set, the cluster triples included (the CR's list is a map keyed
        # on the triple, so it never repeats; a repeated triple in install.env
        # is Terraform's distinct validation to refuse, with its own message).
        proc = self._run(_LIVE_SCOPE_CR, "norelease", keys={
            "SCOPE_PROJECTS": "p3-project,p2-project p2-project",
            "SCOPE_EXCLUDE_CLUSTERS": "p2-project/us-central1/c1, p2-project/us-central1/c1",
        })
        self._assert_rc(proc, 0)

    def test_a_scope_the_installer_wrote_may_be_changed_or_emptied(self):
        # L == R: the record shows the installer rendered it; the keys are the
        # new declaration, and dropping the last project needs no override.
        record = ('{"platformAgent":{"scope":{"projects":["p2-project","p3-project"],"exclude":{"projects":[],'
                  '"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}')
        for keys in ({}, {"SCOPE_PROJECTS": "p2-project"}, {"SCOPE_PROJECTS": "p2-project p3-project p4-project"}):
            with self.subTest(keys=keys):
                self._assert_rc(self._run(_LIVE_SCOPE_CR, record, keys=keys), 0)

    def test_a_failed_upgrades_values_do_not_make_the_served_scope_a_hand_edit(self):
        # Revision 4 failed with projects [p2]; the CR still holds revision 3's
        # [p2, p3]. The retry with the same keys must pass, not tell the operator
        # to record a scope the installer itself wrote.
        latest = '{"platformAgent":{"scope":{"projects":["p2-project"],"exclude":{"projects":[],"clusters":[]}}}}'
        served = ('{"platformAgent":{"scope":{"projects":["p3-project","p2-project"],"exclude":{"projects":[],'
                  '"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}')
        proc = self._run(_LIVE_SCOPE_CR, latest, keys={"SCOPE_PROJECTS": "p2-project"}, served=served)
        self._assert_rc(proc, 0)
        # And without a served revision that matches, the same shape is refused.
        proc = self._run(_LIVE_SCOPE_CR, latest, keys={"SCOPE_PROJECTS": "p2-project"}, served=latest)
        self._assert_rc(proc, 1)

    def test_a_previous_revisions_scope_is_not_a_record_once_the_latest_served(self):
        # Revision 3 (superseded) rendered [p2, p3]; revision 4 (deployed)
        # rendered [p2] and the CR held it, until a hand edit put p3 back. The
        # latest revision served, so it is the one record: the hand edit is
        # refused, not read as the installer's own earlier declaration.
        latest = '{"platformAgent":{"scope":{"projects":["p2-project"],"exclude":{"projects":[],"clusters":[]}}}}'
        previous = ('{"platformAgent":{"scope":{"projects":["p3-project","p2-project"],"exclude":{"projects":[],'
                    '"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}')
        proc = self._run(_LIVE_SCOPE_CR, latest, keys={"SCOPE_PROJECTS": "p2-project"},
                         served=previous, latest_failed=False)
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_PROJECTS="p2-project p3-project"', proc.stdout)

    def test_containers_on_the_live_cr_are_reported_and_never_weighed(self):
        # folders and organizations (phase 2) have no installer key and the
        # chart renders neither, so an apply leaves them alone: a CR carrying
        # only containers passes with a note, and a refused mixed edit still
        # prints the three lines it can reproduce plus the note.
        only_containers = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"folders":["123456789012"],'
                           '"organizations":["987654321098"]}}}]}')
        proc = self._run(only_containers, "norelease")
        self._assert_rc(proc, 0)
        self.assertIn("also declares folders: 123456789012 organizations: 987654321098, which the installer has no key for yet", proc.stdout)
        mixed = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"projects":["p2-project"],'
                 '"folders":["123456789012"]}}}]}')
        proc = self._run(mixed, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_PROJECTS="p2-project"', proc.stdout)
        self.assertIn("also declares folders: 123456789012, which the installer has no key for yet", proc.stdout)

    def test_a_hand_edit_after_the_installer_wrote_it_is_refused(self):
        # L != R (p3-project and the exclusion were added by hand) and L != K.
        record = '{"platformAgent":{"scope":{"projects":["p2-project"],"exclude":{"projects":[],"clusters":[]}}}}'
        proc = self._run(_LIVE_SCOPE_CR, record, keys={"SCOPE_PROJECTS": "p2-project"})
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_PROJECTS="p2-project p3-project"', proc.stdout)

    def test_a_hand_set_exclusion_alone_is_protected(self):
        cr = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"exclude":{"projects":["*-sandbox"]}}}}]}')
        proc = self._run(cr, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_EXCLUDE_PROJECTS="*-sandbox"', proc.stdout)

    def test_warn_mode_speaks_and_passes(self):
        proc = self._run(_LIVE_SCOPE_CR, "norelease", mode="warn")
        self._assert_rc(proc, 0)
        self.assertIn("WARN: The PlatformAgent 'platform-agent'", proc.stdout)
        self.assertIn("the plan below shows the change", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)

    def test_a_missing_context_refuses_and_names_the_fetch(self):
        # The apply needs no kubeconfig (the helm provider authenticates with a
        # token against the endpoint), so a check that could not read must
        # stop the run rather than let the apply proceed over a scope nobody read.
        proc = self._run(_LIVE_SCOPE_CR, "norelease", context_present=False)
        self._assert_rc(proc, 1)
        self.assertIn("ERROR: Refusing to apply: the kubeconfig has no context 'gke_test-project_us-central1_test-cluster'", proc.stdout)
        self.assertIn("gcloud container clusters get-credentials test-cluster --location us-central1 --project test-project", proc.stdout)

    def test_a_missing_context_only_warns_under_a_plan(self):
        proc = self._run(_LIVE_SCOPE_CR, "norelease", mode="warn", context_present=False)
        self._assert_rc(proc, 0)
        self.assertIn("WARN: The scope check did not run: the kubeconfig has no context", proc.stdout)

    def test_interpreter_noise_on_stderr_does_not_become_a_refusal(self):
        record = ('{"platformAgent":{"scope":{"projects":["p2-project","p3-project"],"exclude":{"projects":[],'
                  '"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}')
        proc = self._run(_LIVE_SCOPE_CR, record, python_noise=True)
        self._assert_rc(proc, 0)
        self.assertNotIn("declares a scope", proc.stdout)
        # And a genuine failure still carries the interpreter's message.
        two = '{"items":[{"metadata":{"name":"a"},"spec":{"scope":{"projects":["p2-project"]}}},{"metadata":{"name":"b"},"spec":{}}]}'
        proc = self._run(two, "norelease", python_noise=True)
        self._assert_rc(proc, 1)
        self.assertIn("more than one PlatformAgent is served", proc.stdout)

    def test_a_read_that_cannot_decide_fails_closed_unless_warning(self):
        for label, cr, record in (
            ("the CR", "err", "norelease"),
            ("the record", _LIVE_SCOPE_CR, "err"),
            ("two PlatformAgents",
             '{"items":[{"metadata":{"name":"a"},"spec":{"scope":{"projects":["p2-project"]}}},{"metadata":{"name":"b"},"spec":{}}]}',
             "norelease"),
        ):
            with self.subTest(case=label):
                proc = self._run(cr, record)
                self._assert_rc(proc, 1)
                self.assertIn("ERROR: Refusing to apply:", proc.stdout)
                proc = self._run(cr, record, mode="warn")
                self._assert_rc(proc, 0)
                self.assertIn("WARN: The scope check did not run:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
