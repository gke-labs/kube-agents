import json
import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).parent.absolute() / "cluster_preflight.sh"

PROJECT = "demo-project"
CLUSTER = "cluster-a"
LOCATION = "us-central1"
EXPECTED_CONTEXT = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"

# A fake `kubectl` covering only the three invocations the preflight makes. It
# reads the context out of a kubeconfig the way the real one does, so a test
# fixes the *file* and the script's own logic decides the verdict.
#
# Two knobs reproduce the failure modes that matter:
#   FAKE_AMBIENT_CONTEXT   - what a plain `kubectl` resolves to when it ignores
#                            KUBECONFIG, i.e. the credential-proxy sidecar's own
#                            context leaking in. Unset means KUBECONFIG is honoured.
#   FAKE_UNREACHABLE       - make `cluster-info` fail like a dead API server.
#   FAKE_CONFIG_FAILS      - make `config current-context` fail the way a
#                            credential-proxy outage does: non-zero, error on
#                            stderr, nothing on stdout.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/bin/bash
    set -u
    from_file() { sed -n 's/^current-context:[[:space:]]*//p' "$1" | head -n1; }

    KCFG=""
    ARGS=()
    for arg in "$@"; do
        case "$arg" in
            --kubeconfig=*) KCFG="${arg#--kubeconfig=}" ;;
            *) ARGS+=("$arg") ;;
        esac
    done

    case "${ARGS[*]}" in
        "config current-context")
            if [ -n "${FAKE_CONFIG_FAILS:-}" ]; then
                echo "credential proxy unavailable: [Errno 111] Connection refused" >&2
                exit 1
            fi
            if [ -n "$KCFG" ]; then
                from_file "$KCFG"
            elif [ -n "${FAKE_AMBIENT_CONTEXT:-}" ]; then
                printf '%s\\n' "$FAKE_AMBIENT_CONTEXT"
            else
                from_file "${KUBECONFIG:-/nonexistent}"
            fi
            ;;
        "cluster-info"*)
            if [ -n "${FAKE_UNREACHABLE:-}" ]; then
                echo "Unable to connect to the server: dial tcp: i/o timeout" >&2
                exit 1
            fi
            echo "Kubernetes control plane is running at https://198.51.100.1"
            ;;
        *) echo "fake kubectl: unexpected args: ${ARGS[*]}" >&2; exit 64 ;;
    esac
    """
)

USER_MD = f"""# Cluster Agent Context

This Cluster Agent is permanently scoped to the following GKE cluster:

