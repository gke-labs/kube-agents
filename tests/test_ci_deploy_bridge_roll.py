"""The deploy waits for the gateway's second roll before checking anything against it (#5962).

On a fresh install the operator adds the bridge sidecar to the agent pod only
once it has recorded the bus provisioned (`a2aBridgeInPod` in
`k8s-operator/internal/controller/platformagent_a2a_bridge.go`). So the agent
Deployment rolls a second time a reconcile or two after the provisioning Job,
and the first `kubectl rollout status` in step 6 can pass on the ReplicaSet
that is about to be replaced. The web-console check that follows then hits a
gateway that is restarting: two smoke and nightly runs on 11-15/16 failed
that way, with HTTP 502 `agent_unreachable`.

`hack/ci-deploy.sh` now waits for the template to carry the bridge and for
that rollout, before the shell StatefulSet and the web console. These tests
run that block against a stub kubectl:

- with the A2A stack rendered, it waits for the bridge and gates the rollout;
- with no callout Deployment (an operator that renders no A2A stack), it
  skips the wait at once;
- a failed second rollout stops the deploy.
"""

import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_START = "BRIDGE_ROLL_START=$SECONDS"
_END_MARKER = "carrying on without the second-roll wait"


def _block() -> str:
    text = _CI_DEPLOY.read_text()
    start = text.index(_START)
    end = text.index("\nfi\n", text.index(_END_MARKER, start)) + len("\nfi\n")
    return text[start:end]


# A stub kubectl driven by files in $STATE: callout (exists or not), polls
# (how many template reads return no bridge first) and rollout_rc. Each call
# is appended to $STATE/calls.
_STUB = r"""#!/bin/bash
echo "$*" >> "$STATE/calls"
case "$*" in
  "get deployment/platform-agent-a2a-callout"*)
    if [ -f "$STATE/callout" ]; then echo deployment.apps/platform-agent-a2a-callout; exit 0; fi
    if [ -f "$STATE/callout_flaky" ]; then echo "Unable to connect to the server: dial tcp: i/o timeout" >&2; exit 1; fi
    echo 'Error from server (NotFound): deployments.apps "platform-agent-a2a-callout" not found' >&2; exit 1 ;;
  "get deployment/platform-agent-gateway"*jsonpath*)
    n=$(cat "$STATE/polls")
    if [ "$n" -gt 0 ]; then
      echo $((n - 1)) > "$STATE/polls"
      echo "platform-agent fluent-bit"
    else
      echo "platform-agent fluent-bit hermes-bridge"
    fi ;;
  "rollout status"*) exit "$(cat "$STATE/rollout_rc")" ;;
  *) exit 0 ;;
esac
"""


class BridgeRollWaitTest(unittest.TestCase):
    def _run(self, *, callout: bool, polls: int, rollout_rc: int = 0, callout_flaky: bool = False):
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d)
            stub_dir = state / "bin"
            stub_dir.mkdir()
            kubectl = stub_dir / "kubectl"
            kubectl.write_text(_STUB)
            kubectl.chmod(0o755)
            if callout:
                (state / "callout").write_text("")
            if callout_flaky:
                (state / "callout_flaky").write_text("")
            (state / "polls").write_text(str(polls))
            (state / "rollout_rc").write_text(str(rollout_rc))
            (state / "calls").write_text("")
            script = "\n".join(
                [
                    "set -euo pipefail",
                    'NAMESPACE="kubeagents-system"',
                    'PLATFORM_AGENT_CR_NAME="platform-agent"',
                    'AGENT_DEPLOYMENT_NAME="platform-agent-gateway"',
                    'BRIDGE_SIDECAR_NAME="hermes-bridge"',
                    "MODE_NEXT_GENERATION_ATTEMPTS=5",
                    "MODE_NEXT_POLL_SECONDS=0",
                    'MODE_NEXT_ROLLOUT_TIMEOUT="600s"',
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

    def test_waits_for_the_bridge_then_gates_its_rollout(self):
        proc, calls = self._run(callout=True, polls=2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        self.assertIn("rolled out with the hermes-bridge sidecar", proc.stdout)
        template_reads = [c for c in calls if "jsonpath" in c]
        self.assertEqual(len(template_reads), 3, calls)
        self.assertEqual(sum(c.startswith("rollout status") for c in calls), 1, calls)

    def test_skips_the_wait_without_an_a2a_stack(self):
        proc, calls = self._run(callout=False, polls=0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("renders no A2A stack", proc.stdout)
        self.assertFalse([c for c in calls if "jsonpath" in c], calls)
        self.assertFalse([c for c in calls if c.startswith("rollout status")], calls)

    def test_a_bridge_that_never_arrives_does_not_fail_the_deploy(self):
        proc, calls = self._run(callout=True, polls=99)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WARNING: the A2A stack is rendered", proc.stdout)
        self.assertEqual(len([c for c in calls if "jsonpath" in c]), 5, calls)

    def test_a_callout_read_that_fails_transiently_still_waits(self):
        # Only a NotFound means no A2A stack; skipping on a flaky read would
        # bring back the restart the wait is for.
        proc, calls = self._run(callout=False, callout_flaky=True, polls=1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("rolled out with the hermes-bridge sidecar", proc.stdout)
        self.assertEqual(sum(c.startswith("rollout status") for c in calls), 1, calls)

    def test_a_failed_second_rollout_stops_the_deploy(self):
        proc, _ = self._run(callout=True, polls=0, rollout_rc=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("DONE", proc.stdout)
        self.assertIn("rollout with the hermes-bridge sidecar failed", proc.stdout)

    def test_the_wait_comes_before_the_web_console_check(self):
        text = _CI_DEPLOY.read_text()
        first_rollout = text.index("Verifying platform-agent rollout")
        wait = text.index(_START)
        web_console = text.index("\ngate_web_console today\n")
        self.assertLess(first_rollout, wait)
        self.assertLess(wait, web_console)


if __name__ == "__main__":
    unittest.main()
