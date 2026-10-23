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

"""Offline tests for `bench/tf/modules/drift-inject/drift_inject.sh` (#3478).

`drift_inject.sh` holds the shell scaffolding that three `gitops-drift-*`
stacks share. Its Python siblings -- `test_drift_inject_payload.py` and
`test_drift_inject_call_sites.py` -- test the payload generator and the
Terraform invocation contracts, but the shell library itself was previously
untested.

`drift_clear_namespace` contains the only unconditional `kubectl delete
namespace` in any drift stack, running against the shared cluster where the
Platform Agent install lives. Its safety property hinges on validating the
`managed-by` label before issuing a delete. Similarly, the exit trap in
`drift_init` must only delete the namespace when `drift_planted_ns` is set.

Furthermore, `drift_require_daemon` verifies that the Session KV daemon's
`/healthz` endpoint advertises the `gitops-drift` inject kind, and must
carefully distinguish between a transport/curl/exec failure and an active
daemon that lacks the requested capability.

These tests lift functions from `drift_inject.sh` using `tests/_lift_shell.py`
and exercise them against a stubbed `kubectl` and `curl` without
requiring a real cluster or live network calls.
"""

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from _lift_shell import lift_function  # noqa: E402
from tests.testing.common import get_isolated_test_env  # noqa: E402

MODULE_DIR = REPO_ROOT / "bench" / "tf" / "modules" / "drift-inject"
SCRIPT = MODULE_DIR / "drift_inject.sh"

_STUB_KUBECTL_SCRIPT = """\
#!/usr/bin/env python3
import json
import os
import subprocess
import sys

log_path = os.environ.get("KUBECTL_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")

config_path = os.environ.get("KUBECTL_CONFIG")
config = {}
if config_path and os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

argv = sys.argv[1:]
if not argv:
    sys.exit(0)

cmd = argv[0]

if cmd == "get":
    if len(argv) >= 3 and argv[1] == "namespace":
        ns = argv[2]
        ns_info = config.get("namespaces", {}).get(ns)
        if ns_info is None or not ns_info.get("exists", False):
            sys.stderr.write(f'Error from server (NotFound): namespaces "{ns}" not found\\n')
            sys.exit(1)

        # Check jsonpath expressions
        for i, arg in enumerate(argv[3:]):
            expr = None
            if arg.startswith("jsonpath=") or arg.startswith("-o=jsonpath="):
                expr = arg.split("jsonpath=", 1)[1].strip("'\\"")
            elif arg in ("-o", "--output") and (i + 4) < len(argv):
                next_arg = argv[i + 4]
                if next_arg.startswith("jsonpath="):
                    expr = next_arg.split("jsonpath=", 1)[1].strip("'\\"")
            if expr is not None:
                read_rc = ns_info.get("read_exit_code", 0)
                if read_rc != 0:
                    sys.stderr.write(ns_info.get("read_error", f'Error from server: failed to get namespace "{ns}"\\n'))
                    sys.exit(read_rc)
                if expr == "{.metadata.labels.managed-by}":
                    owner = ns_info.get("managed-by", "")
                    sys.stdout.write(owner)
                else:
                    # Key fixture strictly on the expected expression
                    sys.stdout.write("")
                sys.exit(0)
            elif ("-o" in arg or "--output" in arg) and "yaml" in arg:
                sys.stdout.write(f"apiVersion: v1\\nkind: Namespace\\nmetadata:\\n  name: {ns}\\n")
                sys.exit(0)
        sys.exit(0)
    else:
        # Generic get (e.g. failure resources or events in exit trap)
        get_rc = config.get("get_exit_code", 0)
        if get_rc != 0:
            sys.stderr.write(config.get("get_error", "Error from server: generic get failed\\n"))
            sys.exit(get_rc)
        sys.stdout.write("NAME READY STATUS AGE\\n")
        sys.exit(0)

elif cmd == "delete":
    if len(argv) >= 3 and argv[1] == "namespace":
        rc = config.get("delete_exit_code", 0)
        if rc != 0:
            sys.stderr.write("timed out waiting for the condition\\n")
            sys.exit(rc)
        ns = argv[2]
        sys.stdout.write(f'namespace "{ns}" deleted\\n')
        sys.exit(0)

elif cmd == "exec":
    # If a simulated kubectl-level failure is configured (e.g. apiserver unreachable / pod not found)
    if "kubectl_exec_error" in config:
        rc = config.get("kubectl_exec_exit_code", 1)
        sys.stderr.write(config.get("kubectl_exec_error", "Error from server: pod not found\\n"))
        sys.exit(rc)

    # Subshell execution of command after '--'
    if "--" in argv:
        subcmd = argv[argv.index("--") + 1:]
    else:
        subcmd = argv[1:]

    if subcmd:
        res = subprocess.run(subcmd)
        sys.exit(res.returncode)
    sys.exit(0)

sys.exit(0)
"""

