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
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_SCRIPT = REPO_ROOT / "hack" / "ci-env.sh"
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
FUNCTION = "collect_agent_pod_diagnostics"

REQUEST_TIMEOUT_PREFIX = "--request-timeout="
WATCH_FILES = ("agent-pods-watch.txt", "agent-events-watch.txt")
# How long a loop gets to notice its kubectl or its parent is gone; generous
# so a loaded runner does not flake, far below the stub's 60 s block.
EXIT_DEADLINE_SECONDS = 10

# A kubectl that records every call's arguments in $STUB_CALLS; answers the
# Deployment's container-name read with $STUB_CONTAINERS; for a watch, prints
# its arguments, records its pid in $STUB_WATCH_PIDS and then either blocks
# for STUB_WATCH_SECONDS (the apiserver holding the watch open, 60 unless
# set, which is the request timeout cutting it) or, with STUB_WATCH_EXITS,
# returns (the apiserver closing it), or with STUB_WATCH_PLAN ("3,3:1") takes
# its per-resource open's duration and exit status from the list in turn; prints its arguments then STUB_LINES lines of
# STUB_LINE_BYTES for anything else; or fails outright when STUB_FAIL is set.
KUBECTL_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_CALLS}"
case "$*" in
  *"--watch"*)
    echo "WATCH: $*"
    echo "$$" >> "${STUB_WATCH_PIDS}"
    [ -n "${STUB_WATCH_EXITS:-}" ] && exit 0
    if [ -n "${STUB_WATCH_PLAN:-}" ]; then
      case "$*" in *"get pods"*) n_file="${STUB_WATCH_PIDS}.pods" ;; *) n_file="${STUB_WATCH_PIDS}.events" ;; esac
      n=$(cat "${n_file}" 2>/dev/null || echo 0)
      echo $((n + 1)) > "${n_file}"
      IFS=, read -r -a plan <<< "${STUB_WATCH_PLAN}"
      step="${plan[$((n % ${#plan[@]}))]}"
      sleep "${step%%:*}"
      case "${step}" in *:*) exit "${step#*:}" ;; esac
      exit 0
    fi
    exec sleep "${STUB_WATCH_SECONDS:-60}" ;;
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


FUNCTIONS = ("_agent_pod_watch_loop", "start_agent_pod_watch", "_stop_agent_pod_watch", FUNCTION, "collect_a2a_component_logs")


def lifted(restart_seconds: str | None = None, healthy_seconds: str | None = None) -> str:
    """The functions, their state and the constants they read, as written in ci-env.sh."""
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    constants = re.findall(r"^readonly AGENT_DIAG_[A-Z_]+=.*$", src, re.MULTILINE)
    if len(constants) != 13:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"expected thirteen AGENT_DIAG_ constants in {ENV_SCRIPT}, found {constants}")
    # The A2A component logs the collector ends with; their own suite is
    # tests/test_ci_a2a_component_logs.py.
    a2a_constants = re.findall(r"^readonly A2A_DIAG_[A-Z_]+=.*$", src, re.MULTILINE)
    if len(a2a_constants) != 7:  # pragma: no cover
        raise AssertionError(f"expected seven A2A_DIAG_ constants in {ENV_SCRIPT}, found {a2a_constants}")
    constants += a2a_constants
    if restart_seconds is not None:
        constants = [re.sub(r"(WATCH_RESTART_SECONDS=).*", rf"\g<1>{restart_seconds}", c) for c in constants]
    if healthy_seconds is not None:
        constants = [re.sub(r"(WATCH_HEALTHY_SECONDS=).*", rf"\g<1>{healthy_seconds}", c) for c in constants]
    state = re.findall(r'^AGENT_DIAG_(?:WATCH_[A-Z]+|COLLECTED|PREFIX)=""$', src, re.MULTILINE)
    if len(state) != 4:  # pragma: no cover
        raise AssertionError(f"expected the four state variables in {ENV_SCRIPT}, found {state}")
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


def strip_timeout(calls: list[str], watches: bool = False) -> list[str]:
    """The snapshot's calls, or with `watches` the watch calls, with their
    request timeout checked and stripped: no kubectl here is unbounded."""
    out = []
    for call in calls:
        if ("--watch" in call) != watches:
            continue
        first, _, rest = call.partition(" ")
        assert first.startswith(REQUEST_TIMEOUT_PREFIX), call
        out.append(rest)
    return out


