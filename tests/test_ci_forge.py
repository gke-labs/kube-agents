"""hack/ci-forge.sh: the lane step that stands a self-managed forge up for one
run and takes it down (kube-agents#3684).

The script runs whole, against stubs for gcloud, kubectl and openssl on PATH
and a stub make-certs.sh in a staged SCRIPT_DIR that also carries the real
ci-env.sh and ci-deploy.sh (the forge table is lifted from the latter). Every
gcloud call is recorded, so the tests pin the shape of the create and the
delete rather than their effect.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.testing.common import get_isolated_test_env

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HACK = REPO_ROOT / "hack"
PROJECT = "kube-agents-evals-2"
FORGE_IP = "10.128.0.251"

# gcloud: records argv; answers the reads `up` makes from a per-test state
# directory (what exists is a file in GCLOUD_STATE), and the serial console from
# a file the test writes.
STUB_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "${STUB_CALLS}"
    state="${GCLOUD_STATE}"
    case "$*" in
      *"compute addresses describe"*)
        [ -e "$state/address" ] && { cat "$state/address"; exit 0; } || { echo "not found" >&2; exit 1; } ;;
      *"dns record-sets describe"*)
        case "$*" in *"describe ghes.eval.internal. "*) ;; *) echo "not found" >&2; exit 1 ;; esac
        [ -e "$state/record" ] && { cat "$state/record"; exit 0; } || { echo "not found" >&2; exit 1; } ;;
      *"compute firewall-rules describe allow-forge-443"*)
        [ -e "$state/firewall" ] && { echo allow-forge-443; exit 0; } || exit 1 ;;
      *"compute firewall-rules describe deny-forge-ingress"*)
        [ -e "$state/deny" ] && { echo deny-forge-ingress; exit 0; } || exit 1 ;;
      *"iam service-accounts describe"*)
        [ -e "$state/sa" ] && { echo forge-vm@x; exit 0; } || exit 1 ;;
      *"compute instances list"*)
        [ -e "$state/list-fails" ] && { echo "listing failed" >&2; exit 1; }
        [ -e "$state/leftovers" ] && cat "$state/leftovers"; exit 0 ;;
      *"compute instances get-serial-port-output"*)
        [ -e "$state/serial-fails" ] && { echo "ERROR: (gcloud.compute.instances.get-serial-port-output) PERMISSION_DENIED: compute.instances.getSerialPortOutput" >&2; exit 1; }
        [ -e "$state/serial" ] && cat "$state/serial"; exit 0 ;;
      *"builds submit"*)
        [ -e "$state/build-hangs" ] && exec sleep 5
        [ -e "$state/build-fails" ] && exit 1; exit 0 ;;
      *"compute instances create"*) exit 0 ;;
      *"compute instances remove-metadata"*) [ -e "$state/remove-metadata-fails" ] && { echo "ERROR: setMetadata denied" >&2; exit 1; }; exit 0 ;;
      *"compute instances delete"*) exit 0 ;;
      *"container clusters get-credentials"*) [ -e "$state/no-credentials" ] && exit 1; exit 0 ;;
    esac
    exit 0
    """
)

# The knobs the script and its stubs read with a default: popped from the child
# environment, so a developer's own exports (a dev host, a skipped check) do
# not reach the suite.
FORGE_KNOBS_ABSENT = (
    "JOB_NAME", "EVAL_GHES_HOST", "EVAL_GITLAB_CE_HOST", "FORGE_SKIP_CLUSTER_CHECK", "FORGE_VM_ZONE", "AR_REPO",
    "FORGE_IMAGE_TAG", "GHES_READY_TIMEOUT_SECONDS", "FORGE_BUILD_TIMEOUT_SECONDS", "FORGE_BUILD_CLIENT_BOUND_SECONDS",
    "FORGE_READY_POLL_SECONDS", "KUBECTL_CODE", "KUBECTL_EXIT", "MAKE_CERTS_LEAF_ONLY",
)

