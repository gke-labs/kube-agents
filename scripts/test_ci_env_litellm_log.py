"""The LiteLLM log is captured on every eval run, bounded, and never fails the run.

`hack/ci-env.sh`'s `collect_litellm_log` runs from the eval script's EXIT trap
on green and red exits alike, beside `collect_gateway_log`. The thinking-replay
hook's per-request "thinking replay: dropped N" line lives only in this log, so
a green run that kept no litellm.log could not say whether the hook touched its
traffic. These tests run the real function, lifted from the real file with the
constants it reads, under bash with a `kubectl` stub on PATH, so the bounds it
applies and the exit status it leaves behind are the ones the script has.
"""

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
FUNCTION = "collect_litellm_log"
ARTIFACT = "litellm.log"
# The chart's pod label and container name (charts/kube-agents/templates/litellm.yaml).
POD_SELECTOR = "app=litellm"
CONTAINER = "litellm-container"

# A kubectl that answers `get` for the Deployment (or refuses it with
# NotFound when STUB_NO_DEPLOYMENT is set, or fails outright when STUB_FAIL
# is), and answers `logs` with its arguments on one line, then, for each of
# STUB_PODS pods, the last --tail of STUB_LINES lines, each STUB_LINE_BYTES
# wide: the line tail is applied the way kubectl applies it, per container.
KUBECTL_STUB = """#!/usr/bin/env bash
if [ -n "${STUB_FAIL:-}" ]; then
  echo "error: unable to reach the cluster" >&2
  exit 1
fi
for arg in "$@"; do
  case "$arg" in
    get)
      if [ -n "${STUB_NO_DEPLOYMENT:-}" ]; then
        echo "Error from server (NotFound): deployments.apps \"litellm\" not found (args: $*)" >&2
        exit 1
      fi
      echo "deployment.apps/litellm"
      exit 0
      ;;
    logs) break ;;
  esac
done
echo "ARGS: $*"
tail_n=-1
for arg in "$@"; do
  case "$arg" in --tail=*) tail_n="${arg#--tail=}" ;; esac
done
lines="${STUB_LINES:-3}"
if [ "$tail_n" -ge 0 ] && [ "$tail_n" -lt "$lines" ]; then lines="$tail_n"; fi
line=$(head -c "${STUB_LINE_BYTES:-100}" /dev/zero | tr '\\0' 'a')
for pod in $(seq 1 "${STUB_PODS:-1}"); do
  for _ in $(seq 1 "$lines"); do echo "[pod/litellm-$pod/litellm-container] $line"; done
done
"""


