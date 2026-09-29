"""The agent pod diagnostics are captured on every eval run, bounded, and never fail the run.

`hack/ci-env.sh`'s `collect_agent_pod_diagnostics` runs from the eval script's
EXIT trap on green and red exits alike, beside `collect_gateway_log`, and keeps
what says why a pod was replaced or a container restarted mid-run: the bridge
sidecar's log and its previous instance, the agent container's previous
instance, per-container restarts and last terminations, and the namespace's
events. These tests run the real function, lifted from the real file with the
constants it reads, under bash with a `kubectl` stub on PATH.
"""

import math
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_SCRIPT = REPO_ROOT / "hack" / "ci-env.sh"
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
FUNCTION = "collect_agent_pod_diagnostics"

REQUEST_TIMEOUT_PREFIX = "--request-timeout="
WATCH_FILES = ("agent-pods-watch.txt", "agent-events-watch.txt")

# A kubectl that records every call's arguments in $STUB_CALLS; answers the
# Deployment's container-name read with $STUB_CONTAINERS; for a watch, prints
# its arguments, records its pid in $STUB_WATCH_PIDS and then either blocks
# (the apiserver holding the watch open) or, with STUB_WATCH_EXITS, returns
# (the apiserver closing it); prints its arguments then STUB_LINES lines of
# STUB_LINE_BYTES for anything else; or fails outright when STUB_FAIL is set.
KUBECTL_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_CALLS}"
case "$*" in
  *"--watch"*)
    echo "WATCH: $*"
    echo "$$" >> "${STUB_WATCH_PIDS}"
    [ -n "${STUB_WATCH_EXITS:-}" ] && exit 0
    exec sleep 60 ;;
esac
if [ -n "${STUB_FAIL:-}" ]; then
  echo "error: unable to reach the cluster" >&2
  exit 1
fi
case "$*" in
  *"containers[*].name"*) printf '%s' "${STUB_CONTAINERS:-platform-agent}"; exit 0 ;;