# kubectl: the in-cluster check; answers the code the test puts in KUBECTL_CODE
# and exits as the pod's curl would (KUBECTL_EXIT; curl exits 7 with its 000
# on a refused connection).
STUB_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf 'kubectl %s\\n' "$*" >> "${STUB_CALLS}"
    printf '%s' "${KUBECTL_CODE:-200}"
    exit "${KUBECTL_EXIT:-0}"
    """
)

# openssl: the CA:TRUE gate reads basicConstraints; a PEM file passes.
STUB_OPENSSL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    f="${@: -1}"
    grep -q "BEGIN CERTIFICATE" "$f" 2>/dev/null || { echo "unable to load certificate" >&2; exit 1; }
    if grep -q "NOT-A-CA" "$f"; then printf 'X509v3 Basic Constraints: critical\\n    CA:FALSE\\n'; else printf 'X509v3 Basic Constraints: critical\\n    CA:TRUE\\n'; fi
    """
)

# make-certs.sh: the real helper's contract (host, out-dir it creates, days),
# writing CA-shaped files; MAKE_CERTS_LEAF_ONLY=1 writes a CA without CA:TRUE.
STUB_MAKE_CERTS = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf 'make-certs %s\\n' "$*" >> "${STUB_CALLS}"
    mkdir -p "$2"
    marker=""; [ "${MAKE_CERTS_LEAF_ONLY:-}" = "1" ] && marker="NOT-A-CA"
    printf -- '-----BEGIN CERTIFICATE-----\\n%s%s\\n-----END CERTIFICATE-----\\n' "ca-for-$1" "$marker" > "$2/ca.crt"
    printf -- '-----BEGIN CERTIFICATE-----\\nleaf-for-%s\\n-----END CERTIFICATE-----\\n' "$1" > "$2/tls.crt"
    printf -- '-----BEGIN PRIVATE KEY-----\\nkey\\n-----END PRIVATE KEY-----\\n' > "$2/tls.key"
    """
)

READY_SERIAL = "startup-script: FORGE script-start t=1s\nstartup-script: FORGE ready t=9s\nstartup-script: FORGE header: x-github-enterprise-version: 3.17.0\n"


def _write(path: pathlib.Path, text: str, mode: int = 0o755) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


class ForgeScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        _write(self.bin / "gcloud", STUB_GCLOUD)
        _write(self.bin / "kubectl", STUB_KUBECTL)
        _write(self.bin / "openssl", STUB_OPENSSL)
        # The staged SCRIPT_DIR: the script under test beside what it reads.
        self.hack = self.dir / "hack"
        (self.hack / "ghes-proxy").mkdir(parents=True)
        shutil.copy(HACK / "ci-forge.sh", self.hack / "ci-forge.sh")
        shutil.copy(HACK / "ci-env.sh", self.hack / "ci-env.sh")
        shutil.copy(HACK / "ci-deploy.sh", self.hack / "ci-deploy.sh")
        installer = self.dir / "scripts" / "installer"
        installer.mkdir(parents=True)
        shutil.copy(REPO_ROOT / "scripts" / "installer" / "gke_dns_endpoint.sh", installer / "gke_dns_endpoint.sh")
        _write(self.hack / "ghes-proxy" / "make-certs.sh", STUB_MAKE_CERTS)
        self.calls = self.dir / "calls.log"
        self.gstate = self.dir / "gcloud-state"
        self.gstate.mkdir()
        self.state_dir = self.dir / "forge-state"
        self.scratch = self.dir / "scratch"  # TMPDIR for the run: the key and the startup script must not outlive it
        self.scratch.mkdir()

    # The project with every once-per-project piece in place.
    def provision(self, ip: str = FORGE_IP, record_ip: str = None, serial: str = READY_SERIAL):
        (self.gstate / "address").write_text(ip + "\n")
        (self.gstate / "record").write_text((record_ip or ip) + "\n")
        (self.gstate / "firewall").write_text("")
        (self.gstate / "deny").write_text("")
        (self.gstate / "sa").write_text("")
        (self.gstate / "serial").write_text(serial)

    def run_forge(self, action: str, forge: str = "ghes", **env):
        environment = get_isolated_test_env(
            overrides={
                "EVAL_FORGE": forge,
                "PROJECT_ID": PROJECT,
                "REGION": "us-central1",
                "EVAL_FORGE_STATE_DIR": str(self.state_dir),
                "STUB_CALLS": str(self.calls),
                "GCLOUD_STATE": str(self.gstate),
                "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
                "FORGE_READY_POLL_SECONDS": "0",
                "TMPDIR": str(self.scratch),
                "BUILD_ID": "1234",
                "PULL_NUMBER": "99",
                **env,
            },
            absent=FORGE_KNOBS_ABSENT,
        )
        return subprocess.run(["bash", str(self.hack / "ci-forge.sh"), action], capture_output=True, text=True, env=environment, check=False)

    def calls_of(self, verb: str):
        text = self.calls.read_text() if self.calls.exists() else ""
        return [line for line in text.splitlines() if verb in line]

    # ── the public rows ───────────────────────────────────────────────────────

    def test_the_public_rows_do_nothing(self):
        for forge in ("github", "gitlab"):
            with self.subTest(forge=forge):
                proc = self.run_forge("up", forge=forge)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("a public forge; nothing to stand up or take down", proc.stdout)
                self.assertFalse(self.calls.exists(), "no gcloud call on a public row")

    def test_an_unknown_forge_and_a_bad_action_are_refused(self):
        self.assertNotEqual(self.run_forge("up", forge="bitbucket").returncode, 0)
        self.assertIn("is not a forge this script knows", self.run_forge("up", forge="bitbucket").stderr)
        self.assertEqual(self.run_forge("sideways").returncode, 64)

    # ── up ────────────────────────────────────────────────────────────────────

    def test_up_builds_creates_waits_checks_and_writes_the_ca(self):
        self.provision()
        proc = self.run_forge("up")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        # The project pieces were read, never written.
        self.assertTrue(self.calls_of("compute addresses describe forge-ghes"))
        self.assertTrue(self.calls_of("dns record-sets describe ghes.eval.internal. --zone=eval-internal"))
        self.assertFalse(self.calls_of("addresses create") or self.calls_of("record-sets create") or self.calls_of("firewall-rules create"))
        # Certificates for the host, one day, then the proxy image in the project's registry.
        self.assertTrue(self.calls_of("make-certs ghes.eval.internal"))
        self.assertRegex(self.calls_of("make-certs")[0], r" 1$")
        build = self.calls_of("builds submit")[0]
        self.assertIn(f"--tag=us-central1-docker.pkg.dev/{PROJECT}/kube-agents/ghes-proxy:pr-99-1234", build)
        self.assertIn(f"--project={PROJECT}", build)
        # The VM: at the reserved address, with an external one, the VM identity, the tag and the labels.
        create = self.calls_of("compute instances create forge-ghes")[0]
        for flag in (
            f"--zone=us-central1-a", "--machine-type=e2-small", "--image-family=cos-stable", "--image-project=cos-cloud",
            f"--private-network-ip={FORGE_IP}", f"--service-account=forge-vm@{PROJECT}.iam.gserviceaccount.com",
            "--tags=forge", "--labels=kube-agents-forge=ghes,kube-agents-build=1234",
            "host=ghes.eval.internal", "enterprise-version=3.17.0", "block-project-ssh-keys=TRUE", "startup-script=", "tls-crt=", "tls-key=", "ca-crt=",
        ):
            self.assertIn(flag, create)
        # Readiness from the serial console, the key out of metadata, then one curl from a pod in the host cluster, with the CA.
        self.assertTrue(self.calls_of("get-serial-port-output forge-ghes"))
        self.assertTrue(self.calls_of("compute instances remove-metadata forge-ghes --zone=us-central1-a"))
        self.assertIn("--keys=tls-key", self.calls_of("remove-metadata")[0])
        calls = self.calls.read_text()
        self.assertLess(calls.index("remove-metadata"), calls.index("kubectl run"), "the key leaves metadata before anything else runs")
        self.assertTrue(self.calls_of("container clusters get-credentials platform-agent-host"))
        # The CA is passed as a multi-line value, so the call spans lines in the log.
        log = self.calls.read_text()
        run = log[log.index("kubectl run"):]
        self.assertIn("--image=curlimages/curl:8.11.1@sha256:c1fe1679c34d9784c1b0d1e5f62ac0a79fca01fb6377cdd33e90473c6f9f9a69", run)
        self.assertIn("--rm -i", run)
        self.assertIn("FORGE_URL=https://ghes.eval.internal/api/v3/meta", run)
        self.assertIn("FORGE_CA=-----BEGIN CERTIFICATE-----\nca-for-ghes.eval.internal", run)
        # The state directory: the CA, readable, and the record `down` reads.
        self.assertEqual((self.state_dir / "ca.crt").read_text().splitlines()[1], "ca-for-ghes.eval.internal")
        self.assertEqual(oct((self.state_dir / "ca.crt").stat().st_mode & 0o777), "0o644")
        self.assertEqual((self.state_dir / "forge-vm").read_text().strip(), "forge-ghes us-central1-a")
        self.assertIn("Forge up finished", proc.stdout)
        self.assertNotIn("BEGIN PRIVATE KEY", proc.stdout + proc.stderr)
        self.assertEqual(list(self.scratch.iterdir()), [], "the key and the startup script are gone after a successful up")

    def test_up_deletes_what_an_earlier_run_left_before_creating(self):
        self.provision()
        (self.gstate / "leftovers").write_text("forge-ghes us-central1-a\nforge-gitlab-ce us-central1-b\n")
        proc = self.run_forge("up")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deletes = self.calls_of("compute instances delete")
        self.assertEqual(len(deletes), 2)
        self.assertIn("forge-gitlab-ce --zone=us-central1-b", deletes[1])
        order = [line for line in self.calls.read_text().splitlines() if "instances delete" in line or "instances create" in line]
        self.assertTrue(order[-1].startswith("compute instances create"), "the leftovers go before this run's VM comes")

    def test_up_refuses_a_project_missing_its_pieces_by_name(self):
        # Nothing provisioned: every piece named, nothing created.
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        for piece in ("the reserved address forge-ghes in us-central1", "the A record ghes.eval.internal. in the private zone eval-internal", "the firewall rule allow-forge-443", "the deny rule deny-forge-ingress", f"the VM identity forge-vm@{PROJECT}.iam.gserviceaccount.com"):
            self.assertIn(piece, proc.stderr)
        self.assertIn("docs/ci-pool-projects.md 5.8", proc.stderr)
        self.assertFalse(self.calls_of("instances create") or self.calls_of("builds submit") or self.calls_of("make-certs"))
        self.assertFalse(self.state_dir.exists())

    def test_up_refuses_a_record_that_points_elsewhere(self):
        self.provision(record_ip="10.128.0.9")
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"resolves to 10.128.0.9 in zone eval-internal, but the reserved address forge-ghes is {FORGE_IP}", proc.stderr)
        self.assertFalse(self.calls_of("instances create"))

    def test_up_refuses_a_stale_state_directory(self):
        self.provision()
        self.state_dir.mkdir()
        (self.state_dir / "ca.crt").write_text("old\n")
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already exists; a forge from an earlier run is still recorded", proc.stderr)
        self.assertIn("down' first", proc.stderr)
        self.assertFalse(self.calls_of("instances create") or self.calls_of("builds submit"))

    def test_up_refuses_a_relative_state_directory(self):
        self.provision()
        proc = self.run_forge("up", EVAL_FORGE_STATE_DIR="forge-state")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("EVAL_FORGE_STATE_DIR='forge-state' must be an absolute path", proc.stderr)

    def test_up_refuses_a_ca_the_helper_made_without_ca_true(self):
        self.provision()
        proc = self.run_forge("up", MAKE_CERTS_LEAF_ONLY="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is not a CA certificate; make-certs.sh needs OpenSSL (Linux)", proc.stderr)
        self.assertFalse(self.calls_of("builds submit") or self.calls_of("instances create"))

    def test_up_bounds_the_image_build_on_the_client(self):
        if not shutil.which("timeout"):
            self.skipTest("coreutils timeout is not on PATH; the bound is skipped without it")
        self.provision()
        (self.gstate / "build-hangs").write_text("")
        proc = self.run_forge("up", FORGE_BUILD_CLIENT_BOUND_SECONDS="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("did not return within 1s", proc.stderr)
        self.assertFalse(self.calls_of("instances create"))

    def test_up_stops_when_the_image_build_fails(self):
        self.provision()
        (self.gstate / "build-fails").write_text("")
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("failed or did not return", proc.stderr)
        self.assertFalse(self.calls_of("instances create"))

    def test_up_takes_the_vm_down_when_the_serial_console_never_reports(self):
        self.provision(serial="startup-script: FORGE script-start t=1s\n")
        proc = self.run_forge("up", GHES_READY_TIMEOUT_SECONDS="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("did not report ready within 1s", proc.stderr)
        self.assertTrue(self.calls_of("compute instances delete forge-ghes --zone=us-central1-a"))
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.calls_of("kubectl run"))

    def test_up_names_a_failed_serial_read_instead_of_blaming_the_vm(self):
        self.provision()
        (self.gstate / "serial-fails").write_text("")
        proc = self.run_forge("up", GHES_READY_TIMEOUT_SECONDS="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("serial-console read(s) failed; the last error:", proc.stderr)
        self.assertIn("PERMISSION_DENIED: compute.instances.getSerialPortOutput", proc.stderr)
        self.assertTrue(self.calls_of("compute instances delete forge-ghes"))
        self.assertEqual(list(self.scratch.iterdir()), [], "the key and the startup script are gone after a failed up")

    def test_up_stops_when_the_key_cannot_leave_the_vms_metadata(self):
        self.provision()
        (self.gstate / "remove-metadata-fails").write_text("")
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the key must not stay readable there", proc.stderr)
        self.assertFalse(self.calls_of("kubectl run"), "no in-cluster check on a forge whose key is still in metadata")
        self.assertTrue(self.calls_of("compute instances delete forge-ghes"))
        self.assertFalse(self.state_dir.exists())

    def test_up_takes_the_vm_down_when_the_serial_console_reports_a_failure(self):
        self.provision(serial="startup-script: FORGE script-start t=1s\nstartup-script: FORGE failed pull us-central1-docker.pkg.dev/x\n")
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("startup failed: FORGE failed pull", proc.stderr)
        self.assertTrue(self.calls_of("compute instances delete forge-ghes --zone=us-central1-a"))
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.calls_of("kubectl run"), "no in-cluster check on a forge that never came up")

    def test_up_takes_the_vm_down_when_the_cluster_cannot_reach_it(self):
        self.provision()
        proc = self.run_forge("up", KUBECTL_CODE="000", KUBECTL_EXIT="7")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("could not reach https://ghes.eval.internal/api/v3/meta with the CA (got '000')", proc.stderr)
        self.assertTrue(self.calls_of("compute instances delete forge-ghes"))
        self.assertFalse(self.state_dir.exists())

    def test_up_stops_when_the_host_clusters_credentials_cannot_be_fetched(self):
        self.provision()
        (self.gstate / "no-credentials").write_text("")
        proc = self.run_forge("up")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("could not fetch credentials for platform-agent-host", proc.stderr)
        self.assertFalse(self.calls_of("kubectl run"), "no probe against whatever context the kubeconfig names")
        self.assertTrue(self.calls_of("compute instances delete forge-ghes"))

    def test_up_can_skip_the_cluster_check(self):
        self.provision()
        proc = self.run_forge("up", FORGE_SKIP_CLUSTER_CHECK="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.calls_of("kubectl run"))
        self.assertIn("in-cluster reachability check skipped", proc.stdout)

    def test_up_on_gitlab_ce_is_refused_until_its_lane(self):
        self.provision()
        proc = self.run_forge("up", forge="gitlab-ce")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs a gitlab forge built per run, which this script does not do yet (kube-agents#3683)", proc.stderr)
        self.assertFalse(self.calls_of("builds submit") or self.calls_of("instances create"))

    # ── down ──────────────────────────────────────────────────────────────────

    def test_down_deletes_the_rows_vms_by_label_and_removes_the_state_directory(self):
        (self.gstate / "leftovers").write_text("forge-ghes us-central1-a\n")
        self.state_dir.mkdir()
        (self.state_dir / "ca.crt").write_text("ca\n")
        (self.state_dir / "forge-vm").write_text("forge-ghes us-central1-a\n")
        proc = self.run_forge("down")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("labels.kube-agents-forge=ghes", self.calls_of("compute instances list")[0])
        # Once by the record, once more by the label sweep (the stub keeps listing it).
        self.assertEqual(len(self.calls_of("compute instances delete forge-ghes --zone=us-central1-a")), 2)
        self.assertFalse(self.state_dir.exists())
        self.assertIn("deleting this run's forge-ghes in us-central1-a", proc.stdout)

    def test_down_does_not_count_a_failed_list_as_nothing_to_delete(self):
        (self.gstate / "list-fails").write_text("")
        proc = self.run_forge("down")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("could not list the forge VMs", proc.stderr)
        self.assertNotIn("VM(s) deleted", proc.stdout)
        self.assertFalse(self.calls_of("compute instances delete"))

    def test_up_leaves_a_pre_made_state_directorys_mode_and_other_files(self):
        self.provision()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o755)
        (self.state_dir / "unrelated.txt").write_text("the caller's\n")
        proc = self.run_forge("up")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(oct(self.state_dir.stat().st_mode & 0o777), "0o755", "an existing directory keeps its mode")
        self.assertEqual((self.state_dir / "unrelated.txt").read_text(), "the caller's\n")
        self.assertTrue((self.state_dir / "ca.crt").exists())

    def test_down_leaves_the_callers_other_files_and_the_directory(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o755)
        (self.state_dir / "forge-vm").write_text("forge-ghes us-central1-a\n")
        (self.state_dir / "ca.crt").write_text("x\n")
        (self.state_dir / "unrelated.txt").write_text("the caller's\n")
        proc = self.run_forge("down")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.calls_of("compute instances delete forge-ghes --zone=us-central1-a"))
        self.assertFalse((self.state_dir / "ca.crt").exists() or (self.state_dir / "forge-vm").exists())
        self.assertEqual((self.state_dir / "unrelated.txt").read_text(), "the caller's\n")
        self.assertEqual(oct(self.state_dir.stat().st_mode & 0o777), "0o755")

    def test_down_with_nothing_to_delete_is_clean(self):
        proc = self.run_forge("down")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.calls_of("compute instances delete"))
        self.assertIn("0 forge VM(s) deleted", proc.stdout)

    # ── the table is the deploy's ─────────────────────────────────────────────

    def test_the_table_is_lifted_from_the_deploy(self):
        self.provision()
        proc = self.run_forge("up", EVAL_GHES_HOST="ghes.dev.example")
        self.assertNotEqual(proc.returncode, 0)  # the project's record is for ghes.eval.internal
        self.assertIn("the A record ghes.dev.example. in the private zone eval-internal", proc.stderr)
        bad = self.run_forge("up", EVAL_GHES_HOST="ghes.dev.example:8443")
        self.assertIn("is not a dotted bare hostname", bad.stderr)
        self.assertNotIn("is not a forge this script knows", bad.stderr)


if __name__ == "__main__":
    unittest.main()
