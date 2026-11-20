"""The one-run stalled RootSync: what the stack writes is what check 2.17 reads.

bench/tf/prebuilt/gitops-configsync-rootsync-stalled plants a RootSync on
the host cluster for one run, with its status in the manifest, because no
operator runs there. These tests pin the stack against the things it has to
agree with: the compliance audit's own predicate, the status Config Sync
writes for a reconciler Deployment past its progress deadline, the case's
safeguards, and the suite's rule for a cluster-scoped plant (bench/README.md,
rule 6). The CRD script runs against stub `kubectl` and `gcloud` on PATH; the
rest reads files. No cluster and no tofu binary.
"""

from __future__ import annotations

import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CASE = "gitops-configsync-rootsync-stalled"
_STACK_DIR = _REPO_ROOT / "bench" / "tf" / "prebuilt" / _CASE
_MAIN = _STACK_DIR / "main.tf"
_VARIABLES = _STACK_DIR / "variables.tf"
_CRD_SCRIPT = _STACK_DIR / "rootsync_crd.sh"
_TASK = _REPO_ROOT / "bench" / "tasks" / _CASE / "task.yaml"
_COLLECTOR_DIRS = (
    _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts",
    _REPO_ROOT / "agents" / "platform" / "scripts",
)
# A `name = value` line of a tofu file, with whatever padding `tofu fmt` gives it.
_ASSIGN = r"\s*=\s*"
_CHECK = "configsync-reconciler-stalled"
_CRD = "rootsyncs.configsync.gke.io"
_OTHER_STACK = "some-other-stack"
# The Stalled message Config Sync v1.25.2 writes for a failed reconciler
# Deployment of `root-sync` (errors.go, ObjectReconcileError.Error): the
# object's kind, key and kstatus, then the kstatus message.
_UPSTREAM_STALLED_MESSAGE = "Deployment (config-management-system/root-reconciler) Failed: Progress deadline exceeded"
_SAFEGUARD_SOURCE = "audit-did-not-edit-the-rootsync-source"
_SAFEGUARD_STATUS = "audit-did-not-clear-the-stalled-condition"
_SCRIPT_TIMEOUT_SECONDS = 60
#: The signal test's waits: for the plant to reach `kubectl wait` (read off the
#: stub's call log), and for the script to finish once signalled.
_SIGNAL_REACH_DEADLINE_SECONDS = 20.0
_SIGNAL_POLL_SECONDS = 0.05
_SIGNAL_STUB_SLEEP_SECONDS = 3
_SIGNAL_EXIT = 143


def _local(name: str) -> str:
    """The quoted value of one `locals` entry of the stack."""
    match = re.search(rf'^\s*{name}{_ASSIGN}"([^"\n]*)"$', _MAIN.read_text(), re.MULTILINE)
    assert match, f"no local {name} in {_MAIN}"
    return match.group(1)


def _default(name: str) -> str:
    match = re.search(rf'variable "{name}" \{{[^}}]*?^\s*default{_ASSIGN}"([^"]+)"', _VARIABLES.read_text(), re.MULTILINE)
    assert match, f"no default for {name} in {_VARIABLES}"
    return match.group(1)


def _planted_manifest() -> dict:
    """The RootSync the stack creates, with its variables at their defaults."""
    body = re.search(r"manifest = <<-MANIFEST\n(.*?)\n\s*MANIFEST\n", _MAIN.read_text(), re.DOTALL).group(1)
    message = (
        _local("stalled_message")
        .replace("${var.namespace}", _default("namespace"))
        .replace("${local.reconciler}", _local("reconciler_prefix"))
    )
    for placeholder, value in (
        ("${local.api_version}", _local("api_version")),
        ("${var.target_resource_name}", _default("target_resource_name")),
        ("${local.stack}", _local("stack")),
        ("${local.stalled_stage}", _local("stalled_stage")),
        ("${local.stalled_reason}", _local("stalled_reason")),
        ("${local.stalled_message}", message),
        ("${local.stalled_at}", _local("stalled_at")),
        ("${local.reconciling_since}", _local("reconciling_since")),
    ):
        body = body.replace(placeholder, value)
    assert "${" not in body, body
    return yaml.safe_load(body)


