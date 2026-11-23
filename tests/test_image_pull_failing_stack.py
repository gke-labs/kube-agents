# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The image-pull-failing stack must hand the audit a failed pull older than
the check's minimum, or fail the apply.

`bench/tf/prebuilt/image-pull-failing` plants two Deployments whose image pull
is refused and then waits (`wait_for_failed_pull_age.sh`). The case that
follows grades obtainability 3.28, which names a pull only once it has failed
for `IMAGE_PULL_FAILING_MIN_SECONDS`. A wait shorter than that, or one that
returns over a plant that never failed, turns the case red for a reason that
is not the product's. The script runs here against stub `kubectl`, `gcloud`
and `sleep` on PATH; the stack's default wait is read from its variables file
and held above the collector's constant.
"""

import importlib.util
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STACK = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "image-pull-failing"
_SCRIPT = _STACK / "wait_for_failed_pull_age.sh"
_VARIABLES = _STACK / "variables.tf"
_COLLECTOR = _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "collect.py"

_NAMESPACE = "eval-image-pull"
_WORKLOADS = ("eval-pull-node-identity", "eval-pull-dead-secret")
_AGE_SECONDS = "660"
_FAILING = "ImagePullBackOff"
_PULLING = "ContainerCreating"
_AGE_VARIABLE_RE = re.compile(r'variable "failed_pull_age_seconds" \{.*?default\s*=\s*(\d+)', re.DOTALL)

#: `kubectl` records every call to $CALLS and answers a pod read with the
#: "<pod uid> <waiting reason>" line the test wrote to $STATE/<label selector>.
_KUBECTL_STUB = """#!/bin/bash
echo "kubectl $*" >> "$CALLS"
selector=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-l" ]; then selector="$2"; fi
  shift
done
if [ -n "$selector" ] && [ -f "$STATE/$selector" ]; then cat "$STATE/$selector"; fi
exit 0
"""

_GCLOUD_STUB = '#!/bin/bash\necho "gcloud $*" >> "$CALLS"\nexit 0\n'

#: `sleep` records and returns at once. After the long wait it rewrites one
#: workload's pod line to $AFTER_WAIT_POD when the test sets one.
_SLEEP_STUB = """#!/bin/bash
echo "sleep $*" >> "$CALLS"
if [ "$1" = "$WAIT_AGE_SECONDS" ] && [ -n "${AFTER_WAIT_POD:-}" ]; then
  printf '%s\\n' "$AFTER_WAIT_POD" > "$STATE/$AFTER_WAIT_SELECTOR"
fi
exit 0
"""


def _pod_uid(workload: str, generation: int = 1) -> str:
    return f"uid-{workload}-{generation}"


def _collector_minimum() -> int:
    spec = importlib.util.spec_from_file_location("fleet_audit_collect_for_stack_test", _COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.IMAGE_PULL_FAILING_MIN_SECONDS


class WaitForFailedPullAge(unittest.TestCase):
    def run_wait(self, reasons, seen_timeout="600", **extra_env):
        """Run the script with each workload's pod in `reasons[workload]`. Returns the result and the recorded calls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bin_dir, state = root / "bin", root / "state"
            bin_dir.mkdir()
            state.mkdir()
            for name, body in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB), ("sleep", _SLEEP_STUB)):
                stub = bin_dir / name
                stub.write_text(body)
                stub.chmod(0o755)
            for workload, reason in reasons.items():
                (state / f"app={workload}").write_text(f"{_pod_uid(workload)} {reason}\n")
            calls = root / "calls"
            calls.touch()
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls),
                "STATE": str(state),
                "WAIT_HOST_CLUSTER": "host",
                "WAIT_HOST_LOCATION": "us-central1",
                "WAIT_PROJECT": "eval-project",
                "WAIT_NAMESPACE": _NAMESPACE,
                "WAIT_WORKLOADS": " ".join(_WORKLOADS),
                "WAIT_AGE_SECONDS": _AGE_SECONDS,
                "WAIT_SEEN_TIMEOUT_SECONDS": seen_timeout,
                **extra_env,
            }
            result = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False)
            return result, calls.read_text().splitlines()

    def test_it_waits_the_age_once_both_workloads_show_the_failed_pull(self):
        result, calls = self.run_wait({workload: _FAILING for workload in _WORKLOADS})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"sleep {_AGE_SECONDS}", calls)
        first_read = next(i for i, call in enumerate(calls) if call.startswith("kubectl get pods"))
        self.assertLess(first_read, calls.index(f"sleep {_AGE_SECONDS}"))
        self.assertTrue(all(f"--namespace {_NAMESPACE}" in call for call in calls if call.startswith("kubectl get pods")))

    def test_a_plant_that_never_fails_stops_the_apply_without_the_long_wait(self):
        reasons = {_WORKLOADS[0]: _FAILING, _WORKLOADS[1]: _PULLING}
        result, calls = self.run_wait(reasons, seen_timeout="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the plant did not take", result.stderr)
        self.assertNotIn(f"sleep {_AGE_SECONDS}", calls)

    def test_a_pod_that_leaves_the_failed_pull_during_the_wait_stops_the_apply(self):
        result, calls = self.run_wait(
            {workload: _FAILING for workload in _WORKLOADS},
            AFTER_WAIT_POD=f"{_pod_uid(_WORKLOADS[0])} {_PULLING}",
            AFTER_WAIT_SELECTOR=f"app={_WORKLOADS[0]}",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the ones seen before the wait", result.stderr)
        self.assertIn(f"sleep {_AGE_SECONDS}", calls)

    def test_a_pod_replaced_during_the_wait_stops_the_apply_though_its_successor_fails_too(self):
        # The check counts each pod's own wait: a pod recreated at minute nine has failed for two.
        result, _ = self.run_wait(
            {workload: _FAILING for workload in _WORKLOADS},
            AFTER_WAIT_POD=f"{_pod_uid(_WORKLOADS[1], generation=2)} {_FAILING}",
            AFTER_WAIT_SELECTOR=f"app={_WORKLOADS[1]}",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the ones seen before the wait", result.stderr)

    def test_the_stacks_wait_is_longer_than_the_checks_minimum(self):
        match = _AGE_VARIABLE_RE.search(_VARIABLES.read_text())
        self.assertIsNotNone(match, "failed_pull_age_seconds has no default in the stack's variables.tf")
        self.assertGreater(int(match.group(1)), _collector_minimum())


if __name__ == "__main__":
    unittest.main()
