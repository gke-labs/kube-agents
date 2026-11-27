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

"""The node-nf-conntrack-table-saturation stack must hand the audit the
kernel's line in Cloud Logging, or fail the apply.

`bench/tf/prebuilt/node-nf-conntrack-table-saturation` plants a node whose
connection tracking table was full and then waits (`wait_for_kernel_line.sh`)
until Cloud Logging holds the kernel's line from that node. The case that
follows grades GCE compute §2.6, which reads that line there. A wait that
returns before the line is stored, or over a line that an earlier run left,
turns the case red for a reason that is not the product's. The script runs
here against stub `gcloud` and `sleep` on PATH. Its log query is held equal to
the collector's, and the names that the stack, the script and the task file
repeat are held equal to each other.
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CASE = "node-nf-conntrack-table-saturation"
_STACK = _REPO_ROOT / "bench" / "tf" / "prebuilt" / _CASE
_SCRIPT = _STACK / "wait_for_kernel_line.sh"
_MAIN = _STACK / "main.tf"
_VARIABLES = _STACK / "variables.tf"
_TASK = _REPO_ROOT / "bench" / "tasks" / _CASE / "task.yaml"
_COLLECTOR_DIR = _REPO_ROOT / "agents" / "platform" / "skills" / "gce-compute-fleet-audit" / "scripts"

_PROJECT = "eval-project"
_CLUSTER = "bench-conntrack-0a1b2c3d"
_NODE = "gke-bench-conntrack-0a1b-default-pool-1234abcd-wxyz"
_EARLIER_NODE = "gke-bench-conntrack-9f8e-default-pool-5678efab-qrst"
_ENTRY_ID = "entry-1"
_POLL_SECONDS = "15"
_PLANT_WITNESS = "the-planted-node-is-still-there"
_OBJECTIVE = "ledger-files-conntrack-saturation-on-the-planted-node-pool"

#: `gcloud` records every call to $CALLS. The instance list answers with
#: $STATE/nodes. A log read answers with $STATE/line-<node> for the node that
#: its filter names, and with nothing for any other node.
_GCLOUD_STUB = """#!/bin/bash
echo "gcloud $*" >> "$CALLS"
if [ "$1 $2 $3" = "compute instances list" ]; then
  [ -f "$STATE/nodes" ] && cat "$STATE/nodes"
  exit 0
fi
if [ "$1 $2" = "logging read" ]; then
  for stored in "$STATE"/line-*; do
    [ -e "$stored" ] || continue
    node="${stored##*/line-}"
    case "$3" in *"=\\"$node\\""*) cat "$stored" ;; esac
  done
fi
exit 0
"""

#: `sleep` records and returns at once. It stores $AFTER_SLEEP_NODE's line
#: when the test sets one: the line that reaches Cloud Logging between polls.
_SLEEP_STUB = """#!/bin/bash
echo "sleep $*" >> "$CALLS"
if [ -n "${AFTER_SLEEP_NODE:-}" ]; then echo "$ENTRY_ID" > "$STATE/line-$AFTER_SLEEP_NODE"; fi
exit 0
"""


def _collector():
    sys.path.insert(0, str(_COLLECTOR_DIR))
    try:
        import compute_fleet_audit  # noqa: PLC0415 -- the path is set up above
    finally:
        sys.path.remove(str(_COLLECTOR_DIR))
    return compute_fleet_audit


def _local(name: str) -> str:
    """The quoted string value of `name` in the stack's locals."""
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', _MAIN.read_text(), re.MULTILINE)
    assert match, f"main.tf has no local {name} with a quoted value"
    return match.group(1)


def _script_constant(name: str) -> str:
    match = re.search(rf'^readonly {name}="([^"]+)"', _SCRIPT.read_text(), re.MULTILINE)
    assert match, f"wait_for_kernel_line.sh has no constant {name}"
    return match.group(1)


def _task_entry(name: str) -> dict:
    spec = yaml.safe_load(_TASK.read_text())["verification_spec"]
    return next(entry for entry in spec if entry["name"] == name)


