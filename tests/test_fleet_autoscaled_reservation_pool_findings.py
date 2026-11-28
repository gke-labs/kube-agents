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

"""What the stockout audit files on the seeded fleet's `dev-reserved-pool`
and its Pending pod (bench/tf/fleet/main.tf and defects-a.tf, role
missing-reservation-autoscaled).

The pool autoscales from 0 to 1 node and names a reservation that does not
exist, and `reserved-batch` has one pod that only this pool can hold. The
shape was chosen so that the reservation check (3.10) does not name the pool,
and so that the Pending pod is the backlog of this pool and of no other. These
tests ask the collector itself, with the pools and the pod as GKE holds them,
so a change of a check that moves a finding onto the fixture, or that moves
the pod into another pool's backlog, fails here and not on a pool project.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"

sys.path.insert(0, str(SCRIPTS))

import fleet_stockout  # noqa: E402

PROJECT = "kube-agents-evals"
ZONE = "us-central1-a"
RESERVATION = {"consumeReservationType": "SPECIFIC_RESERVATION", "key": "compute.googleapis.com/reservation-name", "values": ["seeded-absent-reservation"]}
FLEET_LABELS = {"managed-by": "kube-agents-seeded-fleet"}


def _pool(name: str, machine: str, role: str | None, **more) -> dict:
    """A pool of slot a as `clusters describe` reports it: one zone, and the
    `seeded-role` label and taint for each pool but the default one."""
    config: dict = {"machineType": machine, "diskSizeGb": 20, "resourceLabels": FLEET_LABELS}
    if role:
        config["labels"] = {"seeded-role": role}
        config["taints"] = [{"key": "seeded-role", "value": role, "effect": "NO_SCHEDULE"}]
    config.update(more.pop("config", {}))
    return {"name": name, "status": "RUNNING", "locations": [ZONE], "config": config, **more}


# The fixture's pool, and the five other pools of slot a (main.tf).
DEV_RESERVED_POOL = _pool("dev-reserved-pool", "n2-standard-2", "missing-reservation-autoscaled", autoscaling={"enabled": True, "maxNodeCount": 1}, config={"reservationAffinity": RESERVATION})
MISSING_RESERVATION_POOL = _pool("missing-reservation-pool", "n2-standard-2", "missing-reservation", config={"reservationAffinity": RESERVATION})
PINNED_INFERENCE_POOL = _pool("pinned-inference-pool", "e2-small", "pinned-inference", autoscaling={"enabled": True, "minNodeCount": 1, "maxNodeCount": 1})
SLOT_A_POOLS = [
    _pool("default-pool", "e2-standard-2", None),
    _pool("idle-batch-pool", "e2-small", "idle-batch"),
    PINNED_INFERENCE_POOL,
    _pool("declared-zonal-pool", "e2-small", "declared-zonal", autoscaling={"enabled": True, "maxNodeCount": 2}),
    MISSING_RESERVATION_POOL,
    DEV_RESERVED_POOL,
]
# The scheduler's refusal for a pod whose selector no node meets.
NO_NODE_MATCHES = "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."


def _pending_pod(workload: str, namespace: str, selector: dict, role: str, message: str = NO_NODE_MATCHES) -> dict:
    return {
        "metadata": {
            "name": f"{workload}-7c9d8b6f5-x2x7k",
            "namespace": namespace,
            "labels": {"app": workload, "pod-template-hash": "7c9d8b6f5"},
            "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": f"{workload}-7c9d8b6f5", "controller": True}],
        },
        "spec": {"nodeSelector": selector, "tolerations": [{"key": "seeded-role", "operator": "Equal", "value": role, "effect": "NoSchedule"}]},
        "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": message}]},
    }


# The fixture's one pod (defects-a.tf), and a Pending replica of slot a's
# other backlog, inference-server on pinned-inference-pool.
RESERVED_BATCH_POD = _pending_pod(
    "reserved-batch", "seeded-reservation", {"cloud.google.com/gke-nodepool": "dev-reserved-pool", "seeded-role": "missing-reservation-autoscaled"}, "missing-reservation-autoscaled"
)
INFERENCE_POD = _pending_pod("inference-server", "seeded-capacity", {"seeded-role": "pinned-inference"}, "pinned-inference", "0/4 nodes are available: 1 Insufficient cpu, 3 node(s) didn't match Pod's node affinity/selector.")


def _pinned(pod: dict, pools: list[dict] = SLOT_A_POOLS) -> str | None:
    """The pool that the collector takes as the pod's only place. No pool of
    the two reservation pools has a node, so the index comes from the pool
    records alone."""
    return fleet_stockout.pinned_pool(pod, pools, fleet_stockout.pool_label_index(pools, []))


class ReservationCheckTest(unittest.TestCase):
    """Check 3.10(d) names a production pool that consumes only a named
    reservation, and `missing-reservation-pool` is the fixture for that. This
    pool is the fixture for the autoscaler's message (check 3.11), so it stays
    out of 3.10: one fault then gets one finding, on one pool each."""

    def test_the_reservation_check_does_not_name_the_pool(self):
        self.assertIsNone(fleet_stockout.reserved_pool(DEV_RESERVED_POOL, PROJECT))

    def test_the_reservation_check_still_names_the_pool_with_no_autoscaling(self):
        hit = fleet_stockout.reserved_pool(MISSING_RESERVATION_POOL, PROJECT)
        self.assertEqual(
            hit,
            {"pool": "missing-reservation-pool", "zones": [ZONE], "reservations": [(PROJECT, "seeded-absent-reservation")], "groups": []},
        )

    def test_a_name_with_no_non_production_part_puts_the_pool_into_the_reservation_check(self):
        # Why the pool's name starts with `dev`.
        for name in ("reserved-pool", "autoscaled-reservation-pool", "devreserved-pool"):
            with self.subTest(name=name):
                self.assertIsNotNone(fleet_stockout.reserved_pool({**DEV_RESERVED_POOL, "name": name}, PROJECT))

    def test_the_fleet_labels_and_the_role_label_do_not_make_a_pool_non_production(self):
        # The name alone keeps the pool out: the pool with no autoscaling has
        # the same kinds of labels and is named.
        config = DEV_RESERVED_POOL["config"]
        self.assertFalse(fleet_stockout.is_non_production("reserved-pool", {**config["resourceLabels"], **config["labels"]}))
        self.assertTrue(fleet_stockout.is_non_production("dev-reserved-pool"))


class BacklogTest(unittest.TestCase):
    """The fixture's Pending pod is the backlog of its own pool only."""

    def test_the_collector_reads_the_pod_as_unschedulable(self):
        self.assertTrue(fleet_stockout.is_unschedulable(RESERVED_BATCH_POD))

    def test_the_pod_pins_to_the_fixtures_pool(self):
        self.assertEqual(_pinned(RESERVED_BATCH_POD), "dev-reserved-pool")

    def test_the_other_backlog_of_slot_a_stays_on_its_own_pool(self):
        self.assertEqual(_pinned(INFERENCE_POD), "pinned-inference-pool")

    def test_without_the_pool_name_in_its_selector_the_pod_still_pins_to_the_pool(self):
        # The role label alone names the pool too. The pool name is in the
        # selector so that the pod pins there also if a second pool gets the label.
        by_role = copy.deepcopy(RESERVED_BATCH_POD)
        del by_role["spec"]["nodeSelector"]["cloud.google.com/gke-nodepool"]
        self.assertEqual(_pinned(by_role), "dev-reserved-pool")

    def test_without_its_toleration_the_pod_is_the_backlog_of_no_pool(self):
        bare = copy.deepcopy(RESERVED_BATCH_POD)
        bare["spec"]["tolerations"] = []
        self.assertIsNone(_pinned(bare))


