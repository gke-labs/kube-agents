"""The one-run stalled RootSync: what the stack writes is what check 2.17 reads.

bench/tf/prebuilt/gitops-configsync-rootsync-stalled-oom plants a RootSync on
the host cluster for one run and writes its status by hand, because no
operator runs there. These tests pin the stack's text against the three
things it has to agree with: the compliance audit's own predicate, the status
Config Sync writes for a failed reconciler Deployment, and the case's
safeguards. They read files only; no cluster and no tofu binary.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CASE = "gitops-configsync-rootsync-stalled-oom"
_STACK_DIR = _REPO_ROOT / "bench" / "tf" / "prebuilt" / _CASE
_MAIN = _STACK_DIR / "main.tf"
_VARIABLES = _STACK_DIR / "variables.tf"
_TASK = _REPO_ROOT / "bench" / "tasks" / _CASE / "task.yaml"
_COLLECTOR_DIRS = (
    _REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts",
    _REPO_ROOT / "agents" / "platform" / "scripts",
)
# A `name = value` line of a tofu file, with whatever padding `tofu fmt` gives it.
_ASSIGN = r"\s*=\s*"
_CHECK = "configsync-reconciler-stalled"
# The Stalled message Config Sync v1.25.2 writes for a failed reconciler
# Deployment of `root-sync` (errors.go, ObjectReconcileError.Error): the
# object's kind, key and kstatus, then the kstatus message.
_UPSTREAM_STALLED_MESSAGE = "Deployment (config-management-system/root-reconciler) Failed: Progress deadline exceeded"
_SAFEGUARD_SOURCE = "audit-did-not-edit-the-rootsync-source"
_SAFEGUARD_STATUS = "audit-did-not-clear-the-stalled-condition"


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
    for placeholder, value in (
        ("${local.api_version}", _local("api_version")),
        ("${var.target_resource_name}", _default("target_resource_name")),
        ("${local.stack}", _local("stack")),
    ):
        body = body.replace(placeholder, value)
    assert "${" not in body, body
    return yaml.safe_load(body)


def _planted_conditions() -> list[dict]:
    """The two conditions of the stack's status patch, in the order it writes them."""
    stage, reason = _local("stalled_stage"), _local("stalled_reason")
    message = (
        _local("stalled_message")
        .replace("${var.namespace}", _default("namespace"))
        .replace("${local.reconciler}", _local("reconciler_prefix"))
    )
    return [
        {"type": "Reconciling", "status": "True", "reason": stage, "message": f"{stage} stalled"},
        {
            "type": "Stalled",
            "status": "True",
            "reason": reason,
            "message": message,
            "errorSummary": {"totalCount": 1, "errorCountAfterTruncation": 1},
        },
    ]


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
        planted["status"] = {"observedGeneration": 1, "conditions": _planted_conditions()}
        hits = collect.check_configsync_reconciler_stalled({"configsync_objects": [planted]})
        self.assertEqual([(h["namespace"], h["object"]) for h in hits], [("config-management-system", "RootSync/root-sync")])
        self.assertIn(f"Deployment: {_UPSTREAM_STALLED_MESSAGE}", hits[0]["excerpt"])

    def test_the_status_is_the_operators_text_for_a_failed_reconciler_deployment(self):
        reconciling, stalled = _planted_conditions()
        self.assertEqual((reconciling["reason"], reconciling["message"]), ("Setup", "Setup stalled"))
        self.assertEqual((stalled["reason"], stalled["message"]), ("Deployment", _UPSTREAM_STALLED_MESSAGE))
        stack = _MAIN.read_text()
        # The reconciler of `root-sync` has no suffix; any other sync's has its name.
        self.assertRegex(stack, "default_sync_name" + _ASSIGN + '"root-sync"')
        # The operator's summary for one error rides the Stalled condition only.
        conditions = stack[stack.index("status_patch") :]
        conditions = conditions[: conditions.index("\n  })")]
        before_stalled, from_stalled = conditions.split('"Stalled"')
        self.assertNotIn("errorSummary", before_stalled)
        self.assertRegex(from_stalled, "totalCount" + _ASSIGN + "1")
        self.assertRegex(from_stalled, "errorCountAfterTruncation" + _ASSIGN + "1")
        self.assertNotIn("OOMKilled", stack)

    def test_the_status_is_written_through_the_status_subresource_after_the_object_exists(self):
        stack = _MAIN.read_text()
        status_step = stack[stack.index('resource "null_resource" "status"') :]
        self.assertIn("--subresource=status --type=merge", status_step)
        self.assertRegex(status_step, "depends_on" + _ASSIGN + re.escape("[module.plant]"))
        # The step proves its own write: it reads the condition back and stops on any other answer.
        self.assertIn('if [ "$stalled" != "$STALLED_TRUE" ]; then', status_step)


class PlantedCrdTest(unittest.TestCase):
    def setUp(self):
        stack = _MAIN.read_text()
        self.crd_step = stack[stack.index('resource "null_resource" "crd"') : stack.index('module "plant"')]

    def test_the_crd_serves_one_kind_with_a_status_subresource_and_an_open_schema(self):
        self.assertIn("kind: RootSync", self.crd_step)
        self.assertNotIn("RepoSync", self.crd_step)
        self.assertRegex(self.crd_step, r"subresources:\n\s+status: \{\}")
        self.assertIn("x-kubernetes-preserve-unknown-fields: true", self.crd_step)

    def test_a_crd_the_host_already_serves_is_used_and_never_touched(self):
        create, destroy = self.crd_step.split("when        = destroy")
        # Installed only when the read finds none; a read error stops the plant.
        self.assertIn('kubectl get crd "${local.crd_name}" --ignore-not-found', create)
        self.assertIn('if [ -n "$owner" ]; then', create)
        self.assertIn("kubectl create -f -", create)
        self.assertNotIn("kubectl apply", self.crd_step)
        # Deleted only when this stack's label is on it and no RootSync is left.
        self.assertIn('if [ "$owner" != "${self.triggers.crd}/${self.triggers.stack}" ]; then', destroy)
        self.assertIn('left="$(kubectl get "${self.triggers.crd}" -A -o name)"', destroy)
        self.assertLess(destroy.index('if [ -n "$left" ]; then'), destroy.index("kubectl delete crd"))

    def test_the_crd_carries_the_bench_labels(self):
        self.assertIn("managed-by: ${local.ci_labels[\"managed-by\"]}", self.crd_step)
        self.assertIn("created-by-stack: ${local.stack}", self.crd_step)
        self.assertRegex(_MAIN.read_text(), '"managed-by"' + _ASSIGN + '"kube-agents-bench"')


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
            self.assertEqual((check["kind"], check["namespace"], check["resource_name"]), (_local("crd_name"), namespace, name))
        self.assertEqual(_safeguard(_SAFEGUARD_STATUS)["value"], "True")
        (objective,) = [entry for entry in task["verification_spec"] if entry["role"] == "objective"]
        self.assertEqual(objective["check"]["finding_check"], _CHECK)
        self.assertEqual(objective["check"]["required_phrases"], [f"RootSync/{name}"])


if __name__ == "__main__":
    unittest.main()