_STUB_CURL_SCRIPT = """\
#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ.get("CURL_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")

config_path = os.environ.get("KUBECTL_CONFIG")
config = {}
if config_path and os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

curl_cfg = config.get("curl", {})

# Check if transport/curl failure configured
exit_code = curl_cfg.get("exit_code", 0)
if exit_code != 0:
    err_msg = curl_cfg.get("stderr", f"curl: ({exit_code}) Failed to connect to host\\n")
    sys.stderr.write(err_msg)
    sys.exit(exit_code)

# Check HTTP status code and fail flags
http_code = curl_cfg.get("http_code", 200)
argv = sys.argv[1:]
fail_flag = any(a in ("-f", "--fail") or (a.startswith("-") and not a.startswith("--") and "f" in a) for a in argv)
if fail_flag and http_code >= 400:
    sys.stderr.write(f"curl: (22) The requested URL returned error: {http_code}\\n")
    sys.exit(22)

# Output response body
body = curl_cfg.get("body", "")
sys.stdout.write(body)
sys.exit(0)
"""


class DriftShellTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bin_dir = pathlib.Path(self.temp_dir.name) / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

        self.kubectl_bin = self.bin_dir / "kubectl"
        self.kubectl_bin.write_text(_STUB_KUBECTL_SCRIPT, encoding="utf-8")
        self.kubectl_bin.chmod(self.kubectl_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.curl_bin = self.bin_dir / "curl"
        self.curl_bin.write_text(_STUB_CURL_SCRIPT, encoding="utf-8")
        self.curl_bin.chmod(self.curl_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.log_file = pathlib.Path(self.temp_dir.name) / "kubectl_calls.log"
        self.curl_log_file = pathlib.Path(self.temp_dir.name) / "curl_calls.log"
        self.config_file = pathlib.Path(self.temp_dir.name) / "kubectl_config.json"
        self.script_text = SCRIPT.read_text(encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def set_kubectl_config(self, config: dict):
        self.config_file.write_text(json.dumps(config), encoding="utf-8")

    def get_kubectl_calls(self) -> list[list[str]]:
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def get_curl_calls(self) -> list[list[str]]:
        if not self.curl_log_file.exists():
            return []
        lines = self.curl_log_file.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def run_bash(
        self, script: str, extra_env: dict | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        overrides = {
            "KUBECTL_LOG": str(self.log_file),
            "CURL_LOG": str(self.curl_log_file),
            "KUBECTL_CONFIG": str(self.config_file),
        }
        if extra_env:
            overrides.update(extra_env)
        env = get_isolated_test_env(
            overrides=overrides,
            bin_dir=str(self.bin_dir),
            absent=("drift_planted_ns", "drift_failure_resources"),
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    def test_run_bash_isolates_ambient_drift_env_variables(self):
        old_planted = os.environ.get("drift_planted_ns")
        old_failure = os.environ.get("drift_failure_resources")
        os.environ["drift_planted_ns"] = "ambient_ns"
        os.environ["drift_failure_resources"] = "ambient_res"
        try:
            proc = self.run_bash('echo "planted: ${drift_planted_ns:-empty}, failure: ${drift_failure_resources:-empty}"')
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "planted: empty, failure: empty")
        finally:
            if old_planted is not None:
                os.environ["drift_planted_ns"] = old_planted
            else:
                os.environ.pop("drift_planted_ns", None)
            if old_failure is not None:
                os.environ["drift_failure_resources"] = old_failure
            else:
                os.environ.pop("drift_failure_resources", None)


class DriftClearNamespaceTests(DriftShellTestBase):
    def test_absent_namespace_is_noop_returning_zero(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {"exists": False}
            }
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 0, f"Expected 0, stderr: {proc.stderr}")
        calls = self.get_kubectl_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["get", "namespace", "eval-ns"])
        # Ensure delete was never called
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 0)

    def test_namespace_labelled_by_another_owner_is_refused_and_not_deleted(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {
                    "exists": True,
                    "managed-by": "someone-else",
                }
            }
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 1, f"Expected 1, proc: {proc}")
        self.assertIn("ERROR: eval-ns already exists on eval-cluster", proc.stderr)
        self.assertIn("not labelled managed-by=eval-drift (found 'someone-else')", proc.stderr)
        self.assertIn("This stack did not create it, so it will not delete it", proc.stderr)

        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 0, "kubectl delete was called on a foreign namespace")

    def test_namespace_with_no_managed_by_label_is_refused_and_not_deleted(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {
                    "exists": True,
                    "managed-by": "",
                }
            }
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not labelled managed-by=eval-drift (found '')", proc.stderr)
        self.assertIn("This stack did not create it, so it will not delete it", proc.stderr)

        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 0)

    def test_namespace_labelled_by_managed_by_is_deleted(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {
                    "exists": True,
                    "managed-by": "eval-drift",
                }
            },
            "delete_exit_code": 0,
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 0, f"Expected 0, stderr: {proc.stderr}")
        self.assertIn("Found a leftover eval-ns from an earlier run; deleting it before planting.", proc.stdout)

        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(
            delete_calls[0],
            ["delete", "namespace", "eval-ns", "--ignore-not-found", "--wait=true", "--timeout=30s"],
        )

    def test_namespace_read_pins_managed_by_jsonpath_query(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {
                    "exists": True,
                    "managed-by": "eval-drift",
                }
            },
            "delete_exit_code": 0,
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 0)
        calls = self.get_kubectl_calls()
        get_calls = [c for c in calls if len(c) > 1 and c[0] == "get" and c[1] == "namespace" and "-o" in c]
        self.assertEqual(len(get_calls), 1)
        # Check that the jsonpath requested is specifically {.metadata.labels.managed-by}
        self.assertEqual(
            get_calls[0],
            ["get", "namespace", "eval-ns", "-o", "jsonpath={.metadata.labels.managed-by}"],
        )

    def test_delete_failure_reports_timeout_and_dumps_yaml(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {
                    "exists": True,
                    "managed-by": "eval-drift",
                }
            },
            "delete_exit_code": 1,
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: could not clear the leftover eval-ns within 30s", proc.stderr)
        self.assertIn("A namespace wedged on a finalizer is the usual cause", proc.stderr)

        calls = self.get_kubectl_calls()
        yaml_calls = [c for c in calls if "yaml" in " ".join(c)]
        self.assertEqual(len(yaml_calls), 1)

    def test_namespace_second_read_failure_aborts_without_misreporting_label(self):
        self.set_kubectl_config({
            "namespaces": {
                "eval-ns": {
                    "exists": True,
                    "read_exit_code": 1,
                    "read_error": "Error from server (ServiceUnavailable): apiserver connection lost\\n",
                }
            }
        })
        func = lift_function("drift_clear_namespace", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_managed_by="eval-drift"
drift_leftover_timeout=30

{func}

drift_clear_namespace
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Error from server (ServiceUnavailable): apiserver connection lost", proc.stderr)
        self.assertNotIn("not labelled managed-by=", proc.stderr)
        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 0)


