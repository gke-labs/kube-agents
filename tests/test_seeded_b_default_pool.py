"""seeded-b's default pool keeps room for GKE's kube-system pods and rolls in one step.

`bench/tf/fleet/main.tf`'s `seeded_b_default` runs two nodes because seeded-b's
second node (no-surge-pool) is tainted for pinned-batch-runner alone: on one
untainted e2-small, kube-dns's second replica and event-exporter stayed Pending
and workload_triage.py reported seeded-b as `system unhealthy`. It surges both
nodes at once because each node's step waits out the LB drain fixture's 720
seconds, so a one-at-a-time roll would take about 36 minutes and could pass
the fleet reconcile's one-hour ceiling beside a master update.
"""

import pathlib
import re
import unittest

_MAIN_TF = pathlib.Path(__file__).resolve().parents[1] / "bench" / "tf" / "fleet" / "main.tf"
_POOL_HEAD = 'resource "google_container_node_pool" "seeded_b_default" {'


def _pool_block() -> str:
    text = _MAIN_TF.read_text(encoding="utf-8")
    start = text.index(_POOL_HEAD)
    end = text.index("\n}\n", start)
    # Comments carry numbers too; only the HCL is read.
    return "\n".join(line for line in text[start:end].splitlines() if not line.lstrip().startswith("#"))


def _setting(block: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*(\S+)", block, re.MULTILINE)
    if match is None:
        raise AssertionError(f"seeded_b_default sets no {name}")
    return match.group(1)


def _nested(block: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*\{{(.*?)^\s*\}}", block, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"seeded_b_default has no {name} block")
    return match.group(1)


class SeededBDefaultPoolTest(unittest.TestCase):
    def test_two_nodes_so_kube_system_has_room_beside_the_tainted_node(self):
        self.assertEqual(_setting(_pool_block(), "node_count"), "2", "one untainted e2-small leaves kube-dns and event-exporter Pending")

    def test_both_nodes_surge_at_once_so_a_roll_is_one_drain_wait(self):
        upgrade = _nested(_pool_block(), "upgrade_settings")
        self.assertEqual(_setting(upgrade, "max_surge"), "2", "a surge of 1 rolls the two nodes one 720 s drain after the other")
        self.assertEqual(_setting(upgrade, "max_unavailable"), "0", "a node removed first is the planted no-surge-pool shape, not this pool's")

    def test_the_update_timeout_covers_the_one_step_roll(self):
        self.assertEqual(_setting(_nested(_pool_block(), "timeouts"), "update"), '"45m"', "a roll step is about 18 minutes; 45m clears the provider's 30-minute default inside the one-hour ceiling")


if __name__ == "__main__":
    unittest.main()
