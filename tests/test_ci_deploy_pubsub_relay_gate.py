"""The presubmit's section 7b: the Pub/Sub platform adapter pulls through the broker.

The gate reads two logs for lines other files print: the adapter's WARNING
when it connects through the relay, and the broker's audit line for a relayed
pull of the platform family (the forwarded marker and the pull's rule id, since
the preflight read logs the marker too). Neither side knows a shell constant
reads it, so this file derives the markers from their sources and holds the
constants to them, and runs the real block against a stub
`kubectl` so the pipefail trap the drift-detector gate documents cannot come
back here (a `grep -q` that stops at the first match SIGPIPEs the kubectl
feeding it and the pipeline reports failure).

It also pins what the deploy turns on for the gate: both AgentPlugins, with
the stockout investigator's own execution limits left off, so the eval agent
keeps the tuning it is graded under.
"""

import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_ADAPTER = _REPO_ROOT / "agentplugins" / "pubsub-platform" / "files" / "platforms" / "pubsub" / "adapter.py"
_BROKER = _REPO_ROOT / "agents" / "platform" / "scripts" / "credential_proxy.py"
_GATE_START = "pubsub_relay_connected=false"
_GATE_END = 'echo "✓ Pub/Sub platform adapter pulls through the broker'
_GATE_NAMESPACE = "kubeagents-system"
_GATE_RESULT = "GATE_RESULT"
_FILLER_LINE = "2026-10-21T14:00:00Z PubSub: pull on route 'x' idle\n"
_FILLER_LINES = 40000
_PLUGIN_FLAGS = (
    '--set "plugins.pubsubPlatform.enabled=true"',
    '--set "plugins.stockoutInvestigator.enabled=true"',
    '--set "plugins.stockoutInvestigator.applyTuning=false"',
)
# The adapter's connect line: `PubSub: <marker> at <url> for <n> route(s)`.
_ADAPTER_CONNECT_RE = re.compile(r'logger\.warning\("PubSub: (connected through the credential broker relay) at ')
# The broker: the label the platform handler passes, and the forwarded line's format.
_BROKER_LABEL_RE = re.compile(r'_relay_google_call\(\s*"(platform pubsub)",')
_BROKER_FORWARDED_RE = re.compile(r'"%s forwarded request_id=%s rule=%s status=%d bytes=%d"')
# The platform family's allowed-rule prefix; a pull's rule id is it plus `-pull`
# (watcher_policy._evaluate_subscription_call).
_WATCHER_POLICY = _REPO_ROOT / "agents" / "platform" / "scripts" / "watcher_policy.py"
_PLATFORM_ALLOWED_PREFIX_RE = re.compile(r'_PLATFORM_SUBSCRIPTION_RULES = _SubscriptionRules\([^)]*?allowed_prefix="([^"]+)"', re.S)
_PULL_VERB = "-pull"
# The one helm call the gate depends on: the step-5 install, the only call of
# this shape in the script.
_HELM_CALL_START = 'helm upgrade --install "${HELM_RELEASE_NAME}"'
_HELM_CALL_END = "--wait"


def _shell_constant(name: str) -> str:
    match = re.search(rf'^readonly {name}="?([^"\n]*)"?$', _CI_DEPLOY.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"hack/ci-deploy.sh declares no readonly {name}")
    return match.group(1)


def _head_constants(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.startswith("readonly "))


def _gate(text: str) -> str:
    start = text.find(_GATE_START)
    assert start != -1, f"{_GATE_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_GATE_END, start)
    assert end != -1, f"{_GATE_END!r} not found after {_GATE_START!r}"
    return text[start:end]


class TheMarkersAreWhatTheSourcesPrintTest(unittest.TestCase):
    def test_the_adapter_marker_is_the_adapters_connect_line(self) -> None:
        match = _ADAPTER_CONNECT_RE.search(_ADAPTER.read_text())
        self.assertIsNotNone(match, "adapter.py no longer logs the connect line the gate waits on")
        self.assertEqual(match.group(1), _shell_constant("PUBSUB_RELAY_ADAPTER_MARKER"))

    def test_the_broker_marker_is_the_platform_familys_forwarded_line(self) -> None:
        text = _BROKER.read_text()
        label = _BROKER_LABEL_RE.search(text)
        self.assertIsNotNone(label, "credential_proxy.py no longer labels the platform family 'platform pubsub'")
        self.assertIsNotNone(_BROKER_FORWARDED_RE.search(text), "the relay's forwarded audit line changed shape")
        self.assertEqual(f"{label.group(1)} forwarded", _shell_constant("PUBSUB_RELAY_BROKER_MARKER"))

    def test_the_broker_rule_is_the_platform_familys_pull(self) -> None:
        """The forwarded line is logged for the preflight read too; only the
        pull's rule id says the adapter pulled."""
        match = _PLATFORM_ALLOWED_PREFIX_RE.search(_WATCHER_POLICY.read_text())
        self.assertIsNotNone(match, "watcher_policy.py no longer declares the platform family's allowed prefix")
        self.assertEqual(_shell_constant("PUBSUB_RELAY_BROKER_RULE"), f"rule={match.group(1)}{_PULL_VERB}")