class DriftOnExitTrapTests(DriftShellTestBase):
    def test_exit_trap_deletes_when_status_nonzero_and_drift_planted_ns_set(self):
        script = f"""\
set -euo pipefail
# Stub gcloud to avoid network calls during drift_init
gcloud() {{ return 0; }}

drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_location="us-central1"
drift_project_id="test-project"
drift_agent_deployment="agent"
drift_agent_namespace="kubeagents-system"
drift_agent_container="platform-agent"
drift_planted_ns=""

source "{SCRIPT}"

drift_init
drift_planted_ns="1"

echo "Simulating a plant failure..." >&2
exit 42
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 42)
        self.assertIn("Plant failed (exit 42). State of eval-ns before cleanup:", proc.stderr)
        self.assertIn("Deleting eval-ns so the next run starts from a clean namespace.", proc.stderr)

        calls = self.get_kubectl_calls()
        dump_calls = [c for c in calls if len(c) > 0 and c[0] == "get" and c[1] != "namespace"]
        self.assertEqual(len(dump_calls), 2)
        self.assertEqual(
            dump_calls[0],
            ["get", "deployments,replicasets,pods", "-n", "eval-ns", "-o", "wide"],
        )
        self.assertEqual(
            dump_calls[1],
            ["get", "events", "-n", "eval-ns", "--sort-by=.metadata.creationTimestamp"],
        )

        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(
            delete_calls[0],
            ["delete", "namespace", "eval-ns", "--ignore-not-found", "--wait=false"],
        )

    def test_exit_trap_does_not_delete_when_drift_planted_ns_unset(self):
        script = f"""\