esac
echo "ARGS: $*"
line=$(head -c "${STUB_LINE_BYTES:-100}" /dev/zero | tr '\\0' 'a')
for _ in $(seq 1 "${STUB_LINES:-3}"); do echo "$line"; done
"""


FUNCTIONS = ("_agent_pod_watch_loop", "start_agent_pod_watch", "_stop_agent_pod_watch", FUNCTION)


def lifted(restart_seconds: str | None = None) -> str:
    """The functions, their state and the constants they read, as written in ci-env.sh."""
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    constants = re.findall(r"^readonly AGENT_DIAG_[A-Z_]+=.*$", src, re.MULTILINE)
    if len(constants) != 11:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"expected eleven AGENT_DIAG_ constants in {ENV_SCRIPT}, found {constants}")
    if restart_seconds is not None:
        constants = [re.sub(r"(WATCH_RESTART_SECONDS=).*", rf"\g<1>{restart_seconds}", c) for c in constants]
    state = re.findall(r'^AGENT_DIAG_WATCH_[A-Z]+=""$', src, re.MULTILINE)
    if len(state) != 2:  # pragma: no cover
        raise AssertionError(f"expected the watch's two state variables in {ENV_SCRIPT}, found {state}")
    bodies = []
    for name in FUNCTIONS:
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", src, re.DOTALL | re.MULTILINE)
        if match is None:  # pragma: no cover
            raise AssertionError(f"{name}() not found in {ENV_SCRIPT}")
        bodies.append(match.group(0))
    return "\n".join([*constants, *state, *bodies])


def constant(name: str) -> int:
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {name}=(.+)$", src, re.MULTILINE)
    assert match is not None, name
    value = match.group(1).strip()
    if value.startswith("$(("):
        return math.prod(int(factor) for factor in value[3:-2].split("*"))
    return int(value)


def one_shots(calls: list[str]) -> list[str]:
    """The snapshot's calls with their request timeout checked and stripped."""
    out = []
    for call in calls:
        if "--watch" in call:
            continue
        first, _, rest = call.partition(" ")
        assert first.startswith(REQUEST_TIMEOUT_PREFIX), call
        out.append(rest)
    return out


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def run_collect(
    watch: bool = False, restart_seconds: str | None = None, watch_for: float = 1.0, **stub_env: str
) -> tuple[subprocess.CompletedProcess, pathlib.Path, list[str]]:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="poddiag-"))
    stubs = tmp / "bin"
    stubs.mkdir()
    kubectl = stubs / "kubectl"
    kubectl.write_text(KUBECTL_STUB, encoding="utf-8")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    artifacts = tmp / "artifacts"
    calls = tmp / "calls"
    calls.touch()
    watch_pids = tmp / "watch-pids"
    watch_pids.touch()
    start = f"start_agent_pod_watch\nsleep {watch_for}\n" if watch else ""
    script = f'set -euo pipefail\n{lifted(restart_seconds)}\n{start}{FUNCTION}\necho "STATUS AFTER: $?"\n'
    env = {k: v for k, v in os.environ.items() if k != "AGENT_CLUSTER_CONTEXT"}
    env.update(
        PATH=f"{stubs}:{os.environ['PATH']}",
        ARTIFACTS=str(artifacts),
        TARGET_NAMESPACE="test-ns",
        STUB_CALLS=str(calls),
        STUB_WATCH_PIDS=str(watch_pids),
        **stub_env,
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
    run_collect.watch_pids = [int(p) for p in watch_pids.read_text(encoding="utf-8").split()]
    return proc, artifacts, calls.read_text(encoding="utf-8").splitlines()


class AgentPodDiagnosticsTest(unittest.TestCase):
    def test_status_events_and_the_previous_agent_container_are_written(self):
        proc, artifacts, calls = run_collect()
        calls = one_shots(calls)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("agent-pod-status.txt", "k8s-events.txt", "agent-pod-top.txt", "platform-agent-previous.log"):
            self.assertTrue((artifacts / name).is_file(), name)
        status = next(c for c in calls if c.startswith("get pods"))
        for field in ("restartCount", "lastState.terminated.reason", "lastState.terminated.exitCode", "lastState.terminated.finishedAt", "status.reason"):
            self.assertIn(field, status)
        events = next(c for c in calls if c.startswith("get events"))
        self.assertIn("-n test-ns", events)
        self.assertIn("--sort-by=.lastTimestamp", events)
        previous = next(c for c in calls if c.startswith("logs") and "-c platform-agent" in c)
        self.assertIn("--previous", previous)
        self.assertIn(f"--tail={constant('AGENT_DIAG_LOG_TAIL_LINES')}", previous)

    def test_the_bridge_logs_are_read_only_when_the_sidecar_is_declared(self):
        proc, artifacts, calls = run_collect(STUB_CONTAINERS="platform-agent platform-agent-dashboard fluent-bit")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((artifacts / "hermes-bridge.log").exists(), "a today-mode run gains no empty bridge file")
        self.assertFalse(any("-c hermes-bridge" in c for c in calls))

        proc, artifacts, calls = run_collect(STUB_CONTAINERS="platform-agent platform-agent-dashboard fluent-bit hermes-bridge")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((artifacts / "hermes-bridge.log").is_file())
        self.assertTrue((artifacts / "hermes-bridge-previous.log").is_file())
        bridge = [c for c in calls if "-c hermes-bridge" in c]
        self.assertEqual(len(bridge), 2, bridge)
        self.assertEqual(sum("--previous" in c for c in bridge), 1, bridge)

    def test_a_container_named_like_the_bridge_is_not_the_bridge(self):
        proc, artifacts, _ = run_collect(STUB_CONTAINERS="platform-agent hermes-bridge-legacy")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((artifacts / "hermes-bridge.log").exists())

    def test_the_byte_cap_holds_on_the_logs(self):
        cap = constant("AGENT_DIAG_LOG_MAX_BYTES")
        proc, artifacts, _ = run_collect(STUB_CONTAINERS="platform-agent hermes-bridge", STUB_LINES="200", STUB_LINE_BYTES=str(cap // 100))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("platform-agent-previous.log", "hermes-bridge.log", "hermes-bridge-previous.log"):
            self.assertEqual((artifacts / name).stat().st_size, cap, name)

    def test_the_events_are_line_bounded(self):
        bound = constant("AGENT_DIAG_EVENTS_TAIL_LINES")
        proc, artifacts, _ = run_collect(STUB_LINES=str(bound + 50), STUB_LINE_BYTES="10")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len((artifacts / "k8s-events.txt").read_text(encoding="utf-8").splitlines()), bound)

    def test_a_kubectl_that_fails_leaves_the_status_alone(self):
        """The trap reads `$?` for the dumper after this runs."""
        proc, artifacts, _ = run_collect(STUB_FAIL="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        self.assertTrue((artifacts / "agent-pod-status.txt").is_file())

    def test_every_read_is_pinned_to_the_agent_cluster_when_the_pin_is_known(self):
        proc, _, calls = run_collect(
            watch=True, AGENT_CLUSTER_CONTEXT="gke_proj_region_host", STUB_CONTAINERS="platform-agent hermes-bridge"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sum("--watch" in c for c in calls), 2, calls)
        for c in [*one_shots(calls), *(c for c in calls if "--watch" in c)]:
            self.assertTrue(c.startswith("--context gke_proj_region_host "), c)
        proc, _, calls = run_collect(watch=True, AGENT_CLUSTER_CONTEXT="")
        for c in calls:
            self.assertNotIn("--context", c)

    def test_the_watch_is_kept_and_stopped_by_the_snapshot(self):
        """Events age out and a replaced pod takes its logs with it, so what
        happened early in a long run survives only in the watch."""
        proc, artifacts, _ = run_collect(watch=True, AGENT_CLUSTER_CONTEXT="ctx")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        pods = (artifacts / "agent-pods-watch.txt").read_text(encoding="utf-8")
        events = (artifacts / "agent-events-watch.txt").read_text(encoding="utf-8")
        self.assertIn("get pods -n test-ns --watch", pods)
        self.assertIn("lastState.terminated.reason", pods)
        self.assertIn(".metadata.deletionTimestamp", pods)
        self.assertIn("get events -n test-ns --watch", events)
        self.assertIn(".lastTimestamp", events)
        self.assertEqual(len(run_collect.watch_pids), 2)
        for pid in run_collect.watch_pids:
            self.assertFalse(pid_alive(pid), "a stopped watch must not leave its kubectl running")

    def test_a_closed_watch_is_reopened(self):
        _, artifacts, _ = run_collect(watch=True, restart_seconds="0", watch_for=1.5, STUB_WATCH_EXITS="1")
        pods = (artifacts / "agent-pods-watch.txt").read_text(encoding="utf-8")
        self.assertGreater(pods.count("WATCH: "), 1, pods)

    def test_without_a_watch_the_snapshot_writes_no_watch_files(self):
        proc, artifacts, _ = run_collect()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in WATCH_FILES:
            self.assertFalse((artifacts / name).exists(), name)

    def test_the_eval_starts_the_watch_once_it_knows_the_host_cluster(self):
        eval_src = EVAL_SCRIPT.read_text(encoding="utf-8")
        pin = eval_src.index("export AGENT_CLUSTER_CONTEXT=")
        start = eval_src.index("\nstart_agent_pod_watch\n")
        self.assertLess(pin, start)

    def test_the_eval_trap_and_the_failure_dumper_both_call_it(self):
        env_src = ENV_SCRIPT.read_text(encoding="utf-8")
        eval_src = EVAL_SCRIPT.read_text(encoding="utf-8")
        dumper = re.search(r"^dump_prow_artifacts_on_failure\(\) \{\n.*?^\}$", env_src, re.DOTALL | re.MULTILINE)
        trap = re.search(r"^profile_and_dump_on_exit\(\) \{\n.*?^\}$", eval_src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(dumper)
        self.assertIsNotNone(trap)
        self.assertIn(f"    {FUNCTION}\n", dumper.group(0))
        self.assertIn(f"  collect_gateway_log\n  {FUNCTION}\n", trap.group(0))
        # The dumper's own shorter, unpinned --previous tail is gone.
        self.assertNotIn("previous-crash.log", dumper.group(0))


if __name__ == "__main__":
    unittest.main()