def lifted() -> str:
    """The function and the constants it reads, as written in ci-env.sh."""
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    constants = re.findall(r"^readonly LITELLM_[A-Z_]+=.*$", src, re.MULTILINE)
    if not constants:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"no LITELLM_ constants in {ENV_SCRIPT}")
    match = re.search(rf"^{FUNCTION}\(\) \{{\n.*?^\}}$", src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover
        raise AssertionError(f"{FUNCTION}() not found in {ENV_SCRIPT}")
    return "\n".join([*constants, match.group(0)])


def constant(name: str) -> int:
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {name}=(.+)$", src, re.MULTILINE)
    assert match is not None, name
    value = match.group(1).strip()
    if value.startswith("$(("):
        return int(eval(value[3:-2]))
    return int(value)


def constant_str(name: str) -> str:
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf'^readonly {name}="([^"]*)"$', src, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def run_collect(*then: str, **stub_env: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    """Run the lifted function once, then each line of `then` in the same bash."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="litellmlog-"))
    stubs = tmp / "bin"
    stubs.mkdir()
    kubectl = stubs / "kubectl"
    kubectl.write_text(KUBECTL_STUB, encoding="utf-8")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    artifacts = tmp / "artifacts"
    script = "\n".join(
        [
            "set -euo pipefail",
            lifted(),
            f"{FUNCTION}",
            'echo "STATUS AFTER: $?"',
            *then,
        ]
    )
    env = dict(
        os.environ,
        PATH=f"{stubs}:{os.environ['PATH']}",
        ARTIFACTS=str(artifacts),
        TARGET_NAMESPACE="test-ns",
        **stub_env,
    )
    if "AGENT_CLUSTER_CONTEXT" not in stub_env:
        # A pin in the caller's environment would leak into every case.
        env.pop("AGENT_CLUSTER_CONTEXT", None)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
    return proc, artifacts / ARTIFACT


class LiteLLMLogCollectionTest(unittest.TestCase):
    def test_the_log_is_read_from_every_replica_with_the_line_tail(self):
        proc, log = run_collect(STUB_LINES="3", STUB_PODS="2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(log.is_file())
        lines = log.read_text(encoding="utf-8").splitlines()
        first = lines[0]
        self.assertIn(f"logs -l {POD_SELECTOR}", first)
        self.assertIn(f"-c {CONTAINER}", first)
        self.assertIn("-n test-ns", first)
        self.assertIn("--prefix", first)
        self.assertIn(f"--tail={constant('LITELLM_LOG_TAIL_LINES')}", first)
        # The chart runs two replicas and each serves its own share of the
        # requests: both pods' lines are in the one file.
        self.assertTrue(any("[pod/litellm-1/" in line for line in lines))
        self.assertTrue(any("[pod/litellm-2/" in line for line in lines))

    def test_the_line_tail_caps_each_replica(self):
        cap = constant("LITELLM_LOG_TAIL_LINES")
        proc, log = run_collect(STUB_LINES=str(cap + 500), STUB_LINE_BYTES="10")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The ARGS line, then the last `cap` lines of the pod's log.
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), cap + 1)

    def test_the_byte_cap_holds_whatever_the_lines_carry(self):
        cap = constant("LITELLM_LOG_MAX_BYTES")
        proc, log = run_collect(STUB_LINES="200", STUB_LINE_BYTES=str(cap // 100))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.stat().st_size, cap)

    def test_a_missing_deployment_is_a_note_not_a_failure(self):
        """A today-mode or next-mode install without LiteLLM, or one that
        renders it under another name, costs the run this file and nothing
        else: the trap reads `$?` for the dumper after this runs."""
        proc, log = run_collect(STUB_NO_DEPLOYMENT="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        self.assertIn("deployment/litellm", proc.stdout)
        self.assertIn("not found", proc.stdout)
        # The note is the file's content, so the artifact says why it is empty.
        text = log.read_text(encoding="utf-8")
        self.assertIn("not found", text)
        self.assertNotIn("ARGS:", text, "no logs read is attempted without the Deployment")
        # The check is bounded, so an unreachable cluster costs the trap
        # seconds rather than a hang inside the deadline's grace.
        self.assertIn(f"--request-timeout={constant_str('LITELLM_LOG_REQUEST_TIMEOUT')}", text)

    def test_a_kubectl_that_fails_leaves_the_status_alone(self):
        proc, log = run_collect(STUB_FAIL="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        self.assertIn("unable to reach the cluster", log.read_text(encoding="utf-8"))

    def test_the_read_is_pinned_to_the_agent_cluster_when_the_pin_is_known(self):
        proc, log = run_collect(AGENT_CLUSTER_CONTEXT="gke_proj_region_host")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        first = log.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("ARGS: --context gke_proj_region_host logs"), first)
        proc, log = run_collect(AGENT_CLUSTER_CONTEXT="")
        first = log.read_text(encoding="utf-8").splitlines()[0]
        self.assertNotIn("--context", first)

    def test_a_second_call_in_the_same_process_keeps_the_first_capture(self):
        """The trap takes it on every exit and the failure dumper calls it
        again on a red one: the file is written once, not twice with
        different contents."""
        proc, log = run_collect(f"STUB_LINES=7 {FUNCTION}", 'echo "SECOND STATUS: $?"', STUB_LINES="3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SECOND STATUS: 0", proc.stdout)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 4)

    def test_the_eval_trap_and_the_failure_dumper_both_call_it(self):
        """A green run collects it from the trap; the dumper calls the same
        function for a caller without a green-path collector (ci-deploy.sh)
        and keeps no read of its own that would write the file differently."""
        env_src = ENV_SCRIPT.read_text(encoding="utf-8")
        eval_src = EVAL_SCRIPT.read_text(encoding="utf-8")
        dumper = re.search(r"^dump_prow_artifacts_on_failure\(\) \{\n.*?^\}$", env_src, re.DOTALL | re.MULTILINE)
        trap = re.search(r"^profile_and_dump_on_exit\(\) \{\n.*?^\}$", eval_src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(dumper)
        self.assertIsNotNone(trap)
        self.assertIn(f"    {FUNCTION}\n", dumper.group(0))
        self.assertIn(f"  collect_agent_pod_diagnostics\n  {FUNCTION}\n", trap.group(0))
        # Ahead of the dumper in the trap, so a green exit (where the dumper
        # does nothing) still takes it.
        self.assertLess(trap.group(0).index(f"  {FUNCTION}\n"), trap.group(0).index("dump_prow_artifacts_on_failure"))
        self.assertNotIn("logs deployment/litellm", dumper.group(0))
        self.assertNotIn(ARTIFACT, dumper.group(0))


if __name__ == "__main__":
    unittest.main()
