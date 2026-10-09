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
import shlex
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

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
        sa_list_stub="exit 1",
        gcloud_stderr=None,
        gcloud_extra_cases="",
        get_credentials_stub=None,
    ):
        """Source installer_common.sh with print stubs and run `script`.

        A stub `gcloud` on PATH prints `gcloud_stdout` (when given) and exits
        `gcloud_exit` for `storage cat` calls on the state object;
        `clusters describe` runs `describe_stub` (default: exit 1, meaning
        the cluster does not exist).

        `gcloud_extra_cases` is spliced in ahead of those arms, for a caller
        that needs to answer a subcommand none of them match — `get-credentials`
        and its `--help` probe, which fall through to the state read otherwise.
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
                f"{gcloud_extra_cases}"
                f"  *\"clusters describe\"*) {describe_stub} ;;\n"
                f"{get_cred_case}"
                f"  *\"keys versions list\"*) printf '%s' '{kms_versions}'; exit 0 ;;\n"
                f"  *\"service-accounts describe\"*) {sa_describe_stub} ;;\n"
                f"  *\"service-accounts list\"*) {sa_list_stub} ;;\n"
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
                    # The generator reads both as ${VAR:-}, so empty is unset;
                    # a developer's exported memory mode must not steer a test.
                    "MEMORY": "",
                    "MEMORY_PROVIDER": "",
                    # Same reasoning, and the same ${VAR:-} read in
                    # write_tfvars_from_state. get_isolated_test_env filters
                    # the CI names and nothing else, so without this a shell
                    # exporting ENABLE_DRIFT_DETECTOR=false reaches every case
                    # that does not set it -- including the arm below that
                    # asserts the drift keys are written when nobody asks.
                    # Blanking it is "unset", which that arm wants: `:-` takes
                    # the default for an empty value as well as an absent one.
                    "ENABLE_DRIFT_DETECTOR": "",
                    # Same again for the scoped pool's two keys, read as
                    # ${VAR:-} and refused (rc=1, no tfvars) on a malformed
                    # value: a shell exporting SCOPED_SA_POOL_MAX_ACCOUNTS=0
                    # would otherwise fail every generator case that does not
                    # set them, and SCOPED_SA_POOL_ENABLED=true would arm the
                    # pool in every file the cases read.
                    "SCOPED_SA_POOL_ENABLED": "",
                    "SCOPED_SA_POOL_MAX_ACCOUNTS": "",
                    # And the mode, refused on a value outside the CRD's enum.
                    "PLATFORM_AGENT_MODE": "",
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

    # ── the drift keys: written only when on, and both together ─────────────

    def _drift_tfvars(self, **env):
        """_tfvars under these keys, with a credential and an existing cluster.

        API_SERVER_KEY because the generator refuses to write without one, and
        the Autopilot describe stub because the default says the cluster does
        not exist, which writes a different file.
        """
        return self._tfvars(
            {"API_SERVER_KEY": "k", **env},
            describe_stub=_autopilot_describe_stub(),
        )

    def test_tfvars_writes_both_drift_keys_when_the_detector_is_on(self):
        """The ingress and the consumer travel together, on every truthy
        spelling install.env accepts.

        The composition's helm_release precondition refuses
        enable_drift_detector without enable_drift_pubsub, so one key here has
        to produce both. The spellings are the point of the loop: every other
        boolean in this generator reaches is_truthy through hcl_bool, and a
        compare against the lowercase literal would read
        ENABLE_DRIFT_DETECTOR=True as off and provision nothing at all, which
        is the outcome with no error and nothing to observe afterwards.
        """
        for value in ("true", "True", "TRUE", "yes", "y", "1", "on", " true "):
            with self.subTest(value=value):
                content = self._drift_tfvars(ENABLE_DRIFT_DETECTOR=value)
                self.assertIn("enable_drift_pubsub   = true", content)
                self.assertIn("enable_drift_detector = true", content)

    def test_tfvars_omits_the_drift_keys_when_the_detector_is_off(self):
        """Omitted rather than written false, which is this generator's one
        boolean exception, and the reason for it.

        enable_drift_pubsub is also reachable on its own as a TF_VAR_ line in
        install.env, and terraform.tfvars beats TF_VAR_. `enable_drift_pubsub
        = false` here would therefore override an install already running the
        audit-log ingress that way, and the next upgrade would destroy its
        sink, topic and subscription under -auto-approve.

        Every arm here is an explicit opt-out, because that is the only way to
        reach the off branch now that DEFAULT_ENABLE_DRIFT_DETECTOR is true.
        The companion below owns the arms that say nothing.
        """
        for value in ("false", "False", "no", "0", "off"):
            with self.subTest(value=value):
                content = self._drift_tfvars(ENABLE_DRIFT_DETECTOR=value)
                self.assertNotIn("enable_drift_pubsub", content)
                self.assertNotIn("enable_drift_detector", content)

    def test_tfvars_writes_both_drift_keys_when_nobody_says_anything(self):
        """Saying nothing provisions the trio, which is what flipping
        DEFAULT_ENABLE_DRIFT_DETECTOR to true means and the one arm that
        proves the generator reads the default rather than a literal.

        Both arms are "unset" to the `${ENABLE_DRIFT_DETECTOR:-...}` the gate
        expands: `:-` takes the default for an empty value as well as an
        absent one, so an install.env carrying `ENABLE_DRIFT_DETECTOR=` gets
        the detector, not the off branch. The None arm only means "absent"
        because _run blanks ENABLE_DRIFT_DETECTOR in the isolated environment
        it builds; get_isolated_test_env copies the rest of os.environ
        through, so without that blank this arm would assert against whatever
        the developer's shell happened to export.

        This is also what an install predating the key gets: its install.env
        records no choice, so the next run of either front door reads the
        default here and provisions the sink, topic and subscription. That is
        the intended behaviour -- running an installer is the consent -- and
        this case is where it is pinned.
        """
        for value in ("", None):
            with self.subTest(value=value):
                env = {} if value is None else {"ENABLE_DRIFT_DETECTOR": value}
                content = self._drift_tfvars(**env)
                self.assertIn("enable_drift_pubsub   = true", content)
                self.assertIn("enable_drift_detector = true", content)

    def test_an_exported_value_survives_the_file_load_into_the_tfvars(self):
        """install.env is not this generator's only input, and no front door makes it one.

        install.sh's unrecorded-value guard tells the operator what upgrade.sh
        will do with a key their install.env does not record, so that sentence
        has to match this. `write_tfvars_from_state` reads
        ${ENABLE_DRIFT_DETECTOR:-...} out of the environment;
        `load_install_env` clears NAMESPACE and the seven scope keys before
        sourcing, and upgrade.sh clears PROJECT_ID, CLUSTER_NAME and REGION;
        ENABLE_DRIFT_DETECTOR is on neither list. So `ENABLE_DRIFT_DETECTOR=true
        ./upgrade.sh` over a file predating the key provisions the sink, topic
        and subscription -- on a front door with no guard on that route at all
        -- and the next upgrade from a shell without the export writes neither
        key and destroys them under -auto-approve.

        The scope key is the contrast, and the reason this is asserted as a
        pair: the clearing list is what decides, and a sentence claiming either
        key comes from the file alone is true of exactly one of them.

        Only load_install_env's half of that list is read here -- the body
        sources installer_common.sh and never runs upgrade.sh. The other half
        is pinned by test_upgrade_script.py's
        test_the_upgrade_clearing_list_is_the_three_coordinates, which is the
        one that fails if ENABLE_DRIFT_DETECTOR is ever added to it and the
        guard's sentence goes stale.
        """
        with tempfile.TemporaryDirectory() as tmp:
            install_env = pathlib.Path(tmp) / "install.env"
            install_env.write_text("PROJECT_ID=p\n")
            dest = pathlib.Path(tmp) / "terraform.tfvars"
            proc = self._run(
                f'load_install_env "{install_env}"; write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    "ENABLE_DRIFT_DETECTOR": "true",
                    "SCOPE_PROJECTS": "exported-project",
                },
                describe_stub=_autopilot_describe_stub(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn("enable_drift_pubsub   = true", content)
            self.assertIn("enable_drift_detector = true", content)
            self.assertNotIn("exported-project", content)

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
                     "SCOPE_EXCLUDE_CLUSTERS": "s/l/c", "SCOPE_MAX_PROJECTS": "250"}
            probe = 'echo "P=${SCOPE_PROJECTS:-unset} X=${SCOPE_EXCLUDE_PROJECTS:-unset} C=${SCOPE_EXCLUDE_CLUSTERS:-unset} M=${SCOPE_MAX_PROJECTS:-unset}"'
            proc = self._run(f'load_install_env "{env_file}"; {probe}', env=stray)
            self.assertIn("P=unset X=unset C=unset M=unset", proc.stdout, proc.stderr)
            env_file.write_text("PROJECT_ID=p\nSCOPE_PROJECTS=from-the-file\nSCOPE_MAX_PROJECTS=300\n")
            proc = self._run(f'load_install_env "{env_file}"; {probe}', env=stray)
            self.assertIn("P=from-the-file X=unset C=unset M=300", proc.stdout, proc.stderr)

    def test_load_install_env_drops_a_shell_exported_pool_switch(self):
        # Same rule as the scope keys: the pool's switch renders into the
        # PlatformAgent and arms the broker, and a pre-change install.env
        # records neither key, so an inherited SCOPED_SA_POOL_ENABLED=true
        # would arm the pool for one upgrade.sh run on accounts the next run
        # from a clean shell deletes again.
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("PROJECT_ID=p\n")
            stray = {"SCOPED_SA_POOL_ENABLED": "true", "SCOPED_SA_POOL_MAX_ACCOUNTS": "250"}
            probe = 'echo "E=${SCOPED_SA_POOL_ENABLED:-unset} N=${SCOPED_SA_POOL_MAX_ACCOUNTS:-unset}"'
            proc = self._run(f'load_install_env "{env_file}"; {probe}', env=stray)
            self.assertIn("E=unset N=unset", proc.stdout, proc.stderr)
            env_file.write_text("PROJECT_ID=p\nSCOPED_SA_POOL_ENABLED=false\nSCOPED_SA_POOL_MAX_ACCOUNTS=120\n")
            proc = self._run(f'load_install_env "{env_file}"; {probe}', env=stray)
            self.assertIn("E=false N=120", proc.stdout, proc.stderr)

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

    # A `service-accounts list` stub that answers only a filter carrying this
    # install's marker, the way the real API applies the --filter: a member
    # whose description names another install's agent is never listed.
    _POOL_MEMBER_EMAIL = "ka-team-alpha-0ed42166@test-project.iam.gserviceaccount.com"
    _POOL_LIST_STUB = (
        '[[ "$*" == *"email:ka-*"* && "$*" == *"Pool member of kubeagents-platform-gsa for "* ]]'
        f' && {{ echo "{_POOL_MEMBER_EMAIL}"; exit 0; }}; exit 0'
    )

    def test_service_account_ownership_refuses_a_pool_member_this_state_does_not_own(self):
        # The lost-state re-install: the agent's account is gone (describe
        # misses) but the pool members it derived are still there, so the
        # refusal lists them by the marker their description carries.
        proc = self._run(
            self._SHOW_REMEDY,
            gcloud_exit=1,
            sa_list_stub=self._POOL_LIST_STUB,
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("a scoped pool member", proc.stderr)
        self.assertIn(f"gcloud iam service-accounts delete {self._POOL_MEMBER_EMAIL}", proc.stderr)
        self.assertIn("PLATFORM_AGENT_GSA_NAME", proc.stderr)

    def test_service_account_ownership_passes_a_pool_member_this_state_owns(self):
        proc = self._run(
            'check_service_account_ownership; echo "rc=$?"',
            gcloud_stdout=_service_account_state("kubeagents-platform-gsa", "ka-team-alpha-0ed42166"),
            sa_describe_stub="exit 0",
            sa_list_stub=self._POOL_LIST_STUB,
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)

    def test_service_account_ownership_ignores_another_installs_pool_members(self):
        # The filter names this install's agent, so a member marked for
        # kubeagents-platform-gsa is not this install's business.
        proc = self._run(
            'check_service_account_ownership; echo "rc=$?"',
            gcloud_exit=1,
            sa_list_stub=self._POOL_LIST_STUB,
            env={"PLATFORM_AGENT_GSA_NAME": "other-agent-gsa"},
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("ka-team-alpha", proc.stderr)

    def test_service_account_ownership_treats_a_failing_list_as_no_members(self):
        proc = self._run(
            'check_service_account_ownership; echo "rc=$?"',
            gcloud_exit=1,
            sa_list_stub='echo "ERROR: (gcloud.iam.service-accounts.list) PERMISSION_DENIED" >&2; exit 1',
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("ERROR: Service account", proc.stderr)

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

    # Empty reads as unset, so a developer's own exported value cannot stand in.
    _REDACTION_UNSET = {
        "LITELLM_REDACTION_ENABLED": "",
        "LITELLM_REDACTION_IP_ACTION": "",
        "LITELLM_REDACTION_IP_ALLOW_CIDRS": "",
        "LITELLM_REDACTION_RULES": "",
    }

    def test_tfvars_carry_litellm_redaction(self):
        # Off writes the toggle alone: the other keys are inert, so a leftover
        # value is neither read nor checked. The composition renders nothing
        # into the chart while enabled is false.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for env, expected in (
                ({}, "litellm_redaction = { enabled = false }\n"),
                (
                    {
                        "LITELLM_REDACTION_ENABLED": "off",
                        "LITELLM_REDACTION_IP_ACTION": "hash",
                        "LITELLM_REDACTION_RULES": "not json",
                    },
                    "litellm_redaction = { enabled = false }\n",
                ),
                (
                    {
                        "LITELLM_REDACTION_ENABLED": "yes",
                        "LITELLM_REDACTION_IP_ACTION": "off",
                        "LITELLM_REDACTION_IP_ALLOW_CIDRS": "127.0.0.0/8, fd00::/8 10.0.0.0/8",
                        "LITELLM_REDACTION_RULES": '[{"name":"cluster-name","literal":"prod-eu-1","action":"pseudonym"}]',
                    },
                    "litellm_redaction = {\n"
                    "  enabled     = true\n"
                    '  ip_action   = "off"\n'
                    '  allow_cidrs = ["127.0.0.0/8", "fd00::/8", "10.0.0.0/8"]\n'
                    '  rules       = [{ name = "cluster-name", literal = "prod-eu-1", action = "pseudonym" }]\n'
                    "}\n",
                ),
            ):
                with self.subTest(env=env):
                    proc = self._run(
                        f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                        env={"API_SERVER_KEY": "k", **self._REDACTION_UNSET, **env},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=0", proc.stdout, proc.stderr)
                    self.assertIn(expected, dest.read_text())

    def test_tfvars_carry_the_scoped_sa_pool(self):
        # The switch is always written, false by default, so the file states
        # whether the pool is armed; the cap only when set, like the scope's.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for env, expected, absent in (
                ({}, "scoped_pool_enabled      = false\n", "scoped_pool_max_accounts"),
                ({"SCOPED_SA_POOL_ENABLED": "off", "SCOPED_SA_POOL_MAX_ACCOUNTS": "50"},
                 "scoped_pool_enabled      = false\nscoped_pool_max_accounts = 50\n", None),
                ({"SCOPED_SA_POOL_ENABLED": "yes", "SCOPED_SA_POOL_MAX_ACCOUNTS": "250"},
                 "scoped_pool_enabled      = true\nscoped_pool_max_accounts = 250\n", None),
                ({"SCOPED_SA_POOL_ENABLED": "true"},
                 "scoped_pool_enabled      = true\n", "scoped_pool_max_accounts"),
            ):
                with self.subTest(env=env):
                    proc = self._run(
                        f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                        env={"API_SERVER_KEY": "k", **env},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=0", proc.stdout, proc.stderr)
                    content = dest.read_text()
                    self.assertIn(expected, content)
                    if absent:
                        self.assertNotIn(absent, content)

    def test_tfvars_refuse_scoped_sa_pool_values_terraform_cannot_take(self):
        # upgrade.sh regenerates from install.env without install.sh's checks,
        # so the generator names the key and writes nothing; a misspelt switch
        # is refused rather than read as off.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for key, value, message in (
                ("SCOPED_SA_POOL_ENABLED", "ture", "is neither true nor false"),
                ("SCOPED_SA_POOL_ENABLED", "armed", "is neither true nor false"),
                ("SCOPED_SA_POOL_MAX_ACCOUNTS", "0", "is not a whole number of at least 1"),
                ("SCOPED_SA_POOL_MAX_ACCOUNTS", "lots", "is not a whole number of at least 1"),
                ("SCOPED_SA_POOL_MAX_ACCOUNTS", "2.5", "is not a whole number of at least 1"),
            ):
                with self.subTest(key=key, value=value):
                    proc = self._run(
                        f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                        env={"API_SERVER_KEY": "k", key: value},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=1", proc.stdout, proc.stderr)
                    self.assertIn(f"{key}='{value}'", proc.stderr + proc.stdout)
                    self.assertIn(message, proc.stderr + proc.stdout)
                    self.assertFalse(dest.exists(), "no tfvars is written for a value Terraform would refuse")

    def test_the_scoped_sa_pool_cap_is_a_whole_number_of_at_least_one(self):
        for bad in ("0", "00", "abc", "2.5", "-3", " 12", "1e3"):
            with self.subTest(bad=bad):
                proc = subprocess.run(
                    ["bash", "-c",
                     'print_error() { echo "ERROR: $*"; }; print_info() { :; }; print_warning() { :; }; print_success() { :; }\n'
                     f'source "{_INSTALLER_COMMON}"\nrequire_scoped_sa_pool_max_accounts {shlex.quote(bad)}; echo "rc=$?"'],
                    capture_output=True, text=True, env=get_isolated_test_env(), cwd=str(_REPO_ROOT),
                )
                self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn(f"SCOPED_SA_POOL_MAX_ACCOUNTS='{bad}' is not a whole number of at least 1", proc.stdout)
                self.assertIn("default (100)", proc.stdout)
        for good in ("", "1", "100", "0250", "5000"):
            with self.subTest(good=good):
                proc = subprocess.run(
                    ["bash", "-c",
                     'print_error() { echo "ERROR: $*"; }; print_info() { :; }; print_warning() { :; }; print_success() { :; }\n'
                     f'source "{_INSTALLER_COMMON}"\nrequire_scoped_sa_pool_max_accounts {shlex.quote(good)}; echo "rc=$?"'],
                    capture_output=True, text=True, env=get_isolated_test_env(), cwd=str(_REPO_ROOT),
                )
                self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)

    def test_tfvars_escape_litellm_redaction_rules_for_hcl(self):
        # A regular expression may hold ${ or %{, which HCL reads as a
        # template, as well as backslashes and quotes.
        rules = json.dumps([{"name": "tmpl", "pattern": 'a${b}%{c}\\d"\n'}])
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={
                    "API_SERVER_KEY": "k",
                    **self._REDACTION_UNSET,
                    "LITELLM_REDACTION_ENABLED": "true",
                    "LITELLM_REDACTION_RULES": rules,
                },
                describe_stub="printf '\\n'; exit 0",
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn(
                '  rules       = [{ name = "tmpl", pattern = "a$${b}%%{c}\\\\d\\"\\n" }]\n',
                dest.read_text(),
            )

    def test_tfvars_refuse_litellm_redaction_values_terraform_cannot_take(self):
        # upgrade.sh and uninstall.sh regenerate from install.env without
        # install.sh's checks, so the generator names the key and writes nothing.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for key, value, message in (
                ("LITELLM_REDACTION_ENABLED", "ture", "is neither true nor false"),
                ("LITELLM_REDACTION_ENABLED", "enabled", "is neither true nor false"),
                ("LITELLM_REDACTION_IP_ACTION", "hash", "is not one of mask, pseudonym, off"),
                ("LITELLM_REDACTION_IP_ACTION", "OFF", "is not one of mask, pseudonym, off"),
                ("LITELLM_REDACTION_RULES", "not json", "is not valid JSON"),
                ("LITELLM_REDACTION_RULES", '{"name":"x","literal":"y"}', "must be a JSON array"),
                ("LITELLM_REDACTION_RULES", '["x"]', "entry 0 is not an object"),
                ("LITELLM_REDACTION_RULES", '[{"name":"x","literl":"y"}]', "unknown key(s) ['literl']"),
                ("LITELLM_REDACTION_RULES", '[{"literal":"prod-eu-1"}]', "entry 0 has no name"),
                ("LITELLM_REDACTION_RULES", "[{}]", "entry 0 has no name"),
                ("LITELLM_REDACTION_RULES", '[{"name":"x","literal":7}]', "entry 0: literal must be a string"),
                ("LITELLM_REDACTION_RULES", '[{"name":"x","literal":"\\ud800"}]', "entry 0: literal is not valid UTF-8 text"),
            ):
                with self.subTest(key=key, value=value):
                    proc = self._run(
                        f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                        env={
                            "API_SERVER_KEY": "k",
                            **self._REDACTION_UNSET,
                            "LITELLM_REDACTION_ENABLED": "true",
                            key: value,
                        },
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=1", proc.stdout, proc.stderr)
                    self.assertIn(f"{key}", proc.stderr)
                    self.assertIn(message, proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr + proc.stdout)
                    self.assertFalse(dest.exists(), "no tfvars is written for a value Terraform would refuse")

    def test_a_rules_refusal_reaches_the_operator_through_the_command_substitution(self):
        # The generator captures hcl_redaction_rules' output, so its message
        # must go to stderr even with the real print_error, which writes to
        # stdout.
        proc = self._run(
            "print_error() { echo \"ERROR: $*\"; }\n"
            'rc=0; out="$(hcl_redaction_rules "not json")" || rc=$?; echo "rc=$rc out=[$out]"'
        )
        self.assertIn("rc=1 out=[]", proc.stdout)
        self.assertIn("LITELLM_REDACTION_RULES is not valid JSON", proc.stderr)

    # ── PLATFORM_AGENT_MODE: the PlatformAgent's spec.mode ──────────────────

    def test_tfvars_carry_the_platform_agent_mode(self):
        # Written on every run, the default included, so upgrade.sh carries the
        # recorded mode forward; the composition renders nothing for "today".
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for env, expected in (
                ({}, 'platform_agent_mode = "today"\n'),
                ({"PLATFORM_AGENT_MODE": "today"}, 'platform_agent_mode = "today"\n'),
                ({"PLATFORM_AGENT_MODE": "next"}, 'platform_agent_mode = "next"\n'),
            ):
                with self.subTest(env=env):
                    proc = self._run(
                        f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                        env={"API_SERVER_KEY": "k", **env},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=0", proc.stdout, proc.stderr)
                    self.assertIn(expected, dest.read_text())

    def test_tfvars_refuse_a_platform_agent_mode_outside_the_crd_enum(self):
        # upgrade.sh and the menu regenerate from install.env without
        # install.sh's check: the generator names the key and writes nothing.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            for value in ("Next", "later", "today "):
                with self.subTest(value=value):
                    proc = self._run(
                        f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                        env={"API_SERVER_KEY": "k", "PLATFORM_AGENT_MODE": value},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=1", proc.stdout, proc.stderr)
                    self.assertIn(f"PLATFORM_AGENT_MODE='{value}' is not one of today, next. Fix it in install.env.", proc.stderr)
                    self.assertFalse(dest.exists(), "no tfvars is written for a value Terraform would refuse")

    def test_load_install_env_reads_the_mode_from_the_file_alone(self):
        # An inherited PLATFORM_AGENT_MODE would switch the install for one run;
        # the file's own key still loads.
        with tempfile.TemporaryDirectory() as tmp:
            silent = pathlib.Path(tmp) / "silent.env"
            silent.write_text("PROJECT_ID=p\n")
            recorded = pathlib.Path(tmp) / "recorded.env"
            recorded.write_text("PLATFORM_AGENT_MODE=next\n")
            for file, expected in ((silent, "MODE=[]"), (recorded, "MODE=[next]")):
                with self.subTest(file=file.name):
                    proc = self._run(
                        f'load_install_env "{file}"; echo "MODE=[${{PLATFORM_AGENT_MODE:-}}]"',
                        env={"PLATFORM_AGENT_MODE": "next" if file is silent else "today"},
                    )
                    self.assertIn(expected, proc.stdout, proc.stderr)

    def test_a_recorded_mode_is_carried_into_the_regenerated_tfvars(self):
        # upgrade.sh's full path, and the menu's: load install.env, then
        # regenerate. The recorded mode reaches the tfvars; an exported one
        # over a file that records none does not.
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "terraform.tfvars"
            recorded = pathlib.Path(tmp) / "recorded.env"
            recorded.write_text("PLATFORM_AGENT_MODE=next\n")
            silent = pathlib.Path(tmp) / "silent.env"
            silent.write_text("PROJECT_ID=test-project\n")
            for file, exported, expected in (
                (recorded, "", 'platform_agent_mode = "next"\n'),
                (recorded, "today", 'platform_agent_mode = "next"\n'),
                (silent, "next", 'platform_agent_mode = "today"\n'),
            ):
                with self.subTest(file=file.name, exported=exported):
                    proc = self._run(
                        f'load_install_env "{file}"; write_tfvars_from_state "{dest}"; echo "rc=$?"',
                        env={"API_SERVER_KEY": "k", "PLATFORM_AGENT_MODE": exported},
                        describe_stub="printf '\\n'; exit 0",
                    )
                    self.assertIn("rc=0", proc.stdout, proc.stderr)
                    self.assertIn(expected, dest.read_text())

    _CONTEXT_KUBECTL = '#!/usr/bin/env bash\ncase "$*" in *"config get-contexts"*) exit 0 ;; esac\nexit 1\n'

    # A CR list as `kubectl get platformagents -o json` prints it: no CR, a CR
    # with no mode, or a CR with the mode given.
    @staticmethod
    def _cr_list(mode=None, present=True):
        if not present:
            return '{"items": []}'
        spec = f'{{"mode": "{mode}"}}' if mode else "{}"
        return f'{{"items": [{{"metadata": {{"name": "platform-agent"}}, "spec": {spec}}}]}}'

    _NO_TYPE = "error: the server doesn't have a resource type platformagents"
    _UNREACHABLE = "Unable to connect to the server: dial tcp: i/o timeout"

    @staticmethod
    def _cr_kubectl(listing, exit_code=0, stderr=""):
        """A kubectl with this cluster's context whose CR read prints
        `listing` (and `stderr`) and exits `exit_code`. Every CR read leaves a
        line in $KUBECTL_LOG when the caller sets one."""
        return (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '  *"config get-contexts"*) exit 0 ;;\n'
            '  *"get platformagents"*) [ -n "${KUBECTL_LOG:-}" ] && echo cr >> "$KUBECTL_LOG"\n'
            f"    printf '%s' '{listing}'; printf '%s' \"{stderr}\" >&2; exit {exit_code} ;;\n"
            "esac\nexit 1\n"
        )

    # A helm stub, as a function: `history` prints the revisions given, `get
    # values` the values of the revision asked for, or `latest` without
    # --revision. A value of None fails that read; `latest="norelease"` is
    # Helm's missing-release answer. Each call leaves a line in $HELM_LOG.
    @staticmethod
    def _helm(history, values_by_revision, latest="{}"):
        history_arm = (
            "echo 'Error: Kubernetes cluster unreachable' >&2; return 1"
            if history is None else f"printf '%s' '{history}'"
        )

        def arm(vals):
            if vals is None:
                return "echo 'Error: Kubernetes cluster unreachable' >&2; return 1"
            if vals == "norelease":
                return "echo 'Error: release: not found' >&2; return 1"
            return f"printf '%s' '{vals}'"
        cases = "".join(
            f'      *"--revision {rev}"*) {arm(vals)} ;;\n' for rev, vals in values_by_revision.items()
        )
        return (
            "helm() {\n"
            '  [ -n "${HELM_LOG:-}" ] && echo "$*" >> "$HELM_LOG"\n'
            '  case "$1" in\n'
            f"    history) {history_arm} ;;\n"
            '    get) case "$*" in\n'
            f"{cases}"
            '      *"--revision"*) return 1 ;;\n'
            f"      *) {arm(latest)} ;;\n"
            "    esac ;;\n"
            "    *) return 1 ;;\n"
            "  esac\n"
            "}\n"
        )

    _HEALTHY = '[{"revision": 1, "status": "superseded"}, {"revision": 2, "status": "deployed"}]'

    @staticmethod
    def _no_helm(log):
        """A helm that must not run: each call leaves a line in `log`, which
        a command substitution cannot swallow the way it swallows stdout, so
        the case asserts `log` was never created."""
        return f'helm() {{ echo "$*" >> "{log}"; return 99; }}\n'
    _LOUD_PRINTS = 'print_warning() { echo "WARN: $*"; }\nprint_info() { echo "INFO: $*"; }\n'

    def _announce(self, kubectl, helm_stub, key="next", call=None):
        call = call or f"announce_platform_agent_mode_for_apply kubeagents-system {key}"
        return self._run(f"{self._LOUD_PRINTS}{helm_stub}{call}", kubectl_script=kubectl)

    def test_the_notice_compares_the_cr_with_the_served_revision(self):
        failed_latest = '[{"revision": 1, "status": "deployed"}, {"revision": 2, "status": "failed"}]'
        for kubectl, helm_stub, expect in (
            # Installed today; the key now asks for next.
            (self._cr_kubectl(self._cr_list()), self._helm(self._HEALTHY, {}, latest="{}"),
             "WARN: This apply switches the install from spec.mode today to next"),
            # A failed latest revision records next, but Helm patches from the
            # one that served, which has no mode: still a switch.
            (self._cr_kubectl(self._cr_list()),
             self._helm(failed_latest, {1: "{}"}, latest='{"platformAgent":{"mode":"next"}}'),
             "WARN: This apply switches the install from spec.mode today to next"),
            # Recorded and running next: a re-apply says nothing.
            (self._cr_kubectl(self._cr_list("next")),
             self._helm(self._HEALTHY, {}, latest='{"platformAgent":{"mode":"next"}}'), None),
        ):
            with self.subTest(helm_stub=helm_stub, kubectl=kubectl):
                proc = self._announce(kubectl, helm_stub)
                if expect is None:
                    self.assertEqual(proc.stdout, "", proc.stderr)
                else:
                    self.assertIn(expect, proc.stdout, proc.stderr)

    def test_a_first_install_is_quiet(self):
        # Nothing to switch: no PlatformAgent type, no CR, or no release. A
        # first install's missing cluster is the TFVARS_CLUSTER_EXISTS=false
        # case below, which reads nothing.
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "helm-must-not-run.log"
            for kubectl, helm_stub, helm_runs in (
                (self._cr_kubectl("", exit_code=1, stderr=self._NO_TYPE), self._no_helm(log), False),
                (self._cr_kubectl(self._cr_list(present=False)), self._helm(self._HEALTHY, {}, latest="norelease"), True),
                (self._cr_kubectl(self._cr_list()), self._helm(self._HEALTHY, {}, latest="norelease"), True),
            ):
                with self.subTest(kubectl=kubectl, helm_stub=helm_stub):
                    log.unlink(missing_ok=True)
                    proc = self._announce(kubectl, helm_stub)
                    self.assertEqual(proc.stdout, "", proc.stderr)
                    if not helm_runs:
                        self.assertFalse(log.exists(), log.read_text() if log.exists() else "")

    def test_a_cluster_with_no_context_says_the_check_did_not_run(self):
        # The generator found the cluster, but the kubeconfig has no context
        # for it (kubectl absent, or a get-credentials that did not land, on
        # the --dry-run and --generate-only routes the scope check skips). Not
        # a first install, so the notice says it did not run, with the fix.
        with tempfile.TemporaryDirectory() as tmp:
            hlog = pathlib.Path(tmp) / "helm.log"
            proc = self._run(
                f"{self._LOUD_PRINTS}{self._no_helm(hlog)}"
                "TFVARS_CLUSTER_EXISTS=true announce_platform_agent_mode_for_apply kubeagents-system next\n",
                kubectl_script=None,
            )
            self.assertIn("WARN: The mode-switch check did not run: the kubeconfig has no context", proc.stdout, proc.stderr)
            self.assertIn("gcloud container clusters get-credentials", proc.stdout)
            self.assertFalse(hlog.exists())

    def test_a_cluster_the_generator_found_absent_is_not_read(self):
        # A reinstall under a name used before: the tfvars generator's describe
        # said NOT_FOUND (TFVARS_CLUSTER_EXISTS=false), but the kubeconfig
        # still has the old install's context, whose endpoint is gone. Nothing
        # is asked and nothing is said; with the cluster there, the same
        # failed read still says the check did not run.
        unreachable = self._cr_kubectl("", exit_code=1, stderr=self._UNREACHABLE)
        with tempfile.TemporaryDirectory() as tmp:
            klog = pathlib.Path(tmp) / "kubectl.log"
            hlog = pathlib.Path(tmp) / "helm.log"
            proc = self._run(
                f'export KUBECTL_LOG="{klog}"\n{self._LOUD_PRINTS}{self._no_helm(hlog)}'
                "TFVARS_CLUSTER_EXISTS=false announce_platform_agent_mode_for_apply kubeagents-system next\n",
                kubectl_script=unreachable,
            )
            self.assertEqual(proc.stdout, "", proc.stderr)
            self.assertFalse(klog.exists(), "the PlatformAgent was read through a stale context")
            self.assertFalse(hlog.exists())
            loud = self._run(
                f"{self._LOUD_PRINTS}{self._no_helm(hlog)}"
                "TFVARS_CLUSTER_EXISTS=true announce_platform_agent_mode_for_apply kubeagents-system next\n",
                kubectl_script=unreachable,
            )
            self.assertIn("WARN: The mode-switch check did not run: the PlatformAgent", loud.stdout, loud.stderr)

    def test_a_read_that_fails_says_the_check_did_not_run(self):
        # Told apart from no CR by kubectl's and helm's exit status: the apply
        # goes on, so the run says the notice was not weighed.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = pathlib.Path(tmp.name) / "helm-must-not-run.log"
        failed_next = '{"platformAgent":{"mode":"next"}}'
        for kubectl, helm_stub, what in (
            (self._cr_kubectl("", exit_code=1, stderr=self._UNREACHABLE),
             self._no_helm(log),
             "the PlatformAgent in namespace 'kubeagents-system' could not be read (Unable to connect to the server"),
            (self._cr_kubectl(self._cr_list()), self._helm(self._HEALTHY, {}, latest=None),
             "the values of release 'kube-agents' in namespace 'kubeagents-system' could not be read (Error: Kubernetes cluster unreachable"),
            (self._cr_kubectl(self._cr_list()),
             self._helm('[{"revision": 1, "status": "deployed"}, {"revision": 2, "status": "failed"}]', {1: None}),
             "the values of release 'kube-agents' in namespace 'kubeagents-system' could not be read (revision 1, the last one that served, did not answer)"),
            # The history that names the served revision: without it the
            # notice would model from the latest revision (a failed one that
            # recorded next) and announce a switch to today that Helm, diffing
            # the served revision, does not make.
            (self._cr_kubectl(self._cr_list("next")), self._helm(None, {}, latest=failed_next),
             "the values of release 'kube-agents' in namespace 'kubeagents-system' could not be read (the release history did not answer: Error: Kubernetes cluster unreachable)"),
            (self._cr_kubectl(self._cr_list("next")), self._helm("not json", {}, latest=failed_next),
             "the values of release 'kube-agents' in namespace 'kubeagents-system' could not be read (the release history did not answer: Traceback"),
        ):
            with self.subTest(what=what):
                log.unlink(missing_ok=True)
                proc = self._announce(kubectl, helm_stub, key="today")
                self.assertIn(f"WARN: The mode-switch check did not run: {what}", proc.stdout, proc.stderr)
                self.assertIn("renders spec.mode from PLATFORM_AGENT_MODE=today in install.env", proc.stdout)
                # The by-hand check names the context the reads asked.
                self.assertIn(
                    "kubectl get platformagents -n kubeagents-system"
                    " --context gke_test-project_us-central1_test-cluster -o jsonpath=",
                    proc.stdout,
                )
                self.assertNotIn("This apply switches", proc.stdout)
                self.assertFalse(log.exists(), log.read_text() if log.exists() else "")

    def test_the_reads_ask_this_clusters_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "helm.log"
            self._announce(
                self._cr_kubectl(self._cr_list()),
                f'HELM_LOG="{log}"\n'
                + self._helm('[{"revision": 1, "status": "deployed"}, {"revision": 2, "status": "failed"}]', {1: "{}"}),
            )
            calls = log.read_text()
        ctx = "--kube-context gke_test-project_us-central1_test-cluster"
        self.assertIn(f"get values kube-agents -n kubeagents-system {ctx} -o json", calls)
        self.assertIn(f"history kube-agents -n kubeagents-system {ctx} -o json", calls)
        self.assertIn(f"get values kube-agents -n kubeagents-system {ctx} --revision 1 -o json", calls)

    def test_the_notice_takes_the_scope_checks_reads(self):
        # The scope check beside it has just read the CR and the release: the
        # notice asks neither again, and a later notice (the menu's next
        # apply) reads afresh.
        with tempfile.TemporaryDirectory() as tmp:
            klog = pathlib.Path(tmp) / "kubectl.log"
            hlog = pathlib.Path(tmp) / "helm.log"
            proc = self._run(
                f'export KUBECTL_LOG="{klog}"; HELM_LOG="{hlog}"\n'
                + self._LOUD_PRINTS
                + 'print_error() { echo "ERR: $*"; }\n'
                + self._helm(self._HEALTHY, {}, latest="{}")
                + "refuse_apply_over_undeclared_scope kubeagents-system; echo \"scope rc=$?\"\n"
                + 'echo "after scope: $(wc -l < "$KUBECTL_LOG") $(wc -l < "$HELM_LOG")"\n'
                + "announce_platform_agent_mode_for_apply kubeagents-system next\n"
                + 'echo "after notice: $(wc -l < "$KUBECTL_LOG") $(wc -l < "$HELM_LOG")"\n'
                + "announce_platform_agent_mode_for_apply kubeagents-system next >/dev/null\n"
                + 'echo "after second notice: $(wc -l < "$KUBECTL_LOG") $(wc -l < "$HELM_LOG")"\n',
                kubectl_script=self._cr_kubectl(self._cr_list()),
            )
        out = proc.stdout
        self.assertIn("scope rc=0", out, proc.stderr)
        before = re.search(r"after scope: +(\d+) +(\d+)", out)
        after = re.search(r"after notice: +(\d+) +(\d+)", out)
        again = re.search(r"after second notice: +(\d+) +(\d+)", out)
        self.assertIsNotNone(before, out)
        self.assertEqual(before.groups(), after.groups(), out)
        self.assertIn("WARN: This apply switches the install from spec.mode today to next", out)
        self.assertGreater(int(again.group(1)), int(after.group(1)), out)

    def test_the_notice_names_the_extra_helm_values_caveat(self):
        for args in ("unset unset next", "unset next today"):
            with self.subTest(args=args):
                proc = self._run(f"{self._LOUD_PRINTS}announce_platform_agent_mode_switch {args}")
                self.assertIn("This compares PLATFORM_AGENT_MODE in install.env with the cluster.", proc.stdout)
                self.assertIn("extra_helm_values wins over the key", proc.stdout)
        proc = self._run(f"{self._LOUD_PRINTS}note_platform_agent_mode_not_applied unset unset next harness")
        self.assertIn("extra_helm_values wins over the key", proc.stdout)

    def test_a_mode_switch_is_announced_from_what_the_cr_runs(self):
        # record, live CR, key -> what the warning says.
        for args, expect in (
            # Installed today through the installer; the key now asks for next.
            ("unset unset next", ("WARN: This apply switches the install from spec.mode today to next",
                                  "renders the NATS bus and the A2A gateway", "Google Chat moves")),
            # Installed next through the installer; the key now asks for today,
            # which renders nothing and so removes the recorded field.
            ("next next today", ("WARN: This apply switches the install from spec.mode next to today",
                                 "retires the A2A stack", "set PLATFORM_AGENT_MODE=next in install.env")),
            # A render that drops a recorded today removes the field from a CR
            # patched to next since.
            ("today next today", ("WARN: This apply switches the install from spec.mode next to today",)),
        ):
            with self.subTest(args=args):
                proc = self._run(f"{self._LOUD_PRINTS}announce_platform_agent_mode_switch {args}")
                for line in expect:
                    self.assertIn(line, proc.stdout)
                self.assertIn("docs/designs/spec-mode-switch.md", proc.stdout)

    def test_an_ungated_switch_does_not_offer_run_again(self):
        # upgrade.sh's full arm, the menu's apply and install.sh -y apply with
        # no confirmation after the notice: "run again" is not a way out there.
        for args in ("next next today", "unset unset next"):
            with self.subTest(args=args):
                gated = self._run(f"{self._LOUD_PRINTS}announce_platform_agent_mode_switch {args}")
                self.assertIn("in install.env and run again.", gated.stdout)
                ungated = self._run(
                    f'{self._LOUD_PRINTS}announce_platform_agent_mode_switch {args} "$PLATFORM_AGENT_MODE_NOTICE_UNGATED"'
                )
                self.assertIn("WARN: This apply switches the install", ungated.stdout)
                self.assertIn("This run applies it without asking first", ungated.stdout)
                self.assertIn("stop it now (Ctrl-C)", ungated.stdout)
                self.assertIn("./upgrade.sh --plan previews", ungated.stdout)
                self.assertNotIn("run again", ungated.stdout)
        # And through the front door's call, which passes its third argument on.
        proc = self._announce(
            self._cr_kubectl(self._cr_list()),
            self._helm(self._HEALTHY, {}, latest="{}"),
            call='announce_platform_agent_mode_for_apply kubeagents-system next "$PLATFORM_AGENT_MODE_NOTICE_UNGATED"',
        )
        self.assertIn("This run applies it without asking first", proc.stdout, proc.stderr)

    def test_a_re_apply_and_an_unread_install_are_silent(self):
        for args in ("unset unset today", "next next next", "unset next next", '"" unset next', 'unset "" next'):
            with self.subTest(args=args):
                proc = self._run(f"{self._LOUD_PRINTS}announce_platform_agent_mode_switch {args}")
                self.assertEqual(proc.stdout, "")

    def test_a_mode_set_by_hand_is_named_rather_than_announced_as_a_switch(self):
        # The release renders the key already, so Helm sends no change and a
        # mode set on the CR by hand stays: a CR patched to next under a
        # record with no mode, and one patched back to today under a record
        # of next (a rollback round trip's state).
        for args, running, key in (("unset next today", "next", "today"), ("next today next", "today", "next"), ("next unset next", "today", "next")):
            with self.subTest(args=args):
                proc = self._run(f"{self._LOUD_PRINTS}announce_platform_agent_mode_switch {args}")
                self.assertNotIn("This apply switches", proc.stdout)
                self.assertIn(
                    f"WARN: install.env sets PLATFORM_AGENT_MODE={key}, but the PlatformAgent carries spec.mode {running}, set outside this installer",
                    proc.stdout,
                )

    def test_a_retag_says_what_a_full_upgrade_would_do(self):
        proc = self._run(f"{self._LOUD_PRINTS}note_platform_agent_mode_not_applied unset unset next harness")
        self.assertIn("WARN: install.env sets PLATFORM_AGENT_MODE=next, but this --upgrade-mode=harness run", proc.stdout)
        self.assertIn("leaves it at spec.mode today", proc.stdout)
        self.assertIn("A full upgrade applies the switch to next", proc.stdout)
        # Quiet where a full upgrade would not move the CR either: the same
        # mode, a hand-set mode the key cannot reach, or nothing read.
        for args in ("next next next harness", "unset unset today operator", "unset next today harness", "next today next harness", '"" unset next operator', 'unset "" next operator'):
            with self.subTest(args=args):
                quiet = self._run(f"{self._LOUD_PRINTS}note_platform_agent_mode_not_applied {args}")
                self.assertEqual(quiet.stdout, "")

    def test_the_retag_note_reads_the_record_from_the_retag_values(self):
        # The values the retag re-applies are the record a later full upgrade
        # diffs against, so they are passed in and helm is not asked again.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = pathlib.Path(tmp.name) / "helm-must-not-run.log"
        no_helm = self._no_helm(log)
        for kubectl, values, key, expect in (
            (self._cr_kubectl(self._cr_list()), "{}", "next", "WARN: install.env sets PLATFORM_AGENT_MODE=next, but this --upgrade-mode=harness run"),
            (self._cr_kubectl(self._cr_list("next")), '{"platformAgent":{"mode":"next"}}', "next", None),
            # The record decides here: a recorded next that the key drops is a
            # switch a full upgrade makes; with no recorded mode it would make none.
            (self._cr_kubectl(self._cr_list("next")), '{"platformAgent":{"mode":"next"}}', "today", "A full upgrade applies the switch to today"),
            (self._cr_kubectl(self._cr_list("next")), "{}", "today", None),
            (self._cr_kubectl(self._cr_list(present=False)), "{}", "next", None),
            (self._cr_kubectl("", exit_code=1, stderr=self._UNREACHABLE), "{}", "next",
             "INFO: Whether a full upgrade would switch spec.mode was not checked: the PlatformAgent in namespace 'kubeagents-system' could not be read (Unable to connect"),
        ):
            with self.subTest(values=values, key=key, kubectl=kubectl):
                log.unlink(missing_ok=True)
                proc = self._announce(
                    kubectl, no_helm,
                    call=f"note_platform_agent_mode_for_retag kubeagents-system {key} harness '{values}'",
                )
                self.assertFalse(log.exists(), log.read_text() if log.exists() else "")
                if expect is None:
                    self.assertEqual(proc.stdout, "", proc.stderr)
                else:
                    self.assertIn(expect, proc.stdout, proc.stderr)

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
        # uninstall.sh treats install.env as optional -- the documented
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

    def _tfvars(self, env, **run_kwargs):
        """Generate a terraform.tfvars and return its text.

        The generator writes `<dest>.tmp` and renames it into place, so the
        destination has to be a real path in a writable directory.

        `run_kwargs` reach `_run`, for a caller that needs a different stub
        than the defaults — `_drift_tfvars` wants an Autopilot cluster.
        """
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(f'write_tfvars_from_state "{dest}"; echo "rc=$?"', env=env, **run_kwargs)
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

    # `kubectl get … --ignore-not-found -o name` prints the object's own
    # name when it is there and nothing at all when the API server says it
    # is not, which is how the probe tells the two apart. Exiting 0 in
    # silence, as this stub used to, is the *absent* answer.
    _HINDSIGHT_KUBECTL = (
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"current-context"*) echo "gke_test-project_us-central1_test-cluster"; exit 0 ;;\n'
        '  *"get statefulset hindsight-postgresql"*"--context gke_test-project_us-central1_test-cluster"*)\n'
        '    echo "statefulset.apps/hindsight-postgresql"; exit 0 ;;\n'
        "esac\n"
        "exit 1\n"
    )

    def test_memory_provider_preserves_live_hindsight_on_existing_cluster_when_unspecified(self):
        """When neither MEMORY nor MEMORY_PROVIDER is set (e.g., a non-interactive
        re-install without install.env or --memory), write_tfvars_from_state probes
        the live cluster and preserves kube_agents_memory if Hindsight is deployed,
        while still respecting an explicit MEMORY=file override."""
        hindsight_kubectl = self._HINDSIGHT_KUBECTL
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$? provider=${{MEMORY_PROVIDER:-}}"',
                env={"API_SERVER_KEY": "k"},
                describe_stub="printf 'True\\n'; exit 0",
                kubectl_script=hindsight_kubectl,
            )
            self.assertIn("rc=0 provider=kube_agents_memory", proc.stdout, proc.stderr)
            self.assertIn('memory_provider          = "kube_agents_memory"', dest.read_text())

            # An explicit MEMORY=file (--memory=file or install.env) still wins.
            proc_explicit = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$? provider=${{MEMORY_PROVIDER:-}}"',
                env={"API_SERVER_KEY": "k", "MEMORY": "file"},
                describe_stub="printf 'True\\n'; exit 0",
                kubectl_script=hindsight_kubectl,
            )
            self.assertIn("rc=0", proc_explicit.stdout, proc_explicit.stderr)
            self.assertIn('memory_provider          = "multiuser_memory"', dest.read_text())

    def test_found_hindsight_is_called_preserved_only_to_a_caller_that_applies(self):
        """The found arm is said per caller, as the could-not-ask arm is.

        install.sh and upgrade.sh opt in and apply next, so "preserved … to
        replace it" is true for them. uninstall.sh does not opt in and runs
        lifecycle.sh destroy straight after generating, so telling it the
        database is kept would be false; it gets the same provider with a
        statement that holds for a teardown too.
        """
        for label, extra_env, expected, forbidden in (
            ("applier", {"KUBE_AGENTS_REQUIRE_MEMORY_ANSWER": "true"}, "so it is preserved", None),
            ("teardown", {}, "to match the live install", "preserved"),
        ):
            with self.subTest(caller=label), tempfile.TemporaryDirectory() as out_dir:
                dest = pathlib.Path(out_dir) / "terraform.tfvars"
                proc = self._run(
                    'print_info() { echo "INFO: $*" >&2; }; '
                    f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                    env={"API_SERVER_KEY": "k", **extra_env},
                    describe_stub="printf 'True\\n'; exit 0",
                    kubectl_script=self._HINDSIGHT_KUBECTL,
                )
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertIn('memory_provider          = "kube_agents_memory"', dest.read_text())
                self.assertIn("This cluster runs the Hindsight memory store", proc.stderr)
                self.assertIn(expected, proc.stderr)
                if forbidden:
                    self.assertNotIn(forbidden, proc.stderr)

    # ── the live Hindsight probe: found / confirmed absent / could not ask ───
    #
    # The third outcome is the point of these. Reading "could not ask" as
    # "not deployed" writes memory_provider = "multiuser_memory" and the apply
    # deletes hindsight-postgresql and the volume holding the database.

    # What gke_context_name() builds from _run's exported coordinates.
    _THIS_CLUSTERS_CONTEXT = "gke_test-project_us-central1_test-cluster"

    def _kubectl_that_cannot_answer(self):
        """kubectl is pointed at this cluster but its reads fail for a reason
        that is not NotFound — the shape of an expired credential, a 403, a
        missing auth plugin, or an API server that times out."""
        return (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            f'  *"current-context"*) echo "{self._THIS_CLUSTERS_CONTEXT}"; exit 0 ;;\n'
            "esac\n"
            'echo "Unable to connect to the server: dial tcp 10.0.0.2:443: i/o timeout" >&2\n'
            "exit 1\n"
        )

    def _kubectl_that_says_not_found(self):
        """kubectl is pointed at this cluster and the API server answers
        NotFound for both objects — a real, trustworthy absence."""
        return (
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            f'  *"current-context"*) echo "{self._THIS_CLUSTERS_CONTEXT}"; exit 0 ;;\n'
            "esac\n"
            'echo "Error from server (NotFound): the server could not find the requested resource" >&2\n'
            "exit 1\n"
        )

    def test_memory_probe_refuses_an_applying_caller_when_the_cluster_cannot_be_asked(self):
        """A kubectl failure that is not NotFound must stop install.sh and
        upgrade.sh rather than default to multiuser_memory."""
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                'print_info() { echo "INFO: $*" >&2; }; '
                f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                env={"API_SERVER_KEY": "k", "KUBE_AGENTS_REQUIRE_MEMORY_ANSWER": "true"},
                describe_stub="printf 'True\\n'; exit 0",
                kubectl_script=self._kubectl_that_cannot_answer(),
            )
            self.assertIn("rc=1", proc.stdout, proc.stderr)
            self.assertIn("Cannot tell whether this cluster runs the Hindsight", proc.stderr)
            # The reason reaches the operator, not just the verdict.
            self.assertIn("i/o timeout", proc.stderr)
            # The remedy has to work for whoever hit it: upgrade.sh has no
            # --memory flag, so recording MEMORY is what gets named first.
            self.assertIn("MEMORY=hindsight|file|off", proc.stderr)
            # And nothing was written: a refusal that leaves tfvars behind is a
            # refusal the next run reads as configuration.
            self.assertFalse(dest.exists(), proc.stderr)

    def test_memory_probe_warns_rather_than_refuses_for_a_caller_that_did_not_opt_in(self):
        """uninstall.sh does not opt in: a destroy removes the store either
        way, and an install has to keep a working way to remove itself."""
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                'print_warning() { echo "WARN: $*" >&2; }; '
                f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                env={"API_SERVER_KEY": "k"},
                describe_stub="printf 'True\\n'; exit 0",
                kubectl_script=self._kubectl_that_cannot_answer(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn("Could not tell whether this cluster runs the Hindsight", proc.stderr)
            self.assertIn('memory_provider          = "multiuser_memory"', dest.read_text())

    def test_memory_probe_takes_a_definite_no_on_both_objects_as_a_real_absence(self):
        """The one answer that does mean "no Hindsight here" still defaults,
        and does it quietly — otherwise every ordinary install warns.

        Two shapes, because --ignore-not-found changed which one is common: a
        current kubectl exits 0 and prints nothing, while the API server's own
        "Error from server (NotFound)" still arrives from older builds and for
        a namespace that does not exist, where --ignore-not-found does not
        apply. Both are the server having answered; neither may warn."""
        shapes = {
            "silent under --ignore-not-found": (
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"current-context"*) echo "{self._THIS_CLUSTERS_CONTEXT}"; exit 0 ;;\n'
                "esac\n"
                "exit 0\n"
            ),
            "Error from server (NotFound)": self._kubectl_that_says_not_found(),
        }
        for shape, kubectl_script in shapes.items():
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as out_dir:
                dest = pathlib.Path(out_dir) / "terraform.tfvars"
                proc = self._run(
                    'print_warning() { echo "WARN: $*" >&2; }; '
                    f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                    env={"API_SERVER_KEY": "k", "KUBE_AGENTS_REQUIRE_MEMORY_ANSWER": "true"},
                    describe_stub="printf 'True\\n'; exit 0",
                    kubectl_script=kubectl_script,
                )
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertNotIn("Hindsight", proc.stderr)
                self.assertIn('memory_provider          = "multiuser_memory"', dest.read_text())

    def test_memory_probe_is_not_fooled_by_a_failure_that_merely_says_not_found(self):
        """The regression this probe exists for. A workstation without the GKE
        auth plugin fails with "executable gke-gcloud-auth-plugin not found" —
        no API server was reached at all, but a substring match for "not found"
        scores the whole cluster as having no Hindsight and the apply deletes
        the database. Only the API server's own "Error from server (NotFound)"
        is an absence."""
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            no_auth_plugin = (
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"current-context"*) echo "{self._THIS_CLUSTERS_CONTEXT}"; exit 0 ;;\n'
                "esac\n"
                'echo "Unable to connect to the server: getting credentials: exec: '
                'executable gke-gcloud-auth-plugin not found" >&2\n'
                "exit 1\n"
            )
            proc = self._run(
                'print_info() { echo "INFO: $*" >&2; }; '
                f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                env={"API_SERVER_KEY": "k", "KUBE_AGENTS_REQUIRE_MEMORY_ANSWER": "true"},
                describe_stub="printf 'True\\n'; exit 0",
                kubectl_script=no_auth_plugin,
            )
            self.assertIn("rc=1", proc.stdout, proc.stderr)
            self.assertIn("Cannot tell whether this cluster runs the Hindsight", proc.stderr)
            self.assertIn("gke-gcloud-auth-plugin", proc.stderr)
            self.assertFalse(dest.exists(), proc.stderr)

    def test_memory_probe_does_not_run_at_all_for_a_cluster_that_does_not_exist(self):
        """A first install has nothing to preserve, and must not be stopped by
        a question about a cluster that is not there yet."""
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                env={"API_SERVER_KEY": "k", "KUBE_AGENTS_REQUIRE_MEMORY_ANSWER": "true"},
                kubectl_script=self._kubectl_that_cannot_answer(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertIn('memory_provider          = "multiuser_memory"', dest.read_text())

    def test_memory_probe_fetches_credentials_for_a_terraform_managed_cluster(self):
        """The `get-credentials` gate in write_tfvars_from_state is `cluster_exists = "true"`,
        not `create_cluster = "false"`.

        On a Terraform-managed cluster (`gcloud_stdout=MANAGED_CLUSTER_STATE`,
        so `create_cluster = "true"` and `cluster_exists = "true"`), an
        adoption-only gate (`create_cluster = "false"`) skips `get-credentials`
        and leaves `live_hindsight_state` without a kubeconfig context for the
        cluster it needs to probe. Here `kubectl config current-context` only
        reports the cluster's context after `gcloud container clusters
        get-credentials` has actually run.
        """
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            cred_marker = pathlib.Path(out_dir) / "credentials.fetched"
            extra_cases = (
                f'  *"get-credentials --help"*) exit 0 ;;\n'
                f'  *"get-credentials"*) : > "{cred_marker}"; exit 0 ;;\n'
            )
            kubectl_requiring_get_credentials = (
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"current-context"*) [ -f "{cred_marker}" ] && echo "{self._THIS_CLUSTERS_CONTEXT}"; exit 0 ;;\n'
                '  *"statefulset hindsight-postgresql"*) echo "statefulset.apps/hindsight-postgresql"; exit 0 ;;\n'
                "esac\n"
                "exit 1\n"
            )
            proc = self._run(
                f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                env={"API_SERVER_KEY": "k", "KUBE_AGENTS_REQUIRE_MEMORY_ANSWER": "true"},
                gcloud_stdout=MANAGED_CLUSTER_STATE,
                describe_stub="printf 'True\\n'; exit 0",
                gcloud_extra_cases=extra_cases,
                kubectl_script=kubectl_requiring_get_credentials,
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertTrue(cred_marker.exists(), "get-credentials was not called for a Terraform-managed cluster")
            tfvars = dest.read_text()
            self.assertIn("create_cluster             = true", tfvars)
            self.assertIn('memory_provider          = "kube_agents_memory"', tfvars)

    def test_the_generators_credentials_fetch_asks_for_the_dns_endpoint(self):
        """Without --dns-endpoint the fetch fails on a DNS-endpoint-only
        cluster, the context gate misses, and every check that gate protects is
        skipped against a cluster `terraform apply` reaches fine."""
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            recorded = pathlib.Path(out_dir) / "get-credentials.args"
            # A gcloud that supports the flag, and a cluster publishing a DNS
            # endpoint that accepts external traffic. `describe` answers on the
            # --format it is given, as the real one does.
            extra_cases = (
                f'  *"get-credentials --help"*) echo "  --dns-endpoint"; exit 0 ;;\n'
                f'  *"get-credentials"*) printf \'%s\\n\' "$*" >> "{recorded}"; exit 0 ;;\n'
            )
            describe_stub = (
                'case "$*" in\n'
                "  *dnsEndpointConfig*) printf 'gke-abc.us-central1.gke.goog\\tTrue\\n'; exit 0 ;;\n"
                "esac\n"
                "printf 'True\\n'; exit 0"
            )
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "MEMORY": "file"},
                describe_stub=describe_stub,
                gcloud_extra_cases=extra_cases,
                kubectl_script=self._kubectl_that_says_not_found(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            self.assertTrue(recorded.exists(), f"get-credentials never ran: {proc.stderr}")
            self.assertIn("--dns-endpoint", recorded.read_text())

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

    # ── the adoption fetch: which control-plane endpoint it writes ───────────

    def _run_adoption_fetch(self, dns_endpoint, allow_external, supports_flag=True):
        """Drive write_tfvars_from_state down the adoption path.

        Returns the recorded `gcloud container clusters get-credentials`
        invocation. create_cluster is false only when the cluster is already
        there, so the existence probe has to succeed; the helper's own describe
        asks for the dnsEndpointConfig fields and is answered from the same arm.
        """
        with tempfile.TemporaryDirectory() as tmp:
            record = pathlib.Path(tmp) / "fetch.args"
            describe_stub = (
                'if [[ "$*" == *dnsEndpointConfig* ]]; then '
                f"printf '{dns_endpoint}\\t{allow_external}\\n'; exit 0; fi\n"
                'case "$*" in\n'
                "  *currentMasterVersion*) printf '1.30.1-gke.100\\n' ;;\n"
                "  *) printf 'True\\n' ;;\n"
                "esac\n"
                "exit 0"
            )
            # An older gcloud has no --dns-endpoint at all, and the helper is
            # meant to notice that from the help text before offering the flag.
            help_text = "--dns-endpoint" if supports_flag else "--internal-ip"
            extra = (
                f"  *\"get-credentials --help\"*) printf -- '{help_text}\\n'; exit 0 ;;\n"
                f"  *get-credentials*) printf '%s\\n' \"$*\" >> '{record}'; exit 0 ;;\n"
            )
            with tempfile.TemporaryDirectory() as out_dir:
                dest = pathlib.Path(out_dir) / "terraform.tfvars"
                proc = self._run(
                    f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                    env={"API_SERVER_KEY": "k"},
                    describe_stub=describe_stub,
                    gcloud_extra_cases=extra,
                )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            return record.read_text() if record.exists() else ""

    def test_the_helper_is_in_scope_for_a_caller_that_sources_only_this_file(self):
        # uninstall.sh sources installer_common.sh and nothing else, so the
        # predicate has to come with the file. Left to the caller, the fetch
        # below would be an undefined function and `set -e` would end the run.
        proc = self._run('echo "kind=$(type -t gke_dns_endpoint_flag)"')
        self.assertIn("kind=function", proc.stdout, proc.stderr)

    def test_the_adoption_fetch_uses_the_dns_endpoint_when_one_accepts_traffic(self):
        # The whole point of the call: on a cluster whose IP endpoint this host
        # cannot route to, the IP kubeconfig makes every secret read below time
        # out, and the generator cannot tell that from "nothing to recover" --
        # so it mints a new SESSION_KV_SALT over the live one.
        args = self._run_adoption_fetch("gke-abc.us-central1.gke.goog", "True")
        self.assertIn("--dns-endpoint", args)

    def test_the_adoption_fetch_leaves_an_ordinary_cluster_on_its_ip_endpoint(self):
        # gcloud rejects the flag on a cluster with no externally reachable DNS
        # endpoint, so passing it blind would break the clusters that work.
        for dns_endpoint, allow_external, supports_flag, why in (
            ("gke-abc.us-central1.gke.goog", "False", True, "external traffic is off"),
            ("", "True", True, "no DNS endpoint is published"),
            ("gke-abc.us-central1.gke.goog", "True", False, "this gcloud has no such flag"),
        ):
            with self.subTest(why=why):
                args = self._run_adoption_fetch(dns_endpoint, allow_external, supports_flag)
                self.assertNotIn("--dns-endpoint", args)
                # Still fetched, just over the IP endpoint as before.
                self.assertIn("get-credentials test-cluster", args)

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
    """install.env's MEMORY must beat an inherited MEMORY_PROVIDER.

    The input and the generator spell the setting differently, so the load
    order that gives install.env the last word on every other key cannot do it
    for this one. MEMORY_PROVIDER still reaches a run from the environment --
    a CI job, a dev shell that sourced scripts/installer/vars.sh, or an
    install.env that carries both -- and write_tfvars_from_state prefers it, so
    without the normalizer the inherited provider wins and an upgrade
    regenerates the tfvars against the wrong store, the apply then deleting the
    Hindsight API and its Postgres. #1060 item 5, on the front doors install.sh
    does not cover.
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

    def test_the_install_env_mode_overrides_an_inherited_provider(self):
        """The environment says the file store, the operator's install.env says
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
        """An install whose configuration never carried MEMORY must keep the
        provider it was given."""
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

    def test_the_front_doors_that_generate_tfvars_call_it(self):
        """upgrade.sh, uninstall.sh and install.sh's Day-2 menu each load
        install.env into an environment they did not clear, and each generates
        tfvars without passing through install.sh's parameter block. A caller
        that skips the normalizer has the defect back."""
        for name in ("upgrade.sh", "uninstall.sh", "install.sh"):
            with self.subTest(name=name):
                self.assertIn(
                    "normalize_memory_vars",
                    (_REPO_ROOT / name).read_text(),
                    f"{name} generates tfvars outside install.sh's parameter "
                    "block; it must normalize MEMORY against MEMORY_PROVIDER",
                )


class HelmReleaseSelfHealingTest(unittest.TestCase):
    # Appended to an ensure_clean_helm_release call: reports the flag
    # upgrade.sh's restore_moved_checkout reads to say "after repairing the
    # pending Helm release" instead of "Nothing was applied", and keeps the
    # function's own exit code. test_upgrade_script.py sets the flag by hand to
    # pin that reader; these pin the writer, at each arm that repairs.
    _REPORT_REPAIRED = '; rc=$?; echo "REPAIRED=[${HELM_RELEASE_REPAIRED:-}]"; exit "$rc"'

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
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system' + self._REPORT_REPAIRED,
            helm_script,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Rolling back", proc.stderr)
        self.assertIn("REPAIRED=[]", proc.stdout, proc.stderr)

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
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system' + self._REPORT_REPAIRED,
            helm_script,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Automatic uninstall is blocked", proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)
        # A refusal repaired nothing, and must not claim to have.
        self.assertIn("REPAIRED=[]", proc.stdout, proc.stderr)

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
            'ensure_clean_helm_release kube-agents kubeagents-system' + self._REPORT_REPAIRED,
            helm_script,
            env_overrides={"ALLOW_UNINSTALL_PENDING_RELEASE": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Successfully cleaned up stuck pending-install release", proc.stderr)
        self.assertIn("REPAIRED=[true]", proc.stdout, proc.stderr)

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
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system' + self._REPORT_REPAIRED,
            helm_script,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Rolling back 'kube-agents' to revision 2", proc.stderr)
        self.assertIn("REPAIRED=[true]", proc.stdout, proc.stderr)

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
        proc = self._run_helm_test(
            'ensure_clean_helm_release kube-agents kubeagents-system' + self._REPORT_REPAIRED,
            helm_script,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("Automatic uninstall is blocked", proc.stderr)
        self.assertNotIn("UNINSTALL EXECUTED", proc.stderr)
        # A refusal repaired nothing, and must not claim to have.
        self.assertIn("REPAIRED=[]", proc.stdout, proc.stderr)

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
            'ensure_clean_helm_release kube-agents kubeagents-system' + self._REPORT_REPAIRED,
            helm_script,
            env_overrides={"ALLOW_UNINSTALL_PENDING_RELEASE": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REPAIRED=[true]", proc.stdout, proc.stderr)

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


class MissingEndpointHelperTest(unittest.TestCase):
    """The fallback for a tree with no gke_dns_endpoint.sh beside this file.

    Nothing else reaches it. Every other test here sources the checkout's own
    installer_common.sh, where the helper is always its neighbour, so they all
    take the branch that sources it for real. The arm below is the one an
    incomplete checkout takes, and the two ways it can break are both silent
    until then: a slip in its syntax stops the source at load time, and a stub
    that does not define the function leaves every caller with an undefined
    command.
    """

    def _source_without_helper(self, probe):
        """Source a copy of installer_common.sh with no helper beside it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            installer_dir = root / "scripts" / "installer"
            installer_dir.mkdir(parents=True)
            copied = installer_dir / "installer_common.sh"
            shutil.copy(_INSTALLER_COMMON, copied)
            # Two levels up, where the file looks for them. The defaults are
            # copied because their absence is a hard failure by design -- this
            # test is about the helper's absence alone.
            shutil.copy(_REPO_ROOT / "install.defaults.env", root / "install.defaults.env")
            # gke_dns_endpoint.sh is deliberately NOT created beside the copy.
            bin_dir = root / "bin"
            bin_dir.mkdir()
            body = f'set -u\n{_PRINT_STUBS}\nsource "{copied}"\n{probe}'
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(root),
            )

    def test_a_tree_without_the_helper_still_defines_the_predicate(self):
        # uninstall.sh calls this unconditionally. Were the stub missing or the
        # arm broken, the call would be an undefined command and `set -e` would
        # end a teardown over which endpoint to dial.
        proc = self._source_without_helper('echo "kind=$(type -t gke_dns_endpoint_flag)"')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("kind=function", proc.stdout, proc.stderr)

    def test_the_stub_leaves_the_flag_empty_so_the_fetch_is_unchanged(self):
        # The empty flag is the command that ran before the helper existed, and
        # it still reaches every cluster with a routable IP endpoint. A stub
        # that left a stale value in place would splice it into get-credentials.
        proc = self._source_without_helper(
            'GKE_DNS_ENDPOINT_FLAG=--stale\n'
            'gke_dns_endpoint_flag some-cluster us-central1 some-project\n'
            'echo "flag=[${GKE_DNS_ENDPOINT_FLAG}]"'
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("flag=[]", proc.stdout, proc.stderr)

    def test_it_says_so_rather_than_falling_back_in_silence(self):
        # uninstall.sh and upgrade.sh are the front doors that reach this; the
        # next thing their operator sees is an unrelated-looking missing key.
        proc = self._source_without_helper("true")
        self.assertIn("gke_dns_endpoint.sh", proc.stderr)
        self.assertIn("IP endpoint", proc.stderr)


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
        (_INSTALLER_COMMON, 'history_json="$(trap - ERR; helm history ', 3),
        (_INSTALLER_COMMON, 'last_good_rev="$(trap - ERR; printf ', 1),
        (_INSTALLER_COMMON, 'out="$({ trap - ERR; kubectl get ', 1),
        (_GKE_DNS_ENDPOINT, 'described=$(trap - ERR; gcloud container clusters describe ', 1),
        (_INSTALLER_COMMON, 'PLATFORM_AGENT_CR_JSON="$(trap - ERR; kubectl --context ', 1),
        (_INSTALLER_COMMON, 'PLATFORM_AGENT_RECORD_JSON="$(trap - ERR; helm get values ', 1),
        (_INSTALLER_COMMON, 'served_rev="$(trap - ERR; printf ', 1),
        (_INSTALLER_COMMON, 'PLATFORM_AGENT_SERVED_JSON="$(trap - ERR; helm get values ', 1),
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
    """The five SCOPE_* keys become the composition's `scope` object.

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
        "  projects         = []\n"
        "  folders          = []\n"
        "  organizations    = []\n"
        "  shared_vpc_hosts = []\n"
        "  metrics_scopes   = []\n"
        "  exclude = {\n"
        "    projects = []\n"
        "    clusters = []\n"
        "  }\n"
        "}\n"
    )

    def _scope_env(self, **keys):
        env = {"API_SERVER_KEY": "k", "SCOPE_PROJECTS": "", "SCOPE_FOLDERS": "", "SCOPE_ORGANIZATIONS": "",
               "SCOPE_SHARED_VPC_HOSTS": "", "SCOPE_METRICS_SCOPES": "",
               "SCOPE_EXCLUDE_PROJECTS": "", "SCOPE_EXCLUDE_CLUSTERS": ""}
        env.update(keys)
        return env

    def test_an_install_with_no_scope_key_renders_an_empty_present_block(self):
        content = self._tfvars(self._scope_env())
        self.assertIn(self.EMPTY_BLOCK, content)

    def test_the_cap_is_written_only_when_set(self):
        # Unset, the CRD's and the module's default apply and the block names
        # no cap; set, it is written where the module's object carries it.
        self.assertNotIn("max_projects", self._tfvars(self._scope_env()))
        content = self._tfvars(self._scope_env(SCOPE_MAX_PROJECTS="250"))
        self.assertIn("  metrics_scopes   = []\n  max_projects     = 250\n  exclude = {\n", content)

    def test_a_malformed_cap_in_install_env_stops_the_writer_before_a_file_exists(self):
        # upgrade.sh and the Day-2 menu reach the generator without install.sh's
        # parameter block, so the writer checks the value itself.
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(f'write_tfvars_from_state "{dest}"; echo "rc=$?"', env=self._scope_env(SCOPE_MAX_PROJECTS="lots"))
            self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
            self.assertIn("SCOPE_MAX_PROJECTS='lots' is not a whole number from 1 to 5000", proc.stdout + proc.stderr)
            self.assertFalse(dest.exists())

    def test_a_cap_outside_the_crds_bounds_stops_the_run_naming_the_key(self):
        for bad in ("0", "abc", "5001", "2.5", "-3", " 12", "18446744073709551716"):
            with self.subTest(bad=bad):
                proc = subprocess.run(
                    ["bash", "-c",
                     'print_error() { echo "ERROR: $*"; }; print_info() { :; }; print_warning() { :; }; print_success() { :; }\n'
                     f'source "{_INSTALLER_COMMON}"\nrequire_scope_max_projects {shlex.quote(bad)}; echo "rc=$?"'],
                    capture_output=True, text=True, env=get_isolated_test_env(), cwd=str(_REPO_ROOT),
                )
                self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn(f"SCOPE_MAX_PROJECTS='{bad}' is not a whole number from 1 to 5000", proc.stdout)
        for good in ("", "1", "250", "5000", "0250"):
            with self.subTest(good=good):
                proc = subprocess.run(
                    ["bash", "-c",
                     'print_error() { echo "ERROR: $*"; }; print_info() { :; }; print_warning() { :; }; print_success() { :; }\n'
                     f'source "{_INSTALLER_COMMON}"\nrequire_scope_max_projects {shlex.quote(good)}; echo "rc=$?"'],
                    capture_output=True, text=True, env=get_isolated_test_env(), cwd=str(_REPO_ROOT),
                )
                self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)

    def test_the_keys_are_carried_verbatim_into_the_block(self):
        content = self._tfvars(self._scope_env(
            SCOPE_PROJECTS="payments-prod, payments-staging",
            SCOPE_FOLDERS="123456789012 210987654321",
            SCOPE_ORGANIZATIONS="987654321098",
            SCOPE_SHARED_VPC_HOSTS="shared-net-host, shared-net-host-2",
            SCOPE_METRICS_SCOPES="observability-hub",
            SCOPE_EXCLUDE_PROJECTS="*-sandbox kube-agents-demo-0[2-9]",
            SCOPE_EXCLUDE_CLUSTERS="payments-staging/us-central1/scratch-cluster,p2/us-east1-b/c2",
        ))
        self.assertIn(
            "scope = {\n"
            '  projects         = ["payments-prod", "payments-staging"]\n'
            '  folders          = ["123456789012", "210987654321"]\n'
            '  organizations    = ["987654321098"]\n'
            '  shared_vpc_hosts = ["shared-net-host", "shared-net-host-2"]\n'
            '  metrics_scopes   = ["observability-hub"]\n'
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

    def test_a_container_id_that_is_not_a_bare_number_is_refused_before_the_file_is_written(self):
        # A folders/<id> spelling would otherwise reach terraform's variable
        # validation with a message naming neither the key nor the entry.
        for key, bad in (("SCOPE_FOLDERS", "folders/123456789012"), ("SCOPE_ORGANIZATIONS", "organizations/1"),
                         ("SCOPE_FOLDERS", "my-folder"), ("SCOPE_ORGANIZATIONS", "123456789012345678901")):
            with self.subTest(key=key, entry=bad), tempfile.TemporaryDirectory() as out_dir:
                dest = pathlib.Path(out_dir) / "terraform.tfvars"
                proc = self._run(
                    f'rc=0; write_tfvars_from_state "{dest}" || rc=$?; echo "rc=$rc"',
                    env=self._scope_env(**{key: f"123456789012, {bad}"}),
                )
                self.assertIn("rc=1", proc.stdout, proc.stderr)
                self.assertIn(f"{key} entry '{bad}' is not a numeric Resource Manager ID", proc.stderr + proc.stdout)
                self.assertFalse(dest.exists())


_LIVE_SCOPE_CR = (
    '{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"projects":["p3-project","p2-project"],'
    '"exclude":{"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}]}'
)
_LIVE_SCOPE_LINES = (
    'SCOPE_PROJECTS="p2-project p3-project"',
    'SCOPE_FOLDERS=""',
    'SCOPE_ORGANIZATIONS=""',
    'SCOPE_SHARED_VPC_HOSTS=""',
    'SCOPE_METRICS_SCOPES=""',
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
                   "SCOPE_PROJECTS": "", "SCOPE_FOLDERS": "", "SCOPE_ORGANIZATIONS": "",
                   "SCOPE_SHARED_VPC_HOSTS": "", "SCOPE_METRICS_SCOPES": "",
                   "SCOPE_EXCLUDE_PROJECTS": "", "SCOPE_EXCLUDE_CLUSTERS": "",
                   # Read as ${VAR:-} like the scope keys: a developer's exported
                   # switch must not arm the declaration a case compares against.
                   "SCOPED_SA_POOL_ENABLED": ""}
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

    def test_a_cap_alone_on_a_scope_less_cr_is_a_hand_edit(self):
        # A CR whose only hand edit is the cap is not "nothing live to protect": the apply
        # would render the default over it. It is refused with the key among the lines
        # until the key records it; the default alone is still an empty declaration.
        capped_only = '{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"maxProjects":250}}}]}'
        proc = self._run(capped_only, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_PROJECTS=""', proc.stdout)
        self.assertIn('INFO:   SCOPE_MAX_PROJECTS="250"', proc.stdout)
        self._assert_rc(self._run(capped_only, "norelease", keys={"SCOPE_MAX_PROJECTS": "250"}), 0)
        self._assert_rc(self._run(capped_only.replace("250", "100"), "norelease"), 0)

    def test_a_cap_set_on_the_cr_by_hand_is_a_hand_edit_until_the_key_records_it(self):
        # The cap is part of the declaration: a CR whose maxProjects differs from
        # what the record and the keys say is refused, with the key among the
        # lines; one at the CRD's default reads as unset, since the API server
        # defaults the field on every CR that carries a scope block.
        capped = _LIVE_SCOPE_CR.replace('"projects":["p3-project","p2-project"],', '"projects":["p3-project","p2-project"],"maxProjects":250,')
        record = ('{"platformAgent":{"scope":{"projects":["p2-project","p3-project"],"exclude":{"projects":[],'
                  '"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}')
        proc = self._run(capped, record)
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_MAX_PROJECTS="250"', proc.stdout)
        self._assert_rc(self._run(capped, record, keys={"SCOPE_PROJECTS": "p2-project p3-project",
                                                       "SCOPE_EXCLUDE_CLUSTERS": "p2-project/us-central1/c1",
                                                       "SCOPE_MAX_PROJECTS": "250"}), 0)
        self._assert_rc(self._run(capped, record.replace('"projects":["p2-project","p3-project"],', '"projects":["p2-project","p3-project"],"maxProjects":250,')), 0)
        defaulted = _LIVE_SCOPE_CR.replace('"projects":["p3-project","p2-project"],', '"projects":["p3-project","p2-project"],"maxProjects":100,')
        self._assert_rc(self._run(defaulted, record), 0)
        proc = self._run(defaulted, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_MAX_PROJECTS=""', proc.stdout)
        # The key is among the lines even when the live cap is the default, because it
        # may be the one key the operator has to blank: a record and keys at 250 beside
        # a CR the API server re-defaulted refuse on the cap alone, and the lines the
        # refusal prints must not reproduce the install.env that was refused.
        recorded_at_250 = record.replace('"projects":["p2-project","p3-project"],', '"projects":["p2-project","p3-project"],"maxProjects":250,')
        proc = self._run(defaulted, recorded_at_250, keys={"SCOPE_PROJECTS": "p2-project p3-project",
                                                           "SCOPE_EXCLUDE_CLUSTERS": "p2-project/us-central1/c1",
                                                           "SCOPE_MAX_PROJECTS": "250"})
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_MAX_PROJECTS=""', proc.stdout)

    def test_an_armed_pool_alone_on_a_scope_less_cr_is_a_hand_edit(self):
        # spec.security.scopedServiceAccountPool.enabled is part of the declaration the
        # apply replaces: the generator writes scoped_pool_enabled from
        # SCOPED_SA_POOL_ENABLED alone, false when the key is absent, so a live true
        # the key does not record is disarmed by the next full apply -- its members
        # destroyed and the broker put on the ambient credential. It is refused with
        # the key among the lines, and an armed pool alone is not "nothing live to
        # protect" even with no scope block beside it. The members list is derived
        # from the scope, so only the switch is compared.
        armed_only = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"security":{"scopedServiceAccountPool":'
                      '{"enabled":true,"serviceAccounts":[{"projectId":"p2-project","serviceAccountEmail":"ka-x@p.iam.gserviceaccount.com"}]}}}}]}')
        proc = self._run(armed_only, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_PROJECTS=""', proc.stdout)
        self.assertIn('INFO:   SCOPED_SA_POOL_ENABLED="true"', proc.stdout)
        self._assert_rc(self._run(armed_only, "norelease", keys={"SCOPED_SA_POOL_ENABLED": "true"}), 0)
        self._assert_rc(self._run(armed_only, "norelease", keys={"SCOPED_SA_POOL_ENABLED": "yes"}), 0)
        for label, disarmed in (
            ("enabled false", armed_only.replace('"enabled":true', '"enabled":false')),
            ("an empty pool block", '{"items":[{"metadata":{"name":"platform-agent"},"spec":{"security":{"scopedServiceAccountPool":{}}}}]}'),
            ("no security block", '{"items":[{"metadata":{"name":"platform-agent"},"spec":{}}]}'),
        ):
            with self.subTest(case=label):
                proc = self._run(disarmed, "norelease")
                self._assert_rc(proc, 0)
                self.assertNotIn("ERROR", proc.stdout)
                self.assertNotIn("WARN", proc.stdout)

    def test_an_armed_pool_beside_a_scope_is_a_hand_edit_until_the_key_records_it(self):
        # The switch is weighed with the lists and the cap: a CR armed by hand beside a
        # scope the record accounts for is refused on the switch alone, passes once the
        # key records it, and passes when the record carries it (the installer armed it).
        armed = _LIVE_SCOPE_CR.replace('"spec":{"scope"', '"spec":{"security":{"scopedServiceAccountPool":{"enabled":true}},"scope"')
        record = ('{"platformAgent":{"scope":{"projects":["p2-project","p3-project"],"exclude":{"projects":[],'
                  '"clusters":[{"projectId":"p2-project","location":"us-central1","clusterName":"c1"}]}}}}')
        scope_keys = {"SCOPE_PROJECTS": "p2-project p3-project", "SCOPE_EXCLUDE_CLUSTERS": "p2-project/us-central1/c1"}
        proc = self._run(armed, record)
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPED_SA_POOL_ENABLED="true"', proc.stdout)
        self._assert_rc(self._run(armed, record, keys={**scope_keys, "SCOPED_SA_POOL_ENABLED": "true"}), 0)
        armed_record = record.replace('{"platformAgent":{', '{"platformAgent":{"security":{"scopedServiceAccountPool":{"enabled":true}},')
        self._assert_rc(self._run(armed, armed_record), 0)
        # A record and keys that arm it beside a CR disarmed by hand refuse on the switch
        # alone, and the line is blank: the lines never reproduce the false the apply
        # would render, nor the true that was refused.
        proc = self._run(_LIVE_SCOPE_CR, armed_record, keys={**scope_keys, "SCOPED_SA_POOL_ENABLED": "true"})
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPED_SA_POOL_ENABLED=""', proc.stdout)
        # And the switch is among the lines of every refusal, blank when the live
        # pool is off, beside the cap.
        proc = self._run(_LIVE_SCOPE_CR, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPED_SA_POOL_ENABLED=""', proc.stdout)

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

    def test_a_hand_declared_container_is_protected_like_a_project(self):
        # The chart renders folders and organizations now, so an apply over a
        # CR that carries one the record and the keys do not is the same
        # silent replace as for a project: refused, with the two lines that
        # reproduce it, and passed once the keys carry it.
        only_containers = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"folders":["123456789012"],'
                           '"organizations":["987654321098"]}}}]}')
        proc = self._run(only_containers, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_FOLDERS="123456789012"', proc.stdout)
        self.assertIn('INFO:   SCOPE_ORGANIZATIONS="987654321098"', proc.stdout)
        proc = self._run(only_containers, "norelease",
                         keys={"SCOPE_FOLDERS": "123456789012", "SCOPE_ORGANIZATIONS": "987654321098"})
        self._assert_rc(proc, 0)
        # A record that carries the folder makes the keys the new declaration,
        # dropping it included.
        record = ('{"platformAgent":{"scope":{"projects":[],"folders":["123456789012"],"organizations":["987654321098"],'
                  '"exclude":{"projects":[],"clusters":[]}}}}')
        self._assert_rc(self._run(only_containers, record), 0)
        # And a record from before the chart rendered the lists (no folders
        # key) does not account for a folder the CR carries.
        older = '{"platformAgent":{"scope":{"projects":[],"exclude":{"projects":[],"clusters":[]}}}}'
        self._assert_rc(self._run(only_containers, older), 1)

    def test_a_hand_declared_selector_is_protected_like_a_project(self):
        # The chart renders sharedVpcHosts and metricsScopes now, so an apply
        # over a CR that carries one the record and the keys do not is the
        # same silent replace as for a project: refused, with the two lines
        # that reproduce it, passed once the keys carry it, and never a note
        # about a key the installer lacks.
        only_selectors = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"sharedVpcHosts":["shared-net-host"],'
                          '"metricsScopes":["observability-hub"]}}}]}')
        proc = self._run(only_selectors, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_SHARED_VPC_HOSTS="shared-net-host"', proc.stdout)
        self.assertIn('INFO:   SCOPE_METRICS_SCOPES="observability-hub"', proc.stdout)
        self.assertNotIn("has no key for", proc.stdout)
        proc = self._run(only_selectors, "norelease",
                         keys={"SCOPE_SHARED_VPC_HOSTS": "shared-net-host", "SCOPE_METRICS_SCOPES": "observability-hub"})
        self._assert_rc(proc, 0)
        self.assertNotIn("has no key for", proc.stdout)
        # A record that carries the selectors makes the keys the new
        # declaration, dropping them included; a record from before the chart
        # rendered them (the container keys only) does not account for them.
        record = ('{"platformAgent":{"scope":{"projects":[],"folders":[],"organizations":[],"sharedVpcHosts":["shared-net-host"],'
                  '"metricsScopes":["observability-hub"],"exclude":{"projects":[],"clusters":[]}}}}')
        self._assert_rc(self._run(only_selectors, record), 0)
        between = ('{"platformAgent":{"scope":{"projects":[],"folders":[],"organizations":[],'
                   '"exclude":{"projects":[],"clusters":[]}}}}')
        self._assert_rc(self._run(only_selectors, between), 1)
        mixed = ('{"items":[{"metadata":{"name":"platform-agent"},"spec":{"scope":{"projects":["p2-project"],'
                 '"sharedVpcHosts":["shared-net-host"]}}}]}')
        proc = self._run(mixed, "norelease")
        self._assert_rc(proc, 1)
        self.assertIn('INFO:   SCOPE_PROJECTS="p2-project"', proc.stdout)
        self.assertIn('INFO:   SCOPE_SHARED_VPC_HOSTS="shared-net-host"', proc.stdout)

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


# Every variable the preflight's token minting, or the gcloud that does it,
# reads from the environment; blanked in the test environment and set per case.
_GOOGLE_CREDENTIAL_VARIABLES = (
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_OAUTH_ACCESS_TOKEN", "GOOGLE_CREDENTIALS",
    "GOOGLE_CLOUD_KEYFILE_JSON", "GCLOUD_KEYFILE_JSON", "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "STUB_PROPERTY_IMPERSONATE", "STUB_PROPERTY_TOKEN_FILE",
)


class ScopeSelectorApisTest(unittest.TestCase):
    """enable_scope_selector_apis: silent with no selector declared; with one,
    lists the management project's enabled APIs and enables whichever of the
    APIs the plan-time resolution of that selector reads is off (Resource
    Manager and Monitoring for a Metrics Scope, Compute for a Shared VPC host,
    the composition's own split), since the reads run in the plan and the
    composition enables the APIs only in the apply that follows. The scoped
    service account pool armed beside a folder or organisation counts as a
    third kind: the plan lists the container's members through the Asset API,
    so that API is enabled first too, with the pool's listing as the reason.
    Nothing is called when they are on; a listing that fails enables every
    API the declared selectors and the pool read; an enable that fails is a
    warning, not an abort."""

    ALL = "cloudresourcemanager.googleapis.com monitoring.googleapis.com compute.googleapis.com"

    def _run(self, keys, enabled=ALL, list_fails=False, enable_fails=False):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            log = pathlib.Path(tmp) / "gcloud.log"
            log.write_text("")
            listing = "exit 1" if list_fails else "printf '%s\\n' " + " ".join(enabled.split()) + "; exit 0"
            (bin_dir / "gcloud").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"services list --enabled"*) {listing} ;;\n'
                f'  *"services enable"*) echo "$*" >>"$GCLOUD_LOG"; exit {1 if enable_fails else 0} ;;\n'
                "esac\nexit 1\n"
            )
            (bin_dir / "gcloud").chmod(0o755)
            # Every key the helper reads is blanked here, the pool's switch and
            # the container keys beside the selectors', so a developer's shell
            # (`set -a; source install.env`) cannot arm the pool in a case that
            # did not; each case sets its own.
            env = {"PROJECT_ID": "test-project", "SCOPE_SHARED_VPC_HOSTS": "", "SCOPE_METRICS_SCOPES": "",
                   "SCOPE_FOLDERS": "", "SCOPE_ORGANIZATIONS": "", "SCOPED_SA_POOL_ENABLED": "",
                   "GCLOUD_LOG": str(log)}
            env.update(keys)
            body = (
                "set -u\n"
                'print_info() { echo "INFO: $*"; }; print_error() { echo "ERROR: $*"; }\n'
                'print_warning() { echo "WARN: $*"; }; print_success() { :; }\n'
                f'source "{_INSTALLER_COMMON}"\n'
                'trap \'echo TRAP-FIRED\' ERR; set -eEo pipefail; enable_scope_selector_apis; echo "rc=$?"\n'
            )
            proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                                  env=get_isolated_test_env(overrides=env, bin_dir=str(bin_dir)), cwd=str(_REPO_ROOT))
            return proc, log.read_text()

    def test_no_selector_calls_nothing(self):
        proc, calls = self._run({"SCOPE_PROJECTS": "p2-project", "SCOPE_FOLDERS": "123456789012"}, list_fails=True)
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertEqual(calls, "")
        self.assertNotIn("INFO", proc.stdout)

    def test_every_api_already_on_calls_nothing(self):
        # A re-run or a Day-2 apply of an existing install: the previous apply
        # enabled all three, so no enable and no line.
        proc, calls = self._run({"SCOPE_METRICS_SCOPES": "observability-hub"})
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertEqual(calls, "")
        self.assertNotIn("INFO", proc.stdout)

    def test_only_the_apis_that_are_off_and_that_the_declared_selector_reads_are_enabled(self):
        # Monitoring on, Resource Manager and Compute off: a Metrics Scope wants Resource
        # Manager alone (Compute is not among its reads, as the composition's API list
        # says), a Shared VPC host Compute alone, and both declared want both.
        cases = (({"SCOPE_METRICS_SCOPES": "observability-hub"}, "cloudresourcemanager.googleapis.com"),
                 ({"SCOPE_SHARED_VPC_HOSTS": "shared-net-host, other-host"}, "compute.googleapis.com"),
                 ({"SCOPE_METRICS_SCOPES": "observability-hub", "SCOPE_SHARED_VPC_HOSTS": "shared-net-host"},
                  "cloudresourcemanager.googleapis.com compute.googleapis.com"))
        for keys, expected in cases:
            with self.subTest(keys=keys):
                proc, calls = self._run(keys, enabled="monitoring.googleapis.com container.googleapis.com")
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertEqual(calls, f"services enable {expected} --project=test-project\n")
                self.assertIn(f"INFO: Enabling {expected.replace(' ', ', ')} in project 'test-project'", proc.stdout)

    def test_a_listing_that_fails_enables_every_api_the_declared_selectors_read(self):
        cases = (({"SCOPE_METRICS_SCOPES": "observability-hub"}, "cloudresourcemanager.googleapis.com monitoring.googleapis.com"),
                 ({"SCOPE_SHARED_VPC_HOSTS": "shared-net-host"}, "compute.googleapis.com"),
                 ({"SCOPE_METRICS_SCOPES": "observability-hub", "SCOPE_SHARED_VPC_HOSTS": "shared-net-host"}, self.ALL))
        for keys, expected in cases:
            with self.subTest(keys=keys):
                proc, calls = self._run(keys, list_fails=True)
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertEqual(calls, f"services enable {expected} --project=test-project\n")
                self.assertIn("could not be listed", proc.stdout)

    def test_an_enable_that_fails_warns_and_goes_on(self):
        # Under the front doors' set -eE and ERR trap: the plan reports a
        # disabled API with the same command as its remedy, and gcloud's active
        # account need not be the one Terraform applies with.
        proc, calls = self._run({"SCOPE_METRICS_SCOPES": "observability-hub"}, enabled="monitoring.googleapis.com", enable_fails=True)
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("TRAP-FIRED", proc.stdout)
        self.assertIn("WARN: Could not enable cloudresourcemanager.googleapis.com in project 'test-project'", proc.stdout)
        self.assertIn("gcloud services enable cloudresourcemanager.googleapis.com --project=test-project", proc.stdout)

    # The scoped service account pool lists a declared folder's or
    # organisation's members at plan time through the Asset API, which the
    # composition enables only in the apply that follows, so the pool armed
    # beside a container is the third reason the plan needs an API on first.
    POOL_REASON = "lists the declared folder's or organisation's members for the scoped service account pool"

    def test_the_pool_armed_beside_a_container_enables_the_asset_api_and_names_the_pool(self):
        for keys in ({"SCOPED_SA_POOL_ENABLED": "true", "SCOPE_FOLDERS": "123456789012"},
                     {"SCOPED_SA_POOL_ENABLED": "yes", "SCOPE_ORGANIZATIONS": "987654321098"},
                     {"SCOPED_SA_POOL_ENABLED": "True", "SCOPE_FOLDERS": " 123456789012, 123456789013 "}):
            with self.subTest(keys=keys):
                proc, calls = self._run(keys)
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertEqual(calls, "services enable cloudasset.googleapis.com --project=test-project\n")
                self.assertIn("INFO: Enabling cloudasset.googleapis.com in project 'test-project'", proc.stdout)
                self.assertIn(self.POOL_REASON, proc.stdout)
                self.assertNotIn("Shared VPC host", proc.stdout)

    def test_the_pool_armed_beside_a_container_and_a_selector_enables_both_and_names_both(self):
        proc, calls = self._run({"SCOPED_SA_POOL_ENABLED": "true", "SCOPE_FOLDERS": "123456789012",
                                 "SCOPE_SHARED_VPC_HOSTS": "shared-net-host"}, enabled="monitoring.googleapis.com")
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertEqual(calls, "services enable compute.googleapis.com cloudasset.googleapis.com --project=test-project\n")
        self.assertIn("INFO: Enabling compute.googleapis.com, cloudasset.googleapis.com in project 'test-project'", proc.stdout)
        self.assertIn("Shared VPC host or Metrics Scope", proc.stdout)
        self.assertIn(self.POOL_REASON, proc.stdout)

    def test_the_pool_armed_beside_a_container_with_the_asset_api_on_calls_nothing(self):
        proc, calls = self._run({"SCOPED_SA_POOL_ENABLED": "true", "SCOPE_FOLDERS": "123456789012"},
                                enabled="cloudasset.googleapis.com")
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertEqual(calls, "")
        self.assertNotIn("INFO", proc.stdout)

    def test_a_listing_that_fails_with_the_pool_armed_beside_a_container_enables_the_asset_api(self):
        proc, calls = self._run({"SCOPED_SA_POOL_ENABLED": "true", "SCOPE_FOLDERS": "123456789012"}, list_fails=True)
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertEqual(calls, "services enable cloudasset.googleapis.com --project=test-project\n")
        self.assertIn("could not be listed", proc.stdout)
        self.assertIn(self.POOL_REASON, proc.stdout)

    def test_the_pool_armed_without_a_container_calls_nothing(self):
        # Explicit projects and selector members need no Asset read: the
        # resolver lists a selector through its own API, and a project is
        # named already.
        for keys in ({"SCOPED_SA_POOL_ENABLED": "true", "SCOPE_PROJECTS": "p2-project"},
                     {"SCOPED_SA_POOL_ENABLED": "true"},
                     {"SCOPED_SA_POOL_ENABLED": "true", "SCOPE_FOLDERS": " , "}):
            with self.subTest(keys=keys):
                proc, calls = self._run(keys, list_fails=True)
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertEqual(calls, "")
                self.assertNotIn("INFO", proc.stdout)

    def test_a_container_with_the_pool_off_calls_nothing(self):
        # The composition enables the Asset API for the reconcile's container
        # search in the apply; the plan reads nothing under a container unless
        # the pool is armed, so there is nothing to enable first.
        for keys in ({"SCOPED_SA_POOL_ENABLED": "false", "SCOPE_FOLDERS": "123456789012"},
                     {"SCOPED_SA_POOL_ENABLED": "", "SCOPE_ORGANIZATIONS": "987654321098"},
                     {"SCOPE_FOLDERS": "123456789012", "SCOPE_ORGANIZATIONS": "987654321098"}):
            with self.subTest(keys=keys):
                proc, calls = self._run(keys, list_fails=True)
                self.assertIn("rc=0", proc.stdout, proc.stderr)
                self.assertEqual(calls, "")
                self.assertNotIn("INFO", proc.stdout)


class ScopeContainerPreflightTest(unittest.TestCase):
    """check_scope_container_access: silent with no container; with one, the
    Asset API must be enabled in the host project or no effective policy may
    deny it, and the applying identity must hold setIamPolicy on every
    container, asked through testIamPermissions, and, while the scoped
    service account pool is armed, cloudasset.assets.searchAllResources there
    too, since the plan lists the container's members for the pool. Every
    failure is named before the refusal; a probe that cannot decide warns and
    lets the apply speak; "warn" turns the refusal into a warning."""

    def _run(self, keys=None, mode="", api_enabled=True, policy=None, policy_error=False,
             probe=None, token=True, curl_present=True, strict=False, env_extra=None, policy_garbage=False,
             search_probe=None):
        """probe: a dict from resource ("folders/1") to what curl answers:
        "granted", "denied", "forbidden", "service-disabled", "missing",
        "garbage", "down". search_probe: the same, answered only to a request
        for the pool's cloudasset.assets.searchAllResources on that resource
        ("granted" or "denied"); a resource absent from it answers from
        probe, whose granted body holds setIamPolicy alone. The stubs record
        what they saw in a log the test folds into proc.stderr: the bearer
        curl read from its stdin (-H @-), every permission it was asked
        (PROBED:<permission>), the impersonation flag gcloud saw, the first
        bytes of a key file it was pointed at, and any CLOUDSDK_AUTH_*
        override that reached it."""
        probe = probe or {}
        search_probe = search_probe or {}
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            policy_json = "<not json>" if policy_garbage else json.dumps(policy or {"spec": {"rules": []}})
            services = "cloudasset.googleapis.com" if api_enabled else ""
            (bin_dir / "gcloud").write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"services list"*) printf "%s\\n" "{services}"; exit 0 ;;\n'
                + ('  *"org-policies describe"*) echo "ERROR: PERMISSION_DENIED" >&2; exit 1 ;;\n' if policy_error else
                   f"  *\"org-policies describe\"*) printf '%s\\n' '{policy_json}'; exit 0 ;;\n")
                # Terraform's credentials, not gcloud's active account: the stub
                # answers the ADC form and refuses the plain one.
                + ('  *"application-default print-access-token"*) [ -z "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:-}${CLOUDSDK_AUTH_ACCESS_TOKEN:-}${CLOUDSDK_AUTH_ACCESS_TOKEN_FILE:-}${CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE:-}" ] || echo "CLOUDSDK-LEAKED" >>"$SCOPE_PROBE_LOG"; [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] || echo "KEYFILE-BYTES:$(head -c 12 "$GOOGLE_APPLICATION_CREDENTIALS" | tr -d "\\n")" >>"$SCOPE_PROBE_LOG"; echo "tok${GOOGLE_APPLICATION_CREDENTIALS:+-from-keyfile}"; case "$*" in *--impersonate-service-account=*) echo "IMPERSONATED:${*##*--impersonate-service-account=}" >>"$SCOPE_PROBE_LOG" ;; esac; exit 0 ;;\n' if token else
                   '  *"application-default print-access-token"*) exit 1 ;;\n')
                # The raw-token form: only right with a source token in
                # CLOUDSDK_AUTH_ACCESS_TOKEN and the impersonation flag.
                + '  *"auth print-access-token"*"--impersonate-service-account="*) echo "imp-from-${CLOUDSDK_AUTH_ACCESS_TOKEN:-none}"; echo "IMPERSONATED:${*##*--impersonate-service-account=}" >>"$SCOPE_PROBE_LOG"; exit 0 ;;\n'
                + '  *"auth print-access-token"*) echo "wrong-identity"; exit 0 ;;\n'
                + '  *"config get-value account"*) echo "tester@example.com"; exit 0 ;;\n'
                # gcloud configuration properties: `config get-value` reports the
                # effective value, so a CLOUDSDK_AUTH_* variable wins over the
                # configuration file, as in the real binary; the file's value is
                # set per case through STUB_PROPERTY_*.
                + '  *"config get-value auth/impersonate_service_account"*) echo "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:-${STUB_PROPERTY_IMPERSONATE:-}}"; exit 0 ;;\n'
                + '  *"config get-value auth/access_token_file"*) echo "${CLOUDSDK_AUTH_ACCESS_TOKEN_FILE:-${STUB_PROPERTY_TOKEN_FILE:-}}"; exit 0 ;;\n'
                "esac\nexit 1\n"
            )
            if curl_present:
                cases = []
                # The request body (-d) precedes the URL on curl's argv, so a
                # search case matches the permission then the resource, and
                # sits before the resource-only cases.
                for resource, answer in search_probe.items():
                    body = {"granted": '{"permissions":["cloudasset.assets.searchAllResources"]}', "denied": "{}"}[answer]
                    cases.append(f"  *\"cloudasset.assets.searchAllResources\"*\"/{resource}:testIamPermissions\"*) printf '%s\\n%s' '{body}' 200; exit 0 ;;")
                for resource, answer in probe.items():
                    body, status = {
                        "granted": ('{"permissions":["%s"]}' % ("resourcemanager.folders.setIamPolicy" if resource.startswith("folders") else "resourcemanager.organizations.setIamPolicy"), 200),
                        "denied": ("{}", 200),
                        "forbidden": ('{"error":{"code":403,"status":"PERMISSION_DENIED"}}', 403),
                        "service-disabled": ('{"error":{"code":403,"status":"PERMISSION_DENIED","details":[{"reason":"SERVICE_DISABLED"}]}}', 403),
                        "scope-insufficient": ('{"error":{"code":403,"message":"Request had insufficient authentication scopes.","status":"PERMISSION_DENIED","details":[{"reason":"ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}', 403),
                        "missing": ('{"error":{"code":404}}', 404),
                        "garbage": ("<html>", 200),
                        "down": ("", None),
                    }[answer]
                    if status is None:
                        cases.append(f'  *"/{resource}:testIamPermissions"*) exit 7 ;;')
                    else:
                        cases.append(f"  *\"/{resource}:testIamPermissions\"*) printf '%s\\n%s' '{body}' '{status}'; exit 0 ;;")
                # Records the bearer read from the header file, and refuses a
                # token on argv, before answering.
                (bin_dir / "curl").write_text(
                    "#!/usr/bin/env bash\n"
                    'case "$*" in *"Bearer "*) echo "TOKEN-ON-ARGV" >>"$SCOPE_PROBE_LOG"; exit 99 ;; esac\n'
                    'for a in "$@"; do case "$a" in @-) echo "BEARER:$(sed -n \'s/^Authorization: Bearer //p\')" >>"$SCOPE_PROBE_LOG" ;; @*) echo "BEARER-FROM-FILE" >>"$SCOPE_PROBE_LOG" ;; "{\\"permissions\\""*) echo "PROBED:$(printf \'%s\' "$a" | sed \'s/.*\\["\\(.*\\)"\\].*/\\1/\')" >>"$SCOPE_PROBE_LOG" ;; esac; done\n'
                    "case \"$*\" in\n" + "\n".join(cases) + "\nesac\nexit 22\n")
            # env is an external binary whose argv any local user can read: the
            # stub records what it was handed, then hands over to the real one.
            (bin_dir / "env").write_text(
                "#!/usr/bin/env bash\n"
                'echo "ENV-ARGV:$*" >>"$SCOPE_PROBE_LOG"\n'
                'exec /usr/bin/env "$@"\n'
            )
            for stub in ("gcloud", "env") + (("curl",) if curl_present else ()):
                path = bin_dir / stub
                path.chmod(path.stat().st_mode | stat.S_IEXEC)
            # The library discards the stubs' stderr, so what they saw is
            # recorded in a file the test reads back into proc.stderr.
            probe_log = pathlib.Path(tmp) / "probe.log"
            probe_log.write_text("")
            # Hermetic: the library and the gcloud stub read the provider's and
            # gcloud's credential variables straight from the environment, so a
            # developer's shell must not reach them; each case sets its own.
            env = {"PROJECT_ID": "test-project", "SCOPE_FOLDERS": "", "SCOPE_ORGANIZATIONS": "",
                   "SCOPED_SA_POOL_ENABLED": "", "SCOPE_PROBE_LOG": str(probe_log)}
            env.update({name: "" for name in _GOOGLE_CREDENTIAL_VARIABLES})
            env.update(keys or {})
            env.update(env_extra or {})
            # strict: the front doors' shell options and ERR trap, under which
            # upgrade.sh --plan calls the check bare (no `|| exit 1`), so a probe
            # answering "denied" must not read as an error.
            call = (f'trap \'echo TRAP-FIRED\' ERR; set -eEo pipefail; check_scope_container_access {mode}; echo "rc=$?"\n'
                    if strict else f'check_scope_container_access {mode}; echo "rc=$?"\n')
            body = (
                "set -u\n"
                'print_error() { echo "ERROR: $*"; }; print_info() { echo "INFO: $*"; }\n'
                'print_warning() { echo "WARN: $*"; }; print_success() { :; }\n'
                f'source "{_INSTALLER_COMMON}"\n'
                + call
            )
            # For a curl-absent run PATH is the stubs plus a minimal toolbox
            # (bash, coreutils, python3), so the real curl is not found behind it.
            isolated = get_isolated_test_env(overrides=env, bin_dir=str(bin_dir))
            if not curl_present:
                tools = create_minimal_tools_bin(pathlib.Path(tmp) / "tools")
                (tools / "python3").symlink_to(shutil.which("python3"))
                isolated["PATH"] = f"{bin_dir}:{tools}"
            proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                                  env=isolated, cwd=str(_REPO_ROOT))
            proc.stderr += probe_log.read_text()
            return proc

    def _assert_rc(self, proc, rc):
        self.assertIn(f"rc={rc}", proc.stdout, proc.stdout + proc.stderr)

    def test_no_container_is_silent_and_touches_nothing(self):
        proc = self._run(keys={"SCOPE_PROJECTS": "p2-project"}, api_enabled=False, policy_error=True, token=False)
        self._assert_rc(proc, 0)
        self.assertEqual("rc=0\n", proc.stdout)

    def test_a_bindable_folder_with_the_api_enabled_passes_silently(self):
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, probe={"folders/123456789012": "granted"})
        self._assert_rc(proc, 0)
        self.assertNotIn("WARN", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)

    def test_every_unbindable_container_is_named_before_the_refusal(self):
        proc = self._run(keys={"SCOPE_FOLDERS": "111111111111, 222222222222", "SCOPE_ORGANIZATIONS": "333333333333"},
                         probe={"folders/111111111111": "denied", "folders/222222222222": "granted",
                                "organizations/333333333333": "missing"})
        self._assert_rc(proc, 1)
        self.assertIn("ERROR: Refusing to apply: the Application Default Credentials (the identity Terraform applies with) cannot set IAM policy on folders/111111111111 (resourcemanager.folders.setIamPolicy)", proc.stdout)
        self.assertIn("for that identity, or drop it from SCOPE_FOLDERS", proc.stdout)
        self.assertIn("cannot set IAM policy on organizations/333333333333 (resourcemanager.organizations.setIamPolicy)", proc.stdout)
        self.assertNotIn("folders/222222222222 (", proc.stdout)
        self.assertIn("INFO: Nothing was changed.", proc.stdout)
        # An organisation is always warned about, bindable or not.
        self.assertIn("WARN: SCOPE_ORGANIZATIONS binds the agent's read roles on the whole organisation", proc.stdout)

    def test_the_pool_armed_beside_a_folder_the_identity_cannot_search_names_the_viewer_role(self):
        # The plan lists the folder's members for the pool, so setIamPolicy
        # alone is not enough: the search permission is probed too, and a
        # folder lacking it is reported with the role that grants it.
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012", "SCOPED_SA_POOL_ENABLED": "true"},
                         probe={"folders/123456789012": "granted"},
                         search_probe={"folders/123456789012": "denied"})
        self._assert_rc(proc, 1)
        self.assertIn("ERROR: Refusing to apply: the Application Default Credentials (the identity Terraform applies with) cannot list the members of folders/123456789012 (cloudasset.assets.searchAllResources)", proc.stdout)
        self.assertIn("roles/cloudasset.viewer on the folder for that identity", proc.stdout)
        self.assertIn("scoped service account pool", proc.stdout)
        self.assertNotIn("cannot set IAM policy on folders/123456789012", proc.stdout)
        self.assertIn("PROBED:resourcemanager.folders.setIamPolicy\n", proc.stderr)
        self.assertIn("PROBED:cloudasset.assets.searchAllResources\n", proc.stderr)
        # An organisation reads the same way, and the warn mode warns instead.
        proc = self._run(keys={"SCOPE_ORGANIZATIONS": "987654321098", "SCOPED_SA_POOL_ENABLED": "yes"}, mode="warn",
                         probe={"organizations/987654321098": "granted"},
                         search_probe={"organizations/987654321098": "denied"})
        self._assert_rc(proc, 0)
        self.assertIn("WARN: An applying run would be refused: the Application Default Credentials (the identity Terraform applies with) cannot list the members of organizations/987654321098 (cloudasset.assets.searchAllResources)", proc.stdout)
        self.assertIn("roles/cloudasset.viewer on the organisation for that identity", proc.stdout)

    def test_the_pool_armed_beside_a_folder_the_identity_can_bind_and_search_passes_silently(self):
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012", "SCOPED_SA_POOL_ENABLED": "true"},
                         probe={"folders/123456789012": "granted"},
                         search_probe={"folders/123456789012": "granted"})
        self._assert_rc(proc, 0)
        self.assertNotIn("WARN", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)
        self.assertIn("PROBED:cloudasset.assets.searchAllResources\n", proc.stderr)

    def test_the_pool_off_never_probes_the_search_permission(self):
        # With the pool off the plan reads no container, so the only
        # permission asked is setIamPolicy, whatever the search probe would say.
        for armed in ("false", ""):
            with self.subTest(armed=armed):
                proc = self._run(keys={"SCOPE_FOLDERS": "123456789012", "SCOPED_SA_POOL_ENABLED": armed},
                                 probe={"folders/123456789012": "granted"},
                                 search_probe={"folders/123456789012": "denied"})
                self._assert_rc(proc, 0)
                self.assertNotIn("ERROR", proc.stdout)
                probed = [line for line in proc.stderr.splitlines() if line.startswith("PROBED:")]
                self.assertEqual(["PROBED:resourcemanager.folders.setIamPolicy"], probed, proc.stderr)

    def test_the_probe_uses_the_credentials_the_provider_would(self):
        # GOOGLE_OAUTH_ACCESS_TOKEN as is; GOOGLE_IMPERSONATE_SERVICE_ACCOUNT
        # through gcloud's flag; a key file or inline key JSON in
        # GOOGLE_CREDENTIALS through GOOGLE_APPLICATION_CREDENTIALS; and the
        # token reaches curl on its stdin, never argv and never a file.
        base = {"SCOPE_FOLDERS": "123456789012"}
        probe = {"folders/123456789012": "granted"}
        proc = self._run(keys=base, probe=probe)
        self.assertIn("BEARER:tok\n", proc.stderr)
        self.assertNotIn("TOKEN-ON-ARGV", proc.stderr)
        proc = self._run(keys=base, probe=probe, env_extra={"GOOGLE_OAUTH_ACCESS_TOKEN": "from-env"})
        self.assertIn("BEARER:from-env\n", proc.stderr)
        proc = self._run(keys=base, probe=probe, env_extra={"GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "tf@p.iam.gserviceaccount.com"})
        self.assertRegex(proc.stderr, r"IMPERSONATED:.*tf@p\.iam\.gserviceaccount\.com")
        self.assertIn("BEARER:tok\n", proc.stderr)
        # gcloud's own overrides in the operator's shell reach neither the mint
        # nor the property read that guards it: the provider does not read
        # them, and `config get-value` would otherwise report them as a
        # property the operator never set.
        for var in ("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "CLOUDSDK_AUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
                    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"):
            with self.subTest(var=var):
                proc = self._run(keys=base, probe=probe, env_extra={var: "other@p.iam.gserviceaccount.com"})
                self._assert_rc(proc, 0)
                self.assertIn("BEARER:tok\n", proc.stderr)
                self.assertNotIn("CLOUDSDK-LEAKED", proc.stderr)
                self.assertNotIn("active configuration sets", proc.stdout)
        # A raw token plus impersonation: the token is the source credential
        # and the probe is made as the impersonated account, as the provider
        # does, never as the raw token's identity.
        proc = self._run(keys=base, probe=probe, env_extra={"GOOGLE_OAUTH_ACCESS_TOKEN": "from-env",
                                                            "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "tf@p.iam.gserviceaccount.com"})
        self.assertIn("BEARER:imp-from-from-env\n", proc.stderr)
        self.assertNotIn("BEARER:from-env\n", proc.stderr)
        # The source token reaches gcloud through the environment, never on
        # env's argv, which any local user can read.
        self.assertIn("ENV-ARGV:", proc.stderr)
        for line in proc.stderr.splitlines():
            if line.startswith("ENV-ARGV:"):
                self.assertNotIn("from-env", line)
                self.assertNotIn("CLOUDSDK_AUTH_ACCESS_TOKEN=", line)
        # A tilde path is a key file to the provider (its pathOrContents
        # expands the home directory), so it is one here too.
        with tempfile.TemporaryDirectory() as home:
            (pathlib.Path(home) / "sa.json").write_text("{}")
            proc = self._run(keys=base, probe=probe, env_extra={"HOME": home, "GOOGLE_CREDENTIALS": "~/sa.json"})
            self._assert_rc(proc, 0)
            self.assertIn("BEARER:tok-from-keyfile\n", proc.stderr)
            self.assertIn("KEYFILE-BYTES:{}", proc.stderr)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as key:
            key.write("{}")
        self.addCleanup(pathlib.Path(key.name).unlink)
        for var in ("GOOGLE_CREDENTIALS", "GOOGLE_CLOUD_KEYFILE_JSON", "GCLOUD_KEYFILE_JSON"):
            for creds in (key.name, '{"type":"service_account"}', '\n  {"type":"service_account"}', "eyJ0eXBlIjoic2VydmljZV9hY2NvdW50In0="):
                with self.subTest(var=var, creds=creds[:12]):
                    proc = self._run(keys=base, probe=probe, env_extra={var: creds})
                    self.assertIn("BEARER:tok-from-keyfile\n", proc.stderr)
                    self._assert_rc(proc, 0)
                    # An inline value reaches gcloud byte for byte, whatever it
                    # starts with: the provider's rule is "an existing path,
                    # else JSON", never the first byte.
                    if creds != key.name:
                        self.assertIn("KEYFILE-BYTES:" + creds[:12].replace("\n", ""), proc.stderr)
        # The bearer reaches curl on stdin, never through a file or argv.
        self.assertNotIn("BEARER-FROM-FILE", proc.stderr)
        self.assertNotIn("TOKEN-ON-ARGV", proc.stderr)

    def test_a_refusal_names_the_identity_it_probed(self):
        # A denied probe under a key file sends the operator to that key's
        # account, not to their ADC principal; the remedy for a token that
        # could not be minted names the source that failed.
        base = {"SCOPE_FOLDERS": "123456789012"}
        denied = {"folders/123456789012": "denied"}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as key:
            key.write("{}")
        self.addCleanup(pathlib.Path(key.name).unlink)
        proc = self._run(keys=base, probe=denied, env_extra={"GOOGLE_CREDENTIALS": '{"type":"service_account"}'})
        self._assert_rc(proc, 1)
        self.assertIn("the credentials in GOOGLE_CREDENTIALS (the identity Terraform applies with) cannot set IAM policy on folders/123456789012", proc.stdout)
        proc = self._run(keys=base, probe=denied, env_extra={"GOOGLE_OAUTH_ACCESS_TOKEN": "t",
                                                              "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "tf@p.iam.gserviceaccount.com"})
        self.assertIn("the identity behind GOOGLE_OAUTH_ACCESS_TOKEN impersonating tf@p.iam.gserviceaccount.com (the identity Terraform applies with) cannot set IAM policy", proc.stdout)
        # A path that does not exist: gcloud is handed it as key JSON (the
        # provider's rule), mints nothing, and the remedy names the variable
        # and says the file is missing, without printing the value.
        proc = self._run(keys=base, probe=denied, token=False, env_extra={"GOOGLE_CREDENTIALS": "/nonexistent/key.json"})
        self._assert_rc(proc, 0)
        self.assertIn("WARN: The scope container preflight could not decide whether the credentials in GOOGLE_CREDENTIALS (the identity Terraform applies with) can set IAM policy on the declared containers (GOOGLE_CREDENTIALS is neither a file that exists nor key JSON gcloud accepts; check the path, or that the value is the key's JSON itself (not base64))", proc.stdout)
        self.assertNotIn("/nonexistent/key.json", proc.stdout)
        self.assertNotIn("application-default login", proc.stdout)
        # A key that gcloud refuses: the remedy names the key, not ADC, and
        # never prints it, whatever byte the key starts with.
        for creds in ('{"type":"service_account"}', '\n{"type":"service_account","private_key":"SECRET-BYTES"}', "eyJ0eXBlIjoic2VydmljZV9hY2NvdW50In0="):
            with self.subTest(creds=creds[:10]):
                proc = self._run(keys=base, probe=denied, token=False, env_extra={"GOOGLE_CLOUD_KEYFILE_JSON": creds})
                self.assertIn("(GOOGLE_CLOUD_KEYFILE_JSON is neither a file that exists nor key JSON gcloud accepts; check the path, or that the value is the key's JSON itself (not base64))", proc.stdout)
                self.assertNotIn("SECRET-BYTES", proc.stdout + proc.stderr)
                self.assertNotIn("eyJ0eXBl", proc.stdout)
        # ADC, no token: the ADC remedy; with GOOGLE_APPLICATION_CREDENTIALS
        # set, the remedy and the label name that variable instead, because
        # the login the plain remedy suggests writes a file the variable
        # overrides.
        proc = self._run(keys=base, probe=denied, token=False)
        self.assertIn("run: gcloud auth application-default login", proc.stdout)
        proc = self._run(keys=base, probe=denied, token=False, env_extra={"GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/adc.json"})
        self.assertIn("whether the Application Default Credentials in GOOGLE_APPLICATION_CREDENTIALS (the identity Terraform applies with) can set IAM policy on the declared containers (GOOGLE_APPLICATION_CREDENTIALS names a file that does not exist; the apply would fail the same way)", proc.stdout)
        self.assertNotIn("application-default login", proc.stdout)
        proc = self._run(keys=base, probe=denied, token=False, env_extra={"GOOGLE_APPLICATION_CREDENTIALS": key.name})
        self.assertIn("(the key file GOOGLE_APPLICATION_CREDENTIALS names could not mint a token; check it is a valid service-account key, or unset the variable to use the login credentials)", proc.stdout)
        proc = self._run(keys=base, probe=denied, env_extra={"GOOGLE_APPLICATION_CREDENTIALS": key.name})
        self._assert_rc(proc, 1)
        self.assertIn("the Application Default Credentials in GOOGLE_APPLICATION_CREDENTIALS (the identity Terraform applies with) cannot set IAM policy", proc.stdout)

    def test_a_gcloud_impersonation_property_makes_the_probe_undecided(self):
        # gcloud config set auth/impersonate_service_account lives in the
        # active configuration, out of env -u's reach, and a mint under it
        # answers for an identity Terraform never uses: undecided, naming the
        # property, unless GOOGLE_IMPERSONATE_SERVICE_ACCOUNT overrides it
        # explicitly or no gcloud mint is made at all.
        base = {"SCOPE_FOLDERS": "123456789012"}
        probe = {"folders/123456789012": "granted"}
        proc = self._run(keys=base, probe=probe, env_extra={"STUB_PROPERTY_IMPERSONATE": "other@p.iam.gserviceaccount.com"})
        self._assert_rc(proc, 0)
        self.assertIn("gcloud's active configuration sets auth/impersonate_service_account, which its token mint honours and Terraform does not", proc.stdout)
        self.assertNotIn("BEARER:", proc.stderr)
        proc = self._run(keys=base, probe=probe, env_extra={"STUB_PROPERTY_TOKEN_FILE": "/tmp/t"})
        self.assertIn("sets auth/access_token_file,", proc.stdout)
        # Both set: both named.
        proc = self._run(keys=base, probe=probe, env_extra={"STUB_PROPERTY_IMPERSONATE": "x@p.iam.gserviceaccount.com", "STUB_PROPERTY_TOKEN_FILE": "/tmp/t"})
        self.assertIn("sets auth/impersonate_service_account auth/access_token_file,", proc.stdout)
        # An explicit GOOGLE_IMPERSONATE_SERVICE_ACCOUNT overrides the impersonation property: the probe runs.
        proc = self._run(keys=base, probe=probe, env_extra={"STUB_PROPERTY_IMPERSONATE": "other@p.iam.gserviceaccount.com",
                                                            "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "tf@p.iam.gserviceaccount.com"})
        self._assert_rc(proc, 0)
        self.assertIn("BEARER:tok\n", proc.stderr)
        self.assertNotIn("active configuration sets", proc.stdout)
        # A raw token with no impersonation makes no gcloud mint: the property is irrelevant.
        proc = self._run(keys=base, probe=probe, env_extra={"STUB_PROPERTY_IMPERSONATE": "other@p.iam.gserviceaccount.com",
                                                            "GOOGLE_OAUTH_ACCESS_TOKEN": "from-env"})
        self.assertIn("BEARER:from-env\n", proc.stderr)
        self.assertNotIn("active configuration sets", proc.stdout)

    def test_a_403_for_a_disabled_api_is_undecided_not_denied(self):
        # Resource Manager answers 403 with reason SERVICE_DISABLED when its
        # API is off in the credential's quota project; the apply enables it
        # before binding, so this is not a permission answer, and the warning
        # says why.
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, probe={"folders/123456789012": "service-disabled"})
        self._assert_rc(proc, 0)
        self.assertIn("could not decide whether the Application Default Credentials (the identity Terraform applies with) can set IAM policy on folders/123456789012 (Resource Manager answered 403 for its API being off in the credentials' quota project, which the apply enables before it binds)", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)

    def test_a_403_for_an_insufficiently_scoped_token_names_the_scope_not_a_grant(self):
        # A raw token minted without cloud-platform gets 403
        # ACCESS_TOKEN_SCOPE_INSUFFICIENT; the role may be held, so the
        # remedy is the token's scope, never "ask for folderIamAdmin".
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, probe={"folders/123456789012": "scope-insufficient"},
                         env_extra={"GOOGLE_OAUTH_ACCESS_TOKEN": "narrow"})
        self._assert_rc(proc, 0)
        self.assertIn("could not decide whether the identity behind GOOGLE_OAUTH_ACCESS_TOKEN (the identity Terraform applies with) can set IAM policy on folders/123456789012 (the token lacks the cloud-platform scope; mint it with that scope, as gcloud does)", proc.stdout)
        self.assertNotIn("folderIamAdmin", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)
        # The other undecided answers carry their reason too.
        proc = self._run(keys={"SCOPE_FOLDERS": "111111111111 222222222222"},
                         probe={"folders/111111111111": "down", "folders/222222222222": "garbage"})
        self.assertIn("folders/111111111111 (the request to Resource Manager did not complete)", proc.stdout)
        self.assertIn("folders/222222222222 (Resource Manager answered 200 with a body that is not the JSON it documents)", proc.stdout)

    def test_a_403_is_a_refusal_and_a_transport_failure_is_a_warning(self):
        proc = self._run(keys={"SCOPE_FOLDERS": "111111111111 222222222222 333333333333"},
                         probe={"folders/111111111111": "forbidden", "folders/222222222222": "down",
                                "folders/333333333333": "garbage"})
        self._assert_rc(proc, 1)
        self.assertIn("cannot set IAM policy on folders/111111111111", proc.stdout)
        self.assertIn("WARN: The scope container preflight could not decide whether the Application Default Credentials (the identity Terraform applies with) can set IAM policy on folders/222222222222", proc.stdout)
        self.assertIn("can set IAM policy on folders/333333333333", proc.stdout)

    def test_a_policy_that_denies_the_api_is_named_when_the_api_is_off(self):
        for label, policy in (
            ("deniedValues", {"spec": {"rules": [{"values": {"deniedValues": ["cloudasset.googleapis.com"]}}]}}),
            ("allowedValues without it", {"spec": {"rules": [{"values": {"allowedValues": ["container.googleapis.com"]}}]}}),
            ("denyAll", {"spec": {"rules": [{"denyAll": True}]}}),
        ):
            with self.subTest(policy=label):
                proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, api_enabled=False, policy=policy,
                                 probe={"folders/123456789012": "granted"})
                self._assert_rc(proc, 1)
                self.assertIn("ERROR: Refusing to apply: cloudasset.googleapis.com cannot be enabled in project 'test-project': the effective organisation policy constraints/gcp.restrictServiceUsage denies it", proc.stdout)
        # The API already on: the policy is never consulted.
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, api_enabled=True,
                         policy={"spec": {"rules": [{"denyAll": True}]}}, probe={"folders/123456789012": "granted"})
        self._assert_rc(proc, 0)
        # The API off and no policy denying it: fine, the apply enables it.
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, api_enabled=False,
                         policy={"spec": {"rules": [{"values": {"allowedValues": ["cloudasset.googleapis.com"]}}]}},
                         probe={"folders/123456789012": "granted"})
        self._assert_rc(proc, 0)
        # A dry-run policy enforces nothing: an organisation trialling the
        # constraint must not be refused for it.
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, api_enabled=False,
                         policy={"dryRunSpec": {"rules": [{"denyAll": True}]}},
                         probe={"folders/123456789012": "granted"})
        self._assert_rc(proc, 0)
        self.assertNotIn("denies it", proc.stdout)

    def test_an_unreadable_policy_warns_and_lets_the_apply_speak(self):
        # Unreadable two ways: gcloud fails, or gcloud exits 0 with a body the
        # reader cannot parse. Neither is "not denied"; both warn, like the
        # IAM probe's undocumented body does.
        for kwargs in ({"policy_error": True}, {"policy_garbage": True}):
            with self.subTest(**kwargs):
                proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, api_enabled=False,
                                 probe={"folders/123456789012": "granted"}, **kwargs)
                self._assert_rc(proc, 0)
                self.assertIn("WARN: The scope container preflight could not decide whether cloudasset.googleapis.com can be enabled in project 'test-project'", proc.stdout)
                self.assertNotIn("ERROR", proc.stdout)

    def test_no_token_or_no_curl_warns_about_the_containers_and_passes(self):
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, token=False)
        self._assert_rc(proc, 0)
        self.assertIn("gcloud could not mint an access token for them; run: gcloud auth application-default login", proc.stdout)
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, curl_present=False)
        self._assert_rc(proc, 0)
        self.assertIn("curl is not installed", proc.stdout)

    def test_a_denied_probe_is_an_answer_under_the_front_doors_strict_shell(self):
        # upgrade.sh --plan calls the check bare under set -eE with an ERR
        # trap; a probe that answers denied, or a policy read that says the
        # API is forbidden, must reach the warning rather than abort the run.
        proc = self._run(keys={"SCOPE_FOLDERS": "111111111111 222222222222"}, mode="warn", strict=True,
                         api_enabled=False, policy={"spec": {"rules": [{"denyAll": True}]}},
                         probe={"folders/111111111111": "denied", "folders/222222222222": "down"})
        self._assert_rc(proc, 0)
        self.assertNotIn("TRAP-FIRED", proc.stdout + proc.stderr)
        self.assertIn("WARN: An applying run would be refused: cloudasset.googleapis.com cannot be enabled", proc.stdout)
        self.assertIn("WARN: An applying run would be refused: the Application Default Credentials (the identity Terraform applies with) cannot set IAM policy on folders/111111111111", proc.stdout)
        self.assertIn("can set IAM policy on folders/222222222222", proc.stdout)
        # And a clean run under the same options is silent.
        proc = self._run(keys={"SCOPE_FOLDERS": "111111111111"}, strict=True, probe={"folders/111111111111": "granted"})
        self._assert_rc(proc, 0)
        self.assertEqual("rc=0\n", proc.stdout)

    def test_warn_mode_names_the_failures_and_passes(self):
        proc = self._run(keys={"SCOPE_FOLDERS": "123456789012"}, mode="warn", probe={"folders/123456789012": "denied"})
        self._assert_rc(proc, 0)
        self.assertIn("WARN: An applying run would be refused: the Application Default Credentials (the identity Terraform applies with) cannot set IAM policy on folders/123456789012", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)


if __name__ == "__main__":
    unittest.main()
