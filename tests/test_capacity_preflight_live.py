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

    def _run_shell(self, script_body, env_overrides=None):
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env_overrides or {})
        full_env = get_isolated_test_env(overrides=overrides)
        # Inherit PATH, KUBECONFIG and authentication environment
        for k in ("KUBECONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"):
            if k in os.environ:
                full_env[k] = os.environ[k]
        full_script = f"""
source "{_INSTALLER_COMMON}"
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

    def test_live_rollout_failure_diagnosis(self):
        """Validates diagnose_rollout_failure against live namespace upon timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_log = pathlib.Path(tmp) / "timeout.log"
            tmp_log.write_text("Error: context deadline exceeded waiting for condition\n")
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
        """Confirms that diagnose_rollout_failure does not run on successful or unrelated logs."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_log = pathlib.Path(tmp) / "clean.log"
            tmp_log.write_text("Terraform apply completed with exit code 0\n")
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
