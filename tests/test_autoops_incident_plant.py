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

"""The autoops-incident plant must never leave its namespace behind, and must
delete a leftover one before planting.

`bench/tf/prebuilt/autoops-incident/main.tf` plants a crash-looping pod and
waits for `k8s-event-watcher` to open an incident for it. Both halves tested
here exist because of #1143, where the case failed on every run against three
leased projects and no agent ever executed:

  1. A failed plant left `eval-autoops-incident` behind. The teardown that
     should have removed it is a destroy-time provisioner, and Terraform skips
     those on a resource its create-time provisioner tainted -- so the failure
     path is exactly the path with no cleanup.
  2. The leftover namespace then defeated every later run. `kubectl apply` on
     an unchanged Deployment reports `unchanged` and starts no new pod, so the
     pod kept its UID; the watcher's dedup key is {UID, Reason}, its window for
     that pod was long open, and it logged `dedup` where the plant was waiting
     for `fire`.

Neither shows up as an error. The plant reports a healthy debounce and then
times out five minutes later, which is why this is pinned behaviourally rather
than by reading the file: a grep for `delete namespace` would have passed
against the code that shipped the bug.

The provisioner is shell inside HCL, and no Terraform binary is assumed here,
so the heredoc is rendered the way Terraform renders it and run against stub
`kubectl`/`gcloud`/`sleep` on PATH. What is asserted is which commands the
plant issues and in what order, never a real cluster.
"""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE = _REPO_ROOT / "bench" / "tf" / "prebuilt" / "autoops-incident" / "main.tf"

#: The create-time provisioner is the first `command = <<-EOT` block in the
#: module; the destroy-time one is the second.
_HEREDOC_RE = re.compile(r"command\s*=\s*<<-EOT\n(.*?)\n\s*EOT\n", re.S)
_PLANT_BLOCK = 0

#: Stand-ins for the module's interpolations. Only the values the assertions
#: read back need to be realistic; the rest just have to render.
_NAMESPACE = "eval-autoops-incident"
_WORKLOAD = "eval-incident-workload"
_INTERPOLATIONS = {
    "local.ns": _NAMESPACE,
    "local.workload": _WORKLOAD,
    "local.kubectl": f"kubectl --namespace={_NAMESPACE}",
    "local.ci_labels[\"managed-by\"]": "kube-agents-bench",
    "local.ci_labels[\"build-id\"]": "test-build",
    "local.ci_labels[\"pull-number\"]": "none",
    "var.project_id": "kube-agents-evals",
    "var.host_cluster_name": "platform-agent-host",
    "var.host_cluster_location": "us-central1",
    "var.agent_deployment": "platform-agent-gateway",
    "var.agent_namespace": "kubeagents-system",
    "var.agent_container": "envoy-credential-proxy",
    "var.allocate_mib": "96",
    "var.memory_limit_mib": "64",
}

#: The label step 1 writes, and the value step 0b requires before it will
#: delete anything.
_OWNER_LABEL = "kube-agents-bench"

#: `kubectl` records every invocation to $CALLS and answers from the
#: environment, so a test picks a scenario by setting these. Defaults are the
#: healthy path: no leftover namespace, debounce already cleared, watcher fires.
#: `NS_OWNER` uses `${VAR-default}` rather than `${VAR:-default}` so a test can
#: set it empty to mean "the namespace carries no managed-by label".
_KUBECTL_STUB = """#!/bin/bash
echo "kubectl $*" >> "$CALLS"
case "$*" in
  *"get namespace"*jsonpath*)   echo "${NS_OWNER-%(owner)s}"; exit 0 ;;
  *"get namespace"*"-o yaml"*)  echo "kind: Namespace"; exit 0 ;;
  *"get namespace"*)            exit "${NS_EXISTS_RC:-1}" ;;
  *"delete namespace"*)         exit "${NS_DELETE_RC:-0}" ;;
  *"apply -f -"*)               exit "${APPLY_RC:-0}" ;;
  *"get events"*)               echo 7; exit 0 ;;
  *"logs "*)
    if [ "${WATCHER_FIRES:-1}" = "1" ]; then
      echo "fire BackOff pod=%(ns)s/%(workload)s-abc123 -> sid=s1 (mode=live)"
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
""" % {"ns": _NAMESPACE, "workload": _WORKLOAD, "owner": _OWNER_LABEL}

_GCLOUD_STUB = """#!/bin/bash
echo "gcloud $*" >> "$CALLS"
if [ "${GCLOUD_FAILS:-0}" = "1" ]; then
  echo "gcloud: could not fetch credentials" >&2
  exit 1
fi
exit 0
"""