def _safeguard(name: str) -> dict:
    spec = yaml.safe_load(_TASK.read_text())["verification_spec"]
    (found,) = [entry for entry in spec if entry["name"] == name]
    return found["check"]


class PlantedStatusTest(unittest.TestCase):
    def test_the_products_check_names_the_object_the_stack_writes(self):
        for directory in _COLLECTOR_DIRS:
            if str(directory) not in sys.path:
                sys.path.insert(0, str(directory))
        import collect  # noqa: PLC0415 - the collector lives outside this test's package

        planted = _planted_manifest()
        planted["metadata"]["namespace"] = _default("namespace")
        hits = collect.check_configsync_reconciler_stalled({"configsync_objects": [planted]})
        self.assertEqual([(h["namespace"], h["object"]) for h in hits], [("config-management-system", "RootSync/root-sync")])
        self.assertIn(f"Deployment: {_UPSTREAM_STALLED_MESSAGE}", hits[0]["excerpt"])

    def test_the_status_is_the_operators_text_for_a_reconciler_past_its_progress_deadline(self):
        status = _planted_manifest()["status"]
        reconciling, stalled = status["conditions"]
        self.assertEqual(
            (reconciling["type"], reconciling["status"], reconciling["reason"], reconciling["message"]),
            ("Reconciling", "True", "Setup", "Setup stalled"),
        )
        self.assertEqual(
            (stalled["type"], stalled["status"], stalled["reason"], stalled["message"]),
            ("Stalled", "True", "Deployment", _UPSTREAM_STALLED_MESSAGE),
        )
        # The operator's summary for one error rides the Stalled condition only.
        self.assertEqual(stalled["errorSummary"], {"totalCount": 1, "errorCountAfterTruncation": 1})
        self.assertNotIn("errorSummary", reconciling)
        # The live object's times: Reconciling was already True while the
        # Deployment was in progress, so only its transition time is earlier.
        stalled_at = _local("stalled_at")
        self.assertEqual((stalled["lastUpdateTime"], stalled["lastTransitionTime"]), (stalled_at, stalled_at))
        self.assertEqual(reconciling["lastUpdateTime"], stalled_at)
        self.assertLess(reconciling["lastTransitionTime"], stalled_at)
        # Literal times: a time taken at plan would replace the plant on every plan.
        self.assertNotIn("timestamp()", _MAIN.read_text())
        stack = _MAIN.read_text()
        # The reconciler of `root-sync` has no suffix; any other sync's has its name.
        self.assertRegex(stack, "default_sync_name" + _ASSIGN + '"root-sync"')
        self.assertNotIn("OOMKilled", stack)

    def test_the_object_and_its_status_go_in_one_create(self):
        # The suite's rule keeps a namespace-scoped plant to its manifest, so
        # nothing runs after the module. (The CRD the script creates has no
        # status subresource, which would drop `status` on create: CrdScriptTest.)
        resources = re.findall(r'^(?:resource|module) "[^\n]+', _MAIN.read_text(), re.MULTILINE)
        self.assertEqual(resources, ['resource "null_resource" "crd" {', 'module "plant" {'])

    def test_the_planted_object_carries_this_stacks_label(self):
        # The CRD script tells this stack's RootSync from another owner's by
        # this label, before it deletes a leftover CRD with everything in it.
        labels = _planted_manifest()["metadata"]["labels"]
        self.assertEqual(labels, {"managed-by": "kube-agents-bench", "created-by-stack": _CASE})