class WaitForKernelLine(unittest.TestCase):
    def run_wait(self, nodes=(_NODE,), stored=(), timeout="600", **extra_env):
        """Run the script with `nodes` in the cluster and a stored line for each node in `stored`.

        Returns the result and the recorded calls.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bin_dir, state = root / "bin", root / "state"
            bin_dir.mkdir()
            state.mkdir()
            for name, body in (("gcloud", _GCLOUD_STUB), ("sleep", _SLEEP_STUB)):
                stub = bin_dir / name
                stub.write_text(body)
                stub.chmod(0o755)
            (state / "nodes").write_text("".join(f"{node}\n" for node in nodes))
            for node in stored:
                (state / f"line-{node}").write_text(f"{_ENTRY_ID}\n")
            calls = root / "calls"
            calls.touch()
            env = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "CALLS": str(calls),
                "STATE": str(state),
                "ENTRY_ID": _ENTRY_ID,
                "WAIT_PROJECT": _PROJECT,
                "WAIT_CLUSTER": _CLUSTER,
                "WAIT_TIMEOUT_SECONDS": timeout,
                **extra_env,
            }
            result = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False)
            return result, calls.read_text().splitlines()

    def test_it_returns_when_the_nodes_line_is_in_cloud_logging(self):
        result, calls = self.run_wait(stored=(_NODE,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(_NODE, result.stdout)
        self.assertFalse([call for call in calls if call.startswith("sleep")])

    def test_it_reads_the_nodes_of_the_planted_cluster_only(self):
        _, calls = self.run_wait(stored=(_NODE,))
        (listing,) = [call for call in calls if call.startswith("gcloud compute instances list")]
        self.assertIn(f"--project {_PROJECT}", listing)
        self.assertIn(f"labels.{_collector().GKE_CLUSTER_LABEL}={_CLUSTER}", listing)

    def test_its_log_query_is_the_collectors_narrowed_to_the_node(self):
        collector = _collector()
        _, calls = self.run_wait(stored=(_NODE,))
        (read,) = [call for call in calls if call.startswith("gcloud logging read")]
        narrowed = f'{collector.CONNTRACK_LOG_FILTER} AND labels."{collector.LOG_INSTANCE_NAME_LABEL}"="{_NODE}"'
        self.assertIn(f"gcloud logging read {narrowed} --project {_PROJECT}", read)

    def test_a_line_that_an_earlier_runs_node_left_does_not_answer_it(self):
        result, calls = self.run_wait(stored=(_EARLIER_NODE,), timeout="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("The plant did not take", result.stderr)
        self.assertFalse([call for call in calls if _EARLIER_NODE in call])

    def test_it_polls_until_the_line_arrives(self):
        result, calls = self.run_wait(AFTER_SLEEP_NODE=_NODE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call.startswith("sleep")], [f"sleep {_POLL_SECONDS}"])
        self.assertEqual(len([call for call in calls if call.startswith("gcloud logging read")]), 2)

    def test_a_cluster_with_no_node_listed_stops_the_apply_at_the_timeout(self):
        result, calls = self.run_wait(nodes=(), stored=(_NODE,), timeout="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn(_CLUSTER, result.stderr)
        self.assertFalse([call for call in calls if call.startswith("gcloud logging read")])

    def test_a_missing_input_stops_the_apply(self):
        result, _ = self.run_wait(WAIT_CLUSTER="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("WAIT_CLUSTER is not set", result.stderr)


class TheRepeatedNamesAgree(unittest.TestCase):
    def test_the_pod_writes_the_text_the_collector_reads(self):
        collector = _collector()
        self.assertEqual(_script_constant("WAIT_KERNEL_TEXT"), collector.CONNTRACK_FULL_TEXT)
        self.assertEqual(_script_constant("WAIT_SERIAL_LOG_ID"), collector.SERIAL_CONSOLE_LOG_ID)

    def test_the_timeout_message_names_the_planted_pod(self):
        self.assertEqual(_script_constant("WAIT_PLANT_NAMESPACE"), _local("plant_namespace"))
        self.assertEqual(_script_constant("WAIT_PLANT_POD"), _local("plant_pod"))

    def test_the_task_file_names_the_stacks_cluster_prefix(self):
        match = re.search(r'variable "cluster_name_prefix" \{.*?default\s*=\s*"([^"]+)"', _VARIABLES.read_text(), re.DOTALL)
        self.assertIsNotNone(match, "cluster_name_prefix has no default in the stack's variables.tf")
        prefix = match.group(1)
        task = yaml.safe_load(_TASK.read_text())
        self.assertEqual(task["infrastructure"]["variables"]["cluster_name_prefix"], prefix)
        self.assertIn(f"/{prefix}-", _task_entry(_OBJECTIVE)["check"]["required_phrases"])

    def test_the_plant_witness_selects_the_label_the_stack_sets(self):
        label = f"{_local('node_label_key')}={_local('node_label_value')}"
        self.assertEqual(_task_entry(_PLANT_WITNESS)["check"]["selector"], label)


if __name__ == "__main__":
    unittest.main()