- project: {PROJECT}
- cluster: {CLUSTER}
- location: {LOCATION}
"""


class ClusterPreflightTest(unittest.TestCase):
    """Runs the real script against a fake kubectl.

    Every test asserts on the machine-readable `--json` contract, because that is
    what the Cluster Agent quotes into `kanban_block` (cluster SOUL.md §6).
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()
        kubectl = self.bin / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        kubectl.chmod(0o755)

        self.user_md = self.home / "USER.md"
        self.user_md.write_text(USER_MD, encoding="utf-8")
        self.kubeconfig = self.home / "kubeconfig.yaml"
        self.write_kubeconfig(EXPECTED_CONTEXT)

    def write_kubeconfig(self, context: str) -> None:
        body = "apiVersion: v1\nkind: Config\n"
        if context:
            body += f"current-context: {context}\n"
        self.kubeconfig.write_text(body, encoding="utf-8")

    def run_preflight(self, **extra_env) -> dict:
        # KUBECONFIG is exported by default because that is the real dispatch
        # path: cluster_agent_profile.py writes it into the profile's .env and
        # Hermes loads it. Pass KUBECONFIG=None to model a profile that never
        # got the pin - which check 4 must now catch rather than paper over.
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HERMES_HOME": str(self.home),
            "KUBECONFIG": str(self.kubeconfig),
            **extra_env,
        }
        env = {k: v for k, v in env.items() if v is not None}
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--json"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual("", proc.stderr, "preflight must not leak stderr into the card")
        result = json.loads(proc.stdout)
        # The exit code and the JSON status must never disagree; the agent branches
        # on the JSON, the dispatcher on the code.
        self.assertEqual(result["status"] == "ok", proc.returncode == 0)
        return result

    # ---- The happy path ------------------------------------------------------

    def test_passes_when_the_pin_matches_the_declared_cluster(self):
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_accepts_kubeconfig_exported_in_the_environment(self):
        # The dispatch path exports KUBECONFIG from the profile's .env rather than
        # relying on the $HERMES_HOME/kubeconfig.yaml fallback.
        moved = self.home / "pinned.yaml"
        self.kubeconfig.rename(moved)
        self.kubeconfig = moved
        self.assertEqual("ok", self.run_preflight(KUBECONFIG=str(moved))["status"])

    def test_identity_fields_are_read_case_insensitively(self):
        self.user_md.write_text(USER_MD.replace("- project:", "- Project:"), encoding="utf-8")
        self.assertEqual("ok", self.run_preflight()["status"])

    # ---- Identity: the pin points somewhere else -----------------------------

    def test_fails_when_the_pin_is_for_another_cluster(self):
        # The case this check exists for: everything is present and reachable, so
        # the old preflight passed and the agent investigated the wrong cluster.
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_someone-elses-cluster")
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("different cluster", result["reason"])
        self.assertIn(EXPECTED_CONTEXT, result["evidence"])
        self.assertIn("someone-elses-cluster", result["evidence"])

    def test_fails_when_the_pin_is_for_the_management_cluster(self):
        # The observed incident: the sidecar's own host-cluster context.
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_kage-management")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_on_a_cluster_name_that_is_a_prefix_of_the_declared_one(self):
        # `brad` vs `brad-2` — a substring match would pass this.
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_{CLUSTER}-2")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_when_the_pin_is_for_the_right_cluster_in_another_project(self):
        self.write_kubeconfig(f"gke_other-project_{LOCATION}_{CLUSTER}")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_when_the_pin_selects_no_context(self):
        self.write_kubeconfig("")
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("does not select a cluster", result["reason"])

    # ---- Identity: the pin is right but nothing uses it ----------------------

    def test_fails_when_kubectl_ignores_the_pinned_kubeconfig(self):
        # KUBECONFIG dropped on the way to the command (the credential-proxy
        # transport bug): the file is correct, so check 3 passes, but every plain
        # kubectl the agent runs lands on the sidecar's context instead.
        result = self.run_preflight(
            FAKE_AMBIENT_CONTEXT=f"gke_{PROJECT}_{LOCATION}_kage-management"
        )
        self.assertEqual("failed", result["status"])
        self.assertIn("does not use this agent's pinned kubeconfig", result["reason"])
        self.assertIn("kage-management", result["evidence"])

    def test_fails_when_kubeconfig_is_not_exported_at_all(self):
        # The gap a re-injected `env KUBECONFIG=...` hid: the profile's .env never
        # got the pin. The file is present and correct, so checks 2 and 3 pass on
        # it, and check 4 used to force the variable back in and pass as well -
        # while every real `kubectl` the agent runs afterwards has no pin at all.
        result = self.run_preflight(KUBECONFIG=None)
        self.assertEqual("failed", result["status"])
        self.assertIn("does not export KUBECONFIG", result["reason"])
        self.assertIn(".env", result["remediation"])

    def test_reports_a_proxy_outage_as_such_not_as_a_bad_pin(self):
        # A failing `kubectl config current-context` used to be swallowed, leaving
        # an empty context that read as "the pin selects no cluster" - and sent the
        # agent to re-scaffold, which runs through the same broken proxy.
        result = self.run_preflight(FAKE_CONFIG_FAILS="1")
        self.assertEqual("failed", result["status"])
        self.assertIn("kubectl itself failed", result["reason"])
        self.assertIn("Connection refused", result["evidence"])
        self.assertNotIn("does not select a cluster", result["reason"])
        self.assertNotIn("Re-scaffold the profile to re-fetch", result["remediation"])

    # ---- The check number is reported ----------------------------------------

    # The inventory-audit SOP routes on which check failed: 1-4 mean the agent never
    # established which cluster it is and must audit nothing, while 5 means it is
    # identified and merely cut off. The remediation text cannot carry that
    # distinction — a missing USER.md and a missing kubeconfig share one.

    def test_a_missing_identity_reports_check_1(self):
        self.user_md.unlink()
        self.assertEqual("1", self.run_preflight()["check"])

    def test_a_missing_kubeconfig_reports_check_2(self):
        self.kubeconfig.unlink()
        self.assertEqual("2", self.run_preflight()["check"])

    def test_an_unreachable_cluster_reports_check_5(self):
        self.assertEqual("5", self.run_preflight(FAKE_UNREACHABLE="1")["check"])

    def test_a_passing_run_names_no_check(self):
        self.assertEqual("", self.run_preflight()["check"])

    # ---- The pre-existing checks still work ----------------------------------

    def test_fails_when_user_md_is_missing(self):
        self.user_md.unlink()
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("no identity file", result["reason"])

    def test_fails_when_user_md_omits_the_location(self):
        # Previously only `cluster:` was required, so a partial identity passed and
        # left nothing to compare the kubeconfig against.
        self.user_md.write_text(
            USER_MD.replace(f"- location: {LOCATION}\n", ""), encoding="utf-8"
        )
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("incomplete", result["reason"])
        self.assertIn("location", result["evidence"])

    def test_fails_when_the_kubeconfig_is_missing(self):
        self.kubeconfig.unlink()
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("not pinned", result["reason"])

    def test_fails_when_the_kubeconfig_is_empty(self):
        self.kubeconfig.write_text("", encoding="utf-8")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_when_the_cluster_is_unreachable(self):
        result = self.run_preflight(FAKE_UNREACHABLE="1")
        self.assertEqual("failed", result["status"])
        self.assertIn("Cannot reach", result["reason"])
        self.assertIn("i/o timeout", result["evidence"])

    def test_fails_when_kubectl_is_absent(self):
        (self.bin / "kubectl").unlink()
        minimal = f"{self.bin}:/usr/bin:/bin"
        if shutil.which("kubectl", path=minimal):
            self.skipTest("a real kubectl is installed on the minimal PATH")
        result = self.run_preflight(PATH=minimal)
        self.assertEqual("failed", result["status"])
        self.assertIn("kubectl is not available", result["reason"])

    # ---- Output contract -----------------------------------------------------

    def test_human_output_names_the_mismatch(self):
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_someone-elses-cluster")
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True, text=True, timeout=60,
            env={"PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "HERMES_HOME": str(self.home)},
        )
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("PREFLIGHT: FAILED", proc.stdout)
        self.assertIn("someone-elses-cluster", proc.stdout)


if __name__ == "__main__":
    unittest.main()