class TheDeployTurnsThePluginsOnWithoutTheirTuningTest(unittest.TestCase):
    def test_the_helm_upgrade_enables_both_plugins_and_leaves_the_tuning_off(self) -> None:
        # The step-5 call itself, not the whole file: a flag in a comment or on
        # the mode-next upgrade would otherwise satisfy this.
        text = _CI_DEPLOY.read_text()
        self.assertIn(_HELM_CALL_START, text)
        helm_call = text.partition(_HELM_CALL_START)[2].partition(_HELM_CALL_END)[0]
        for flag in _PLUGIN_FLAGS:
            self.assertIn(
                flag, helm_call,
                "hack/ci-deploy.sh must enable both AgentPlugins for the gate and keep "
                "the stockout investigator's execution limits off the eval agent, which is "
                "graded under its own tuning.",
            )


class TheGateReadsBothLogsTest(unittest.TestCase):
    def _run_gate(self, gateway_log: str, broker_log: str):
        text = _CI_DEPLOY.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            (tmp_path / "gateway.log").write_text(gateway_log, encoding="utf-8")
            (tmp_path / "broker.log").write_text(broker_log, encoding="utf-8")
            gateway = _shell_constant("AGENT_DEPLOYMENT_NAME").replace("${PLATFORM_AGENT_CR_NAME}", _shell_constant("PLATFORM_AGENT_CR_NAME"))
            broker = _shell_constant("CREDENTIAL_PROXY_DEPLOYMENT_NAME").replace("${PLATFORM_AGENT_CR_NAME}", _shell_constant("PLATFORM_AGENT_CR_NAME"))
            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(
                f"""#!/usr/bin/env bash
if [ "$1" = "logs" ]; then
  case "$*" in
    *--tail=*) echo "(tail elided)"; exit 0 ;;
    *deployment/{gateway}*) exec cat "{tmp_path / 'gateway.log'}" ;;
    *deployment/{broker}*) exec cat "{tmp_path / 'broker.log'}" ;;
  esac
fi
exit 0
""",
                encoding="utf-8",
            )
            sleep_stub = bin_dir / "sleep"
            sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for stub in (kubectl_stub, sleep_stub):
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            script = "\n".join(
                (
                    "set -euo pipefail",
                    _head_constants(text),
                    f"NAMESPACE={_GATE_NAMESPACE}",
                    _gate(text),
                    f'echo "{_GATE_RESULT}=${{pubsub_relay_connected}}"',
                )
            )
            return subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, cwd=_REPO_ROOT,
                env=get_isolated_test_env(bin_dir=bin_dir),
            )

    def test_both_markers_early_in_long_logs_pass_the_gate(self) -> None:
        adapter = _shell_constant("PUBSUB_RELAY_ADAPTER_MARKER")
        broker = _shell_constant("PUBSUB_RELAY_BROKER_MARKER")
        proc = self._run_gate(
            f"PubSub: {adapter} at http://broker:8765 for 1 route(s)\n" + _FILLER_LINE * _FILLER_LINES,
            f'{{"message":"{broker} request_id=1 rule=gcp.pubsub.subscription-pull status=404 bytes=3"}}\n' + _FILLER_LINE * _FILLER_LINES,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn(f"{_GATE_RESULT}=true", proc.stdout)

    def test_an_adapter_that_never_connected_fails_the_gate(self) -> None:
        broker = _shell_constant("PUBSUB_RELAY_BROKER_MARKER")
        proc = self._run_gate(_FILLER_LINE * _FILLER_LINES, f"{broker} request_id=1\n")
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("did not pull through the credential broker", proc.stdout)

    def test_a_broker_that_relayed_nothing_fails_the_gate(self) -> None:
        adapter = _shell_constant("PUBSUB_RELAY_ADAPTER_MARKER")
        proc = self._run_gate(f"PubSub: {adapter} at http://broker:8765 for 1 route(s)\n", _FILLER_LINE * _FILLER_LINES)
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")

    def test_a_broker_that_relayed_only_the_preflight_read_fails_the_gate(self) -> None:
        """The adapter reads the subscription before it pulls; that line alone
        is not a pull."""
        adapter = _shell_constant("PUBSUB_RELAY_ADAPTER_MARKER")
        broker = _shell_constant("PUBSUB_RELAY_BROKER_MARKER")
        proc = self._run_gate(
            f"PubSub: {adapter} at http://broker:8765 for 1 route(s)\n",
            f'{{"message":"{broker} request_id=1 rule=gcp.pubsub.subscription-get status=200 bytes=120"}}\n' + _FILLER_LINE * _FILLER_LINES,
        )
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("did not pull through the credential broker", proc.stdout)


if __name__ == "__main__":
    unittest.main()
