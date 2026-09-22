"""The gateway log is captured on every eval run, bounded, and never fails the run.

`hack/ci-env.sh`'s `collect_gateway_log` runs from the eval script's EXIT trap
on green and red exits alike (scripts/test_ci_eval_trap.py pins that), so a
passing nightly whose repetitions ran to the delegation ceiling keeps the log
that says what the dispatcher and the workers were doing. These tests run the
real function, lifted from the real file with the constants it reads, under
bash with a `kubectl` stub on PATH, so the bounds it applies and the exit
status it leaves behind are the ones the script has.
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
FUNCTION = "collect_gateway_log"
ARTIFACT = "platform-agent-gateway.log"

# A kubectl that prints its arguments once, then the number of lines the
# harness asks for through STUB_LINES, each STUB_LINE_BYTES wide; or fails
# outright when STUB_FAIL is set.
KUBECTL_STUB = """#!/usr/bin/env bash
if [ -n "${STUB_FAIL:-}" ]; then
  echo "error: unable to reach the cluster" >&2
  exit 1
fi
echo "ARGS: $*"
line=$(head -c "${STUB_LINE_BYTES:-100}" /dev/zero | tr '\\0' 'a')
for _ in $(seq 1 "${STUB_LINES:-3}"); do echo "$line"; done
"""


def lifted() -> str:
    """The function and the constants it reads, as written in ci-env.sh."""
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    constants = re.findall(r"^readonly GATEWAY_LOG_[A-Z_]+=.*$", src, re.MULTILINE)
    if len(constants) != 2:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"expected two GATEWAY_LOG_ constants in {ENV_SCRIPT}, found {constants}")
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


def run_collect(**stub_env: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="gwlog-"))
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
        ]
    )
    env = dict(
        os.environ,
        PATH=f"{stubs}:{os.environ['PATH']}",
        ARTIFACTS=str(artifacts),
        TARGET_NAMESPACE="test-ns",
        **stub_env,
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, env=env)
    return proc, artifacts / ARTIFACT


class GatewayLogCollectionTest(unittest.TestCase):
    def test_the_log_is_written_from_the_bounded_tail(self):
        proc, log = run_collect(STUB_LINES="3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(log.is_file())
        first = log.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("logs deployment/platform-agent-gateway", first)
        self.assertIn("-n test-ns", first)
        self.assertIn(f"--tail={constant('GATEWAY_LOG_TAIL_LINES')}", first)

    def test_the_byte_cap_holds_whatever_the_lines_carry(self):
        cap = constant("GATEWAY_LOG_MAX_BYTES")
        # Fewer lines than the tail allows, each wide enough that together
        # they overrun the byte cap: the cap is what bounds the artifact.
        proc, log = run_collect(STUB_LINES="200", STUB_LINE_BYTES=str(cap // 100))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.stat().st_size, cap)

    def test_a_kubectl_that_fails_leaves_the_status_alone(self):
        """The trap reads `$?` for the dumper after this runs, and a cluster
        that cannot be reached must cost the run its log and nothing else."""
        proc, log = run_collect(STUB_FAIL="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS AFTER: 0", proc.stdout)
        self.assertTrue(log.is_file())

    def test_the_eval_trap_and_the_failure_dumper_both_call_it(self):
        env_src = ENV_SCRIPT.read_text(encoding="utf-8")
        eval_src = (REPO_ROOT / "hack" / "ci-eval-pr.sh").read_text(encoding="utf-8")
        dumper = re.search(r"^dump_prow_artifacts_on_failure\(\) \{\n.*?^\}$", env_src, re.DOTALL | re.MULTILINE)
        trap = re.search(r"^profile_and_dump_on_exit\(\) \{\n.*?^\}$", eval_src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(dumper)
        self.assertIsNotNone(trap)
        self.assertIn(f"    {FUNCTION}\n", dumper.group(0))
        self.assertIn(f"  {FUNCTION}\n", trap.group(0))
        # The dumper's own, shorter tail of the same file is gone: it would
        # overwrite the every-run capture with less on a red run.
        self.assertNotIn(f"--tail=2000 > \"${{artifact_dir}}/{ARTIFACT}\"", dumper.group(0))


if __name__ == "__main__":
    unittest.main()
