"""Live cluster validation for capacity preflight and rollout visibility (#1297).

This file sits in `tests/`, which `make test-python` and `make verify` run on
every pull request, so it is gated on an explicit opt-in rather than on a
kubeconfig probe: `KUBE_AGENTS_LIVE_CAPACITY_CLUSTER` names the GKE context to
run against, and without it every test here skips. A probe would have run this
suite against whichever cluster a developer's shell pointed at — a `kind-*`
context fails the coordinate match, a small GKE Standard cluster fails the
sizing matrix on a real deficit, and either way the sweep goes red on code the
developer did not touch while issuing `kubectl get pods -A` against their
cluster. `tests/conformance/bucket2/__init__.py` gates the same way and says
the same thing.

Run it with:

    KUBE_AGENTS_LIVE_CAPACITY_CLUSTER=gke_<project>_<location>_<cluster> \\
        python3 -m unittest tests.test_capacity_preflight_live

All operations in this suite are read-only against the cluster.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SH = _REPO_ROOT / "install.sh"
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

# The opt-in. Its value is the kubectl context to run against, which must be a
# GKE one: the preflight derives gke_<project>_<location>_<cluster> from its
# arguments and skips on a mismatch, so a context of any other shape would
# leave the suite asserting on a check that never ran.
_LIVE_CLUSTER_ENV_VAR = "KUBE_AGENTS_LIVE_CAPACITY_CLUSTER"
_GKE_CONTEXT_PATTERN = re.compile(r"^gke_([^_]+)_([^_]+)_(.+)$")


def _requested_context() -> str:
    return os.environ.get(_LIVE_CLUSTER_ENV_VAR, "").strip()


def _is_live_cluster_available() -> tuple[bool, str]:
    """Whether the opted-in context is the current one and answers a read."""
    requested = _requested_context()
    if not requested:
        return False, f"{_LIVE_CLUSTER_ENV_VAR} is unset"
    if not _GKE_CONTEXT_PATTERN.match(requested):
        return False, (
            f"{_LIVE_CLUSTER_ENV_VAR}='{requested}' is not a GKE context "
            "(gke_<project>_<location>_<cluster>)"
        )
    if not shutil.which("kubectl"):
        return False, "kubectl binary not found"
    try:
        ctx_proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if ctx_proc.returncode != 0 or not ctx_proc.stdout.strip():
            return False, "no current kubectl context configured"
        ctx = ctx_proc.stdout.strip()
        if ctx != requested:
            return False, f"current context '{ctx}' is not the requested '{requested}'"
        nodes_proc = subprocess.run(
            ["kubectl", "get", "nodes", "--no-headers"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if nodes_proc.returncode != 0:
            return False, f"cannot query nodes on context {ctx}: {nodes_proc.stderr.strip()}"
        return True, ctx
    except Exception as e:
        return False, f"error connecting to cluster: {e}"


class LiveCapacityPreflightTest(unittest.TestCase):
    """Live validation suite for cluster capacity preflight on active cluster."""

    @classmethod
    def setUpClass(cls):
        available, reason = _is_live_cluster_available()
        if not available:
            raise unittest.SkipTest(f"Live cluster not available ({reason}); skipping live test suite.")
        cls.context = reason
        # Guaranteed to match: the gate rejected every other shape.
        match = _GKE_CONTEXT_PATTERN.match(cls.context)
        cls.project = match.group(1)
        cls.region = match.group(2)
        cls.cluster = match.group(3)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._empty_install_env = pathlib.Path(tmp.name) / "install.env"
        self._empty_install_env.write_text("")

    def _run_shell(self, script_body, env_overrides=None, preamble="", source_install_sh=False):
        """Run `script_body` with the installer helpers sourced.

        `preamble` runs before the source, which is the only place a caller
        can get ahead of a `readonly` declaration. `source_install_sh` adds
        the front door in source-only mode, for the functions that live there
        rather than in installer_common.
        """
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env_overrides or {})
        full_env = get_isolated_test_env(overrides=overrides)
        # Inherit PATH, KUBECONFIG and authentication environment
        for k in ("KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"):
            if k in os.environ:
                full_env[k] = os.environ[k]
        install_sh_line = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n' if source_install_sh else ""
        )
        full_script = f"""
{preamble}
{install_sh_line}source "{_INSTALLER_COMMON}"
{script_body}
"""
        return subprocess.run(
            ["bash", "-c", full_script],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )

    def test_live_cluster_nodes_and_schedulable_capacity(self):
        """Validates that real nodes are parsed and have positive schedulable capacity."""
        body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "{self.cluster}" "{self.region}" "{self.project}"
"""
        proc = self._run_shell(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("Cluster capacity preflight check passed", proc.stdout)
        self.assertRegex(proc.stdout, r"\d+m CPU, \d+Mi Memory schedulable across \d+ untainted node\(s\)")

    def test_live_workload_sizing_matrix(self):
        """Tests all supported workload option profiles against the live cluster."""
        profiles = [
            ("Baseline (LiteLLM + Operator)", "false", "file", "false", "", "", "false"),
            ("With Cert-Manager", "false", "file", "false", "", "", "true"),
            ("With WebUI Dashboard", "false", "file", "true", "", "", "true"),
            ("With GitOps Minter", "false", "file", "true", "org", "repo", "true"),
            ("With Hindsight Memory", "false", "hindsight", "false", "", "", "true"),
            ("Full Stack Max Profile", "false", "hindsight", "true", "org", "repo", "true"),
        ]
        for name, gvisor, mem, webui, org, repo, cert in profiles:
            with self.subTest(profile=name):
                body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "{self.cluster}" "{self.region}" "{self.project}" \\
  "{gvisor}" "{mem}" "{webui}" "{org}" "{repo}" "{cert}"
"""
                proc = self._run_shell(body)
                self.assertEqual(proc.returncode, 0, f"Profile '{name}' failed: stdout={proc.stdout}")
                self.assertIn("Cluster capacity preflight check passed", proc.stdout)

    def test_live_skip_guards(self):
        """Validates that skip flags and Autopilot/fresh-cluster modes cleanly bypass preflight."""
        # SKIP_CAPACITY_CHECK=true
        proc = self._run_shell(f"""
SKIP_CAPACITY_CHECK=true check_existing_cluster_capacity_preflight "{self.cluster}" "{self.region}" "{self.project}"
""")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Skipping cluster capacity preflight check", proc.stdout)

        # TFVARS_CREATE_CLUSTER=true
        proc = self._run_shell(f"""
TFVARS_CREATE_CLUSTER=true check_existing_cluster_capacity_preflight "{self.cluster}" "{self.region}" "{self.project}"
""")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Capacity check", proc.stdout)

        # Autopilot mode
        proc = self._run_shell(f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=autopilot check_existing_cluster_capacity_preflight "{self.cluster}" "{self.region}" "{self.project}"
""")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Capacity check", proc.stdout)

        # Coordinates that are not the current context. The check warns and
        # returns 0 rather than refusing, which is what makes every other
        # assertion in this file conditional on the coordinates being right:
        # get them wrong and the suite grades a check that never ran.
        proc = self._run_shell("""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "not-a-cluster-xyz" "us-central1" "not-a-project-xyz"
""")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("does not match target cluster", proc.stdout + proc.stderr)

    def test_live_cluster_is_refused_when_the_requirement_exceeds_it(self):
        """The refusal path, against the same live node and pod data.

        Everything else here asserts a pass, so a preflight that returned 0
        unconditionally would satisfy the whole file: the suite would be
        grading the cluster rather than the check. What is synthesised is the
        requirement, not the cluster — the sizing constant is declared here
        before installer_common is sourced, so its own `readonly` on the same
        name fails (noisily, on stderr) and leaves this value in place. The
        evaluator then runs unmodified against the real nodes.
        """
        huge_cpu_millis = 10_000_000
        proc = self._run_shell(
            f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "{self.cluster}" "{self.region}" "{self.project}" \\
  "false" "file" "false" "" "" "true"
""",
            preamble=f"readonly PREFLIGHT_MIN_CPU_MILLIS_AGENT_UNSANDBOXED={huge_cpu_millis}",
        )
        self.assertEqual(
            proc.returncode,
            1,
            f"a {huge_cpu_millis}m requirement was not refused: {proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("Cluster capacity preflight check failed", proc.stdout + proc.stderr)
        self.assertIn("Insufficient schedulable CPU", proc.stdout)

    def test_live_rollout_monitor_starts_and_stops_on_sigterm(self):
        """The monitor is spawned with `&` and reaped with SIGTERM.

        It polls the live cluster, so a crash on the first iteration and a
        clean start look identical from the caller: the apply carries on
        either way and the diagnostics are simply never printed.
        """
        script = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
source "{_INSTALLER_COMMON}"
PROJECT_ID="{self.project}" REGION="{self.region}" CLUSTER_NAME="{self.cluster}" \\
  monitor_lifecycle_rollout &
mon_pid=$!
sleep 3
if ! kill -0 "$mon_pid" 2>/dev/null; then
  echo "MONITOR_DIED_EARLY"
  exit 0
fi
kill "$mon_pid" 2>/dev/null || true
wait "$mon_pid" 2>/dev/null || true
if kill -0 "$mon_pid" 2>/dev/null; then
  echo "MONITOR_SURVIVED_SIGTERM"
else
  echo "MONITOR_STOPPED_CLEANLY"
fi
"""
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        full_env = get_isolated_test_env(overrides=overrides)
        for k in ("KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"):
            if k in os.environ:
                full_env[k] = os.environ[k]
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
        self.assertNotIn("MONITOR_DIED_EARLY", proc.stdout, f"stderr: {proc.stderr}")
        self.assertIn("MONITOR_STOPPED_CLEANLY", proc.stdout, f"stderr: {proc.stderr}")

    def test_live_rollout_failure_diagnosis(self):
        """Validates diagnose_rollout_failure against live namespace upon timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_log = pathlib.Path(tmp) / "timeout.log"
            # Shaped the way terraform prints it. The diagnoser requires the
            # error-attribution line naming the release, not just the phrase:
            # the phrase alone is also what the Google provider raises for its
            # own long calls, and the release's address on its own appears in
            # the plan output of every apply.
            tmp_log.write_text(
                "Error: context deadline exceeded waiting for condition\n"
                "\n"
                "  with helm_release.kube_agents,\n"
                '  on main.tf line 497, in resource "helm_release" "kube_agents":\n'
            )
            script = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
NAMESPACE=kubeagents-system diagnose_rollout_failure "{tmp_log}"
"""
            overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
            full_env = get_isolated_test_env(overrides=overrides)
            for k in ("KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"):
                if k in os.environ:
                    full_env[k] = os.environ[k]

            proc = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Helm rollout timed out waiting for Kubernetes workloads", proc.stdout)
            self.assertIn("Diagnosing cluster pod states", proc.stdout)

    def test_live_rollout_failure_diagnosis_ignores_non_timeout(self):
        """Confirms that diagnose_rollout_failure stays quiet on logs that are not its own."""
        cases = {
            "a successful apply": "Terraform apply completed with exit code 0\n",
            # Same phrase, different provider. Diagnosing this one announces a
            # Helm rollout failure for a cluster the apply never finished
            # building, and then queries it.
            "a provider timeout": (
                "Error: timed out waiting for the condition\n"
                "\n"
                "  with google_container_cluster.primary,\n"
                '  on main.tf line 120, in resource "google_container_cluster" "primary":\n'
            ),
        }
        for label, contents in cases.items():
            with self.subTest(log=label), tempfile.TemporaryDirectory() as tmp:
                tmp_log = pathlib.Path(tmp) / "clean.log"
                tmp_log.write_text(contents)
                script = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
NAMESPACE=kubeagents-system diagnose_rollout_failure "{tmp_log}"
"""
                overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
                full_env = get_isolated_test_env(overrides=overrides)
                proc = subprocess.run(
                    ["bash", "-c", script],
                    capture_output=True,
                    text=True,
                    env=full_env,
                    cwd=str(_REPO_ROOT),
                )
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn("Helm rollout timed out", proc.stdout)


if __name__ == "__main__":
    unittest.main()