#: `kubectl` records every invocation to $CALLS, with stdin appended for the
#: create, and answers from the environment. CRD_OWNER_OUT is what the
#: ownership read prints before the create: empty for an absent CRD,
#: "<name>/<label>" for a present one; once the create was sent the read
#: prints CRD_OWNER_AFTER_CREATE (default: this stack's CRD, stored), so a
#: create the API server refused is distinct from one that failed after the
#: object existed. CRD_SUBRESOURCES is what the shape read prints. The object
#: list answers FOREIGN_OBJECTS only to the selector that excludes this
#: stack's label, and OWN_OBJECTS to any other selector, so a wrong selector
#: reads as "nothing foreign here". LEFT_OBJECTS is the destroy's list.
_KUBECTL_STUB = """#!/bin/bash
echo "kubectl $*" >> "$CALLS"
case "$*" in
  *"get crd"*"subresources"*) printf '%s' "${CRD_SUBRESOURCES-}"; exit 0 ;;
  *"get crd"*--ignore-not-found*)
    if [ "${CRD_READ_RC:-0}" != "0" ]; then echo "Unable to connect to the server" >&2; exit "$CRD_READ_RC"; fi
    if grep -q "kind: CustomResourceDefinition" "$CALLS"; then printf '%s' "${CRD_OWNER_AFTER_CREATE-rootsyncs.configsync.gke.io/$RC_STACK}"; exit 0; fi
    printf '%s' "${CRD_OWNER_OUT-}"; exit 0 ;;
  *"get rootsyncs.configsync.gke.io --all-namespaces -l created-by-stack!=$RC_STACK -o name") printf '%s' "${FOREIGN_OBJECTS-}"; exit 0 ;;
  *"get rootsyncs.configsync.gke.io --all-namespaces -l"*) printf '%s' "${OWN_OBJECTS-}"; exit 0 ;;
  *"get rootsyncs.configsync.gke.io --all-namespaces -o name"*) printf '%s' "${LEFT_OBJECTS-}"; exit 0 ;;
  *"delete crd"*) exit "${CRD_DELETE_RC:-0}" ;;
  *"create -f -"*)
    echo "stdin: $(cat | tr '\\n' ' ')" >> "$CALLS"
    exit "${CRD_CREATE_RC:-0}" ;;
  *"wait --for condition=established"*) /bin/sleep "${CRD_WAIT_SLEEP:-0}"; exit "${CRD_WAIT_RC:-0}" ;;
  *) exit 0 ;;
esac
"""

_GCLOUD_STUB = '#!/bin/bash\necho "gcloud $*" >> "$CALLS"\nexit 0\n'
_SLEEP_STUB = '#!/bin/bash\necho "sleep $*" >> "$CALLS"\nexit 0\n'

_BASE_ENV = {
    "RC_HOST_CLUSTER": "platform-agent-host",
    "RC_HOST_LOCATION": "us-east4",
    "RC_PROJECT": "kube-agents-evals",
    "RC_STACK": _CASE,
    "RC_MANAGED_BY": "kube-agents-bench",
    "RC_BUILD_ID": "2118356684249239552",
    "RC_PULL_NUMBER": "none",
}


