"""The event watcher's owner lookup reads one named Pod's metadata through the
credential broker: the Accept it sends is declared in
k8s-operator/cmd/k8s-event-watcher/owner.go (podMetadataAccept), and the Accept
the broker admits for a named Pod in agents/platform/scripts/watcher_policy.py
(POD_METADATA_ACCEPT_PARAMS, through accepts_metadata_only). Each side's own
tests pin its own copy, so a change on one side with its test updated is green
on both suites and refused by the broker on every profile cluster at the next
deploy, which leaves every Pod's controller_ref empty. This test fails when the
two copies disagree.

The controller kinds that fold are declared three times: the watcher's
foldableControllerKinds (owner.go), the kinds the broker relays
(POD_METADATA_OWNER_KINDS in watcher_policy.py), and the kinds session_kv folds
on (FOLDABLE_CONTROLLER_KINDS in session_kv_server.py, which takes the
Deployment a ReplicaSet is sent as in the ReplicaSet's place). A kind on one side only is dropped by the
next side without a word, so those are pinned here too.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER_GO = REPO_ROOT / "k8s-operator/cmd/k8s-event-watcher/owner.go"
POLICY_DIR = REPO_ROOT / "agents/platform/scripts"
SESSION_KV = POLICY_DIR / "session_kv_server.py"
GO_KINDS_OPEN = "var foldableControllerKinds = map[string]bool{"
GO_KINDS_CLOSE = "}"
# One entry: a quoted kind or a kind constant, mapped to true.
GO_KIND_ENTRY = re.compile(r'^\s*(?:"(\w+)"|(\w+))\s*:\s*true,\s*$')
GO_STRING_CONST = re.compile(r'^\s*(\w+)\s*=\s*"(\w+)"\s*$', re.MULTILINE)
GO_DEPLOYMENT_CONST = "kindDeployment"
GO_REPLICASET_CONST = "kindReplicaSet"
GO_ACCEPT = re.compile(r'^\s*podMetadataAccept\s*=\s*"([^"]+)"', re.MULTILINE)
# A named Pod path as the watcher builds it (AbsPath("/api/v1/namespaces", ns, "pods", name)).
POD_PATH = "api/v1/namespaces/default/pods/web-0"
TARGET = "kagents-dev/us-east4/fleet-a/"

sys.path.insert(0, str(POLICY_DIR))
import watcher_policy  # noqa: E402


def go_foldable_kinds(source: str) -> set[str]:
    """The kinds in foldableControllerKinds, or AssertionError on a line this cannot read."""
    constants = dict(GO_STRING_CONST.findall(source))
    lines = source.splitlines()
    if GO_KINDS_OPEN not in lines:
        raise AssertionError(f"no line {GO_KINDS_OPEN!r} declares foldableControllerKinds")
    start = lines.index(GO_KINDS_OPEN) + 1
    kinds: set[str] = set()
    for line in lines[start:lines.index(GO_KINDS_CLOSE, start)]:
        entry = GO_KIND_ENTRY.match(line)
        if entry is None or (entry.group(2) and entry.group(2) not in constants):
            raise AssertionError(f"foldableControllerKinds entry this test cannot read: {line!r}")
        kinds.add(entry.group(1) or constants[entry.group(2)])
    return kinds


def session_kv_foldable_kinds(source: str) -> set[str]:
    """FOLDABLE_CONTROLLER_KINDS, its names resolved from module-level string constants."""
    tree = ast.parse(source)
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "FOLDABLE_CONTROLLER_KINDS":
            [members] = node.value.args
            return {e.value if isinstance(e, ast.Constant) else constants[e.id] for e in members.elts}
    raise AssertionError(f"{SESSION_KV.relative_to(REPO_ROOT)} declares no FOLDABLE_CONTROLLER_KINDS")


class WatcherPodMetadataParityTest(unittest.TestCase):
    def test_the_accept_the_watcher_sends_is_one_the_broker_admits_for_a_named_pod(self) -> None:
        found = GO_ACCEPT.search(OWNER_GO.read_text(encoding="utf-8"))
        self.assertIsNotNone(found, f"{OWNER_GO.relative_to(REPO_ROOT)} declares no podMetadataAccept")
        accept = found.group(1)
        decision = watcher_policy.evaluate_k8s("GET", TARGET + POD_PATH, {}, accept)
        self.assertEqual(
            decision.rule_id, watcher_policy.RULE_K8S_POD_METADATA,
            f"the watcher's Accept {accept!r} is not one the broker's named-Pod rule admits",
        )

    def test_the_watcher_the_broker_and_session_kv_fold_the_same_controller_kinds(self) -> None:
        source = OWNER_GO.read_text(encoding="utf-8")
        go_kinds = go_foldable_kinds(source)
        self.assertTrue(go_kinds, "foldableControllerKinds is empty")
        self.assertEqual(
            go_kinds, set(watcher_policy.POD_METADATA_OWNER_KINDS),
            "foldableControllerKinds (Go) and POD_METADATA_OWNER_KINDS (broker) differ; a kind the "
            "broker does not relay reads as none through it, and one the watcher drops is relayed for nothing",
        )
        constants = dict(GO_STRING_CONST.findall(source))
        self.assertEqual(
            session_kv_foldable_kinds(SESSION_KV.read_text(encoding="utf-8")),
            go_kinds - {constants[GO_REPLICASET_CONST]} | {constants[GO_DEPLOYMENT_CONST]},
            "FOLDABLE_CONTROLLER_KINDS (session_kv) is not the watcher's kinds with Deployment for "
            "ReplicaSet; a controller_ref of a kind session_kv lacks folds by name without a word",
        )

    def test_a_kinds_entry_the_parser_cannot_read_fails_rather_than_drops(self) -> None:
        table = "\n".join(['\tkindX = "X"', GO_KINDS_OPEN, "\tkindX: true,", '\t"Job":  true,', "\t{EXTRA}", GO_KINDS_CLOSE])
        self.assertEqual(go_foldable_kinds(table.replace("\t{EXTRA}\n", "")), {"X", "Job"})
        for extra in ("unknownConst: true,", '"Rollout": false,', "otherKinds..."):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(AssertionError, "cannot read"):
                    go_foldable_kinds(table.replace("{EXTRA}", extra))


if __name__ == "__main__":
    unittest.main()
