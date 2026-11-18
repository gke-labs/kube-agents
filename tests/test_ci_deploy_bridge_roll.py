"""The deploy checks once that the bridge is in the agent pod from its first rollout (#5962, #5973).

The operator renders the bridge sidecar before the bus is provisioned
(`a2aBridgeInPod` in `k8s-operator/internal/controller/platformagent_a2a_bridge.go`),
so the agent Deployment's first rollout already carries it and the gateway
doesn't roll a second time. `hack/ci-deploy.sh` used to wait for that second
roll in step 6 and step 6b. It now reads the template once in each:

- step 6 (the fresh install) warns, and doesn't fail, when the A2A stack is
  rendered and the bridge isn't there, and skips the read when there is no
  A2A stack;
- step 6b (the mode upgrade) fails without the bridge, since the bridge log
  loop after it needs the container, and gates the agent's rollout once.

These tests run step 6's block against a stub kubectl and check step 6b's shape.
"""

import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_START = "bridge_check=true"
_END_MARKER = "rolls the gateway again under the checks below (#5962)"


def _block() -> str:
    text = _CI_DEPLOY.read_text()
    start = text.index(_START)
    end = text.index("\nfi\n", text.index(_END_MARKER, start)) + len("\nfi\n")
    return text[start:end]


# A stub kubectl driven by files in $STATE: callout (exists), callout_flaky
# (a read that fails other than NotFound) and bridge (the template carries
# it). Each call is appended to $STATE/calls.
_STUB = r"""#!/bin/bash
echo "$*" >> "$STATE/calls"
case "$*" in
  "get deployment/platform-agent-a2a-callout"*)
    if [ -f "$STATE/callout" ]; then echo deployment.apps/platform-agent-a2a-callout; exit 0; fi
    if [ -f "$STATE/callout_flaky" ]; then echo "Unable to connect to the server: dial tcp: i/o timeout" >&2; exit 1; fi
    echo 'Error from server (NotFound): deployments.apps "platform-agent-a2a-callout" not found' >&2; exit 1 ;;
  "get deployment/platform-agent-gateway"*jsonpath*)
    n=$(cat "$STATE/fails" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$STATE/fails"; echo "Unable to connect" >&2; exit 1; fi
    if [ -f "$STATE/bridge" ]; then echo "platform-agent fluent-bit hermes-bridge"; else echo "platform-agent fluent-bit"; fi ;;
  *) exit 0 ;;
esac
"""