#: The plant polls on 10-second sleeps and its timeouts are minutes long.
#: `STUB_SLEEP` buys back a little real time for the signal test, which needs
#: the script still running when it sends SIGTERM.
_SLEEP_STUB = '#!/bin/bash\nexec /bin/sleep "${STUB_SLEEP:-0}"\n'

#: Bounds for the signal test's two waits -- for the plant to reach the watcher
#: poll, and for it to finish once signalled. Generous, because blowing either
#: is a hang rather than a red.
_SIGNAL_TIMEOUT_SECONDS = 60
_SIGNAL_POLL_SECONDS = 0.05


def _render_plant() -> str:
    """Render the create-time provisioner the way Terraform would.

    `<<-EOT` strips the indentation common to every line -- which matters, since
    the nested `<<'MANIFEST'` terminator is only at column 0 afterwards. `$${x}`
    is Terraform's escape for a literal `${x}` in the output, so it is protected
    before interpolations are substituted and restored after.
    """
    blocks = _HEREDOC_RE.findall(_MODULE.read_text())
    if not blocks:
        raise AssertionError(f"no `command = <<-EOT` provisioner in {_MODULE}")
    body = textwrap.dedent(blocks[_PLANT_BLOCK])

    sentinel = "\x00"
    body = body.replace("$${", sentinel)

    unresolved = []

    def substitute(match: "re.Match[str]") -> str:
        expression = match.group(1).strip()
        if expression not in _INTERPOLATIONS:
            unresolved.append(expression)
            return match.group(0)
        return _INTERPOLATIONS[expression]

    body = re.sub(r"\$\{([^}]*)\}", substitute, body)
    body = body.replace(sentinel, "${")
    if unresolved:
        raise AssertionError(
            "the plant gained interpolations this test does not know how to "
            f"render, so add them to _INTERPOLATIONS: {sorted(set(unresolved))}"
        )
    return body