def one_shots(calls: list[str]) -> list[str]:
    return strip_timeout(calls)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def all_exit(pids: list[int]) -> bool:
    """Whether every pid is gone within the deadline, polled: a loop takes a
    moment to see its TERM, and a reaped child vanishes only once reaped."""
    deadline = time.monotonic() + EXIT_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not any(pid_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


def stub_bin(tmp: pathlib.Path) -> pathlib.Path:
    stubs = tmp / "bin"
    stubs.mkdir()
    kubectl = stubs / "kubectl"
    kubectl.write_text(KUBECTL_STUB, encoding="utf-8")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    return stubs


def run_collect(
    watch: bool = False,
    restart_seconds: str | None = None,
    healthy_seconds: str | None = None,
    watch_for: float = 1.0,
    collect_times: int = 1,
    **stub_env: str,
) -> tuple[subprocess.CompletedProcess, pathlib.Path, list[str]]:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="poddiag-"))
    stubs = stub_bin(tmp)
    artifacts = tmp / "artifacts"
    calls = tmp / "calls"
    calls.touch()
    watch_pids = tmp / "watch-pids"
    watch_pids.touch()
    loops = tmp / "loop-pids"
    start = f'start_agent_pod_watch\necho "$AGENT_DIAG_WATCH_PIDS" > {loops}\nsleep {watch_for}\n' if watch else ""
    collect = f"{FUNCTION}\n" * collect_times
    script = f'set -euo pipefail\n{lifted(restart_seconds, healthy_seconds)}\n{start}{collect}echo "STATUS AFTER: $?"\n'
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
    run_collect.loop_pids = [int(p) for p in loops.read_text(encoding="utf-8").split()] if watch else []
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
        # The container list failed too: unknown is not absent, so the bridge
        # reads are tried and their error is on record.
        bridge = (artifacts / "hermes-bridge.log").read_text(encoding="utf-8")
        self.assertIn("unable to reach the cluster", bridge)

    def test_every_read_is_pinned_to_the_agent_cluster_when_the_pin_is_known(self):
        proc, _, calls = run_collect(
            watch=True, AGENT_CLUSTER_CONTEXT="gke_proj_region_host", STUB_CONTAINERS="platform-agent hermes-bridge"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sum("--watch" in c for c in calls), 2, calls)
        for c in [*one_shots(calls), *strip_timeout(calls, watches=True)]:
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
        self.assertEqual(len(run_collect.loop_pids), 2)
        self.assertTrue(
            all_exit(run_collect.watch_pids + run_collect.loop_pids),
            "a stopped watch must leave neither its loop nor its kubectl running",
        )

    def test_the_loops_die_with_a_killed_eval_and_let_go_of_its_stdout(self):
        """A SIGKILLed eval runs no trap. The loops must still end, within one
        request timeout, and must never have held the job's stdout: Prow
        waits on that pipe, not on the process."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="poddiag-kill-"))
        loops = tmp / "loop-pids"
        script = (
            f"{lifted(restart_seconds='0')}\nstart_agent_pod_watch\n"
            f'echo "$AGENT_DIAG_WATCH_PIDS" > {loops}.tmp && mv {loops}.tmp {loops}\nexec sleep 60\n'
        )
        env = {k: v for k, v in os.environ.items() if k != "AGENT_CLUSTER_CONTEXT"}
        env.update(
            PATH=f"{stub_bin(tmp)}:{os.environ['PATH']}",
            TARGET_NAMESPACE="test-ns",
            STUB_CALLS=str(tmp / "calls"),
            STUB_WATCH_PIDS=str(tmp / "watch-pids"),
            STUB_WATCH_SECONDS="1",
        )
        proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        deadline = time.monotonic() + EXIT_DEADLINE_SECONDS
        while not loops.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(loops.exists(), proc.stderr.read1().decode() if proc.poll() is not None else "")
        pids = [int(p) for p in loops.read_text(encoding="utf-8").split()]
        self.assertEqual(len(pids), 2)
        proc.kill()
        proc.communicate(timeout=EXIT_DEADLINE_SECONDS)  # EOF, or the pipe is still held
        self.assertTrue(all_exit(pids), "a loop outlived its SIGKILLed parent")
        kubectls = [int(p) for p in (tmp / "watch-pids").read_text(encoding="utf-8").split()]
        self.assertTrue(kubectls)
        self.assertTrue(all_exit(kubectls), "a watch kubectl outlived its SIGKILLed eval")

    def test_a_second_call_does_not_repeat_the_snapshot(self):
        """The trap and the failure dumper both call it on a red run; the
        second call must not re-read a cluster that may be what failed."""
        _, _, once = run_collect(STUB_CONTAINERS="platform-agent hermes-bridge")
        proc, _, twice = run_collect(collect_times=2, STUB_CONTAINERS="platform-agent hermes-bridge")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(twice), len(once), twice)

    def test_a_kept_watch_runs_on_and_the_rearmed_call_stops_it_under_a_prefix(self):
        """ci-eval-pr.sh's rollback round trip: the eval's snapshot first, with
        the watch left running through the flip, then the trap's call stops it
        and writes the round trip's own snapshot beside the eval's."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="poddiag-keep-"))
        artifacts = tmp / "artifacts"
        script = "\n".join(
            [
                "set -euo pipefail",
                lifted(),
                "start_agent_pod_watch",
                "sleep 1",
                f"{FUNCTION} --keep-watch",
                'for pid in ${AGENT_DIAG_WATCH_PIDS}; do kill -0 "${pid}" && echo "LOOP ALIVE"; done',
                f'ls {artifacts} | sed "s/^/AFTER KEEP: /"',
                'AGENT_DIAG_COLLECTED=""',
                'AGENT_DIAG_PREFIX="rollback-"',
                f"{FUNCTION}",
                'echo "STATUS AFTER: $?"',
            ]
        )
        env = {k: v for k, v in os.environ.items() if k != "AGENT_CLUSTER_CONTEXT"}
        env.update(
            PATH=f"{stub_bin(tmp)}:{os.environ['PATH']}",
            ARTIFACTS=str(artifacts),
            TARGET_NAMESPACE="test-ns",
            STUB_CALLS=str(tmp / "calls"),
            STUB_WATCH_PIDS=str(tmp / "watch-pids"),
            STUB_CONTAINERS="platform-agent hermes-bridge",
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        self.assertEqual(proc.stdout.count("LOOP ALIVE"), 2, "the kept snapshot must leave both watches running")
        for name in WATCH_FILES:
            self.assertNotIn(f"AFTER KEEP: {name}", proc.stdout, "the kept snapshot must not write the tails yet")
            self.assertTrue((artifacts / name).is_file(), name)
        snapshot = ("agent-pod-status.txt", "k8s-events.txt", "agent-pod-top.txt", "platform-agent-previous.log", "hermes-bridge.log", "hermes-bridge-previous.log")
        for name in snapshot:
            self.assertIn(f"AFTER KEEP: {name}", proc.stdout)
            self.assertTrue((artifacts / f"rollback-{name}").is_file(), name)
        kubectls = [int(p) for p in (tmp / "watch-pids").read_text(encoding="utf-8").split()]
        self.assertTrue(all_exit(kubectls), "the re-armed call must stop the watch")

    def test_a_watch_that_failed_to_open_is_retried_with_its_list(self):
        """Until an open has held, nothing has recorded the list, so a retry
        must not drop it."""
        _, artifacts, _ = run_collect(watch=True, restart_seconds="0", watch_for=1.5, STUB_WATCH_EXITS="1")
        pods = (artifacts / "agent-pods-watch.txt").read_text(encoding="utf-8")
        opens = [line for line in pods.splitlines() if line.startswith("WATCH: ")]
        self.assertGreater(len(opens), 1, pods)
        for line in opens:
            self.assertNotIn("--watch-only", line, "a failed open recorded no list to skip")

    def test_a_watch_that_fails_after_one_held_lists_again(self):
        """A fast exit after a held open is a gap of unknown length; the next
        open must put the list back on record."""
        _, artifacts, _ = run_collect(
            # SECONDS is whole seconds: 3 s against 2 holds and 0 s fails on every alignment.
            watch=True, restart_seconds="0", healthy_seconds="2", watch_for=5.5, STUB_WATCH_PLAN="3,0"
        )
        pods = (artifacts / "agent-pods-watch.txt").read_text(encoding="utf-8")
        opens = [line for line in pods.splitlines() if line.startswith("WATCH: ")]
        self.assertGreaterEqual(len(opens), 3, pods)
        self.assertNotIn("--watch-only", opens[0])
        self.assertIn("--watch-only", opens[1], "the open after a held one must not relist")
        self.assertNotIn("--watch-only", opens[2], "the open after a failed one must relist")

    def test_a_watch_that_fails_slowly_is_not_held(self):
        """A dial timeout or a hang that errors can outlast the threshold; the
        exit status, not the duration alone, says it never watched."""
        _, artifacts, _ = run_collect(
            watch=True, restart_seconds="0", healthy_seconds="2", watch_for=7.5, STUB_WATCH_PLAN="3,3:1"
        )
        pods = (artifacts / "agent-pods-watch.txt").read_text(encoding="utf-8")
        opens = [line for line in pods.splitlines() if line.startswith("WATCH: ")]
        self.assertGreaterEqual(len(opens), 3, pods)
        self.assertIn("--watch-only", opens[1])
        self.assertNotIn("--watch-only", opens[2], "a slow failure must not count as held")

    def test_a_watch_that_held_is_reopened_at_once_without_relisting(self):
        """A cut watch reopens with no pause, so no event falls in a gap, and
        with --watch-only, so the held open's list stays inside the byte cap.
        The 30 s pause here would leave one open in the window if applied."""
        _, artifacts, _ = run_collect(
            watch=True, restart_seconds="30", healthy_seconds="1", watch_for=5, STUB_WATCH_SECONDS="2"
        )
        pods = (artifacts / "agent-pods-watch.txt").read_text(encoding="utf-8")
        opens = [line for line in pods.splitlines() if line.startswith("WATCH: ")]
        self.assertGreater(len(opens), 1, pods)
        self.assertNotIn("--watch-only", opens[0])
        for line in opens[1:]:
            self.assertIn("--watch-only", line, "a reopen must not relist what the held open kept")

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
