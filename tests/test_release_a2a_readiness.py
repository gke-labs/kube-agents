"""The A2A readiness gate on the release path, past the provisioning script.

The provisioning script's half -- the gate after the install -- is in
test_provision_environment.py. This file holds the rest:

- wait_for_gke_readiness.sh, which the E2E job runs before the suites. After
  its own litellm and agent gates it must run the gate
  scripts/release/a2a_readiness.sh defines, and nothing else.
- The workflows around it: deploy-environment.yml and e2e-run.yml take no
  input for a stack to install (every install runs the A2A stack), their job
  timeouts cover the gate, the suites get the reply wait, and the RC
  pipeline runs one leg.
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

from tests.testing.common import get_isolated_test_env
from tests.testing.release import (
    MOCK_CALLS_LOG,
    MOCK_CR_READY_AT_GENERATION_2,
    MOCK_CR_STALE_READY,
    MOCK_GCP_PROJECT_ID,
    MOCK_GCP_REGION,
    MOCK_GKE_CLUSTER_NAME,
    write_a2a_readiness_kubectl_stub,
    write_recording_stub,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_READINESS = _REPO_ROOT / "scripts" / "release" / "wait_for_gke_readiness.sh"
_GATE_HELPER = _REPO_ROOT / "scripts" / "release" / "a2a_readiness.sh"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_NAMESPACE = "kubeagents-system"
# A2A_READINESS_GATE_TIMEOUT_SECONDS's default in a2a_readiness.sh.
_GATE_BUDGET_SECONDS = 1800
# deploy-environment.yml's budget for three provision attempts with teardowns,
# their second runs and the waits between them (the workflow says the sum);
# the gate's budget comes on top of it.
_DEPLOY_STOCKOUT_BUDGET_MINUTES = 170

# The two gates wait_for_gke_readiness.sh ran before the A2A stack existed.
_LITELLM_AND_AGENT_GATES = [
    f"kubectl rollout status deployment/litellm -n {_NAMESPACE} --timeout=420s",
    f"kubectl wait --for=condition=Available deployment/litellm -n {_NAMESPACE} --timeout=420s",
    f"kubectl rollout status deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=1500s",
    f"kubectl wait --for=condition=Available deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=1500s",
]

# The gate it adds after them, in order: the CR's Ready for its generation,
# the two bus conditions, each rollout, and the A2A gateway's. The jsonpath
# reads are abbreviated (see _normalise).
_A2A_GATE = [
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=READ",
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=BusProvisioned",
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=BusCredentialsReady",
    f"kubectl rollout status statefulset/platform-agent-a2a-nats -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-a2a-callout -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-a2a-verifier -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-a2a-gateway -n {_NAMESPACE} --timeout=GATE",
]


# The fixed timeouts of the two gates before the A2A one, left as they are by
# _normalise so the script test can pin them.
_FIXED_TIMEOUTS = ("420", "1500")


def _normalise(lines, keep=()):
    """Abbreviates the gate's jsonpath reads, and its timeouts once checked against the budget.

    A timeout in `keep` is left as it is.
    """
    out = []
    for line in lines:
        if "-o jsonpath=" in line:
            tag = next(
                (c for c in ("BusProvisioned", "BusCredentialsReady") if c in line),
                "READ",
            )
            line = line.split("-o jsonpath=", 1)[0] + "-o jsonpath=" + tag
        match = re.search(r"--timeout=(\d+)s$", line)
        if match and match.group(1) not in keep:
            assert 1 <= int(match.group(1)) <= _GATE_BUDGET_SECONDS, line
            line = line[: match.start()] + "--timeout=GATE"
        out.append(line)
    return out


class ReadinessScriptTest(unittest.TestCase):
    def _run(self, overrides=None, **stub):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)
        calls = tmp_dir / MOCK_CALLS_LOG
        bin_dir = tmp_dir / "bin"
        write_a2a_readiness_kubectl_stub(bin_dir, calls, **stub)
        write_recording_stub(bin_dir, "gcloud", calls)
        base = {
            "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
            "GCP_REGION": MOCK_GCP_REGION,
            "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
            "AGENT_NAMESPACE": _NAMESPACE,
            "A2A_READINESS_POLL_SECONDS": "0",
        }
        base.update(overrides or {})
        proc = subprocess.run(
            ["bash", str(_READINESS)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides=base,
                bin_dir=str(bin_dir),
                absent=("COMMIT_SHA", "GOOGLE_APPLICATION_CREDENTIALS"),
            ),
            cwd=str(tmp_dir),
        )
        lines = calls.read_text().splitlines() if calls.exists() else []
        return proc, lines

    def test_the_a2a_gate_follows_the_litellm_and_agent_gates(self):
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        gated = [c for c in _normalise(calls, keep=_FIXED_TIMEOUTS)
                 if "rollout status" in c or " wait " in c or "platformagent" in c]
        self.assertEqual(gated, _LITELLM_AND_AGENT_GATES + _A2A_GATE)

    def test_the_script_reads_no_spec_mode(self):
        """Every install runs the A2A stack; nothing is read to decide whether to gate it."""
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(any("spec.mode" in c for c in calls), calls)
        self.assertNotIn("spec.mode", _READINESS.read_text())

    def test_the_gate_waits_out_a_ready_from_the_previous_generation(self):
        proc, calls = self._run(ready_reads=(MOCK_CR_STALE_READY, MOCK_CR_READY_AT_GENERATION_2))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reads = [c for c in _normalise(calls) if c.endswith("jsonpath=READ")]
        self.assertEqual(len(reads), 2)

    def test_an_install_that_never_goes_ready_fails_the_script(self):
        proc, calls = self._run(
            {"A2A_READINESS_GATE_TIMEOUT_SECONDS": "0"}, ready_reads=("2 False 2",)
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("::error title=A2A stack: PlatformAgent not Ready::", proc.stdout)
        # Its own two gates ran; none of the A2A rollouts did.
        self.assertFalse(any("rollout status" in c and "a2a-" in c for c in calls), calls)

    def test_a_named_sidecar_is_waited_for_in_the_template_before_the_rollout(self):
        """The hook a sidecar the operator adds in a second roll is gated by."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)
        calls = tmp_dir / MOCK_CALLS_LOG
        bin_dir = tmp_dir / "bin"
        write_a2a_readiness_kubectl_stub(bin_dir, calls, containers="platform-agent hermes-bridge")
        proc = subprocess.run(
            [
                "bash", "-c",
                f'set -euo pipefail; . "{_GATE_HELPER}"; '
                f'a2a_readiness_gate_rollout {_NAMESPACE} "" '
                'deployment/platform-agent-gateway "$((SECONDS + 1500))" hermes-bridge',
            ],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={"A2A_READINESS_POLL_SECONDS": "0"}, bin_dir=str(bin_dir)
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = calls.read_text().splitlines()
        self.assertIn("containers[*].name", lines[0])
        self.assertEqual(
            _normalise(lines[1:]),
            [f"kubectl rollout status deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=GATE"],
        )

    def test_a_spent_budget_still_waits_rather_than_passing_unchecked(self):
        """kubectl reads --timeout=0s as "do not wait", so the floor is one second."""
        out = subprocess.run(
            ["bash", "-c", f'set -euo pipefail; . "{_GATE_HELPER}"; '
             'a2a_readiness_remaining "$((SECONDS - 5))"; '
             'a2a_readiness_remaining "$((SECONDS + 90))"'],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(out[0], "1")
        self.assertIn(int(out[1]), (89, 90))

    def test_the_helper_names_no_mode(self):
        """The one stack has no switch; nothing in the gate's names or output says `mode`."""
        text = _GATE_HELPER.read_text()
        self.assertNotRegex(text, r"(?i)platform_agent_mode|spec\.mode|\bmode\b")


def _workflow(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _needs(job):
    """A job's `needs:` as a list: it may be a scalar, and set() of a str is its characters."""
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _step(doc, job, name):
    for step in doc["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step {name!r} in job {job!r}")


class WorkflowsRunTheOneStackTest(unittest.TestCase):
    """No workflow picks a stack; every job budgets for the gate the one stack adds."""

    _SHARED = ("deploy-environment.yml", "e2e-run.yml", "e2e-manual-runner.yml")

    def test_no_shared_workflow_takes_a_mode_input(self):
        for name in self._SHARED:
            with self.subTest(workflow=name):
                triggers = _workflow(name)[True]
                for trigger, spec in triggers.items():
                    inputs = (spec or {}).get("inputs") or {}
                    self.assertNotIn("mode", inputs, f"{name}: {trigger} still takes a mode")

    def test_no_pipeline_passes_a_mode_to_the_shared_workflows(self):
        for workflow in sorted(_WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(workflow.read_text())
            for job_id, job in (doc.get("jobs") or {}).items():
                if job.get("uses") in ("./.github/workflows/deploy-environment.yml",
                                       "./.github/workflows/e2e-run.yml"):
                    with self.subTest(workflow=workflow.name, job=job_id):
                        self.assertNotIn("mode", job.get("with") or {})

    def test_no_step_hands_the_scripts_a_platform_agent_mode(self):
        for name in self._SHARED + ("reconcile-environment.yml",):
            with self.subTest(workflow=name):
                for job in _workflow(name)["jobs"].values():
                    for step in job.get("steps") or []:
                        self.assertNotIn("PLATFORM_AGENT_MODE", step.get("env") or {}, step.get("name"))

    def test_the_deploy_job_budgets_the_stockout_retries_and_the_gate(self):
        """170 for the provision attempts (deploy-environment.yml says the sum), plus the gate's budget."""
        job = _workflow("deploy-environment.yml")["jobs"]["deploy-environment"]
        minutes = int(job["timeout-minutes"])
        budget = int(re.search(r"A2A_READINESS_GATE_TIMEOUT_SECONDS:-(\d+)\}", _GATE_HELPER.read_text()).group(1))
        self.assertGreaterEqual((minutes - _DEPLOY_STOCKOUT_BUDGET_MINUTES) * 60, budget)

    _SUITE_STEPS = (
        ("e2e-run.yml", "run-e2e", "Execute Blocking E2E Gate"),
        ("e2e-run.yml", "run-e2e", "Execute Optional E2E Suites"),
        ("e2e-manual-runner.yml", "run-e2e", "Execute E2E Tests"),
    )

    def test_the_suites_get_the_reply_wait_and_no_stack_hint(self):
        """300s for gchat_agent_test.py's reply; the Slack suite reads the install itself."""
        for workflow, job, name in self._SUITE_STEPS:
            with self.subTest(workflow=workflow, step=name):
                step = _step(_workflow(workflow), job, name)
                env = step.get("env") or {}
                self.assertEqual(str(env.get("TEST_TIMEOUT_SEC")), "300")
                self.assertNotIn("TEST_TIMEOUT_SEC", step["run"])
                self.assertNotIn("E2E_SLACK_MODE", env)

    def test_the_rc_pipeline_runs_one_leg(self):
        """Five steps, the validated tag waiting on the E2E job and the teardown on every job before it."""
        jobs = _workflow("rc-release-pipeline.yml")["jobs"]
        self.assertEqual(
            list(jobs),
            ["step-1-create-tag", "step-2-deploy-env", "step-3-run-e2e-tests",
             "step-4-tag-validated", "step-5-teardown-env"],
        )
        self.assertEqual(_needs(jobs["step-2-deploy-env"]), ["step-1-create-tag"])
        self.assertEqual(_needs(jobs["step-3-run-e2e-tests"]), ["step-1-create-tag", "step-2-deploy-env"])
        self.assertEqual(_needs(jobs["step-4-tag-validated"]), ["step-1-create-tag", "step-3-run-e2e-tests"])
        self.assertEqual(
            _needs(jobs["step-5-teardown-env"]),
            ["step-1-create-tag", "step-2-deploy-env", "step-3-run-e2e-tests", "step-4-tag-validated"],
        )
        # The tag step fails closed on a red E2E, and the teardown keeps a
        # failed install standing, only through the implicit success().
        for job_id in ("step-4-tag-validated", "step-5-teardown-env"):
            with self.subTest(job=job_id):
                self.assertIsNone(re.search(r"\b(always|cancelled|failure|success)\s*\(", jobs[job_id]["if"]))

    def test_needs_reads_the_scalar_form_as_one_job(self):
        """Step 2 writes `needs:` as a scalar; the pin above must see it as a job name."""
        self.assertEqual(_needs({"needs": "step-1-create-tag"}), ["step-1-create-tag"])
        self.assertEqual(_needs({"needs": ["a", "b"]}), ["a", "b"])
        self.assertEqual(_needs({}), [])


if __name__ == "__main__":
    unittest.main()