class CrdScriptTest(unittest.TestCase):
    """The CRD is cluster-scoped, so it keeps the namespaced-plant module's guards."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = pathlib.Path(self._dir.name)
        stub_dir = root / "bin"
        stub_dir.mkdir()
        for name, source in (("kubectl", _KUBECTL_STUB), ("gcloud", _GCLOUD_STUB), ("sleep", _SLEEP_STUB)):
            stub = stub_dir / name
            stub.write_text(source)
            stub.chmod(0o755)
        self._stub_dir = stub_dir
        self._calls = root / "calls"

    def _run(self, verb, **scenario):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RC_")}
        env.update(_BASE_ENV)
        env.update({k: str(v) for k, v in scenario.items()})
        env["CALLS"] = str(self._calls)
        env["PATH"] = f"{self._stub_dir}{os.pathsep}{env['PATH']}"
        done = subprocess.run(
            ["bash", str(_CRD_SCRIPT), verb], env=env, capture_output=True, text=True, check=False, timeout=_SCRIPT_TIMEOUT_SECONDS
        )
        calls = self._calls.read_text().splitlines() if self._calls.exists() else []
        return done, calls

    @staticmethod
    def _kubectl(calls, needle):
        return [c for c in calls if c.startswith("kubectl ") and needle in c]

    def test_a_host_that_serves_no_such_kind_gets_the_crd_with_the_bench_labels(self):
        done, calls = self._run("plant")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._kubectl(calls, "delete crd"), [])
        (created,) = [c for c in calls if c.startswith("stdin: ")]
        self.assertIn(f"name: {_CRD}", created)
        self.assertIn("kind: RootSync", created)
        self.assertNotIn("RepoSync", created)
        self.assertIn("x-kubernetes-preserve-unknown-fields: true", created)
        # No status subresource: one create must keep the object's status.
        self.assertNotIn("subresources", created)
        self.assertIn('managed-by: "kube-agents-bench"', created)
        self.assertIn(f'created-by-stack: "{_CASE}"', created)
        # Label values are quoted: on the lane the build id is an integer.
        self.assertIn('build-id: "2118356684249239552"', created)
        self.assertEqual(len(self._kubectl(calls, "wait --for condition=established")), 1)
        self.assertEqual(self._kubectl(calls, "apply"), [])

    def test_a_crd_of_another_owner_stops_the_plant_and_is_never_touched(self):
        for owner in (f"{_CRD}/", f"{_CRD}/{_OTHER_STACK}"):
            with self.subTest(owner=owner):
                if self._calls.exists():
                    self._calls.unlink()
                done, calls = self._run("plant", CRD_OWNER_OUT=owner)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("was not installed by this stack", done.stderr)
                self.assertEqual(self._kubectl(calls, "delete crd"), [])
                self.assertEqual(self._kubectl(calls, "create -f -"), [])

    def test_this_stacks_leftover_is_deleted_before_the_create(self):
        done, calls = self._run("plant", CRD_OWNER_OUT=f"{_CRD}/{_CASE}", OWN_OBJECTS="rootsync.configsync.gke.io/root-sync")
        self.assertEqual(done.returncode, 0, done.stderr)
        (deleted,) = self._kubectl(calls, "delete crd")
        # Waited for: the create must not meet a CRD that is still terminating.
        self.assertIn("--wait=true", deleted)
        self.assertLess(calls.index(deleted), calls.index(self._kubectl(calls, "create -f -")[0]))
        # The foreign-object list excludes this stack's label and nothing else.
        self.assertEqual(
            self._kubectl(calls, "--all-namespaces -l"),
            [f"kubectl get {_CRD} --all-namespaces -l created-by-stack!={_CASE} -o name"],
        )

    def test_a_labelled_crd_that_serves_a_subresource_is_the_products_and_is_never_touched(self):
        # A Config Sync install applied over a leftover keeps the label it does not own.
        for verb in ("plant", "destroy"):
            with self.subTest(verb=verb):
                if self._calls.exists():
                    self._calls.unlink()
                done, calls = self._run(verb, CRD_OWNER_OUT=f"{_CRD}/{_CASE}", CRD_SUBRESOURCES='{"status":{}}')
                self.assertEqual(done.returncode == 0, verb == "destroy", done.stderr)
                self.assertEqual(self._kubectl(calls, "delete crd"), [])
                self.assertEqual(self._kubectl(calls, "create -f -"), [])

    def test_a_missing_input_stops_before_any_cluster_call(self):
        for verb, name in (("plant", "RC_BUILD_ID"), ("plant", "RC_STACK"), ("destroy", "RC_HOST_CLUSTER")):
            with self.subTest(verb=verb, name=name):
                if self._calls.exists():
                    self._calls.unlink()
                done, calls = self._run(verb, **{name: ""})
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(name, done.stderr)
                self.assertEqual(calls, [])

    def test_a_signal_after_the_create_removes_the_crd_and_exits_with_the_signal_code(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RC_")}
        env.update(_BASE_ENV)
        env.update(
            {
                "CALLS": str(self._calls),
                "PATH": f"{self._stub_dir}{os.pathsep}{env['PATH']}",
                "CRD_WAIT_SLEEP": str(_SIGNAL_STUB_SLEEP_SECONDS),
            }
        )
        proc = subprocess.Popen(["bash", str(_CRD_SCRIPT), "plant"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + _SIGNAL_REACH_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            if self._calls.exists() and "wait --for condition=established" in self._calls.read_text():
                break
            time.sleep(_SIGNAL_POLL_SECONDS)
        else:
            proc.kill()
            self.fail("the plant never reached kubectl wait")
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=_SCRIPT_TIMEOUT_SECONDS)
        self.assertEqual(proc.returncode, _SIGNAL_EXIT)
        calls = self._calls.read_text().splitlines()
        self.assertEqual(len(self._kubectl(calls, "delete crd")), 1)

    def test_a_leftover_that_holds_someone_elses_rootsync_is_not_deleted(self):
        # Deleting a CRD deletes every object of its kind.
        done, calls = self._run("plant", CRD_OWNER_OUT=f"{_CRD}/{_CASE}", FOREIGN_OBJECTS="rootsync.configsync.gke.io/theirs")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self._kubectl(calls, "delete crd"), [])
        self.assertEqual(self._kubectl(calls, "create -f -"), [])

    def test_a_failed_read_is_never_taken_for_an_absent_crd(self):
        done, calls = self._run("plant", CRD_READ_RC=1)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self._kubectl(calls, "create -f -"), [])
        self.assertEqual(self._kubectl(calls, "delete crd"), [])

    def test_a_plant_that_fails_after_the_create_removes_its_crd(self):
        # Terraform taints the resource and skips its destroy provisioner.
        done, calls = self._run("plant", CRD_WAIT_RC=1)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(len(self._kubectl(calls, "delete crd")), 1)
        self.assertGreater(calls.index(self._kubectl(calls, "delete crd")[0]), calls.index(self._kubectl(calls, "create -f -")[0]))

    def test_a_refused_create_deletes_nothing(self):
        # The API server refused the create: the read in the trap finds no CRD of this stack.
        done, calls = self._run("plant", CRD_CREATE_RC=1, CRD_OWNER_AFTER_CREATE="")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self._kubectl(calls, "delete crd"), [])

    def test_a_create_that_failed_after_the_crd_was_stored_is_cleaned_up(self):
        # A lost response: the create reports failure, and the CRD is there with this stack's label.
        done, calls = self._run("plant", CRD_CREATE_RC=1)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(len(self._kubectl(calls, "delete crd")), 1)

    def test_destroy_deletes_only_this_stacks_empty_crd(self):
        done, calls = self._run("destroy", CRD_OWNER_OUT=f"{_CRD}/{_CASE}")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self._kubectl(calls, "delete crd")), 1)

    def test_destroy_leaves_a_crd_of_another_owner_and_an_absent_one(self):
        for owner in ("", f"{_CRD}/", f"{_CRD}/{_OTHER_STACK}"):
            with self.subTest(owner=owner):
                if self._calls.exists():
                    self._calls.unlink()
                done, calls = self._run("destroy", CRD_OWNER_OUT=owner)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self._kubectl(calls, "delete crd"), [])

    def test_destroy_keeps_the_crd_while_a_rootsync_is_left(self):
        done, calls = self._run("destroy", CRD_OWNER_OUT=f"{_CRD}/{_CASE}", LEFT_OBJECTS="rootsync.configsync.gke.io/root-sync")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._kubectl(calls, "delete crd"), [])
        self.assertIn("left in place", done.stderr)

    def test_the_stack_runs_the_script_for_both_verbs(self):
        stack = _MAIN.read_text()
        self.assertRegex(stack, "crd_script" + _ASSIGN + re.escape('"${path.module}/rootsync_crd.sh"'))
        self.assertIn("bash '${local.crd_script}' plant", stack)
        self.assertIn('bash \\"$RC_SCRIPT\\" destroy', stack)

    def test_the_stack_hands_each_verb_every_input_the_script_requires(self):
        stack = _MAIN.read_text()
        crd = stack[stack.index('resource "null_resource" "crd"') : stack.index('module "plant"')]
        plant_block, destroy_block = crd.split("when        = destroy")
        script = _CRD_SCRIPT.read_text()

        def env_of(block):
            body = block[block.index("environment = {") :]
            return dict(re.findall(r"^\s*(RC_\w+)" + _ASSIGN + r"(.+)$", body[: body.index("\n    }")], re.MULTILINE))

        def required(function):
            body = script[script.index(f"{function}() {{") :]
            return set(re.findall(r"rc_require (RC_\w+)", body[: body.index("\n}\n")]))

        plant_env, destroy_env = env_of(plant_block), env_of(destroy_block)
        self.assertLessEqual(required("rc_plant"), set(plant_env))
        self.assertLessEqual(required("rc_destroy"), set(destroy_env))
        # A destroy provisioner may read only `self` and `path`.
        self.assertEqual(destroy_env["RC_STACK"], "self.triggers.stack")
        self.assertEqual(destroy_env["RC_HOST_CLUSTER"], "self.triggers.host_cluster")
        self.assertEqual(destroy_env["RC_HOST_LOCATION"], "self.triggers.host_location")
        self.assertEqual(destroy_env["RC_PROJECT"], "self.triggers.host_project")
        self.assertEqual(destroy_env["RC_SCRIPT"], '"${path.module}/rootsync_crd.sh"')
        self.assertEqual(plant_env["RC_STACK"], "local.stack")


class CaseBindingTest(unittest.TestCase):
    def test_the_spec_is_inert(self):
        spec = _planted_manifest()["spec"]
        self.assertTrue(spec["git"]["repo"].startswith("https://config-sync-fixture.invalid/"))
        self.assertEqual(spec["git"]["auth"], "none")
        for forbidden in ("secretRef", "gcpServiceAccountEmail", "github.com", "gitlab.com", "googlesource.com"):
            self.assertNotIn(forbidden, _MAIN.read_text())

    def test_the_source_safeguard_reads_the_whole_planted_spec(self):
        check = _safeguard(_SAFEGUARD_SOURCE)
        self.assertEqual((check["path"], check["op"]), ("spec", "eq"))
        self.assertEqual(check["value"], _planted_manifest()["spec"])

    def test_the_safeguards_and_the_objective_name_the_planted_object(self):
        task = yaml.safe_load(_TASK.read_text())
        namespace, name = _default("namespace"), _default("target_resource_name")
        self.assertEqual(task["infrastructure"]["variables"], {"namespace": namespace, "target_resource_name": name})
        self.assertEqual(task["infrastructure"]["stack"], f"prebuilt/{_CASE}")
        for safeguard in (_SAFEGUARD_SOURCE, _SAFEGUARD_STATUS):
            check = _safeguard(safeguard)
            self.assertEqual((check["kind"], check["namespace"], check["resource_name"]), (_CRD, namespace, name))
        self.assertEqual(_safeguard(_SAFEGUARD_STATUS)["value"], "True")
        (objective,) = [entry for entry in task["verification_spec"] if entry["role"] == "objective"]
        self.assertEqual(objective["check"]["finding_check"], _CHECK)
        self.assertEqual(objective["check"]["required_phrases"], [f"RootSync/{name}"])


if __name__ == "__main__":
    unittest.main()
