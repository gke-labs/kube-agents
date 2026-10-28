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
_VARIABLES = _MODULE.with_name("variables.tf")

#: Where the operator names the gateway Pod's container that runs the watcher,
#: and the stack's default that has to match it (#3681: the default kept
#: naming envoy-credential-proxy after #913 moved the watcher).
_OPERATOR_MANIFESTS = (
    _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
)
_OPERATOR_CONTAINER_RE = re.compile(
    r'^\s*agentAPIAuthContainerName\s*=\s*"([^"]+)"', re.M
)
_AGENT_CONTAINER_DEFAULT_RE = re.compile(
    r'variable "agent_container" \{.*?^\s*default\s*=\s*"([^"]+)"', re.S | re.M
)

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
    "var.agent_container": "agent-api-auth",
    "var.allocate_mib": "96",
    "var.memory_limit_mib": "64",
    "var.replicas": "1",
    "local.watcher_handled": "fire",
    # The OOM shape and the incident wait: what the incumbent case plants.
    "var.crash_seconds": "0",
    "var.wait_for": "incident",
    "var.start_settle_seconds": "0",
}

#: The label step 1 writes, and the value step 0b requires before it will
#: delete anything.
_OWNER_LABEL = "kube-agents-bench"

#: `kubectl` records every invocation to $CALLS and answers from the
#: environment, so a test picks a scenario by setting these. Defaults are the
#: healthy path: no leftover namespace, debounce already cleared, watcher fires.
#: `NS_OWNER` uses `${VAR-default}` rather than `${VAR:-default}` so a test can
#: set it empty to mean "the namespace carries no managed-by label".
#: `POD_CONTAINERS` is the gateway pod's container list as step 0a reads it:
#: the native sidecars first, then the regular containers.
_KUBECTL_STUB = """#!/bin/bash
echo "kubectl $*" >> "$CALLS"
case "$*" in
  *"get deployment"*jsonpath*)  echo "${POD_CONTAINERS-agent-api-auth platform-agent}"; exit 0 ;;
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
    if [ "${WATCHER_FOLDS:-0}" = "1" ]; then
      echo "folded BackOff pod=%(ns)s/%(workload)s-abc123 (sid=s1) -- the workload's incident is already open"
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
#: the script still running when it sends SIGTERM. Every invocation is recorded
#: to $CALLS so tests can assert settle sleeps and ordering after the poll.
_SLEEP_STUB = '#!/bin/bash\necho "sleep $*" >> "$CALLS"\nexec /bin/sleep "${STUB_SLEEP:-0}"\n'

#: Bounds for the signal test's two waits -- for the plant to reach the watcher
#: poll, and for it to finish once signalled. Generous, because blowing either
#: is a hang rather than a red.
_SIGNAL_TIMEOUT_SECONDS = 60
_SIGNAL_POLL_SECONDS = 0.05
_TF_BIN = shutil.which("tofu") or shutil.which("terraform")


def _render_plant(overrides: "dict[str, str] | None" = None) -> str:
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

    interpolations = dict(_INTERPOLATIONS)
    if overrides:
        interpolations.update(overrides)
    unresolved = []

    def substitute(match: "re.Match[str]") -> str:
        expression = match.group(1).strip()
        if expression not in interpolations:
            unresolved.append(expression)
            return match.group(0)
        return interpolations[expression]

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

    def test_a_folded_event_is_not_an_incident_unless_the_case_accepts_it(self):
        # A case waiting on its own card gets none for a folded event, so by
        # default only `fire` ends the wait; incident-triage-workload-fold
        # grades the fold and sets accept_folded, rendered here as the pattern.
        completed, _ = self._run(WATCHER_FIRES=0, WATCHER_FOLDS=1)
        self.assertNotEqual(completed.returncode, 0, "a folded event ended the wait for a case that did not accept it")

        accepting = self._plant.replace('grep -qE "fire ', 'grep -qE "(fire|folded) ')
        self.assertNotEqual(accepting, self._plant, "the watcher poll's pattern moved; update this test")
        self._script.write_text(accepting)
        completed, _ = self._run(WATCHER_FIRES=0, WATCHER_FOLDS=1)
        self.assertEqual(completed.returncode, 0, completed.stderr)

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

    def test_a_container_the_pod_lacks_fails_before_anything_is_planted(self):
        # #3681: the watcher moved out of envoy-credential-proxy (#913) while
        # this stack kept reading that container's log with stderr discarded,
        # so every run spent the 300s watcher wait and blamed the watcher. The
        # pod here carries the post-#913 layout minus agent-api-auth.
        completed, calls = self._run(
            POD_CONTAINERS="platform-agent platform-agent-dashboard fluent-bit"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("has no container 'agent-api-auth'", completed.stderr)
        self.assertEqual(
            self._indices(calls, "apply -f -") + self._indices(calls, "logs "),
            [],
            "the plant went on to plant and poll against a container the pod "
            f"does not have:\n{chr(10).join(calls)}",
        )

    def test_the_container_check_reads_native_sidecars(self):
        # agent-api-auth is a native sidecar, so it is listed only under
        # initContainers. A check reading .containers alone would refuse every
        # run on a real install, which the stub's canned answer cannot show.
        _, calls = self._run()
        checks = self._indices(calls, "get deployment")
        self.assertTrue(checks, f"no container check ran:\n{chr(10).join(calls)}")
        self.assertIn(".spec.template.spec.initContainers[*].name", calls[checks[0]])
        self.assertIn(".spec.template.spec.containers[*].name", calls[checks[0]])

    def test_the_default_container_is_the_one_the_operator_runs_the_watcher_in(self):
        operator = _OPERATOR_CONTAINER_RE.search(_OPERATOR_MANIFESTS.read_text())
        default = _AGENT_CONTAINER_DEFAULT_RE.search(_VARIABLES.read_text())
        self.assertIsNotNone(
            operator, f"agentAPIAuthContainerName not found in {_OPERATOR_MANIFESTS}"
        )
        self.assertIsNotNone(
            default, f"agent_container default not found in {_VARIABLES}"
        )
        self.assertEqual(
            default.group(1),
            operator.group(1),
            "the stack would read the watcher's log from a container the "
            "gateway Pod does not run it in",
        )

    def test_a_wedged_leftover_namespace_fails_with_its_own_message(self):
        # A namespace stuck on a finalizer cannot be planted over. Failing here
        # names the real cause; letting it through spends the watcher timeout
        # and reports that nothing fired.
        completed, _ = self._run(NS_EXISTS_RC=0, NS_DELETE_RC=1)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("could not clear the leftover", completed.stderr)

    def test_start_settle_seconds_waits_after_watcher_poll_only_when_positive(self):
        # At the default 0, single-card cases pay no extra sleep after `fire`.
        completed, calls = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Waiting ", completed.stdout)
        logs_calls = self._indices(calls, "logs ")
        self.assertTrue(logs_calls, f"no watcher poll ran:\n{chr(10).join(calls)}")
        self.assertEqual(
            [i for i in self._indices(calls, "sleep ") if i > max(logs_calls)],
            [],
            f"the default 0s plant slept after the watcher poll:\n{chr(10).join(calls)}",
        )

        # With a positive settle duration, the plant prints its settle line and
        # sleeps strictly after `k8s-event-watcher` handles the pod.
        self._calls.unlink(missing_ok=True)
        settled = _render_plant({"var.start_settle_seconds": "7"})
        self.assertNotEqual(settled, self._plant)
        self._script.write_text(settled)
        completed, calls = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        handled_msg = "k8s-event-watcher handled a pod"
        waiting_msg = "Waiting 7s for the incident cards to settle before the board read."
        self.assertIn(handled_msg, completed.stdout)
        self.assertIn(waiting_msg, completed.stdout)
        self.assertLess(
            completed.stdout.index(handled_msg),
            completed.stdout.index(waiting_msg),
        )
        logs_calls = self._indices(calls, "logs ")
        settle_sleeps = self._indices(calls, "sleep 7")
        self.assertTrue(logs_calls, f"no watcher poll ran:\n{chr(10).join(calls)}")
        self.assertTrue(
            settle_sleeps, f"no `sleep 7` settle ran:\n{chr(10).join(calls)}"
        )
        self.assertLess(
            max(logs_calls),
            settle_sleeps[0],
            f"`sleep 7` ran before the watcher poll finished:\n{chr(10).join(calls)}",
        )

    def test_start_settle_seconds_declares_whole_number_validation(self):
        # main.tf gates the settle with `[ "${var.start_settle_seconds}" != "0" ]`
        # (avoiding bash's 64-bit integer `-gt` grammar) and variables.tf bounds
        # `start_settle_seconds` to whole numbers in `0..86400`. Pin the exact
        # validation condition text so CI environments without `tofu` or
        # `terraform` still fail on any edit that weakens or appends to the rule.
        block = re.search(
            r'variable "start_settle_seconds"\s*\{(.*?)\n\}',
            _VARIABLES.read_text(),
            re.S,
        )
        self.assertIsNotNone(
            block, f"start_settle_seconds variable block not found in {_VARIABLES}"
        )
        condition = re.search(
            r"validation\s*\{[^}]*?\bcondition\s*=\s*([^\n]+)",
            block.group(1),
            re.S,
        )
        self.assertIsNotNone(
            condition,
            f"validation condition not found on start_settle_seconds in {_VARIABLES}",
        )
        self.assertEqual(
            condition.group(1).strip(),
            "var.start_settle_seconds >= 0 && var.start_settle_seconds <= 86400 && floor(var.start_settle_seconds) == var.start_settle_seconds",
        )

    @unittest.skipUnless(_TF_BIN, "needs a tofu or terraform binary")
    def test_start_settle_seconds_validation_evaluated_by_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(_VARIABLES, pathlib.Path(tmp) / "variables.tf")
            init = subprocess.run(
                [_TF_BIN, f"-chdir={tmp}", "init", "-input=false", "-no-color"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            def plan(val: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [
                        _TF_BIN,
                        f"-chdir={tmp}",
                        "plan",
                        "-input=false",
                        "-no-color",
                        "-var",
                        "host_cluster_name=platform-agent-host",
                        "-var",
                        "host_cluster_location=us-central1",
                        "-var",
                        f"start_settle_seconds={val}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )

            for accepted in ("0", "180", "86400"):
                with self.subTest(accepted=accepted):
                    res = plan(accepted)
                    self.assertEqual(res.returncode, 0, res.stderr)
            for refused in ("2.5", "-1", "86401", "10000000000000000000"):
                with self.subTest(refused=refused):
                    res = plan(refused)
                    self.assertNotEqual(res.returncode, 0, res.stdout)
                    self.assertIn(
                        "start_settle_seconds must be a whole number of seconds between 0 and 86400",
                        res.stderr,
                    )


if __name__ == "__main__":
    unittest.main()