class BridgeFromTheFirstRolloutTest(unittest.TestCase):
    def _run(self, *, callout: bool, bridge: bool, callout_flaky: bool = False, fails: int = 0):
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            stub_dir = state / "bin"
            stub_dir.mkdir()
            kubectl = stub_dir / "kubectl"
            kubectl.write_text(_STUB)
            kubectl.chmod(0o755)
            for name, on in (("callout", callout), ("callout_flaky", callout_flaky), ("bridge", bridge)):
                if on:
                    (state / name).write_text("")
            (state / "calls").write_text("")
            (state / "fails").write_text(str(fails))
            script = "\n".join(
                [
                    "set -euo pipefail",
                    'NAMESPACE="kubeagents-system"',
                    "MODE_NEXT_POLL_SECONDS=0",
                    "BRIDGE_TEMPLATE_READ_ATTEMPTS=3",
                    'PLATFORM_AGENT_CR_NAME="platform-agent"',
                    'AGENT_DEPLOYMENT_NAME="platform-agent-gateway"',
                    'BRIDGE_SIDECAR_NAME="hermes-bridge"',
                    _block(),
                    "echo DONE",
                ]
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env={"PATH": f"{stub_dir}:/usr/bin:/bin", "STATE": str(state)},
                timeout=60,
                check=False,
            )
            calls = (state / "calls").read_text().splitlines()
            return proc, calls

    def test_the_bridge_from_the_first_rollout_is_one_read_and_no_second_rollout(self):
        proc, calls = self._run(callout=True, bridge=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("carries the hermes-bridge sidecar from its first rollout", proc.stdout)
        self.assertEqual(len([c for c in calls if "jsonpath" in c]), 1, calls)
        self.assertFalse([c for c in calls if c.startswith("rollout status")], calls)

    def test_a_missing_bridge_warns_and_does_not_fail_the_deploy(self):
        proc, calls = self._run(callout=True, bridge=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        self.assertIn("WARNING: the A2A stack is rendered", proc.stdout)
        self.assertEqual(len([c for c in calls if "jsonpath" in c]), 1, calls)

    def test_a_failed_read_is_retried_not_read_as_a_missing_bridge(self):
        proc, calls = self._run(callout=True, bridge=True, fails=2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("from its first rollout", proc.stdout)
        self.assertEqual(len([c for c in calls if "jsonpath" in c]), 3, calls)

    def test_a_template_never_read_warns_as_unreadable(self):
        proc, _ = self._run(callout=True, bridge=True, fails=9)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("could not read", proc.stdout)
        self.assertNotIn("has no hermes-bridge", proc.stdout)

    def test_no_a2a_stack_skips_the_read(self):
        proc, calls = self._run(callout=False, bridge=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("renders no A2A stack", proc.stdout)
        self.assertFalse([c for c in calls if "jsonpath" in c], calls)

    def test_a_callout_read_that_fails_transiently_still_checks(self):
        # Only a NotFound means no A2A stack.
        proc, calls = self._run(callout=False, callout_flaky=True, bridge=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("from its first rollout", proc.stdout)
        self.assertEqual(len([c for c in calls if "jsonpath" in c]), 1, calls)

    def test_the_check_comes_after_the_first_rollout_and_before_the_web_console(self):
        text = _CI_DEPLOY.read_text()
        first_rollout = text.index("Verifying platform-agent rollout")
        check = text.index(_START)
        web_console = text.index("\ngate_web_console today\n")
        self.assertLess(first_rollout, check)
        self.assertLess(check, web_console)

    def test_step_6b_gates_the_agent_rollout_once_and_fails_without_the_bridge(self):
        text = _CI_DEPLOY.read_text()
        gate = 'gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"'
        self.assertEqual(text.count(gate), 1, "step 6b gates the agent rollout once; no second roll to wait for")
        after = text[text.index(gate) :]
        check = after[: after.index("The inject door")]
        self.assertIn("has no ${BRIDGE_SIDECAR_NAME} container after its rollout", check)
        self.assertIn("exit 1", check)
        self.assertNotIn("MODE_NEXT_GENERATION_ATTEMPTS", check, "step 6b no longer polls for the bridge")


_6B_START = "  agent_containers=\"\"\n  agent_read=false"
_6B_END = "after the mode switch\"\n"

_6B_STUB = r"""#!/bin/bash
echo "$*" >> "$STATE/calls"
case "$*" in
  "get deployment/platform-agent-gateway"*jsonpath*)
    n=$(cat "$STATE/fails")
    if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$STATE/fails"; echo "Unable to connect" >&2; exit 1; fi
    if [ -f "$STATE/bridge" ]; then echo "platform-agent fluent-bit hermes-bridge"; else echo "platform-agent fluent-bit"; fi ;;
  *) exit 0 ;;
esac
"""


class Step6bBridgeReadTest(unittest.TestCase):
    """Step 6b's one read, run: a template without the bridge stops the deploy
    with the state dumped, a failed read is retried, and one that never
    answers stops it as unreadable rather than as a missing bridge."""

    def _run(self, *, bridge: bool, fails: int = 0):
        text = _CI_DEPLOY.read_text()
        gate = text.index('gate_mode_next_rollout "deployment/${AGENT_DEPLOYMENT_NAME}"')
        start = text.index(_6B_START, gate)
        block = text[start : text.index(_6B_END, start) + len(_6B_END)]
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            (state / "bin").mkdir()
            kubectl = state / "bin" / "kubectl"
            kubectl.write_text(_6B_STUB)
            kubectl.chmod(0o755)
            if bridge:
                (state / "bridge").write_text("")
            (state / "fails").write_text(str(fails))
            (state / "calls").write_text("")
            script = "\n".join(
                [
                    "set -euo pipefail",
                    'NAMESPACE="kubeagents-system"',
                    'AGENT_DEPLOYMENT_NAME="platform-agent-gateway"',
                    'BRIDGE_SIDECAR_NAME="hermes-bridge"',
                    "MODE_NEXT_POLL_SECONDS=0",
                    "BRIDGE_TEMPLATE_READ_ATTEMPTS=3",
                    "MODE_NEXT_START=$SECONDS",
                    'dump_mode_next_state() { echo DUMPED; }',
                    block,
                    "echo DONE",
                ]
            )
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={"PATH": f"{state / 'bin'}:/usr/bin:/bin", "STATE": str(state)}, timeout=60, check=False,
            )
            return proc, (state / "calls").read_text().splitlines()

    def test_the_bridge_present_passes(self):
        proc, calls = self._run(bridge=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        self.assertEqual(len(calls), 1, calls)

    def test_a_missing_bridge_stops_the_deploy_with_the_state_dumped(self):
        proc, _ = self._run(bridge=False)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("DUMPED", proc.stdout)
        self.assertIn("has no hermes-bridge container", proc.stderr)

    def test_a_failed_read_is_retried(self):
        proc, calls = self._run(bridge=True, fails=2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(calls), 3, calls)

    def test_a_template_never_read_is_unreadable_not_missing(self):
        proc, _ = self._run(bridge=True, fails=9)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("could not read", proc.stderr)
        self.assertNotIn("has no hermes-bridge", proc.stderr)


if __name__ == "__main__":
    unittest.main()