class SingleZonePoolCheckTest(unittest.TestCase):
    """Check 3.9 names each autoscaled pool in one zone that has a taint, so
    it names the fixture's pool: one `major` background finding on slot a."""

    def finding(self, pool: dict, pods: list[dict]) -> dict | None:
        return fleet_stockout.check_single_zone_nodepool(pool, False, 0, unschedulable_pods=pods)

    def test_the_check_names_the_pool_with_its_one_pending_pod(self):
        hit = self.finding(DEV_RESERVED_POOL, [RESERVED_BATCH_POD])
        self.assertEqual(hit["object"], "NodePool/dev-reserved-pool")
        self.assertIn("tainted (seeded-role), so no other pool is counted as its fallback", hit["excerpt"])
        self.assertTrue(hit["excerpt"].endswith("; 1 Unschedulable pods of reserved-batch select this pool and cannot be placed"), hit["excerpt"])
        # With no node, the pool is not at its ceiling of one node.
        self.assertNotIn(fleet_stockout.CEILING_ARM_PREFIX, hit["excerpt"])

    def test_the_backlog_of_the_pinned_pool_names_inference_server_only(self):
        # The capacity cases read "Unschedulable pods of inference-server" on
        # pinned-inference-pool. The fixture's pod is not in that clause.
        pinned = [pod for pod in (INFERENCE_POD, RESERVED_BATCH_POD) if _pinned(pod) == "pinned-inference-pool"]
        hit = fleet_stockout.check_single_zone_nodepool(PINNED_INFERENCE_POOL, False, 1, unschedulable_pods=pinned)
        self.assertIn("1 Unschedulable pods of inference-server select this pool and cannot be placed", hit["excerpt"])
        self.assertNotIn("reserved-batch", hit["excerpt"])

    def test_the_pool_with_no_autoscaling_is_not_named(self):
        self.assertIsNone(self.finding(MISSING_RESERVATION_POOL, []))


if __name__ == "__main__":
    unittest.main()