@unittest.skipUnless(shutil.which("bash"), "no bash on PATH")
class AutoopsIncidentPlantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._plant = _render_plant()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)

        self._script = root / "plant.sh"
        self._script.write_text(self._plant)

        stub_dir = root / "bin"
        stub_dir.mkdir()
        for name, source in (
            ("kubectl", _KUBECTL_STUB),
            ("gcloud", _GCLOUD_STUB),
            ("sleep", _SLEEP_STUB),
        ):
            stub = stub_dir / name
            stub.write_text(source)
            stub.chmod(0o755)
        self._stub_dir = stub_dir
        self._calls = root / "calls"

    def _run(self, **scenario):
        env = dict(os.environ)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env.update({k: str(v) for k, v in scenario.items()})
        completed = subprocess.run(
            ["bash", str(self._script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        calls = (
            self._calls.read_text().splitlines() if self._calls.exists() else []
        )
        return completed, calls

    @staticmethod
    def _indices(calls, *needles):
        return [
            i
            for i, call in enumerate(calls)
            if all(needle in call for needle in needles)
        ]

    def test_bash_syntax_is_valid(self):
        # The provisioner is only ever executed by Terraform on a real run, so
        # a syntax error in it surfaces as a failed presubmit eval rather than
        # as a failed test. Cheap to catch here instead.
        completed = subprocess.run(
            ["bash", "-n", str(self._script)], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_healthy_run_deletes_nothing(self):
        completed, calls = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self._indices(calls, "delete namespace"),
            [],
            "the plant deleted a namespace on a run that succeeded and found "
            f"nothing left over:\n{chr(10).join(calls)}",
        )

    def test_leftover_namespace_is_deleted_before_the_workload_is_planted(self):
        # The self-heal, and the half that recovers a cluster already holding a
        # leaked namespace. Planting on top of one is what produced #1143's
        # `unchanged` apply, so the delete has to come first and has to wait.
        completed, calls = self._run(NS_EXISTS_RC=0)
        self.assertEqual(completed.returncode, 0, completed.stderr)

        deletes = self._indices(calls, "delete namespace", _NAMESPACE)
        applies = self._indices(calls, "apply -f -")
        self.assertTrue(deletes, f"no delete of {_NAMESPACE}:\n{chr(10).join(calls)}")
        self.assertTrue(applies, f"nothing was planted:\n{chr(10).join(calls)}")
        self.assertLess(
            deletes[0],
            applies[0],
            "the leftover namespace must be deleted before the plant, or "
            "`kubectl apply` reports `unchanged` and reuses the old pod",
        )
        self.assertIn(
            "--wait=true",
            calls[deletes[0]],
            "the pre-plant delete must wait, or the plant races the namespace "
            "controller and lands in a Terminating namespace",
        )

    def test_failed_plant_deletes_the_namespace_it_created(self):
        # #1143 itself: the watcher never fires, the plant fails, and the
        # namespace it created must not survive to poison the next run.
        completed, calls = self._run(WATCHER_FIRES=0)
        self.assertNotEqual(
            completed.returncode, 0, "the plant should fail when nothing fires"
        )
        self.assertTrue(
            self._indices(calls, "delete namespace", _NAMESPACE),
            "a failed plant left its namespace behind, which is the leak that "
            f"makes every later run fail:\n{chr(10).join(calls)}",
        )

    def test_failed_plant_reports_its_own_diagnostics(self):
        completed, _ = self._run(WATCHER_FIRES=0)
        self.assertIn("k8s-event-watcher logged no 'fire'", completed.stderr)

    def test_a_failure_carrying_no_diagnostics_still_dumps_before_cleanup(self):
        # Cleanup must not take the evidence with it. Steps 2 and 3 dump before
        # they exit, so asserting order on those proves nothing -- the delete
        # lives in an EXIT trap and is last by construction. Step 1 is the real
        # case: a rejected `apply` exits under `set -e` with no dump of its own,
        # so the only state anybody gets is whatever the trap writes on the way
        # out. Anything reaching the namespace here came from the trap.
        completed, calls = self._run(APPLY_RC=1)
        self.assertNotEqual(
            completed.returncode, 0, "a rejected apply should fail the plant"
        )
        deletes = self._indices(calls, "delete namespace")
        dumps = self._indices(calls, "get pods") + self._indices(calls, "get events")
        self.assertTrue(deletes, f"the namespace leaked:\n{chr(10).join(calls)}")
        self.assertTrue(
            dumps,
            "the plant deleted the namespace without recording any of its "
            f"state, so a rejected apply leaves nothing to debug:\n"
            f"{chr(10).join(calls)}",
        )
        self.assertLess(
            max(dumps),
            deletes[0],
            "the namespace was deleted before its state was recorded",
        )

    def test_a_namespace_the_stack_did_not_plant_is_refused_not_deleted(self):
        # Step 0b is the only unconditional namespace delete in the stack and it
        # runs against the shared host cluster, so the `managed-by` label step 1
        # writes is what separates "our leftover" from someone else's namespace.
        completed, calls = self._run(NS_EXISTS_RC=0, NS_OWNER="")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            self._indices(calls, "delete namespace"),
            [],
            "the plant deleted a namespace it had not labelled as its own:\n"
            f"{chr(10).join(calls)}",
        )
        self.assertIn("will not delete it", completed.stderr)

    def test_a_deadline_sigterm_still_deletes_the_namespace(self):
        # Prow enforces its deadline with SIGTERM, and bash does not run an EXIT
        # trap when the shell dies from an untrapped signal. Steps 2 and 3 can
        # hold this script for twelve minutes, so without a TERM trap the
        # deadline kill leaks the namespace exactly as a failed plant used to.
        env = dict(os.environ)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        env["CALLS"] = str(self._calls)
        env["WATCHER_FIRES"] = "0"
        env["STUB_SLEEP"] = "0.05"

        process = subprocess.Popen(
            ["bash", str(self._script)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Signal only once the plant is inside the watcher poll, which is
            # well past the point the traps are installed.
            deadline = time.monotonic() + _SIGNAL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if self._calls.exists() and "logs " in self._calls.read_text():
                    break
                time.sleep(_SIGNAL_POLL_SECONDS)
            else:
                self.fail("the plant never reached the watcher poll")
            process.terminate()
            process.wait(timeout=_SIGNAL_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=_SIGNAL_TIMEOUT_SECONDS)

        calls = self._calls.read_text().splitlines()
        self.assertTrue(
            self._indices(calls, "delete namespace", _NAMESPACE),
            "SIGTERM ended the plant without running the cleanup, so the "
            f"namespace leaked:\n{chr(10).join(calls)}",
        )

    def test_no_cleanup_runs_before_the_host_kubeconfig_is_fetched(self):
        # Until step 0 fetches credentials, kubectl would resolve against the
        # ambient context -- a different cluster, per the module header. A
        # cleanup firing there would delete a namespace of the same name on
        # whatever cluster happened to be selected.
        completed, calls = self._run(GCLOUD_FAILS=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            [c for c in calls if c.startswith("kubectl")],
            [],
            "the plant ran kubectl against the ambient context after failing "
            f"to fetch the host cluster's credentials:\n{chr(10).join(calls)}",
        )

    def test_a_wedged_leftover_namespace_fails_with_its_own_message(self):
        # A namespace stuck on a finalizer cannot be planted over. Failing here
        # names the real cause; letting it through spends the watcher timeout
        # and reports that nothing fired.
        completed, _ = self._run(NS_EXISTS_RC=0, NS_DELETE_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("could not clear the leftover", completed.stderr)


if __name__ == "__main__":
    unittest.main()
