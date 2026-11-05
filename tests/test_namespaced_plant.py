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

"""The namespaced-plant module's shell must never delete a namespace it did
not create, never leave one it did, and never read a failed read as "absent".

`bench/tf/modules/namespaced-plant/namespaced_plant.sh` is the shell the
namespace-scoped case stacks share for their namespace: the pre-clear, the
labelled create, the exit trap and the destroy. Its rules are pinned here
behaviourally, since a grep for a `delete namespace` line cannot tell a safe
one from an unsafe one: the script runs against stub `kubectl`, `gcloud`
and `rm` on PATH, and what is asserted is which commands it issues, in what
order, and with what exit.
"""

import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "bench" / "tf" / "modules" / "namespaced-plant" / "namespaced_plant.sh"

_NAMESPACE = "seeded-workloads"
_STACK = "workload-missing-memory-limits-oom-baseline"
_OTHER_STACK = "some-other-stack"
_MANIFEST = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: planted\n"

#: `kubectl` records every invocation to $CALLS, with stdin appended for the
#: create calls, and answers from the environment. NS_OWNER_OUT is what the
#: ownership read prints: empty for an absent namespace, "<name>/<label>" for
#: a present one. NS_READ_RC makes the read itself fail.
_KUBECTL_STUB = """#!/bin/bash
echo "kubectl $*" >> "$CALLS"
case "$*" in
  *"get namespace"*--ignore-not-found*)
    if [ "${NS_READ_RC:-0}" != "0" ]; then echo "Unable to connect to the server: getting credentials: exec: executable gke-gcloud-auth-plugin failed" >&2; exit "$NS_READ_RC"; fi
    printf '%s' "${NS_OWNER_OUT-}"; exit 0 ;;
  *"delete namespace"*)  exit "${NS_DELETE_RC:-0}" ;;
  *"create -f -"*)
    body="$(cat)"
    echo "stdin: $(printf '%s' "$body" | tr '\n' ' ')" >> "$CALLS"
    case "$body" in
      *"kind: Namespace"*) exit "${NS_CREATE_RC:-0}" ;;
      *) /bin/sleep "${MANIFEST_CREATE_SLEEP:-0}"; exit "${MANIFEST_CREATE_RC:-0}" ;;
    esac ;;
  *) exit 0 ;;
esac
"""

_GCLOUD_STUB = """#!/bin/bash
echo "gcloud $*" >> "$CALLS"
if [ "${GCLOUD_FAILS:-0}" = "1" ]; then
  echo "gcloud: could not fetch credentials" >&2
  exit 1
fi
exit 0
"""

#: `rm` records then does the real thing, so the trap's kubeconfig cleanup
#: can be asserted after a delete that failed inside the trap.
_RM_STUB = '#!/bin/bash\necho "rm $*" >> "$CALLS"\nexec /bin/rm "$@"\n'

#: The signal test's waits: for the plant to reach the manifest create, and for
#: the script to finish once signalled (bash runs the trap after the foreground
#: command returns, so the stub's sleep bounds the second wait).
_SIGNAL_SETTLE_SECONDS = 1.0
_SIGNAL_STUB_SLEEP_SECONDS = 3
_SIGNAL_EXIT = 143

_BASE_ENV = {
    "NP_HOST_CLUSTER": "platform-agent-host",
    "NP_HOST_LOCATION": "us-east4",
    "NP_PROJECT": "kube-agents-evals",
    "NP_NAMESPACE": _NAMESPACE,
    "NP_STACK": _STACK,
    "NP_MANAGED_BY": "kube-agents-bench",
    "NP_BUILD_ID": "2118356684249239552",
    "NP_PULL_NUMBER": "none",
    "NP_MANIFEST": _MANIFEST,
}


class NamespacedPlantTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)
        stub_dir = root / "bin"
        stub_dir.mkdir()
        for name, source in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB), ("rm", _RM_STUB)):
            stub = stub_dir / name
            stub.write_text(source)
            stub.chmod(0o755)
        self._stub_dir = stub_dir
        self._calls = root / "calls"

    def _run(self, verb, **scenario):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NP_")}
        env.update(_BASE_ENV)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env.update({k: str(v) for k, v in scenario.items()})
        completed = subprocess.run(["bash", str(_SCRIPT), verb], env=env, capture_output=True, text=True, timeout=60)
        calls = self._calls.read_text().splitlines() if self._calls.exists() else []
        return completed, calls

    @staticmethod
    def _deletes(calls):
        return [c for c in calls if "delete namespace" in c]

    @staticmethod
    def _creates(calls):
        return [c for c in calls if "create -f -" in c]

    def test_bash_syntax_is_valid(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True).returncode, 0)

    # -- plant --

    def test_a_clean_plant_creates_the_labelled_namespace_then_the_manifest_and_deletes_nothing(self):
        completed, calls = self._run("plant")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._deletes(calls), [])
        creates = self._creates(calls)
        self.assertEqual(len(creates), 2, calls)
        namespace_body = calls[calls.index(creates[0]) + 1]
        self.assertIn("kind: Namespace", namespace_body)
        for line in (f'name: "{_NAMESPACE}"', 'managed-by: "kube-agents-bench"', 'build-id: "2118356684249239552"', 'pull-number: "none"', f'created-by-stack: "{_STACK}"'):
            self.assertIn(line, namespace_body, "every label value is written quoted")
        self.assertIn(f"--namespace {_NAMESPACE} create -f -", creates[1])
        self.assertIn("kind: ConfigMap", calls[calls.index(creates[1]) + 1])

    def test_the_ownership_read_uses_ignore_not_found_and_the_name_slash_label_path(self):
        _, calls = self._run("plant")
        reads = [c for c in calls if "get namespace" in c]
        self.assertEqual(len(reads), 1, "one read, not two")
        self.assertIn("--ignore-not-found", reads[0])
        self.assertIn("{.metadata.name}/{.metadata.labels.created-by-stack}", reads[0])

    def test_a_leftover_namespace_of_this_stack_is_deleted_with_a_wait_before_the_create(self):
        completed, calls = self._run("plant", NS_OWNER_OUT=f"{_NAMESPACE}/{_STACK}")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        deletes = self._deletes(calls)
        self.assertEqual(len(deletes), 1)
        self.assertIn("--wait=true", deletes[0])
        self.assertLess(calls.index(deletes[0]), calls.index(self._creates(calls)[0]))

    def test_a_namespace_this_stack_did_not_create_is_refused_and_not_deleted(self):
        for owner_out in (f"{_NAMESPACE}/{_OTHER_STACK}", f"{_NAMESPACE}/"):
            with self.subTest(owner=owner_out):
                completed, calls = self._run("plant", NS_OWNER_OUT=owner_out)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("was not created by this stack", completed.stderr)
                self.assertEqual(self._deletes(calls), [])
                self.assertEqual(self._creates(calls), [])

    def test_a_failed_ownership_read_stops_the_plant_and_deletes_nothing(self):
        completed, calls = self._run("plant", NS_READ_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._deletes(calls), [])
        self.assertEqual(self._creates(calls), [])

    def test_a_failed_manifest_create_deletes_the_namespace_this_run_made(self):
        completed, calls = self._run("plant", MANIFEST_CREATE_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        deletes = self._deletes(calls)
        self.assertEqual(len(deletes), 1, calls)
        self.assertIn("--wait=false", deletes[0])
        self.assertGreater(calls.index(deletes[0]), calls.index(self._creates(calls)[0]))

    def test_a_failed_delete_inside_the_trap_still_removes_the_kubeconfig_dir(self):
        # `set +e` in the trap: errexit stays in force there, and without it
        # the failed delete would end the trap before the rm.
        completed, calls = self._run("plant", MANIFEST_CREATE_RC=1, NS_DELETE_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        deletes = self._deletes(calls)
        rms = [c for c in calls if c.startswith("rm -rf ")]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(len(rms), 1, calls)
        self.assertGreater(calls.index(rms[0]), calls.index(deletes[0]))

    def test_a_term_signal_during_the_plant_exits_143_and_deletes_the_namespace(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NP_")}
        env.update(_BASE_ENV)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env["MANIFEST_CREATE_SLEEP"] = str(_SIGNAL_STUB_SLEEP_SECONDS)
        proc = subprocess.Popen(["bash", str(_SCRIPT), "plant"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(_SIGNAL_SETTLE_SECONDS)
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=_SIGNAL_STUB_SLEEP_SECONDS * 10)
        calls = self._calls.read_text().splitlines() if self._calls.exists() else []
        self.assertEqual(proc.returncode, _SIGNAL_EXIT, stderr)
        deletes = self._deletes(calls)
        self.assertEqual(len(deletes), 1, calls)
        self.assertIn("--wait=false", deletes[0])
        self.assertGreater(calls.index(deletes[0]), calls.index(self._creates(calls)[0]))

    def test_a_refused_namespace_create_deletes_nothing(self):
        completed, calls = self._run("plant", NS_CREATE_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._deletes(calls), [], "planted_ns is set only after a successful create")

    def test_a_credentials_failure_stops_before_any_read(self):
        completed, calls = self._run("plant", GCLOUD_FAILS=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual([c for c in calls if c.startswith("kubectl")], [])

    def test_a_missing_input_is_named(self):
        completed, _ = self._run("plant", NP_MANIFEST="")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("NP_MANIFEST is not set", completed.stderr)

    # -- destroy --

    def test_destroy_of_an_absent_namespace_exits_zero_and_deletes_nothing(self):
        completed, calls = self._run("destroy")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._deletes(calls), [])
        self.assertIn("already gone", completed.stdout)

    def test_destroy_deletes_the_namespace_alone_when_the_label_is_this_stacks(self):
        completed, calls = self._run("destroy", NS_OWNER_OUT=f"{_NAMESPACE}/{_STACK}")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        deletes = self._deletes(calls)
        self.assertEqual(len(deletes), 1)
        self.assertIn("--wait=false", deletes[0])
        self.assertEqual([c for c in calls if "delete " in c and "delete namespace" not in c], [], "no per-object deletes: the namespace cascades")

    def test_destroy_leaves_a_foreign_namespace_in_place(self):
        completed, calls = self._run("destroy", NS_OWNER_OUT=f"{_NAMESPACE}/{_OTHER_STACK}")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._deletes(calls), [])
        self.assertIn("leaving it in place", completed.stdout)

    def test_destroy_treats_a_read_error_as_an_error_not_as_absent(self):
        completed, calls = self._run("destroy", NS_READ_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self._deletes(calls), [])
        self.assertNotIn("already gone", completed.stdout)

    def test_an_unknown_verb_is_refused(self):
        completed, _ = self._run("prune")
        self.assertEqual(completed.returncode, 2, "NP_EXIT_USAGE")


if __name__ == "__main__":
    unittest.main()
