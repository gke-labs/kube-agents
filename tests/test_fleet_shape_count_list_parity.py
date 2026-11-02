"""The fleet-shape probe's count lists are declared twice: the paths and the
Accept the event watcher sends (fleetShapeListPaths and fleetShapeAccept in
k8s-operator/cmd/k8s-event-watcher/fleetshape.go) and the paths and Accept
the credential broker admits (COUNT_LIST_PATHS and accepts_metadata_only in
agents/platform/scripts/watcher_policy.py). Each side's own tests pin its own
copy, so a kind added on one side with its test updated is green on both
suites and refused by the broker on every profile cluster at the next deploy.
This test fails when the two copies disagree.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FLEETSHAPE_GO = REPO_ROOT / "k8s-operator/cmd/k8s-event-watcher/fleetshape.go"
POLICY_DIR = REPO_ROOT / "agents/platform/scripts"
# One entry of fleetShapeListPaths: `{path: "/api/v1/pods", pods: true},` or
# `{path: "/apis/apps/v1/deployments"},`.
GO_LIST_PATH = re.compile(r'^\s*\{path:\s*"([^"]+)"', re.MULTILINE)
GO_ACCEPT = re.compile(r'^\s*fleetShapeAccept\s*=\s*"([^"]+)"', re.MULTILINE)
GO_LIMIT = re.compile(r'^\s*fleetShapeListLimit\s*=\s*"([^"]+)"', re.MULTILINE)

sys.path.insert(0, str(POLICY_DIR))
import watcher_policy  # noqa: E402


def go_source() -> str:
    return FLEETSHAPE_GO.read_text(encoding="utf-8")


class FleetShapeCountListParity(unittest.TestCase):
    def test_the_watcher_lists_exactly_the_paths_the_broker_counts(self) -> None:
        paths = GO_LIST_PATH.findall(go_source())
        self.assertTrue(paths, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} declares no fleetShapeListPaths entries")
        self.assertEqual(
            set(paths), {"/" + p for p in watcher_policy.COUNT_LIST_PATHS},
            "fleetShapeListPaths (Go) and COUNT_LIST_PATHS (Python) differ; the broker refuses "
            "what the watcher lists and every profile cluster reads as failed",
        )
        self.assertEqual(len(paths), len(set(paths)), f"fleetShapeListPaths repeats a path: {paths}")

    def test_the_accept_the_watcher_sends_is_one_the_broker_admits(self) -> None:
        found = GO_ACCEPT.search(go_source())
        self.assertIsNotNone(found, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} declares no fleetShapeAccept")
        accept = found.group(1)
        self.assertTrue(
            watcher_policy.accepts_metadata_only(accept),
            f"the watcher's Accept {accept!r} is not one the broker's count-list rule admits",
        )
        decision = watcher_policy.evaluate_k8s(
            "GET", "kagents-dev/us-east4/fleet-a/" + sorted(watcher_policy.COUNT_LIST_PATHS)[0],
            {"limit": ["1"]}, accept,
        )
        self.assertEqual(decision.rule_id, watcher_policy.RULE_K8S_COUNT_LIST)

    def test_the_limit_the_watcher_sends_is_the_query_the_broker_admits(self) -> None:
        found = GO_LIMIT.search(go_source())
        self.assertIsNotNone(found, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} declares no fleetShapeListLimit")
        self.assertEqual({"limit": [found.group(1)]}, watcher_policy.COUNT_LIST_QUERY)


if __name__ == "__main__":
    unittest.main()