set -euo pipefail
gcloud() {{ return 0; }}

drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_location="us-central1"
drift_project_id="test-project"
drift_agent_deployment="agent"
drift_agent_namespace="kubeagents-system"
drift_agent_container="platform-agent"

source "{SCRIPT}"

drift_init

echo "Simulating pre-plant failure..." >&2
exit 1
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Deleting eval-ns", proc.stderr)

        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 0)

    def test_exit_trap_does_not_delete_when_status_zero(self):
        script = f"""\
set -euo pipefail
gcloud() {{ return 0; }}

drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_location="us-central1"
drift_project_id="test-project"
drift_agent_deployment="agent"
drift_agent_namespace="kubeagents-system"
drift_agent_container="platform-agent"

source "{SCRIPT}"

drift_init
drift_planted_ns="1"

echo "Normal exit"
exit 0
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Deleting eval-ns", proc.stderr)

        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 0)

    def test_exit_trap_deletes_even_when_failure_resource_dump_fails(self):
        self.set_kubectl_config({
            "get_exit_code": 1,
            "get_error": "Error from server (Forbidden): cannot list resource deployments in API group\\n",
        })
        script = f"""\
set -euo pipefail
gcloud() {{ return 0; }}

drift_ns="eval-ns"
drift_cluster="eval-cluster"
drift_location="us-central1"
drift_project_id="test-project"
drift_agent_deployment="agent"
drift_agent_namespace="kubeagents-system"
drift_agent_container="platform-agent"
drift_planted_ns=""

source "{SCRIPT}"

drift_init
drift_planted_ns="1"

