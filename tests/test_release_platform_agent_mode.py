"""spec.mode on the release path, past the provisioning script.

The provisioning script's half -- the CR patch after the install, and the
refusal before the teardown -- is in test_provision_environment.py, and the
install.env key in test_render_install_env.py. This file holds the rest:

- wait_for_gke_readiness.sh, which the E2E job runs before the suites. Under
  `today` (or no mode) it must run exactly the commands it ran before the mode
  existed, with nothing new in their environment; under `next` it must add the
  gate scripts/release/platform_agent_mode.sh defines, after its own; and a
  value that is not a mode must stop it before it connects to anything.
- The workflows that carry the mode: deploy-environment.yml and e2e-run.yml
  take it as a `mode` input defaulting to `today`, hand it to the scripts as
  PLATFORM_AGENT_MODE, and run their extra steps only when it is not `today`.
  The two pipelines that call them pass no mode, so they install and test
  `today`, as they did.
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
    write_mode_kubectl_stub,
    write_recording_stub,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_READINESS = _REPO_ROOT / "scripts" / "release" / "wait_for_gke_readiness.sh"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_NAMESPACE = "kubeagents-system"
# PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS's default in platform_agent_mode.sh.
_GATE_BUDGET_SECONDS = 1800
# The readiness script's read of the installed CR's spec.mode.
_INSTALLED_MODE_READ = (
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath={{.spec.mode}}"
)

# The gate wait_for_gke_readiness.sh adds under `next`, in order: the CR's
# Ready for its generation, the two bus conditions, each rollout, and the A2A
# gateway's. The jsonpath reads are abbreviated (see _normalise).
_NEXT_GATE = [
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=READ",
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=BusProvisioned",
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=BusCredentialsReady",
    f"kubectl rollout status statefulset/platform-agent-a2a-nats -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-a2a-callout -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-a2a-verifier -n {_NAMESPACE} --timeout=GATE",
    f"kubectl rollout status deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=GATE",
    f"kubectl get platformagent platform-agent -n {_NAMESPACE} -o jsonpath=GATEWAY",
    f"kubectl rollout status deployment/platform-agent-a2a-gateway -n {_NAMESPACE} --timeout=GATE",
]


def _normalise(lines):
    """Abbreviates the gate's jsonpath reads, and its timeouts once checked against the budget."""
    out = []
    for line in lines:
        if "-o jsonpath=" in line:
            tag = next(
                (c for c in ("A2AGateway", "BusProvisioned", "BusCredentialsReady", ".spec.mode") if c in line),
                "READ",
            ).replace("A2AGateway", "GATEWAY").replace(".spec.mode", "MODE")
            line = line.split("-o jsonpath=", 1)[0] + "-o jsonpath=" + tag
        match = re.search(r"--timeout=(\d+)s$", line)
        if match:
            assert 1 <= int(match.group(1)) <= _GATE_BUDGET_SECONDS, line
            line = line[: match.start()] + "--timeout=GATE"
        out.append(line)
    return out


