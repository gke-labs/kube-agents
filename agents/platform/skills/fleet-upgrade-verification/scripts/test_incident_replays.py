#!/usr/bin/env python3
"""Incident replays for the surge-capacity rule (catalogue entry 2): field incidents replayed
as the inputs the rule reads, on the GPU cluster record and node list recorded under
test_rule_capacity.py and altered to each incident's shape. Each test says in its docstring
what happened and why the finding, filed before the maintenance window, prevents it. The
tests are named by date and scenario only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from readiness_rules import capacity  # noqa: E402
from test_rule_capacity import GPU_TARGET, evaluate, node_with, pod_range, recorded, scheduled_pod, surge  # noqa: E402

GPU_LABEL = {"cloud.google.com/gke-accelerator": "nvidia-l4"}
GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
GPU_RESOURCE = "nvidia.com/gpu"
REMEDY_RESERVATION_FIRST = "; hold a reservation for the surge node, or, on a pool with a spare accelerator node for the drained pods, set the in-place strategy (max-surge 0, max-unavailable 1)"
REMEDY_POD_RANGE = "lower max-surge or add a secondary Pod range"
# The incident's Pod range: a /22, four /24 node blocks, every one held by a node.
FULL_POD_RANGE = "10.4.0.0/22"
EXTRA_NODE_BLOCKS = ("10.4.2.0/24", "10.4.3.0/24")


class SurgeRuleIncidentReplayTest(unittest.TestCase):
    def test_2025_04_28_gpu_pool_upgrade_stalled_halfway_on_accelerator_capacity(self):
        """A GPU node pool's upgrade stalled halfway when the zone had no accelerator capacity
        for its surge node and left the pool on two versions; before the window the rule files a
        risk on the surging accelerator pool with no reservation (the upgrade waits with the old
        node serving, so it is not a blocker), naming the reservation first and
        the in-place strategy only for a pool with a spare node, so the operator holds one or
        changes the strategy before the upgrade starts."""
        record, nodes = recorded()
        surge(record, 1)  # the L4 pool on the surge strategy, maxSurge 1, no reservationAffinity, its one node
        trainer = scheduled_pod("ml", "trainer", nodes[1]["metadata"]["name"], node_selector=GPU_LABEL, tolerations=[GPU_TOLERATION], cpu="1", memory="4Gi")
        trainer["spec"]["containers"][0]["resources"]["requests"][GPU_RESOURCE] = "1"
        out = evaluate(record, nodes + [trainer], target=GPU_TARGET)
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        self.assertEqual([f["finding"] for f in out["risks"]], ["accelerator"])
        finding = out["risks"][0]
        self.assertEqual((finding["pool"], finding["accelerators"], finding["max_surge"], finding["reservation"]), ("gpu-pool", "1 x nvidia-l4", 1, None))
        text = capacity.describe(finding)
        self.assertIn("pool gpu-pool (1 x nvidia-l4) surges (maxSurge 1) with no reservation affinity: each surge node needs accelerator capacity the zone may not have, and a pool upgrade that cannot create one stalls between versions", text)
        self.assertTrue(text.endswith(REMEDY_RESERVATION_FIRST), text)
        self.assertLess(text.index("hold a reservation"), text.index("in-place strategy"))

    def test_2025_09_new_pool_could_not_register_nodes_on_a_full_pod_range(self):
        """A new node pool's nodes never registered because the cluster's Pod secondary range had
        no free per-node block left; before the window the rule counts the range's free blocks
        against the surge need and blocks at zero, naming the free count and the two ways out,
        so the operator lowers max-surge or adds a secondary range before a surge node is
        created with no block to take."""
        record, nodes = recorded()
        pod_range(record, FULL_POD_RANGE)
        nodes += [node_with(block, f"gke-gpu-canary-default-pool-cbd06cf1-{i}", "default-pool") for i, block in enumerate(EXTRA_NODE_BLOCKS, start=2)]
        out = evaluate(record, nodes, target=GPU_TARGET)
        self.assertEqual((out["risks"], out["unknown"]), ([], []))
        self.assertEqual([f["finding"] for f in out["blocking"]], ["pod-range"])
        finding = out["blocking"][0]
        self.assertEqual({key: finding[key] for key in ("range", "prefix", "free", "held", "total", "need")}, {"range": FULL_POD_RANGE, "prefix": 24, "free": 0, "held": 4, "total": 4, "need": 1})
        self.assertEqual(finding["pools"], [{"pool": "default-pool", "max_surge": 1}])
        text = capacity.describe(finding)
        self.assertIn("pod range 10.4.0.0/22 has 0 free /24 block(s) (4 of 4 hold a node) and the 1 surge node(s) of pool(s) default-pool (maxSurge 1) each need one: a surge node with no block never registers and the pool upgrade stalls between versions", text)
        self.assertTrue(text.endswith(REMEDY_POD_RANGE), text)


if __name__ == "__main__":
    unittest.main()