echo "Simulating a plant failure..." >&2
exit 42
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 42)
        self.assertIn("Plant failed (exit 42). State of eval-ns before cleanup:", proc.stderr)
        self.assertIn("Error from server (Forbidden): cannot list resource deployments in API group", proc.stderr)
        self.assertIn("Deleting eval-ns so the next run starts from a clean namespace.", proc.stderr)

        calls = self.get_kubectl_calls()
        delete_calls = [c for c in calls if len(c) > 0 and c[0] == "delete"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(
            delete_calls[0],
            ["delete", "namespace", "eval-ns", "--ignore-not-found", "--wait=false"],
        )


class DriftRequireDaemonTests(DriftShellTestBase):
    def setUp(self):
        super().setUp()
        if not shutil.which("jq"):
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail("jq is not on PATH; the drift_require_daemon tests cannot run")
            self.skipTest("jq is not on PATH")

    _DAEMON_SCRIPT = f"""\
set -euo pipefail
gcloud() {{ return 0; }}

drift_cluster="eval-cluster"
drift_location="us-central1"
drift_project_id="test-project"
drift_agent_deployment="agent"
drift_agent_namespace="kubeagents-system"
drift_agent_container="platform-agent"
drift_daemon_url="http://daemon:8080"
drift_planted_ns=""

source "{SCRIPT}"

drift_init
drift_require_daemon
"""

    def run_daemon(self) -> subprocess.CompletedProcess:
        return self.run_bash(self._DAEMON_SCRIPT)

    def test_refuses_when_transport_failure_and_explains_distinction(self):
        self.set_kubectl_config({
            "curl": {
                "exit_code": 7,
                "stderr": "curl: (7) Failed to connect to daemon:8080: Connection refused\\n",
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: could not read http://daemon:8080/healthz in platform-agent (exit 7)", proc.stderr)
        self.assertIn("curl: (7) Failed to connect to daemon:8080", proc.stderr)
        self.assertIn("This says nothing about which inject kinds the daemon advertises", proc.stderr)
        self.assertIn("it is an exec, RBAC, connection or HTTP failure, and the install may be perfectly current.", proc.stderr)
        self.assertNotIn("does not advertise the 'gitops-drift' inject kind", proc.stderr)

    def test_refuses_when_http_404_or_500_with_fail_flag_and_explains_distinction(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 404,
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: could not read http://daemon:8080/healthz in platform-agent (exit 22)", proc.stderr)
        self.assertIn("curl: (22) The requested URL returned error: 404", proc.stderr)
        self.assertIn("This says nothing about which inject kinds the daemon advertises", proc.stderr)
        self.assertNotIn("does not advertise the 'gitops-drift' inject kind", proc.stderr)

    def test_refuses_when_kubectl_exec_fails_before_container(self):
        self.set_kubectl_config({
            "kubectl_exec_error": 'Error from server (NotFound): pods "agent" not found\\n',
            "kubectl_exec_exit_code": 1,
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: could not read http://daemon:8080/healthz in platform-agent (exit 1)", proc.stderr)
        self.assertIn('Error from server (NotFound): pods "agent" not found', proc.stderr)
        self.assertIn("This says nothing about which inject kinds the daemon advertises", proc.stderr)
        self.assertNotIn("does not advertise the 'gitops-drift' inject kind", proc.stderr)

    def test_refuses_when_daemon_does_not_advertise_gitops_drift(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps({"inject_kinds": ["pod-event", "warning-event"]}),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: the Session KV daemon at http://daemon:8080 does not advertise the 'gitops-drift' inject kind", proc.stderr)
        self.assertIn("(it advertises: pod-event\nwarning-event)", proc.stderr)
        self.assertIn("This install predates the drift dispatch", proc.stderr)
        self.assertNotIn("could not read http://daemon:8080/healthz", proc.stderr)

    def test_refuses_when_daemon_advertises_near_match_kind_without_exact_match(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps({"inject_kinds": ["gitops-drift-v2", "pod-event"]}),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: the Session KV daemon at http://daemon:8080 does not advertise the 'gitops-drift' inject kind", proc.stderr)
        self.assertIn("(it advertises: gitops-drift-v2\npod-event)", proc.stderr)
        self.assertNotIn("could not read http://daemon:8080/healthz", proc.stderr)

    def test_refuses_when_daemon_returns_scalar_inject_kinds(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps({"inject_kinds": "gitops-drift"}),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: the Session KV daemon at http://daemon:8080 does not advertise the 'gitops-drift' inject kind", proc.stderr)
        self.assertNotIn("could not read http://daemon:8080/healthz", proc.stderr)

    def test_refuses_when_daemon_returns_non_json_body(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": "<html>bad gateway</html>",
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: could not read http://daemon:8080/healthz in platform-agent (exit 5)", proc.stderr)
        self.assertIn("This says nothing about which inject kinds the daemon advertises", proc.stderr)

    def test_refuses_when_daemon_returns_non_object_json_body(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps(["gitops-drift"]),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: could not read http://daemon:8080/healthz in platform-agent (exit 5)", proc.stderr)
        self.assertIn("This says nothing about which inject kinds the daemon advertises", proc.stderr)

    def test_refuses_when_daemon_returns_null_body(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": "null",
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: the Session KV daemon at http://daemon:8080 does not advertise the 'gitops-drift' inject kind", proc.stderr)
        self.assertNotIn("could not read http://daemon:8080/healthz", proc.stderr)

    def test_succeeds_when_daemon_advertises_gitops_drift(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps({"inject_kinds": ["pod-event", "gitops-drift", "warning-event"]}),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 0, f"Expected 0, stderr: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_succeeds_when_daemon_advertises_object_valued_inject_kinds(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps({"inject_kinds": {"primary": "gitops-drift"}}),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 0, f"Expected 0, stderr: {proc.stderr}")
        self.assertEqual(proc.stderr, "")

    def test_exec_invocation_contract(self):
        self.set_kubectl_config({
            "curl": {
                "http_code": 200,
                "body": json.dumps({"inject_kinds": ["gitops-drift"]}),
            }
        })
        proc = self.run_daemon()
        self.assertEqual(proc.returncode, 0)
        calls = self.get_kubectl_calls()
        self.assertEqual(len(calls), 1)
        exec_call = calls[0]
        # Validates that drift_init built drift_exec_in_pod from module code
        self.assertEqual(
            exec_call[:7],
            ["exec", "deployment/agent", "-n", "kubeagents-system", "-c", "platform-agent", "--"],
        )
        self.assertEqual(exec_call[7], "sh")
        self.assertEqual(exec_call[8], "-c")
        # Ensure script body preserves curl -fsS, || exit $?, and jq pipeline
        sh_script = exec_call[9]
        self.assertIn("curl -fsS", sh_script)
        self.assertIn("|| exit $?", sh_script)
        self.assertIn('jq -r ".inject_kinds[]?"', sh_script)
        self.assertEqual(exec_call[10:], ["sh", "http://daemon:8080", "10"])

        curl_calls = self.get_curl_calls()
        self.assertEqual(len(curl_calls), 1)
        self.assertEqual(
            curl_calls[0],
            ["-fsS", "--max-time", "10", "http://daemon:8080/healthz"],
        )


class DriftInjectModuleStructureTests(DriftShellTestBase):
    _EXPECTED_FUNCTIONS = (
        "drift_init",
        "drift_clear_namespace",
        "drift_require_daemon",
        "drift_mint_insert_id",
        "drift_build_payload",
        "drift_inject_record",
        "drift_wait_for_card",
        "drift_probe_board",
        "drift_report_probe_error",
    )

    def test_sourcing_drift_inject_sh_runs_no_commands_and_defines_constants(self):
        script = f"""\
set -euo pipefail
source "{SCRIPT}"
printf "KIND=%s\\n" "$DRIFT_INJECT_KIND"
printf "TIMEOUT=%s\\n" "$DRIFT_HEALTHZ_TIMEOUT"
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 0, f"Expected 0, stderr: {proc.stderr}")
        self.assertIn("KIND=gitops-drift", proc.stdout)
        self.assertIn("TIMEOUT=10", proc.stdout)
        # Sourcing must not have called kubectl
        self.assertEqual(len(self.get_kubectl_calls()), 0)

    def test_all_expected_functions_are_liftable(self):
        for name in self._EXPECTED_FUNCTIONS:
            with self.subTest(function=name):
                func = lift_function(name, self.script_text, SCRIPT)
                self.assertTrue(func.startswith(f"{name}() {{"))
                self.assertTrue(func.rstrip().endswith("}"))

    def test_mint_insert_id_format(self):
        func = lift_function("drift_mint_insert_id", self.script_text, SCRIPT)
        script = f"""\
set -euo pipefail
{func}
drift_mint_insert_id "eval-drift"
printf "ID=%s\\n" "$drift_insert_id"
printf "AT=%s\\n" "$drift_changed_at"
"""
        proc = self.run_bash(script)
        self.assertEqual(proc.returncode, 0, f"Expected 0, stderr: {proc.stderr}")

        m_id = re.search(r"^ID=(eval-drift-\d+-\d+)$", proc.stdout, re.M)
        self.assertIsNotNone(m_id, f"Invalid insert_id in: {proc.stdout}")

        m_at = re.search(r"^AT=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$", proc.stdout, re.M)
        self.assertIsNotNone(m_at, f"Invalid changed_at in: {proc.stdout}")



# A stand-in for the pod's interpreter. drift_wait_for_card runs
# `"$pod_python" - "$insert_id" < board_probe.py` through kubectl exec, and the
# kubectl stub above runs whatever follows `--` locally, so this is what answers
# each board read. It replies with the next line of FAKE_PROBE_REPLIES each time
# and repeats the last one when they run out; a reply of `FAIL` exits non-zero
# with a message on stderr, the shape of an exec into a pod that has gone.
_FAKE_POD_PYTHON = """\
#!/usr/bin/env python3
import os, sys
if len(sys.argv) > 1 and sys.argv[1] == "-c":
    sys.exit(0)
sys.stdin.read()
replies = os.environ["FAKE_PROBE_REPLIES"].split("|")
state = os.environ["FAKE_PROBE_STATE"]
n = int(open(state).read()) if os.path.exists(state) else 0
open(state, "w").write(str(n + 1))
reply = replies[min(n, len(replies) - 1)]
if reply == "FAIL":
    sys.stderr.write("error: unable to upgrade connection: pod does not exist\\n")
    sys.exit(1)
sys.stdout.write(reply + "\\n")
"""


# How long one scripted wait may take before it counts as one that never ends.
WAIT_TEST_CEILING_SECONDS = 30


class DriftWaitForCardTests(DriftShellTestBase):
    """The two-stage wait: the card appears, then it finishes, then the turn starts.

    The finish half exists because an agent left to poll a running card is
    stopped by the runtime's repetition filter (#3761), so what these pin is
    that the fixture does not hand over early, does not wait for ever, and does
    not misreport an exec failure as a slow card.
    """

    def setUp(self):
        super().setUp()
        self.fake_python = self.bin_dir / "fake-pod-python"
        self.fake_python.write_text(_FAKE_POD_PYTHON, encoding="utf-8")
        self.fake_python.chmod(self.fake_python.stat().st_mode | stat.S_IXUSR)
        self.state = pathlib.Path(self.temp_dir.name) / "probe_reads"
        self.set_kubectl_config({})

    def wait(self, replies, card_wait=5, finish=5, interval=0):
        script = f"""\
set -euo pipefail
source "{SCRIPT}"
DRIFT_POD_PYTHON_CANDIDATES="{self.fake_python}"
drift_kubeconfig_dir="{self.temp_dir.name}"
drift_agent_container=platform-agent
drift_insert_id=eval-drift-1-2
drift_card_wait_timeout={card_wait}
drift_card_finish_timeout={finish}
drift_poll_interval={interval}
drift_exec_in_pod=(kubectl exec deploy/x -n ns -c c --)
drift_exec_in_pod_stdin=(kubectl exec -i deploy/x -n ns -c c --)
drift_wait_for_card
echo "RETURNED $?"
"""
        # A loop that stops reaching its bound must fail as a named test, not
        # hold the bench job to its own deadline: every scripted wait here ends
        # in a second or two, so this ceiling only ever fires on a regression.
        return self.run_bash(
            script,
            extra_env={"FAKE_PROBE_REPLIES": "|".join(replies), "FAKE_PROBE_STATE": str(self.state)},
            timeout=WAIT_TEST_CEILING_SECONDS,
        )

    def reads(self):
        return int(self.state.read_text()) if self.state.exists() else 0

    def test_waits_through_appearance_and_running_until_finished(self):
        proc = self.wait(["0 0", "1 0", "1 0", "1 1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("appeared after", proc.stdout)
        self.assertIn("finished after a further", proc.stdout)
        self.assertIn("RETURNED 0", proc.stdout)
        self.assertEqual(self.reads(), 4)

    def test_a_card_already_finished_when_it_appears_is_not_probed_twice(self):
        """The appearance read carries `finished`; re-probing it would cost an exec."""
        proc = self.wait(["1 1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("finished after a further", proc.stdout)
        self.assertEqual(self.reads(), 1)

    def test_a_single_token_is_not_read_as_found_and_finished(self):
        """A reply cut off mid-line must read as nothing yet, not as `1 1`."""
        proc = self.wait(["1", "1 0", "1 1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.reads(), 3)

    def test_malformed_replies_read_as_nothing_yet(self):
        for bad in ("", "x y", "1 x", "1 2 3", "-1 1"):
            with self.subTest(reply=bad):
                if self.state.exists():
                    self.state.unlink()
                proc = self.wait([bad, "1 1"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.reads(), 2)

    def test_a_card_that_ended_without_a_result_fails_the_plant(self):
        """Failed or cancelled is the install's failure: exclude the rep, do not grade it."""
        proc = self.wait(["1 0", "1 2"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ended failed or cancelled", proc.stderr)
        self.assertNotIn("RETURNED", proc.stdout)
        self.assertEqual(self.reads(), 2)

    def test_a_board_without_status_hands_over_at_once_with_a_warning(self):
        proc = self.wait(["1 -1"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no status column", proc.stderr)
        self.assertEqual(self.reads(), 1)

    def test_a_card_still_running_at_the_ceiling_is_handed_over_not_failed(self):
        proc = self.wait(["1 0"], finish=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not seen to finish", proc.stderr)
        self.assertIn("RETURNED 0", proc.stdout)

    def test_an_exec_failure_at_the_finish_ceiling_is_reported_not_taken_for_a_slow_card(self):
        """A pod that rolled mid-wait must say so, not read as a card that never finished."""
        proc = self.wait(["1 0", "FAIL"], finish=1, interval=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not seen to finish", proc.stderr)
        self.assertIn("The board probe's last run exited 1", proc.stderr)
        self.assertIn("pod does not exist", proc.stderr)

    def test_an_exec_failure_at_the_appearance_ceiling_is_reported_and_fails(self):
        proc = self.wait(["FAIL"], card_wait=0)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no kanban card carrying insert_id", proc.stderr)
        self.assertIn("The board probe's last run exited 1", proc.stderr)

    def test_a_non_integer_bound_fails_at_once_instead_of_polling_for_ever(self):
        # A digit string too wide for the shell's integers is as bad as a word:
        # `[ -ge ]` errors on it, which reads as false, so the bound is never met.
        cases = [(field, value) for field in ("card_wait", "finish", "interval")
                 for value in ("abc", "99999999999999999999")]
        for field, value in cases:
            with self.subTest(bound=field, value=value):
                if self.state.exists():
                    self.state.unlink()
                proc = self.wait(["1 0"], **{field: value})
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("must be whole seconds", proc.stderr)
                self.assertEqual(self.reads(), 0)


if __name__ == "__main__":
    unittest.main()