class ReadinessScriptModeTest(unittest.TestCase):
    def _run(self, overrides, **stub):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)
        calls = tmp_dir / MOCK_CALLS_LOG
        bin_dir = tmp_dir / "bin"
        # The CR matches the mode asked for unless a test says otherwise.
        stub.setdefault("installed_mode", "next" if overrides.get("PLATFORM_AGENT_MODE") == "next" else "")
        write_mode_kubectl_stub(bin_dir, calls, **stub)
        write_recording_stub(bin_dir, "gcloud", calls)
        # Every child's view of the mode variable, recorded beside its call.
        for name in ("kubectl", "gcloud"):
            stub_path = bin_dir / name
            text = stub_path.read_text().replace(
                'echo "' + name + ' $*" >> ',
                'echo "' + name + ' $* [PLATFORM_AGENT_MODE=${PLATFORM_AGENT_MODE-<absent>}]" >> ',
                1,
            )
            stub_path.write_text(text)
        base = {
            "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
            "GCP_REGION": MOCK_GCP_REGION,
            "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
            "AGENT_NAMESPACE": _NAMESPACE,
            "PLATFORM_AGENT_MODE_POLL_SECONDS": "0",
        }
        base.update(overrides)
        proc = subprocess.run(
            ["bash", str(_READINESS)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides=base,
                bin_dir=str(bin_dir),
                absent=("PLATFORM_AGENT_MODE", "COMMIT_SHA", "GOOGLE_APPLICATION_CREDENTIALS"),
            ),
            cwd=str(tmp_dir),
        )
        lines = calls.read_text().splitlines() if calls.exists() else []
        return proc, lines

    @staticmethod
    def _strip_env(lines):
        return [line.rsplit(" [PLATFORM_AGENT_MODE=", 1)[0] for line in lines]

    def test_today_is_byte_identical_to_no_mode_at_all(self):
        unset_proc, unset_calls = self._run({})
        today_proc, today_calls = self._run({"PLATFORM_AGENT_MODE": "today"})
        self.assertEqual(unset_proc.returncode, 0, unset_proc.stderr)
        self.assertEqual(today_proc.returncode, 0, today_proc.stderr)
        self.assertEqual(today_calls, unset_calls)
        self.assertEqual(today_proc.stdout, unset_proc.stdout)
        self.assertEqual(today_proc.stderr, unset_proc.stderr)

    def test_today_gates_only_litellm_and_the_agent_and_hands_on_no_mode(self):
        proc, calls = self._run({"PLATFORM_AGENT_MODE": "today"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for call in calls:
            self.assertTrue(call.endswith("[PLATFORM_AGENT_MODE=<absent>]"), call)
        gated = [c for c in self._strip_env(calls) if "rollout status" in c or " wait " in c]
        self.assertEqual(
            gated,
            [
                f"kubectl rollout status deployment/litellm -n {_NAMESPACE} --timeout=420s",
                f"kubectl wait --for=condition=Available deployment/litellm -n {_NAMESPACE} --timeout=420s",
                f"kubectl rollout status deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=1500s",
                f"kubectl wait --for=condition=Available deployment/platform-agent-gateway -n {_NAMESPACE} --timeout=1500s",
            ],
        )
        # The one read of the CR: its installed spec.mode, checked against the input.
        reads = [c for c in self._strip_env(calls) if "platformagent" in c]
        self.assertEqual(reads, [_INSTALLED_MODE_READ])

    def test_the_installed_mode_is_checked_before_any_gate(self):
        _, calls = self._run({})
        calls = self._strip_env(calls)
        first_gate = next(i for i, c in enumerate(calls) if "rollout status" in c)
        self.assertLess(calls.index(_INSTALLED_MODE_READ), first_gate)

    def test_a_cr_with_no_mode_is_today(self):
        for value in ({}, {"PLATFORM_AGENT_MODE": "today"}, {"PLATFORM_AGENT_MODE": ""}):
            with self.subTest(value=value):
                proc, _ = self._run(value, installed_mode="")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc, _ = self._run({}, installed_mode="today")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_next_against_a_today_install_is_refused_before_any_gate(self):
        for installed in ("", "today"):
            with self.subTest(installed=installed):
                proc, calls = self._run({"PLATFORM_AGENT_MODE": "next"}, installed_mode=installed)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("::error title=Mode mismatch::", proc.stdout)
                self.assertIn("mode next", proc.stdout)
                self.assertIn("spec.mode today", proc.stdout)
                self.assertFalse(any("rollout status" in c for c in calls), calls)

    def test_today_against_a_next_install_is_refused_before_any_gate(self):
        for value in ({}, {"PLATFORM_AGENT_MODE": "today"}):
            with self.subTest(value=value):
                proc, calls = self._run(value, installed_mode="next")
                self.assertEqual(proc.returncode, 1)
                self.assertIn("::error title=Mode mismatch::", proc.stdout)
                self.assertIn("mode today", proc.stdout)
                self.assertIn("spec.mode next", proc.stdout)
                self.assertFalse(any("rollout status" in c for c in calls), calls)

    def test_next_adds_exactly_the_mode_gate_after_todays(self):
        _, today_calls = self._run({})
        proc, next_calls = self._run({"PLATFORM_AGENT_MODE": "next"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        today_calls = self._strip_env(today_calls)
        next_calls = self._strip_env(next_calls)
        self.assertEqual(next_calls[: len(today_calls)], today_calls)
        self.assertEqual(_normalise(next_calls[len(today_calls):]), _NEXT_GATE)

    def test_next_waits_out_a_ready_from_the_previous_generation(self):
        proc, calls = self._run(
            {"PLATFORM_AGENT_MODE": "next"},
            ready_reads=(MOCK_CR_STALE_READY, MOCK_CR_READY_AT_GENERATION_2),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reads = [c for c in _normalise(self._strip_env(calls)) if c.endswith("jsonpath=READ")]
        self.assertEqual(len(reads), 2)

    def test_a_named_sidecar_is_waited_for_in_the_template_before_the_rollout(self):
        """The hook a sidecar the operator adds in a second roll is gated by."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)
        calls = tmp_dir / MOCK_CALLS_LOG
        bin_dir = tmp_dir / "bin"
        write_mode_kubectl_stub(bin_dir, calls, containers="platform-agent hermes-bridge")
        helper = _REPO_ROOT / "scripts" / "release" / "platform_agent_mode.sh"
        proc = subprocess.run(
            [
                "bash", "-c",
                f'set -euo pipefail; . "{helper}"; '
                f'platform_agent_mode_gate_rollout {_NAMESPACE} "" '
                'deployment/platform-agent-gateway "$((SECONDS + 1500))" hermes-bridge',
            ],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={"PLATFORM_AGENT_MODE_POLL_SECONDS": "0"}, bin_dir=str(bin_dir)
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
        helper = _REPO_ROOT / "scripts" / "release" / "platform_agent_mode.sh"
        out = subprocess.run(
            ["bash", "-c", f'set -euo pipefail; . "{helper}"; '
             'platform_agent_mode_remaining "$((SECONDS - 5))"; '
             'platform_agent_mode_remaining "$((SECONDS + 90))"'],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(out[0], "1")
        self.assertIn(int(out[1]), (89, 90))

    def test_a_mode_that_is_not_a_mode_stops_before_any_connection(self):
        for value in ("Next", "nxt", "today ", "TODAY"):
            with self.subTest(value=value):
                proc, calls = self._run({"PLATFORM_AGENT_MODE": value})
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(calls, [])
                self.assertIn("::error title=PLATFORM_AGENT_MODE is not a mode::", proc.stdout)


def _workflow(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _step(doc, job, name):
    for step in doc["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step {name!r} in job {job!r}")


class WorkflowsCarryTheModeTest(unittest.TestCase):
    """Where the mode enters, how far it travels, and that `today` leaves every step as it was."""

    def test_deploy_environment_takes_a_mode_defaulting_to_today(self):
        doc = _workflow("deploy-environment.yml")
        dispatch = doc[True]["workflow_dispatch"]["inputs"]["mode"]
        self.assertEqual(dispatch["default"], "today")
        self.assertEqual(dispatch["options"], ["today", "next"])
        call = doc[True]["workflow_call"]["inputs"]["mode"]
        self.assertEqual(call["default"], "today")
        self.assertFalse(call["required"])

    def test_e2e_run_takes_a_mode_defaulting_to_today(self):
        call = _workflow("e2e-run.yml")[True]["workflow_call"]["inputs"]["mode"]
        self.assertEqual(call["default"], "today")
        self.assertFalse(call["required"])

    def test_the_scripts_get_the_input_under_the_installers_name(self):
        deploy = _workflow("deploy-environment.yml")
        env = _step(deploy, "deploy-environment", "Provision Environment in GCP")["env"]
        self.assertEqual(env["PLATFORM_AGENT_MODE"], "${{ inputs.mode }}")
        e2e = _workflow("e2e-run.yml")
        env = _step(e2e, "run-e2e", "Connect to GKE & Wait for Pod Readiness")["env"]
        self.assertEqual(env["PLATFORM_AGENT_MODE"], "${{ inputs.mode }}")

    def test_the_lease_check_renders_no_mode(self):
        """It runs only on autopush and staging, where next is refused, so a mode there is dead."""
        env = _step(_workflow("deploy-environment.yml"), "deploy-environment",
                    "Refuse while somebody is live-testing")["env"]
        self.assertNotIn("PLATFORM_AGENT_MODE", env)

    _CHECKS = (
        ("deploy-environment.yml", "deploy-environment"),
        ("e2e-run.yml", "run-e2e"),
    )

    def test_the_mode_check_always_runs(self):
        """GitHub expressions compare strings case-insensitively, so no `if:` may
        decide whether the value is checked: `TODAY` would skip it and reach the
        candidate's exact-match check only after checkout, auth and setup."""
        for workflow, job in self._CHECKS:
            with self.subTest(workflow=workflow):
                step = _step(_workflow(workflow), job, "Check the PlatformAgent mode")
                self.assertNotIn("if", step)
                self.assertEqual(step["id"], "mode")

    def test_the_steps_only_next_needs_are_gated_on_the_checked_value(self):
        cases = (
            ("deploy-environment.yml", "deploy-environment", "Confirm the candidate can install the mode"),
            ("e2e-run.yml", "run-e2e", "Confirm the candidate can gate the mode"),
        )
        for workflow, job, name in cases:
            with self.subTest(workflow=workflow):
                self.assertEqual(_step(_workflow(workflow), job, name)["if"],
                                 "steps.mode.outputs.next == 'true'")

    def _run_mode_check(self, mode, target="rc", workflow="deploy-environment.yml", job="deploy-environment"):
        step = _step(_workflow(workflow), job, "Check the PlatformAgent mode")
        self.assertEqual(step["env"]["MODE"], "${{ inputs.mode }}")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        output = pathlib.Path(tmp.name) / "github_output"
        output.write_text("")
        proc = subprocess.run(
            ["bash", "-e", "-c", step["run"]], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "MODE": mode, "TARGET": target, "GITHUB_OUTPUT": str(output)},
        )
        return proc, output.read_text()

    def test_today_and_empty_pass_the_check_as_today(self):
        for workflow, job in self._CHECKS:
            for value in ("", "today"):
                with self.subTest(workflow=workflow, value=value):
                    proc, output = self._run_mode_check(value, workflow=workflow, job=job)
                    self.assertEqual(proc.returncode, 0, proc.stdout)
                    self.assertEqual(output, "next=false\n")

    def test_a_value_that_is_not_exactly_a_mode_stops_at_the_first_step(self):
        for workflow, job in self._CHECKS:
            for value in ("TODAY", "Today", "Next", "NEXT", "nxt", "today "):
                with self.subTest(workflow=workflow, value=value):
                    proc, output = self._run_mode_check(value, workflow=workflow, job=job)
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn("::error title=Not a PlatformAgent mode::", proc.stdout)
                    self.assertEqual(output, "")

    def test_the_mode_check_refuses_next_on_the_long_lived_environments(self):
        """The sibling guards match these names case-insensitively, so this does too."""
        step = _step(_workflow("deploy-environment.yml"), "deploy-environment", "Check the PlatformAgent mode")
        self.assertEqual(step["env"]["TARGET"], "${{ inputs.github_environment }}")
        for target in ("autopush", "staging", "Staging", "AUTOPUSH"):
            with self.subTest(target=target):
                proc, output = self._run_mode_check("next", target)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("::error title=spec.mode next is for the ephemeral environments::", proc.stdout)
                self.assertEqual(output, "")

    def test_the_mode_check_accepts_next_on_the_ephemeral_environments(self):
        for target in ("rc", "nightly"):
            with self.subTest(target=target):
                proc, output = self._run_mode_check("next", target)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(output, "next=true\n")
        proc, output = self._run_mode_check("next", workflow="e2e-run.yml", job="run-e2e")
        self.assertEqual((proc.returncode, output), (0, "next=true\n"), proc.stdout)

    def test_the_e2e_mode_check_runs_before_the_checkout(self):
        steps = [s.get("name") for s in _workflow("e2e-run.yml")["jobs"]["run-e2e"]["steps"]]
        self.assertLess(steps.index("Check the PlatformAgent mode"), steps.index("Checkout Repository"))

    def test_the_mode_value_is_checked_before_the_checkout(self):
        steps = [s.get("name") for s in _workflow("deploy-environment.yml")["jobs"]["deploy-environment"]["steps"]]
        self.assertLess(steps.index("Check the PlatformAgent mode"), steps.index("Checkout repository"))
        # The candidate check reads the candidate's tree, so it follows that
        # checkout, and precedes the step that tears the environment down.
        self.assertLess(
            steps.index("Checkout repository at exact candidate commit"),
            steps.index("Confirm the candidate can install the mode"),
        )
        self.assertLess(
            steps.index("Confirm the candidate can install the mode"),
            steps.index("Provision Environment in GCP"),
        )

    def test_today_keeps_the_deploy_jobs_sixty_minutes(self):
        job = _workflow("deploy-environment.yml")["jobs"]["deploy-environment"]
        self.assertEqual(job["timeout-minutes"], "${{ inputs.mode == 'next' && 90 || 60 }}")

    def test_next_buys_the_deploy_job_the_whole_gate(self):
        """The 30 extra minutes are the gate's single budget, which runs after the install."""
        expression = _workflow("deploy-environment.yml")["jobs"]["deploy-environment"]["timeout-minutes"]
        next_minutes, today_minutes = (int(n) for n in re.findall(r"\b(\d+)\b", expression))
        helper = (_REPO_ROOT / "scripts" / "release" / "platform_agent_mode.sh").read_text()
        budget = int(re.search(r"PLATFORM_AGENT_MODE_GATE_TIMEOUT_SECONDS:-(\d+)\}", helper).group(1))
        self.assertGreaterEqual((next_minutes - today_minutes) * 60, budget)

    def test_the_pipelines_still_install_and_test_today(self):
        """No caller passes a mode yet; the input's default is what they run."""
        for workflow in ("rc-release-pipeline.yml", "staging-promotion-pipeline.yml"):
            doc = _workflow(workflow)
            for job_id, job in doc["jobs"].items():
                if job.get("uses") in (
                    "./.github/workflows/deploy-environment.yml",
                    "./.github/workflows/e2e-run.yml",
                ):
                    with self.subTest(workflow=workflow, job=job_id):
                        self.assertNotIn("mode", job.get("with") or {})


if __name__ == "__main__":
    unittest.main()
