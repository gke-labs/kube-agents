#!/usr/bin/env python3
"""Tests for fleet_stockout.py, the stockout-prevention collector.

The `capacity-history` and `cluster-autoscaler-visibility` fixtures below are
trimmed copies of real responses read against `adamparco-kage` on 2026-08-29,
not shapes invented to match the parser. Both APIs were the reason those two
checks stayed prose-only, so a hand-written fixture would re-create exactly the
problem converting them was meant to solve."""

import json
import os
import shlex
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_stockout as fs  # noqa: E402


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fs.Run:
    return fs.Run(["x"], rc, stdout, stderr, 0.01)


def dump_of(*items) -> dict:
    return {"items": list(items)}


def compute_class(name, priorities, node_pool_auto_creation=True):
    return {
        "kind": "ComputeClass",
        "metadata": {"name": name},
        "spec": {"priorities": priorities, "nodePoolAutoCreation": {"enabled": node_pool_auto_creation}},
    }


def deployment(name, ns="default", node_selector=None, containers=None, tolerations=None):
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": {"nodeSelector": node_selector or {}, "containers": containers or [{"name": "app"}], "tolerations": tolerations or []}}},
    }


def statefulset(name, ns="default", node_selector=None, storage_class_name=None):
    vcts = [{"spec": {"storageClassName": storage_class_name}}] if storage_class_name else []
    return {
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": {"nodeSelector": node_selector or {}}}, "volumeClaimTemplates": vcts},
    }


def storage_class(name, provisioner="pd.csi.storage.gke.io", params=None):
    return {"kind": "StorageClass", "metadata": {"name": name}, "provisioner": provisioner, "parameters": params or {}}


def node(name, pool):
    return {"kind": "Node", "metadata": {"name": name, "labels": {"cloud.google.com/gke-nodepool": pool}}}


def capacity_history(rates, machine_type="n2-standard-8", price=None):
    """A `gcloud beta compute advice capacity-history` response.

    The frame is verbatim from the live read: a bare object rather than a list,
    `preemptionRate` as a fraction, and a `listPrice` carrying `nanos` with no
    `units` — 0.110192 USD/h, which is what a Spot n2-standard-8 in us-east4
    actually cost that day and is under one currency unit, the case that made
    reading `units` alone wrong."""
    return {
        "location": "https://www.googleapis.com/compute/beta/projects/acme/regions/us-central1",
        "machineType": machine_type,
        "preemptionHistory": [
            {
                "interval": {"startTime": f"2026-08-{i + 1:02d}T07:00:00Z", "endTime": f"2026-08-{i + 2:02d}T07:00:00Z"},
                "preemptionRate": rate,
            }
            for i, rate in enumerate(rates)
        ],
        "priceHistory": [
            {
                "interval": {"startTime": "2026-08-24T07:00:00Z", "endTime": "2026-08-29T07:00:00Z"},
                "listPrice": price if price is not None else {"currencyCode": "USD", "nanos": 110192000},
            }
        ],
    }


# The `errorMsg` arm, verbatim from the entry `adam-new-cluster` logged on
# 2026-08-14 — the affected instance group arrives as a full resource URL in
# `parameters[0]`, which is why the excerpt reports only its last segment.
ERROR_MSG_ENTRY = {
    "timestamp": "2026-08-14T00:05:02.817419660Z",
    "jsonPayload": {
        "resultInfo": {
            "measureTime": "1786665900",
            "results": [
                {
                    "eventId": "63b7917e-ed60-4c15-98d1-0f74797b4c8f",
                    "errorMsg": {
                        "messageId": "scale.up.error.out.of.resources",
                        "parameters": [
                            "https://www.googleapis.com/compute/v1/projects/acme/zones/"
                            "us-central1-b/instanceGroups/gk3-prod-usc1-pool-3-b07eba62-grp"
                        ],
                    },
                }
            ],
        }
    },
}

# The node-auto-provisioning arm. A cluster failing this way never gets as far
# as a scale-up attempt, so it writes nothing under `resultInfo` at all.
NAP_ENTRY = {
    "timestamp": "2026-08-14T01:00:00Z",
    "jsonPayload": {
        "noDecisionStatus": {
            "noScaleUp": {
                "unhandledPodGroups": [
                    {
                        "napFailureReasons": [
                            {"messageId": "scale.up.error.quota.exceeded", "parameters": ["CPUS"]}
                        ]
                    }
                ]
            }
        }
    },
}

# A healthy tick. The SOP's log filter does not return it; it carries neither
# arm, so an entry like it that arrives anyway must count for nothing.
HEALTHY_ENTRY = {"timestamp": "2026-08-14T02:00:00Z", "jsonPayload": {"status": "ok"}}


class EnumerateClustersTest(unittest.TestCase):
    def test_reads_cluster_level_node_auto_provisioning(self):
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1-a", "status": "RUNNING", "autoscaling": {"enableNodeAutoprovisioning": True}},
                {"name": "c2", "location": "us-central1-a", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            return run_of(0, clusters_json)

        clusters, not_running = fs.enumerate_clusters("acme", run=run)
        self.assertTrue(next(c for c in clusters if c["name"] == "c1")["has_nap"])
        self.assertFalse(next(c for c in clusters if c["name"] == "c2")["has_nap"])
        self.assertEqual(not_running, [])

    def test_reads_the_control_plane_version(self):
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING", "currentMasterVersion": "1.35.3-gke.1200"}])
        issued = []

        def run(argv, **kwargs):
            issued.append(argv)
            return run_of(0, clusters_json)

        clusters, _ = fs.enumerate_clusters("acme", run=run)
        self.assertEqual(clusters[0]["version"], "1.35.3-gke.1200")
        self.assertIn("currentMasterVersion", issued[0][-1])

    def test_a_cluster_that_is_not_running_comes_back_as_an_unreachable_target(self):
        # Dropped rather than recorded, a DEGRADED cluster is indistinguishable
        # from one that does not exist, and the run can publish a fleet-wide
        # all-clear over a fleet quietly missing it.
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1-a", "status": "RUNNING"},
                {"name": "sick", "location": "us-east4", "status": "DEGRADED"},
            ]
        )

        def run(argv, **kwargs):
            return run_of(0, clusters_json)

        clusters, not_running = fs.enumerate_clusters("acme", run=run)
        self.assertEqual([c["name"] for c in clusters], ["c1"])
        self.assertEqual(len(not_running), 1)
        self.assertEqual(not_running[0]["name"], "acme/us-east4/sick")
        self.assertEqual(not_running[0]["outcome"], "unreachable")
        self.assertEqual(not_running[0]["location"], "us-east4")
        self.assertIn("DEGRADED", not_running[0]["error"])

    def test_an_answer_that_is_not_a_list_of_clusters_fails_the_listing(self):
        """A non-empty object crashed on its string keys, and `{}` iterated
        nothing and read as a project with no cluster."""
        for answer in ('{"error": "denied"}', "{}", '[{"name": "c1", "status": "RUNNING"}, "c2"]'):
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(RuntimeError, "not a list of clusters"):
                    fs.enumerate_clusters("acme", run=lambda argv, **kwargs: run_of(0, answer))

    def test_the_cluster_s_node_locations_are_read(self):
        """§3.1's zone span needs the zones auto-created pools land in, which
        the cluster's `locations` and auto-provisioning locations give."""
        seen = []
        clusters_json = json.dumps([{
            "name": "c1", "location": "us-central1", "status": "RUNNING",
            "locations": ["us-central1-a", "us-central1-b", "us-central1-c"],
            "autoscaling": {"autoprovisioningLocations": ["us-central1-a"]},
        }])

        def run(argv, **kwargs):
            seen.append(argv)
            return run_of(0, clusters_json)

        [cluster], _ = fs.enumerate_clusters("acme", run=run)
        self.assertIn("locations", seen[0][-1])
        self.assertIn("autoscaling.autoprovisioningLocations", seen[0][-1])
        self.assertEqual(cluster["locations"], ["us-central1-a", "us-central1-b", "us-central1-c"])
        self.assertEqual(cluster["autoprovisioning_locations"], ["us-central1-a"])

    def test_a_reconciling_cluster_is_enumerated(self):
        """A reconcile is work in progress on a cluster whose API server stays
        up, and any config change causes one. Skipping it dropped the cluster
        from the audit for the duration -- see `AUDITABLE_STATUSES`."""
        clusters_json = json.dumps(
            [
                {"name": "busy", "location": "us-east4", "status": "RECONCILING"},
                {"name": "new", "location": "us-west1", "status": "PROVISIONING"},
            ]
        )

        def run(argv, **kwargs):
            return run_of(0, clusters_json)

        clusters, not_running = fs.enumerate_clusters("acme", run=run)
        self.assertEqual([c["name"] for c in clusters], ["busy"])
        self.assertEqual([c["name"] for c in not_running], ["acme/us-west1/new"])


class RegionOfTest(unittest.TestCase):
    def test_zonal_location(self):
        self.assertEqual(fs.region_of("us-central1-a"), "us-central1")

    def test_regional_location(self):
        self.assertEqual(fs.region_of("us-central1"), "us-central1")

    def test_empty(self):
        self.assertEqual(fs.region_of(""), "")


class CccMissingFallbacksTest(unittest.TestCase):
    def test_flags_single_family_single_priority(self):
        # Nothing varies, so the verdict needs no zone span and files.
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}])
        hit = fs.check_ccc_missing_fallbacks(cc)
        self.assertIsNotNone(hit)
        self.assertNotIn("unevaluated", hit)

    def test_flags_family_and_spot_only_varying_one_dimension(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}, {"machineFamily": "c3", "spot": True}])
        # Only the spot dimension varies -- family is constant, no size, no
        # zones -- on a cluster read as spanning one zone. With the span
        # unread this chain is unevaluated instead; see below.
        hit = fs.check_ccc_missing_fallbacks(cc, 1)
        self.assertIsNotNone(hit)
        self.assertNotIn("unevaluated", hit)

    def test_does_not_flag_family_and_spot_both_varying(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}, {"machineFamily": "n4", "spot": True}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_does_not_flag_multi_zone_and_family(self):
        # `location.zones` is the CRD field. This fixture used to write a
        # top-level `zones`, which no ComputeClass has, so it passed on the
        # family dimension alone and never exercised the zone one.
        cc = compute_class(
            "cc1",
            [
                {"machineFamily": "c3", "location": {"zones": ["us-central1-a"]}},
                {"machineFamily": "n4", "location": {"zones": ["us-central1-b"]}},
            ],
        )
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_zone_variation_alone_is_a_dimension_and_the_excerpt_shows_it(self):
        """Zone was scored 0 on every class in the fleet: the code read a
        top-level `priorities[].zones` and the CRD spells it `location.zones`,
        so §3.1 counted three dimensions while publishing "N/4"."""
        cc = compute_class(
            "cc1",
            [
                {"machineFamily": "c3", "location": {"zones": ["us-central1-a"]}},
                {"machineFamily": "c3", "location": {"zones": ["us-central1-b"]}},
            ],
        )
        hit = fs.check_ccc_missing_fallbacks(cc)
        self.assertIsNotNone(hit)  # one dimension varies, not two
        self.assertIn("1/4", hit["excerpt"])
        self.assertIn("us-central1-b", hit["excerpt"])

    def test_specific_reservation_zones_count_as_zone_variation(self):
        # `location.zones` cannot be combined with `affinity: Specific`, so a
        # chain using specific reservations spells its spread in
        # `reservations.specific[].zones` instead.
        cc = compute_class(
            "cc1",
            [
                {"machineFamily": "c3", "reservations": {"affinity": "Specific", "specific": [{"zones": ["us-central1-a"]}]}},
                {"machineFamily": "n4", "reservations": {"affinity": "Specific", "specific": [{"zones": ["us-central1-b"]}]}},
            ],
        )
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_a_top_level_zones_key_is_not_the_crd_field(self):
        # The spelling that masked the bug. Nothing reads it, so a chain
        # varying only this still scores 1/4 on family alone.
        cc = compute_class(
            "cc1",
            [
                {"machineFamily": "c3", "zones": ["us-central1-a"]},
                {"machineFamily": "n4", "zones": ["us-central1-b"]},
            ],
        )
        hit = fs.check_ccc_missing_fallbacks(cc)
        self.assertIsNotNone(hit)
        self.assertIn("zones=[]", hit["excerpt"])

    def test_no_priorities_is_not_a_crash(self):
        self.assertIsNone(fs.check_ccc_missing_fallbacks(compute_class("cc1", [])))

    THREE_ZONES = {"zones": ["us-central1-a", "us-central1-b", "us-central1-c"]}
    FAMILIES = ("c3", "n4", "n2")

    def test_a_multi_family_chain_sharing_three_zones_is_not_flagged(self):
        """§3.1's own Do-NOT-flag example: multi-zone `c3` falling back to
        `n4` and `n2`, the same zones on every priority."""
        cc = compute_class("cc1", [{"machineFamily": f, "location": self.THREE_ZONES} for f in self.FAMILIES])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc, cluster_zones=1))

    def test_a_multi_family_chain_naming_no_zone_on_a_regional_cluster_is_not_flagged(self):
        cc = compute_class("cc1", [{"machineFamily": f} for f in self.FAMILIES])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc, cluster_zones=3))

    def test_a_multi_family_chain_sharing_one_zone_is_still_flagged(self):
        single = {"zones": ["us-central1-a"]}
        cc = compute_class("cc1", [{"machineFamily": f, "location": single} for f in self.FAMILIES])
        hit = fs.check_ccc_missing_fallbacks(cc, cluster_zones=3)
        # A finding, not an unevaluated verdict: the one shared zone is known.
        self.assertNotIn("unevaluated", hit)
        self.assertIn("1/4", hit["excerpt"])
        self.assertIn("zones=[('us-central1-a',)]", hit["excerpt"])

    def test_no_zone_on_a_single_zone_cluster_is_still_flagged(self):
        cc = compute_class("cc1", [{"machineFamily": f} for f in self.FAMILIES])
        self.assertIsNotNone(fs.check_ccc_missing_fallbacks(cc, cluster_zones=1))

    def test_no_zone_where_the_cluster_span_is_unknown_is_left_unevaluated(self):
        """The span decides this chain, so it is not a finding either way."""
        cc = compute_class("cc1", [{"machineFamily": f} for f in self.FAMILIES])
        self.assertTrue(fs.check_ccc_missing_fallbacks(cc)["unevaluated"])

    def test_one_shared_zone_is_flagged_whatever_the_span(self):
        """Every priority names the same single zone: the span cannot rescue it."""
        cc = compute_class("cc1", [{"machineFamily": f, "location": {"zones": ["us-central1-a"]}} for f in self.FAMILIES])
        hit = fs.check_ccc_missing_fallbacks(cc)
        self.assertNotIn("unevaluated", hit)

    def test_nothing_else_varied_is_flagged_whatever_the_span(self):
        """One family, no zone: even a multi-zone span leaves one dimension."""
        cc = compute_class("cc1", [{"machineFamily": "c3"}, {"machineFamily": "c3"}])
        hit = fs.check_ccc_missing_fallbacks(cc)
        self.assertNotIn("unevaluated", hit)


    NO_MACHINE_CHAINS = {
        "nodepools": [{"nodepools": ["a"]}, {"nodepools": ["b"]}],
        "gpu only": [{"gpu": {"type": "nvidia-l4", "count": 1}}],
    }

    def test_a_chain_naming_no_machine_is_unevaluated_not_critical(self):
        """A `nodepools` or accelerator-only rule names no family to score."""
        for label, chain in self.NO_MACHINE_CHAINS.items():
            for zones in (None, 1, 3):
                with self.subTest(label, cluster_zones=zones):
                    hit = fs.check_ccc_missing_fallbacks(compute_class("cc1", chain), zones)
                    self.assertIn(fs.CCC_MACHINE_UNNAMED, hit["unevaluated"])
                    # The unread span is a third unknown only where it was not read.
                    self.assertEqual(fs.CCC_SPAN_UNREAD in hit["unevaluated"], zones is None)

    def test_a_mixed_chain_the_named_priorities_already_pass_is_clean(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}, {"machineFamily": "n4", "spot": True}, {"nodepools": ["a"]}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc, 1))

    def test_a_mixed_chain_the_named_priorities_do_not_pass_is_unevaluated(self):
        """The pool could be the second family the chain is short of."""
        cc = compute_class("cc1", [{"machineFamily": "c3"}, {"nodepools": ["a"]}])
        self.assertEqual(fs.check_ccc_missing_fallbacks(cc, 1)["unevaluated"], (fs.CCC_MACHINE_UNNAMED,))

    def test_class_level_default_zones_count(self):
        """`redis-memory-optimized` from the gke-compute-classes assets: three
        families, its zones only in `priorityDefaults`, on a one-zone cluster."""
        cc = compute_class("redis-memory-optimized", [
            {"machineFamily": f, "minCores": 8, "minMemoryGb": 64, "spot": False} for f in ("c4d", "n4d", "c4")
        ])
        cc["spec"]["priorityDefaults"] = {"location": {"zones": ["us-central1-a", "us-central1-b", "us-central1-c"]}}
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc, 1))

    def test_default_zones_do_not_reach_a_specific_reservation(self):
        """`affinity: Specific` cannot combine with a class-level location, so
        its priorities keep only the zones their reservations name."""
        reserved = {"affinity": "Specific", "specific": [{"name": "r1", "zones": ["us-central1-a"]}]}
        cc = compute_class("cc1", [{"machineFamily": f, "reservations": reserved} for f in ("c3", "n4")])
        cc["spec"]["priorityDefaults"] = {"location": {"zones": ["us-central1-a", "us-central1-b"]}}
        self.assertIsNotNone(fs.check_ccc_missing_fallbacks(cc, 1))

    def test_a_priority_s_own_zones_are_not_widened_by_the_default(self):
        cc = compute_class("cc1", [{"machineFamily": f, "location": {"zones": ["us-central1-a"]}} for f in ("c3", "n4")])
        cc["spec"]["priorityDefaults"] = {"location": {"zones": ["us-central1-a", "us-central1-b"]}}
        self.assertNotIn("unevaluated", fs.check_ccc_missing_fallbacks(cc, 1))

    def test_a_pod_family_fallback_in_a_mixed_chain_is_unevaluated(self):
        """GKE picks the pod-family priority's family and size, either of which
        could be the dimension the chain is short of."""
        cc = compute_class("cc1", [{"machineFamily": "c3"}, {"podFamily": "general-purpose"}])
        for zones in (1, 3):
            with self.subTest(cluster_zones=zones):
                self.assertEqual(fs.check_ccc_missing_fallbacks(cc, zones)["unevaluated"], (fs.CCC_MACHINE_UNNAMED,))

    def test_every_unknown_dimension_s_reason_is_listed(self):
        """An unread span and a pool-targeted priority both leave dimensions open."""
        cc = compute_class("cc1", [{"machineFamily": "c3"}, {"nodepools": ["a"]}])
        self.assertEqual(fs.check_ccc_missing_fallbacks(cc)["unevaluated"], (fs.CCC_SPAN_UNREAD, fs.CCC_MACHINE_UNNAMED))

    def test_custom_shapes_size_by_vcpus_not_memory(self):
        """`n2-custom-4-8192` ends in its memory in MB; reading the trailing
        number as the size made two 4-vCPU shapes of different memory vary
        size, and a 4- and an 8-vCPU shape of equal memory not vary it."""
        varied = compute_class("cc1", [{"machineType": "n2-custom-4-8192", "spot": False}, {"machineType": "n2d-custom-8-8192", "spot": False}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(varied, 1))
        # Only memory differs, on a one-zone cluster: no dimension varies.
        same = compute_class("cc1", [{"machineType": "n2-custom-4-8192"}, {"machineType": "n2-custom-4-16384"}])
        hit = fs.check_ccc_missing_fallbacks(same, 1)
        self.assertIsNotNone(hit)
        self.assertIn("vary 0/4", hit["excerpt"])
        self.assertIn("sizes=['4']", hit["excerpt"])

    def test_standard_shapes_keep_their_size_classes(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-4", "spot": False}, {"machineType": "n2-standard-8", "spot": True}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc, 1))

    def test_an_unparsed_size_one_dimension_short_is_unevaluated(self):
        """An accelerator shape names no vCPU count, so the chain's size may
        be the second dimension it is short of."""
        cc = compute_class("cc1", [{"machineType": "a2-highgpu-1g", "spot": False}, {"machineType": "a2-highgpu-2g", "spot": True}])
        self.assertEqual(fs.check_ccc_missing_fallbacks(cc, 1)["unevaluated"], (fs.CCC_SIZE_UNKNOWN,))


class ClusterZoneSpanTest(unittest.TestCase):
    def test_the_node_pools_locations_decide(self):
        pools = [{"locations": ["us-central1-a"]}, {"locations": ["us-central1-a", "us-central1-b"]}]
        self.assertEqual(fs.cluster_zone_span({"location": "us-central1"}, pools, True), 2)

    def test_a_regional_cluster_with_single_zone_pools_spans_one(self):
        pools = [{"locations": ["us-central1-a"]}]
        self.assertEqual(fs.cluster_zone_span({"location": "us-central1"}, pools, True), 1)

    def test_the_cluster_s_node_locations_outweigh_a_narrower_pool(self):
        """A regional cluster's auto-created pools span its node locations,
        whatever zone its one existing pool sits in."""
        cluster = {"location": "us-central1", "locations": ["us-central1-a", "us-central1-b", "us-central1-c"]}
        pools = [{"locations": ["us-central1-a"]}]
        self.assertEqual(fs.cluster_zone_span(cluster, pools, True), 3)
        cc = compute_class("cc1", [{"machineFamily": "n4"}, {"machineFamily": "n4", "spot": True}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc, fs.cluster_zone_span(cluster, pools, True)))

    def test_the_widest_of_the_three_counts_is_taken(self):
        pools = [{"locations": ["us-central1-a", "us-central1-b"]}]
        for cluster, span in (
            ({"locations": ["us-central1-a"]}, 2),
            ({"locations": ["us-central1-a"], "autoprovisioning_locations": ["us-central1-a", "us-central1-b", "us-central1-c"]}, 3),
        ):
            with self.subTest(cluster=cluster):
                self.assertEqual(fs.cluster_zone_span(cluster, pools, True), span)

    def test_node_locations_answer_when_the_pools_were_not_read(self):
        self.assertEqual(fs.cluster_zone_span({"locations": ["us-central1-a", "us-central1-b"]}, [], False), 2)

    def test_autopilot_spans_more_than_one(self):
        self.assertGreater(fs.cluster_zone_span({"autopilot": True}, [], False), 1)

    def test_an_unread_pool_list_is_unknown(self):
        self.assertIsNone(fs.cluster_zone_span({"location": "us-central1"}, [], False))

class CccPodFamilyChainTest(unittest.TestCase):
    """§3.1 on chains that name a `podFamily`, which pins no machine family."""

    def test_does_not_flag_a_pod_family_chain(self):
        """GKE's built-in `autopilot`, verbatim: one priority, no machine family.

        It pins nothing -- GKE picks the shape -- but `_priority_family` cannot
        read `podFamily`, so `families` used to come back empty and the empty set
        scored 0/4 varied, the same as a chain genuinely pinned to one family.
        """
        cc = compute_class("cc1", [{"podFamily": "general-purpose"}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_does_not_flag_a_spot_pod_family_chain(self):
        """`autopilot-spot`, which pins no machine family here either.

        §3.2 has something to say about the Spot-ness, but only when an
        inference workload selects the class — see
        `CccNoOndemandFloorTest`. Unreferenced, neither section flags it.
        """
        cc = compute_class("cc1", [{"podFamily": "general-purpose", "spot": True}])
        self.assertIsNone(fs.check_ccc_missing_fallbacks(cc))

    def test_a_chain_that_only_partly_delegates_is_unevaluated(self):
        """A mixed chain was hand-authored, but its pod-family entry leaves the
        family and size to GKE, so it is neither exempt nor a `critical`."""
        cc = compute_class("cc1", [{"podFamily": "general-purpose"}, {"machineFamily": "c3"}])
        self.assertEqual(fs.check_ccc_missing_fallbacks(cc, 1)["unevaluated"], (fs.CCC_MACHINE_UNNAMED,))

    def test_a_pod_family_naming_a_machine_type_is_still_a_pin(self):
        """`podFamily` alongside an explicit shape delegates nothing."""
        cc = compute_class("cc1", [{"podFamily": "general-purpose", "machineType": "c3-standard-4"}])
        self.assertIsNotNone(fs.check_ccc_missing_fallbacks(cc))


class CccNoOndemandFloorTest(unittest.TestCase):
    def test_flags_all_spot(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}, {"machineFamily": "n4", "spot": True}])
        self.assertIsNotNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_does_not_flag_with_ondemand_floor(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}, {"machineFamily": "n4", "spot": False}])
        self.assertIsNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_flags_an_inference_class_that_tries_spot_first(self):
        """§3.2's second arm: the On-Demand floor exists but comes after Spot,
        so a serving pod is still preempted before it gets there."""
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}, {"machineFamily": "n4", "spot": False}])
        hit = fs.check_ccc_no_ondemand_floor(cc, True)
        self.assertIn("Spot first with an On-Demand fallback", hit["excerpt"])
        self.assertNotIn("severity", hit)

    def test_does_not_flag_an_inference_class_that_tries_on_demand_first(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": False}, {"machineFamily": "n4", "spot": True}])
        self.assertIsNone(fs.check_ccc_no_ondemand_floor(cc, True))

    def test_recognizes_provisioning_model_spelling(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "provisioningModel": "SPOT"}])
        self.assertIsNotNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_default_severity_is_major(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        self.assertEqual(fs.SEVERITY["ccc-no-ondemand-floor"], "major")
        hit = fs.check_ccc_no_ondemand_floor(cc, False)
        self.assertNotIn("severity", hit)

    def test_escalates_to_critical_when_referenced_by_inference_workload(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        hit = fs.check_ccc_no_ondemand_floor(cc, True)
        self.assertEqual(hit["severity"], "critical")

    def test_does_not_flag_the_built_in_autopilot_spot(self):
        """GKE's own class, verbatim: `{podFamily, spot}` and nothing else.

        Spot-only is the definition of `autopilot-spot`, not a mistake in it,
        and §3.2's `kind: manifest` remediation has nothing to append to on an
        object GKE reconciles — §3.1 excludes the same three classes for that
        exact reason. Unguarded it fired once per Autopilot cluster on every
        run: 17 of the 2026-08-30 run's 18 findings, against a class no
        workload on the fleet even selects.
        """
        cc = compute_class("autopilot-spot", [{"podFamily": "general-purpose", "spot": True}])
        self.assertIsNone(fs.check_ccc_no_ondemand_floor(cc, False))

    def test_still_flags_the_built_in_when_an_inference_workload_selects_it(self):
        """The escalation is worth a finding whose remediation must be manual."""
        cc = compute_class("autopilot-spot", [{"podFamily": "general-purpose", "spot": True}])
        hit = fs.check_ccc_no_ondemand_floor(cc, True)
        self.assertEqual(hit["severity"], "critical")

    def test_a_hand_authored_spot_chain_that_also_names_a_pod_family_is_still_flagged(self):
        """`all()`, not `any()` — same reasoning as §3.1's guard. A chain mixing
        the two was written by a person, and its machine-typed entry is a real
        Spot pin with a real manifest to fix."""
        cc = compute_class("cc1", [{"podFamily": "general-purpose", "spot": True},
                                   {"machineFamily": "c3", "spot": True}])
        self.assertIsNotNone(fs.check_ccc_no_ondemand_floor(cc, False))


class CccLargeVmScarcityTest(unittest.TestCase):
    def test_flags_large_machine_with_one_family(self):
        cc = compute_class("cc1", [{"machineFamily": "m1", "machineType": "m1-ultramem-160"}])
        hits = fs.check_ccc_large_vm_scarcity(cc)
        self.assertEqual(len(hits), 1)

    def test_flags_a_large_extended_memory_custom_shape(self):
        cc = compute_class("cc1", [{"machineFamily": "n2", "machineType": "n2-custom-48-393216-ext"}])
        hits = fs.check_ccc_large_vm_scarcity(cc)
        self.assertEqual(len(hits), 1)
        self.assertIn("48 vCPU", hits[0]["excerpt"])

    def test_does_not_flag_with_multiple_families(self):
        cc = compute_class("cc1", [{"machineFamily": "m1", "machineType": "m1-ultramem-160"}, {"machineFamily": "n4", "machineType": "n4-standard-4"}])
        self.assertEqual(fs.check_ccc_large_vm_scarcity(cc), [])

    def test_does_not_flag_small_machine(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "machineType": "n4-standard-8"}])
        self.assertEqual(fs.check_ccc_large_vm_scarcity(cc), [])


class CccPriorityStarvationTest(unittest.TestCase):
    def test_flags_over_ten_priorities(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}] * 11)
        self.assertIsNotNone(fs.check_ccc_priority_starvation(cc))

    def test_does_not_flag_ten_or_fewer(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}] * 10)
        self.assertIsNone(fs.check_ccc_priority_starvation(cc))


class CccMixedDiskGenerationsTest(unittest.TestCase):
    def test_flags_gen2_and_gen4_mix_on_stateful(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        self.assertIsNotNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=True))

    def test_does_not_flag_when_not_referenced_by_stateful(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        self.assertIsNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=False))

    def test_does_not_flag_pure_gen2(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c2"}])
        self.assertIsNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=True))

    def test_c3d_is_not_in_the_gen4_hyperdisk_list(self):
        """§3.5's own Gen4/Hyperdisk-compatible list is `c4, n4, c3` --
        `c3d` is not on it, even though a different check's (§3.6) list
        does include it."""
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c3d"}])
        self.assertIsNone(fs.check_ccc_mixed_disk_generations(cc, stateful_referencing=True))


class CccHyperdiskIncompatibleTest(unittest.TestCase):
    def test_flags_incompatible_fallback(self):
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        self.assertIsNotNone(fs.check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk=True))

    def test_does_not_flag_when_not_using_hyperdisk(self):
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        self.assertIsNone(fs.check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk=False))

    def test_does_not_flag_all_compatible_families(self):
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "n4"}])
        self.assertIsNone(fs.check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk=True))


class DanglingComputeClassTest(unittest.TestCase):
    def test_flags_reference_to_nonexistent_class(self):
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
        hit = fs.check_dangling_compute_class(d, {}, set())
        self.assertIsNotNone(hit)
        self.assertIn("does not exist", hit["excerpt"])

    def test_does_not_flag_gke_built_in_classes_on_autopilot(self):
        # Built-ins are not ComputeClass objects in the dump.
        for name in ("Balanced", "Scale-Out", "Performance", "Accelerator", "autopilot", "autopilot-spot", "autopilot-arm"):
            with self.subTest(name=name):
                d = deployment("api", node_selector={"cloud.google.com/compute-class": name})
                self.assertIsNone(fs.check_dangling_compute_class(d, {}, None, autopilot=True))

    def test_standard_spares_only_the_autopilot_classes_it_provides(self):
        for name in ("autopilot", "autopilot-spot"):
            with self.subTest(name=name):
                d = deployment("api", node_selector={"cloud.google.com/compute-class": name})
                self.assertIsNone(fs.check_dangling_compute_class(d, {}, set()))
        # Standard does not provide the Autopilot-only classes, so a workload
        # selecting one stays Pending.
        for name in ("Balanced", "Scale-Out", "Performance", "Accelerator", "autopilot-arm"):
            with self.subTest(name=name):
                d = deployment("api", node_selector={"cloud.google.com/compute-class": name})
                self.assertIsNotNone(fs.check_dangling_compute_class(d, {}, set()))

    def test_a_built_in_name_in_the_wrong_case_selects_nothing(self):
        # nodeSelector values are case-sensitive; `balanced` is not `Balanced`.
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "balanced"})
        self.assertIsNotNone(fs.check_dangling_compute_class(d, {}, None, autopilot=True))

    def test_the_collector_tells_the_check_the_cluster_mode(self):
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "Balanced"})
        for autopilot in (True, False):
            with self.subTest(autopilot=autopilot):
                entry = CollectClusterTest().run_with(dump_items=[d], cluster={**CollectClusterTest.CLUSTER, "autopilot": autopilot})
                flagged = {c["object"] for c in entry["candidates"] if c["check"] == "dangling-compute-class"}
                self.assertEqual(flagged, set() if autopilot else {"Deployment/api"})

    def test_does_not_flag_valid_reference(self):
        cc = compute_class("cc1", [])
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_flags_missing_pool_label_when_auto_creation_disabled(self):
        cc = compute_class("cc1", [], node_pool_auto_creation=False)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, {"other-class"})
        self.assertIsNotNone(hit)

    def test_does_not_flag_missing_pool_label_when_auto_creation_enabled(self):
        cc = compute_class("cc1", [], node_pool_auto_creation=True)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_flags_gpu_workload_without_toleration(self):
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
        )
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, set())
        self.assertIsNotNone(hit)
        self.assertIn("toleration", hit["excerpt"])

    def test_does_not_flag_gpu_workload_with_toleration(self):
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
            tolerations=[{"key": "nvidia.com/gpu", "operator": "Exists"}],
        )
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_does_not_flag_gpu_workload_with_a_keyless_exists_toleration(self):
        # No key and `operator: Exists` tolerates every taint, the GPU one included.
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
            tolerations=[{"operator": "Exists"}],
        )
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, set()))

    def test_flags_gpu_workload_whose_keyless_toleration_matches_by_equality(self):
        # Keyless `operator: Equal` matches no taint, so it is no GPU toleration.
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
            tolerations=[{"operator": "Equal", "value": "x"}],
        )
        self.assertIn("without an nvidia.com/gpu toleration", fs.check_dangling_compute_class(d, {"cc1": cc}, set())["excerpt"])

    def test_no_selector_is_never_flagged(self):
        d = deployment("api")
        self.assertIsNone(fs.check_dangling_compute_class(d, {}, set()))

    def test_flags_when_no_pool_carries_the_label_at_all(self):
        """The arm's own target case, and an empty set used to turn it off. A
        Standard cluster whose pools carry no `cloud.google.com/compute-class`
        label has no pool the class can land on, which is exactly what
        `nodePoolAutoCreation: false` makes fatal."""
        cc = compute_class("cc1", [], node_pool_auto_creation=False)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, set())
        self.assertIsNotNone(hit)
        self.assertIn("no matching node pool", hit["excerpt"])

    def test_stays_quiet_when_the_labels_are_unknown(self):
        """`None`, not an empty set: the pools could not be read, or there are
        no user pools to read. Flagging there would accuse every workload on a
        cluster nobody could look at."""
        cc = compute_class("cc1", [], node_pool_auto_creation=False)
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        self.assertIsNone(fs.check_dangling_compute_class(d, {"cc1": cc}, None))

    def test_a_nonexistent_class_is_still_flagged_with_labels_unknown(self):
        """Arm one needs no pool labels, so an unreadable `node-pools list`
        must not take it down with arm two."""
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
        self.assertIsNotNone(fs.check_dangling_compute_class(d, {}, None))

    def test_an_absent_auto_creation_block_is_disabled_not_enabled(self):
        """`compute_class()` above always writes the field; the CRD does not
        require it, it defaults to off, and omitting it is the ordinary way to
        leave auto-creation disabled. Reading absence as "enabled" exempted the
        common case from arm two entirely."""
        cc = {"kind": "ComputeClass", "metadata": {"name": "cc1"}, "spec": {"priorities": []}}
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "cc1"})
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, {"other-class"})
        self.assertIsNotNone(hit)
        self.assertIn("no matching node pool", hit["excerpt"])

    def test_gpu_declared_only_under_limits_still_counts(self):
        """The canonical GPU manifest sets `nvidia.com/gpu` under `limits`
        alone. Kubernetes defaults `requests` from `limits` on a Pod, but these
        are Deployment pod *templates*, which are not defaulted -- so reading
        `requests` alone made arm three inert rather than failing."""
        cc = compute_class("cc1", [])
        d = deployment(
            "api",
            node_selector={"cloud.google.com/compute-class": "cc1"},
            containers=[{"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
        )
        hit = fs.check_dangling_compute_class(d, {"cc1": cc}, set())
        self.assertIsNotNone(hit)
        self.assertIn("toleration", hit["excerpt"])

    def test_every_arm_carries_the_workload_namespace(self):
        """`derive_finding_id` keys on (check, cluster, namespace, object), so
        without this `Deployment/api` in two namespaces is one identity: one is
        dropped, and the delta alternates between them run to run."""
        cc_disabled = compute_class("cc1", [], node_pool_auto_creation=False)
        cc = compute_class("cc1", [])
        gpu = [{"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]
        arms = [
            ("missing class", {}, deployment("api", ns="team-a", node_selector={"cloud.google.com/compute-class": "missing"}), set()),
            ("no pool label", {"cc1": cc_disabled}, deployment("api", ns="team-a", node_selector={"cloud.google.com/compute-class": "cc1"}), set()),
            ("gpu no toleration", {"cc1": cc}, deployment("api", ns="team-a", node_selector={"cloud.google.com/compute-class": "cc1"}, containers=gpu), set()),
        ]
        for label, classes, workload, labels in arms:
            with self.subTest(arm=label):
                hit = fs.check_dangling_compute_class(workload, classes, labels)
                self.assertIsNotNone(hit)
                self.assertEqual(hit["namespace"], "team-a")


class SingleZoneNodepoolTest(unittest.TestCase):
    def test_flags_single_zone_autoscaling_no_nap(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNotNone(fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1))

    def test_does_not_flag_multi_zone(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1))

    def test_does_not_flag_a_zonal_pool_beside_a_multi_zone_pool_of_its_shape(self):
        # §3.9's Do-NOT-flag: a multi-zone pool its pods can move to.
        pool = {"name": "web-a", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "e2-standard-4"}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1, multi_zone_machine_types=frozenset({"e2-standard-4"})))

    def test_flags_a_zonal_pool_whose_shape_no_multi_zone_pool_offers(self):
        # A GPU pool pinned to one zone beside a regional default pool is
        # still zone-locked; any multi-zone pool used to spare it.
        pool = {"name": "gpu", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "a2-highgpu-1g"}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1, multi_zone_machine_types=frozenset({"e2-standard-4"}))
        self.assertIsNotNone(hit)
        self.assertIn("no multi-zone node pool of machine type a2-highgpu-1g on the cluster", hit["excerpt"])

    def test_flags_a_tainted_zonal_pool_beside_a_multi_zone_pool_of_its_shape(self):
        pool = {
            "name": "batch", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10},
            "config": {"machineType": "e2-standard-4", "taints": [{"key": "dedicated", "value": "batch", "effect": "NO_SCHEDULE"}]},
        }
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1, multi_zone_machine_types=frozenset({"e2-standard-4"}))
        self.assertIsNotNone(hit)
        self.assertIn("tainted (dedicated)", hit["excerpt"])

    def test_does_not_flag_single_zone_with_nap(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=1))

    def test_flags_near_max_node_count(self):
        # 2 zones x maxNodeCount 10 = 20 real ceiling, so 90% is 18 nodes.
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=18)
        self.assertIsNotNone(hit)
        self.assertIn("90%", hit["excerpt"])

    def test_the_ceiling_arm_names_itself_and_the_zones_the_pool_spans(self):
        """3.9 publishes two unrelated conditions under one slug, and the
        ceiling one has nothing to do with zones. A bare "9/10 live nodes"
        under a slug called `single-zone-nodepool` is how a two-zone pool got
        told to enable multi-zone node pools it already had, so the excerpt has
        to carry both the arm and the span for 3.9 to key off."""
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=18)
        self.assertTrue(hit["excerpt"].startswith("at its autoscaling ceiling:"), hit["excerpt"])
        self.assertIn("us-central1-b", hit["excerpt"])
        self.assertNotIn("single-zone", hit["excerpt"])

    # ----------------------------------------------------------------- #
    # `maxNodeCount` is per location, and the live count is a pool total
    # ----------------------------------------------------------------- #

    def test_a_multi_zone_pool_is_not_full_at_the_per_zone_number(self):
        """The regression. `maxNodeCount` is the GKE API's *per-location*
        limit, so a three-zone pool declaring 10 stops at 30 nodes. Comparing
        a pool total against the per-zone field called this pool 90% full at
        9 of its 30 nodes -- and multi-zone is what the zone-locked arm's own
        remediation tells operators to build, so following this check's advice
        was what armed its false positive."""
        pool = {
            "name": "p1",
            "locations": ["us-central1-a", "us-central1-b", "us-central1-c"],
            "autoscaling": {"enabled": True, "maxNodeCount": 10},
        }
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=9))
        # ...and it does fire once the pool really is near 30.
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=27)
        self.assertIn("27/30 live nodes", hit["excerpt"])
        self.assertIn("maxNodeCount 10/zone x 3 zones", hit["excerpt"])

    def test_total_max_node_count_is_already_pool_wide(self):
        # The mutually-exclusive pool-wide form. Multiplying it by the zone
        # count would be the same bug in the other direction.
        pool = {
            "name": "p1",
            "locations": ["us-central1-a", "us-central1-b"],
            "autoscaling": {"enabled": True, "totalMaxNodeCount": 10},
        }
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=9)
        self.assertIn("9/10 live nodes", hit["excerpt"])
        self.assertIn("totalMaxNodeCount", hit["excerpt"])
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=4))

    def test_a_single_zone_pool_reads_max_node_count_unchanged(self):
        # Where the two spellings coincide, and the only shape this fleet has.
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=9)
        self.assertIn("9/10 live nodes", hit["excerpt"])
        self.assertIn("90% of maxNodeCount)", hit["excerpt"])

    # ----------------------------------------------------------------- #
    # Each arm carries its own Impact
    # ----------------------------------------------------------------- #

    def test_the_zone_locked_impact_does_not_halt_the_whole_cluster(self):
        """`IMPACT["single-zone-nodepool"]` used to be one blended sentence --
        "locked to a single zone or near its scaling ceiling: any zonal
        stockout or scale event halts cluster auto-scaling" -- so it was half
        false whichever arm published it. The zone-locked half also overstated
        its blast radius: GKE's autoscaler treats each pool-zone pair as its
        own node group and backs off only the one that failed."""
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1)
        self.assertIn("halts scale-up of this pool", hit["impact"])
        self.assertIn("keeps scaling the others", hit["impact"])
        self.assertNotIn("halts cluster auto-scaling", hit["impact"])
        self.assertNotIn("all cluster auto-scaling", hit["impact"])
        # And it must not carry the other arm's condition.
        self.assertNotIn("scaling ceiling", hit["impact"])

    def test_the_zone_locked_impact_names_the_cluster_wide_exception(self):
        """Per-node-group backoff is right, and GKE documents one exception to
        it: "if 45% of nodes in a cluster are unhealthy or not ready, cluster
        autoscaler halts all operations". A pure stockout rarely trips it;
        adjacent failures in the same incident do, and an operator reading
        "other pools keep scaling" while nothing scales stops reading."""
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1)
        self.assertIn("45%", hit["impact"])

    def test_the_zone_locked_impact_pins_pending_pods_to_the_zone(self):
        """"pods only this pool can host" was both too narrow and too strong.
        The pin is to the zone, not the pool: a pod with no pool selector whose
        PVC is bound to a zonal disk in the stalled zone stays Pending too."""
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1)
        self.assertIn("zonal disk in that zone", hit["impact"])
        self.assertNotIn("pods only this pool can host", hit["impact"])

    def test_the_ceiling_impact_claims_nothing_about_zones_or_stockouts(self):
        # This arm fires on regional pools spanning three zones. Every
        # zonal-stockout sentence is false of it, and a scale event reaching a
        # configured limit is the autoscaler working, not a capacity failure.
        pool = {
            "name": "p1",
            "locations": ["us-central1-a", "us-central1-b", "us-central1-c"],
            "autoscaling": {"enabled": True, "maxNodeCount": 10},
        }
        hit = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=27)
        self.assertIn("90% of its effective node ceiling", hit["impact"])
        self.assertIn("configuration, not capacity", hit["impact"])
        self.assertNotIn("stockout", hit["impact"])
        self.assertNotIn("locked to a single zone", hit["impact"])
        # "the zone", singular, on the arm that fires across three of them.
        self.assertNotIn("the zone has", hit["impact"])

    def test_the_ceiling_impact_states_the_headroom_rather_than_a_stop(self):
        """Cluster autoscaler skips a node group on exactly one condition,
        `currentTargetSize >= nodeGroup.MaxSize()`. At 27 of 30 that is false
        and the next scale-up adds three more nodes, so "the next scale-up
        stops there" was wrong everywhere in this arm's band except its single
        top point -- and the test that used to live here asserted it at 27/30.
        """
        pool = {
            "name": "p1",
            "locations": ["us-central1-a", "us-central1-b", "us-central1-c"],
            "autoscaling": {"enabled": True, "maxNodeCount": 10},
        }
        near = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=27)
        self.assertIn("at most 3 more nodes can be added", near["impact"])
        # Live Nodes are what this check can count; the autoscaler compares its
        # own target size, which may already be higher.
        self.assertIn("target already sits above the live count", near["impact"])

        full = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=30)
        self.assertIn("at its effective node ceiling (30/30)", full["impact"])
        self.assertNotIn("at most", full["impact"])

        one_left = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=29)
        self.assertIn("at most 1 more node can be added", one_left["impact"])

    def test_the_ceiling_impact_defers_to_node_auto_provisioning(self):
        """Arm 2 does not require `not has_nap`, so on a NAP cluster every
        at-ceiling pool lands here -- and NAP creates a new pool for pending
        workloads, which is the whole point of it."""
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        with_nap = fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=10)
        self.assertIn("create a different pool instead", with_nap["impact"])
        # The same pool without NAP is the zone-locked arm as well, so read the
        # ceiling sentence off the helper rather than the hit.
        self.assertNotIn("create a different pool", fs._ceiling_impact(10, 10, has_nap=False))

    def test_a_zone_locked_pool_at_its_ceiling_reports_both_arms(self):
        """The live shape. `spot-capacity-test/spot-pool` is single-zone with
        `maxNodeCount: 2`; at 2/2 it is completely full and scale-up is already
        stopped, with no stockout anywhere. Arm 1 returned first, so the
        published sentence made the stall contingent on a future stockout and
        the SOP told the model to state it as given rather than re-derive it.
        """
        pool = {"name": "spot-pool", "locations": ["us-east4-a"], "autoscaling": {"enabled": True, "maxNodeCount": 2}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=2)
        self.assertIn("single-zone (['us-east4-a'])", hit["excerpt"])
        self.assertIn("at its autoscaling ceiling: 2/2 live nodes", hit["excerpt"])
        self.assertIn("locked to a single zone", hit["impact"])
        self.assertIn("at its effective node ceiling (2/2)", hit["impact"])

        # Below the ceiling it is arm 1 alone, which is the pool's state today.
        below = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1)
        self.assertNotIn("ceiling", below["excerpt"])
        self.assertNotIn("ceiling", below["impact"])

    def test_a_pool_ceiling_needs_both_an_enabled_autoscaler_and_a_zone_span(self):
        """Neither shape reaches this from `node-pools list` -- GKE omits the
        `autoscaling` key entirely for a static pool, and `locations` is always
        populated -- so both are guards on a field arriving wrong rather than
        on a state the API produces. The empty-`locations` one matters most: a
        three-zone pool at 30% full would read as 90% full, which is the exact
        false positive `_pool_ceiling` exists to remove."""
        for autoscaling, locations in (
            ({"enabled": False, "maxNodeCount": 10}, ["a", "b", "c"]),
            ({"maxNodeCount": 10}, ["a", "b", "c"]),
            ({"enabled": True, "maxNodeCount": 10}, []),
        ):
            with self.subTest(autoscaling=autoscaling, locations=locations):
                self.assertEqual(fs._pool_ceiling(autoscaling, locations), (None, ""))
                pool = {"name": "p1", "locations": locations, "autoscaling": autoscaling}
                # Both NAP settings: without NAP the zone-locked arm is live,
                # and it must read an empty span as unknown too.
                for has_nap in (True, False):
                    self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=has_nap, current_node_count=27))

    def test_a_string_node_count_is_read_as_a_number(self):
        """`maxNodeCount` is an int32 so proto3 JSON will not stringify it, but
        `"10" * 3` is `"101010"`, and a `TypeError` further on would fail the
        whole cluster's entry. The module has `_gce_int` for exactly this and
        was not using it here."""
        self.assertEqual(
            fs._pool_ceiling({"enabled": True, "maxNodeCount": "10"}, ["a", "b", "c"]),
            (30, "maxNodeCount 10/zone x 3 zones"),
        )
        self.assertEqual(fs._pool_ceiling({"enabled": True, "maxNodeCount": "x"}, ["a"]), (None, ""))

    def test_emit_prefers_the_arm_impact_and_falls_back_otherwise(self):
        # The plumbing the two arms rely on, and the default every
        # single-meaning check still gets.
        armed = fs._emit("single-zone-nodepool", {"object": "NodePool/p1", "excerpt": "x", "impact": "arm sentence"})
        self.assertEqual(armed["impact"], "arm sentence")
        # Which arm fired is the collector's observation, not the model's
        # inference from the excerpt -- so `finish` restores it. Without this
        # flag a corrected sentence would never reach a finding already in the
        # ledger once `finish` gains its planned pass reusing the previous
        # run's prose wherever `adopt_collector_evidence` made evidence identical.
        self.assertIs(armed["impact_authoritative"], True)
        default = fs._emit("single-zone-nodepool", {"object": "NodePool/p1", "excerpt": "x"})
        self.assertEqual(default["impact"], fs.IMPACT["single-zone-nodepool"])
        self.assertNotIn("impact_authoritative", default)
        # The unreachable fallback must at least be true of both arms.
        self.assertNotIn("any zonal stockout or scale event", default["impact"])
        self.assertNotIn("cannot scale when it needs to", default["impact"])

    def test_the_zone_locked_arm_still_starts_with_single_zone(self):
        # The other half of the discriminator 3.9 reads.
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        hit = fs.check_single_zone_nodepool(pool, has_nap=False, current_node_count=1)
        self.assertTrue(hit["excerpt"].startswith("single-zone "), hit["excerpt"])

    def test_does_not_flag_comfortably_under_ceiling(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=3))

    def test_ignores_stale_initial_node_count_field(self):
        """A pool created with 9 nodes that the autoscaler has since scaled
        down to 1 live node must not be flagged on its stale creation-time
        field."""
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 9}
        self.assertIsNone(fs.check_single_zone_nodepool(pool, has_nap=True, current_node_count=1))


class ReservationTest(unittest.TestCase):
    def test_flags_mostly_idle_reservation(self):
        r = {"name": "r1", "specificReservation": {"count": 10, "inUseCount": 2}}
        hit = fs.check_reservation(r)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "major")

    def test_same_named_reservations_in_two_zones_are_two_objects(self):
        """A reservation name is unique per zone, and both are filed under
        `project/<p>`: with the bare name they derived one finding id."""
        objects = {
            fs.check_reservation({"name": "r1", "zone": f"https://www.googleapis.com/compute/v1/projects/p/zones/{zone}", "specificReservation": {"count": 10, "inUseCount": 2}})["object"]
            for zone in ("us-central1-a", "us-central1-b")
        }
        self.assertEqual(objects, {"Reservation/us-central1-a:r1", "Reservation/us-central1-b:r1"})

    def test_does_not_flag_well_utilized_reservation(self):
        r = {"name": "r1", "specificReservation": {"count": 10, "inUseCount": 8}}
        self.assertIsNone(fs.check_reservation(r))

    def test_does_not_flag_small_reservation_with_small_absolute_slack(self):
        # ratio 0/2 = 0 <= 0.5, but only 2 idle -- below the absolute floor of 4
        r = {"name": "r1", "specificReservation": {"count": 2, "inUseCount": 0}}
        self.assertIsNone(fs.check_reservation(r))

    def test_zero_count_is_not_a_crash(self):
        r = {"name": "r1", "specificReservation": {"count": 0, "inUseCount": 0}}
        self.assertIsNone(fs.check_reservation(r))

    def test_int64_strings_are_what_the_api_actually_sends(self):
        """Every fixture above uses Python ints; the GCE API does not.

        `gcloud compute reservations list --format json` serialises int64 as a
        JSON string, so the live shape is `{"count": "10", "inUseCount": "2"}`.
        Dividing those raises `TypeError`, which `collect_fleet` turns into a
        `gate-failed` project entry: one reservation would cost its project
        every check.
        """
        r = {"name": "r1", "specificReservation": {"count": "10", "inUseCount": "2"}}
        hit = fs.check_reservation(r)
        self.assertIsNotNone(hit)
        self.assertIn("2/10", hit["excerpt"])

    def test_absent_in_use_count_is_zero_not_unknown(self):
        """proto3 JSON omits a zero int64, so the reservation nothing is using
        -- §3.10(c)'s maximum-waste case -- arrives with no `inUseCount`."""
        r = {"name": "r1", "specificReservation": {"count": "10"}}
        hit = fs.check_reservation(r)
        self.assertIsNotNone(hit)
        self.assertIn("0/10", hit["excerpt"])


class ReservationAffinityTest(unittest.TestCase):
    def test_flags_automatic_affinity(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "Automatic"}}])
        hit = fs.check_reservation_affinity(cc)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["severity"], "critical")

    def test_flags_any_best_effort_affinity(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "AnyBestEffort"}}])
        self.assertIsNotNone(fs.check_reservation_affinity(cc))

    def test_does_not_flag_specific_affinity(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "SpecificReservation"}}])
        self.assertIsNone(fs.check_reservation_affinity(cc))

    def test_no_reservations_field_is_not_flagged(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}])
        self.assertIsNone(fs.check_reservation_affinity(cc))


class QuotaTest(unittest.TestCase):
    def test_flags_over_90_percent(self):
        hit = fs.check_quota({"metric": "N4_CPUS", "limit": 100, "usage": 92}, "us-central1")
        self.assertIsNotNone(hit)

    def test_does_not_flag_under_90_percent(self):
        self.assertIsNone(fs.check_quota({"metric": "N4_CPUS", "limit": 100, "usage": 70}, "us-central1"))

    def test_zero_limit_is_not_a_crash(self):
        self.assertIsNone(fs.check_quota({"metric": "N4_CPUS", "limit": 0, "usage": 0}, "us-central1"))

    def test_only_node_capacity_metrics_count(self):
        """§3.7 is "GPU/TPU/CPU limits". A region describe returns every
        Compute quota there is -- 164 on `us-east4` -- and without a filter a
        project at 92% of `BACKEND_BUCKETS` publishes a `critical` stockout."""
        for metric in ("CPUS", "CPUS_ALL_REGIONS", "N4_CPUS", "PREEMPTIBLE_CPUS",
                       "NVIDIA_L4_GPUS", "TPU_V5_LITEPOD_SLICES"):
            with self.subTest(metric=metric, capacity=True):
                self.assertIsNotNone(fs.check_quota({"metric": metric, "limit": 100, "usage": 95}, "us-central1"))
        for metric in ("BACKEND_BUCKETS", "AFFINITY_GROUPS", "IN_USE_ADDRESSES",
                       "DISKS_TOTAL_GB", "LOCAL_SSD_TOTAL_GB", "FIREWALLS"):
            with self.subTest(metric=metric, capacity=False):
                self.assertIsNone(fs.check_quota({"metric": metric, "limit": 100, "usage": 95}, "us-central1"))

    def test_a_commitment_quota_is_not_a_capacity_cap(self):
        """A `COMMITTED_*` metric limits what committed-use discounts can
        buy, not how many nodes can run."""
        for metric in ("COMMITTED_CPUS", "COMMITTED_N2_CPUS", "COMMITTED_NVIDIA_A100_GPUS"):
            with self.subTest(metric=metric):
                self.assertIsNone(fs.check_quota({"metric": metric, "limit": 100, "usage": 100}, "us-central1"))

    def test_absent_usage_is_not_a_crash(self):
        self.assertIsNone(fs.check_quota({"metric": "N4_CPUS", "limit": 100}, "us-central1"))


class AutoscalerVisibilityTest(unittest.TestCase):
    def test_the_error_msg_arm_is_read(self):
        found = fs.autoscaler_message_ids([ERROR_MSG_ENTRY])
        self.assertEqual(set(found), {"scale.up.error.out.of.resources"})
        self.assertEqual(found["scale.up.error.out.of.resources"]["count"], 1)

    def test_the_nap_arm_is_read(self):
        """The SOP's own `--format` reads only this arm. A collector that
        copied it would pass every cluster that failed under the other one."""
        found = fs.autoscaler_message_ids([NAP_ENTRY])
        self.assertEqual(set(found), {"scale.up.error.quota.exceeded"})

    def test_both_arms_in_one_window_are_both_reported(self):
        found = fs.autoscaler_message_ids([ERROR_MSG_ENTRY, NAP_ENTRY, HEALTHY_ENTRY])
        self.assertEqual(
            set(found), {"scale.up.error.out.of.resources", "scale.up.error.quota.exceeded"}
        )

    def test_a_healthy_tick_is_not_a_finding(self):
        self.assertEqual(fs.autoscaler_message_ids([HEALTHY_ENTRY]), {})

    def test_an_unrelated_message_id_is_not_a_stockout(self):
        """`waiting.for.instances.timeout` is an ordinary busy cluster. Matching
        every id the autoscaler emits would turn the whole fleet critical."""
        entry = json.loads(json.dumps(ERROR_MSG_ENTRY))
        entry["jsonPayload"]["resultInfo"]["results"][0]["errorMsg"]["messageId"] = (
            "scale.up.error.waiting.for.instances.timeout"
        )
        self.assertEqual(fs.autoscaler_message_ids([entry]), {})

    def test_an_empty_read_is_not_a_crash(self):
        self.assertEqual(fs.autoscaler_message_ids(None), {})
        self.assertEqual(fs.autoscaler_message_ids([]), {})

    def test_repeats_of_one_id_collapse_to_one_finding(self):
        """A wedged cluster emits the same id every autoscaler tick, and the
        remediation branches on the id rather than the occurrence."""
        second = json.loads(json.dumps(ERROR_MSG_ENTRY))
        second["timestamp"] = "2026-08-14T06:00:00Z"
        found = fs.autoscaler_message_ids([ERROR_MSG_ENTRY, second])
        hits = fs.check_autoscaler_out_of_resources(found)
        self.assertEqual(len(hits), 1)
        self.assertIn("2 occurrences", hits[0]["excerpt"])
        self.assertIn("2026-08-14T00:05:02 .. 2026-08-14T06:00:00", hits[0]["excerpt"])

    def test_the_excerpt_does_not_claim_a_window_it_did_not_choose(self):
        """The read's `--freshness` belongs to the caller. Printing it here put
        "over the last 24h" next to timestamps thirteen days apart."""
        hits = fs.check_autoscaler_out_of_resources(
            fs.autoscaler_message_ids([ERROR_MSG_ENTRY])
        )
        self.assertNotIn("24h", hits[0]["excerpt"])

    def test_the_excerpt_names_the_instance_group_not_its_url(self):
        hits = fs.check_autoscaler_out_of_resources(
            fs.autoscaler_message_ids([ERROR_MSG_ENTRY])
        )
        self.assertIn("gk3-prod-usc1-pool-3-b07eba62-grp", hits[0]["excerpt"])
        self.assertNotIn("googleapis.com", hits[0]["excerpt"])
        self.assertEqual(hits[0]["object"], "ScaleUpError/scale.up.error.out.of.resources")

    GROUP = "gk3-prod-usc1-pool-3-b07eba62-grp"

    def test_a_stockout_in_no_class_backed_group_is_marked_for_a_new_class(self):
        """The fix is a new ComputeClass plus the workload that selects it:
        two files where a finding carries one path."""
        hits = fs.check_autoscaler_out_of_resources(fs.autoscaler_message_ids([ERROR_MSG_ENTRY]))
        self.assertEqual(hits[0]["needs_triage"], fs.NEW_COMPUTE_CLASS_TRIAGE)

    def test_a_stockout_in_a_class_backed_group_stays_unmarked(self):
        hits = fs.check_autoscaler_out_of_resources(
            fs.autoscaler_message_ids([ERROR_MSG_ENTRY]), frozenset({self.GROUP})
        )
        self.assertNotIn("needs_triage", hits[0])

    def test_a_stockout_naming_no_group_is_marked(self):
        """The NAP arm names no instance group, so which case applies is
        unknown, and the unknown errs to the one the sweep skips."""
        entry = json.loads(json.dumps(NAP_ENTRY))
        entry["jsonPayload"]["noDecisionStatus"]["noScaleUp"]["unhandledPodGroups"][0]["napFailureReasons"] = [
            {"messageId": "scale.up.error.out.of.resources"}
        ]
        hits = fs.check_autoscaler_out_of_resources(
            fs.autoscaler_message_ids([entry]), frozenset({self.GROUP})
        )
        self.assertEqual(hits[0]["needs_triage"], fs.NEW_COMPUTE_CLASS_TRIAGE)

    def test_the_marker_is_one_the_sweep_withholds(self):
        """The two files carry the string separately; a drift here would
        mark the finding and let the sweep open it anyway."""
        import audit_report

        self.assertIn(fs.NEW_COMPUTE_CLASS_TRIAGE, audit_report.NO_SWEEP_TRIAGE)

    def test_quota_and_ip_findings_are_never_marked(self):
        """Both are `kind: manual`; there is no manifest for the sweep to open."""
        for message_id in ("scale.up.error.quota.exceeded", "scale.up.error.ip.space.exhausted"):
            with self.subTest(message_id=message_id):
                entry = json.loads(json.dumps(ERROR_MSG_ENTRY))
                entry["jsonPayload"]["resultInfo"]["results"][0]["errorMsg"]["messageId"] = message_id
                [hit] = fs.check_autoscaler_out_of_resources(fs.autoscaler_message_ids([entry]))
                self.assertEqual(hit["object"], f"ScaleUpError/{message_id}")
                self.assertNotIn("needs_triage", hit)


class SpotScarcityTest(unittest.TestCase):
    SHAPE = {"owners": ["ComputeClass/cc1"], "families": {"ComputeClass/cc1": 1}}

    def test_the_live_us_east4_response_is_not_a_finding(self):
        """Eight daily intervals averaging 6.6%, the shape of a healthy Spot
        response. If this ever flags, the ceiling moved."""
        hit, limitation = fs.check_spot_scarcity(
            "n2-standard-8", self.SHAPE, "us-east4", capacity_history([0.05, 0.06, 0.04, 0.05, 0.07, 0.09, 0.1, 0.07])
        )
        self.assertIsNone(hit)
        self.assertIsNone(limitation)

    def test_the_object_is_the_same_owner_whatever_order_they_are_listed_in(self):
        """The object is the finding's identity, so listing order must not move it."""
        owners = ["NodePool/zeta", "ComputeClass/alpha"]
        shape = {"owners": owners, "families": {o: 1 for o in owners}}
        reordered = {"owners": owners[::-1], "families": shape["families"]}
        history = capacity_history([0.5] * 8)
        first, _ = fs.check_spot_scarcity("n2-standard-8", shape, "us-east4", history)
        second, _ = fs.check_spot_scarcity("n2-standard-8", reordered, "us-east4", history)
        self.assertEqual(first["object"], "ComputeClass/alpha")
        self.assertEqual(second["object"], first["object"])

    def test_a_shape_over_the_ceiling_with_no_fallback_is_flagged(self):
        hit, limitation = fs.check_spot_scarcity(
            "a2-highgpu-1g", self.SHAPE, "us-central1", capacity_history([0.3] * 10)
        )
        self.assertIsNone(limitation)
        self.assertIn("30.0% per day over 10 days", hit["excerpt"])
        self.assertEqual(hit["object"], "ComputeClass/cc1")

    def test_a_multi_family_chain_over_the_ceiling_is_not_flagged(self):
        """§3.8's "without alternative family fallbacks" — a chain spanning two
        families survives its worst shape being preempted."""
        shape = {"owners": ["ComputeClass/cc1"], "families": {"ComputeClass/cc1": 3}}
        hit, limitation = fs.check_spot_scarcity(
            "a2-highgpu-1g", shape, "us-central1", capacity_history([0.3] * 10)
        )
        self.assertIsNone(hit)
        self.assertIsNone(limitation)

    def test_one_bad_day_inside_a_calm_month_is_not_a_finding(self):
        """The mean, not the maximum. A 90% afternoon is a zonal incident that
        already resolved; flagging the peak turns the fleet critical after it."""
        hit, _ = fs.check_spot_scarcity(
            "n2-standard-8", self.SHAPE, "us-central1", capacity_history([0.9] + [0.02] * 20)
        )
        self.assertIsNone(hit)

    def test_too_short_a_history_is_unmeasured_rather_than_clean(self):
        hit, limitation = fs.check_spot_scarcity(
            "n2-standard-8", self.SHAPE, "us-central1", capacity_history([0.01, 0.01])
        )
        self.assertIsNone(hit)
        self.assertIn("2 daily interval", limitation)

    def test_a_response_with_no_history_is_unmeasured_rather_than_clean(self):
        hit, limitation = fs.check_spot_scarcity("n2-standard-8", self.SHAPE, "us-central1", {})
        self.assertIsNone(hit)
        self.assertIn("no preemptionHistory", limitation)

    def test_the_price_below_one_dollar_survives_the_missing_units_field(self):
        self.assertEqual(fs.spot_list_price(capacity_history([0.3] * 10)), "0.1102 USD")

    def test_a_price_above_one_unit_reads_both_halves(self):
        advice = capacity_history([0.3] * 10, price={"currencyCode": "USD", "units": "3", "nanos": 500000000})
        self.assertEqual(fs.spot_list_price(advice), "3.5000 USD")

    def test_no_price_history_is_an_empty_string_not_a_crash(self):
        self.assertEqual(fs.spot_list_price({"preemptionHistory": []}), "")


class SpotShapeEnumerationTest(unittest.TestCase):
    def test_a_spot_priority_with_a_machine_type_is_a_shape(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        self.assertEqual(set(fs.spot_shapes([cc], [])), {"n2-standard-8"})

    def test_an_on_demand_priority_is_not(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8"}])
        self.assertEqual(fs.spot_shapes([cc], []), {})

    def test_a_spot_node_pool_is_a_shape_with_no_fallback_by_construction(self):
        """A node pool has no priority chain at all: when its shape runs out,
        nothing else is tried."""
        pools = [{"name": "p1", "config": {"spot": True, "machineType": "c3-standard-4"}}]
        shapes = fs.spot_shapes([], pools)
        self.assertEqual(shapes["c3-standard-4"]["families"], {"NodePool/p1": 1})
        self.assertEqual(shapes["c3-standard-4"]["owners"], ["NodePool/p1"])

    def test_the_family_count_spans_the_whole_chain_not_just_its_spot_arm(self):
        """The on-demand tail is exactly the fallback §3.8 asks about."""
        cc = compute_class(
            "cc1",
            [
                {"machineType": "a2-highgpu-1g", "spot": True},
                {"machineType": "n2-standard-8"},
                {"machineFamily": "c3"},
            ],
        )
        self.assertEqual(fs.spot_shapes([cc], [])["a2-highgpu-1g"]["families"], {"ComputeClass/cc1": 3})

    def test_one_shape_requested_twice_is_read_once_and_names_both_owners(self):
        cc1 = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        cc2 = compute_class("cc2", [{"machineType": "n2-standard-8", "spot": True}])
        shapes = fs.spot_shapes([cc1, cc2], [])
        self.assertEqual(list(shapes), ["n2-standard-8"])
        self.assertEqual(shapes["n2-standard-8"]["owners"], ["ComputeClass/cc1", "ComputeClass/cc2"])

    def test_a_family_only_spot_priority_is_unqueryable_not_absent(self):
        """`capacity-history --machine-type` is singular and required, so this
        legal configuration has no shape to ask about. Silence would read as a
        cluster with no Spot at all."""
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        self.assertEqual(fs.spot_shapes([cc], []), {})
        self.assertEqual(fs.spot_without_a_shape([cc], [], brokered=True), (["cc1:c3"], [], []))

    def test_a_priority_with_a_machine_type_is_not_also_reported_unqueryable(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        self.assertEqual(fs.spot_without_a_shape([cc], [], brokered=True), ([], [], []))

    def test_a_shape_free_spot_priority_is_unpinned_rather_than_unqueryable(self):
        """GKE's own `autopilot-spot`: it pins neither family nor type, so on a
        cluster that can scale up through it every family is available to it and
        no shape can be scarce for it. Counting it as an unmeasurable gap put a
        permanent false coverage gap on every Autopilot cluster in the fleet."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        self.assertEqual(
            fs.spot_without_a_shape([cc], [], brokered=True),
            ([], ["ComputeClass/autopilot-spot"], []),
        )

    def test_the_same_priority_is_inert_where_gke_brokers_no_capacity(self):
        """GKE pre-installs `autopilot-spot` on Standard clusters too, and there
        it sits unselected rather than standing behind every scheduling decision.
        Ten of this fleet's sixteen clusters are Standard; folding them in with
        the bucket above had every one of them publish "Every Spot request on
        this cluster leaves the machine shape entirely to GKE" about a cluster
        that makes no Spot request at all.

        `brokered` is about who places the capacity, not about whether capacity
        can be placed: an unbrokered cluster can still grow a node through this
        class, because the class carries its own `nodePoolAutoCreation`. That is
        why the sentence this bucket publishes claims only that nothing requests
        Spot here -- see the reason test below."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        self.assertEqual(
            fs.spot_without_a_shape([cc], [], brokered=False),
            ([], [], ["ComputeClass/autopilot-spot"]),
        )

    def test_brokering_does_not_move_a_priority_that_names_a_shape(self):
        """`brokered` only decides where a *shape-free* priority lands. A family
        is still unqueryable and a machine type is still measurable, whatever the
        cluster can provision."""
        family = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        typed = compute_class("cc2", [{"machineType": "n2-standard-8", "spot": True}])
        pools = [{"name": "p1", "config": {"spot": True}}]
        for brokered in (True, False):
            with self.subTest(brokered=brokered):
                self.assertEqual(
                    fs.spot_without_a_shape([family, typed], pools, brokered=brokered),
                    (["cc1:c3", "NodePool/p1"], [], []),
                )

    def test_non_production_owners_are_no_measurement_gap(self):
        # `spot_shapes` leaves them out because §3.8 does not flag them, so a
        # shape-free Spot request on one is no coverage gap either.
        cc = compute_class("batch-staging", [{"machineFamily": "c3", "spot": True}])
        pools = [{"name": "ci-dev", "config": {"spot": True}}]
        for brokered in (True, False):
            with self.subTest(brokered=brokered):
                self.assertEqual(fs.spot_without_a_shape([cc], pools, brokered=brokered), ([], [], []))

    def test_a_spot_pool_with_no_machine_type_is_unqueryable(self):
        pools = [{"name": "p1", "config": {"spot": True}}]
        self.assertEqual(fs.spot_without_a_shape([], pools, brokered=True), (["NodePool/p1"], [], []))


class CollectClusterTest(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    # GKE's own answer when `node-pools list` is aimed at an Autopilot
    # cluster. The fake returned rc=0 for it whatever the cluster was, which
    # is why `test_autopilot_skips_single_zone_nodepool` below could pass
    # while the collector was still issuing a read the API refuses: a fake
    # that answers every argv successfully cannot tell a command the API runs
    # from one it rejects.
    AUTOPILOT_NODE_POOLS_ERROR = (
        "ERROR: (gcloud.container.node-pools.list) ResponseError: code=400, "
        "message=Autopilot node pools cannot be accessed or modified."
    )

    def run_with(
        self,
        dump_items=(),
        pools=(),
        cluster=None,
        pools_rc=0,
        pools_stderr="denied",
        pools_stdout=None,
        log_entries=None,
        log_rc=0,
        log_stderr="denied",
        log_stdout=None,
        advice=None,
        advice_rc=0,
        advice_stderr="denied",
        advice_stdout=None,
    ):
        target = cluster or self.CLUSTER
        self.issued = []

        def run(argv, **kwargs):
            self.issued.append(argv)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                if target.get("autopilot"):
                    return run_of(1, "", self.AUTOPILOT_NODE_POOLS_ERROR)
                if pools_rc:
                    return run_of(pools_rc, "", pools_stderr)
                if pools_stdout is not None:
                    return run_of(0, pools_stdout, pools_stderr)
                return run_of(0, json.dumps(list(pools)))
            if argv[:3] == ["gcloud", "logging", "read"]:
                if log_rc:
                    return run_of(log_rc, "", log_stderr)
                if log_stdout is not None:
                    return run_of(0, log_stdout, log_stderr)
                # gcloud prints nothing at all when nothing matched, which is
                # what the bare `run_of(0, "")` below stands in for elsewhere.
                return run_of(0, json.dumps(log_entries) if log_entries else "")
            if argv[:5] == ["gcloud", "beta", "compute", "advice", "capacity-history"]:
                machine_type = argv[argv.index("--machine-type") + 1]
                rc = advice_rc(machine_type) if callable(advice_rc) else advice_rc
                if rc:
                    return run_of(rc, "", advice_stderr)
                if advice_stdout is not None:
                    return run_of(0, advice_stdout, advice_stderr)
                body = advice(machine_type) if callable(advice) else advice
                return run_of(0, json.dumps(body) if body is not None else "")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                return fs.collect_cluster(target, run=run)

    def issued_node_pools_read(self):
        return [a for a in self.issued if a[:3] == ["gcloud", "container", "node-pools"]]

    def test_the_collector_tells_the_check_about_the_cluster_s_other_pools(self):
        zonal = {"name": "gpu", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "a2-highgpu-1g"}}
        regional = {"name": "web", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "e2-standard-4"}}
        with patch.object(fs, "check_single_zone_nodepool", wraps=fs.check_single_zone_nodepool) as check:
            for pools, expected in (([zonal, regional], {"e2-standard-4"}), ([zonal], set())):
                check.reset_mock()
                self.run_with(pools=pools)
                shapes = {c.args[0]["name"]: c.kwargs["multi_zone_machine_types"] for c in check.call_args_list}
                self.assertEqual(shapes["gpu"], expected)

    def test_a_zonal_pool_of_a_multi_zone_pool_s_shape_is_spared_end_to_end(self):
        zonal = {"name": "web-a", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "e2-standard-4"}}
        gpu = {"name": "gpu", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "a2-highgpu-1g"}}
        regional = {"name": "web", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "e2-standard-4"}}
        entry = self.run_with(pools=[zonal, gpu, regional])
        flagged = {c["object"] for c in entry["candidates"] if c["check"] == "single-zone-nodepool"}
        self.assertEqual(flagged, {"NodePool/gpu"})

    def test_a_tainted_multi_zone_pool_spares_no_zonal_pool(self):
        """The zonal pool's pods do not tolerate the regional pool's taint, so
        a stockout in its zone leaves them nowhere to go."""
        zonal = {"name": "web-a", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "config": {"machineType": "e2-standard-4"}}
        regional = {
            "name": "web", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10},
            "config": {"machineType": "e2-standard-4", "taints": [{"key": "dedicated", "value": "batch", "effect": "NO_SCHEDULE"}]},
        }
        with patch.object(fs, "check_single_zone_nodepool", wraps=fs.check_single_zone_nodepool) as check:
            entry = self.run_with(pools=[zonal, regional])
        shapes = {c.args[0]["name"]: c.kwargs["multi_zone_machine_types"] for c in check.call_args_list}
        self.assertEqual(shapes["web-a"], frozenset())
        flagged = {c["object"] for c in entry["candidates"] if c["check"] == "single-zone-nodepool"}
        self.assertEqual(flagged, {"NodePool/web-a"})

    def declared_not_applicable(self, entry):
        return {e["check"] for e in entry.get("checks_not_applicable") or []}

    def test_clean_cluster_collects_with_no_candidates(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "spot": False}, {"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["candidates"], [])

    def test_get_credentials_failure_is_unreachable(self):
        def run(argv, **kwargs):
            return run_of(1, "", "denied") if "get-credentials" in argv else run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(self.CLUSTER, run=run)
        self.assertEqual(entry["outcome"], "unreachable")

    def test_a_get_credentials_failure_is_one_the_sop_retries(self):
        """A running cluster whose credentials failed is `unreachable`, as the
        cost collector files it, but not for its state -- so the SOP must not
        send it to `scope.skipped` the way it sends a DEGRADED cluster."""
        def run(argv, **kwargs):
            return run_of(1, "", "denied") if "get-credentials" in argv else run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(self.CLUSTER, run=run)
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertTrue(entry["error"].startswith("get-credentials rc=1"))
        sop = Path(__file__).resolve().parents[3] / "governance" / "stockout_prevention_sop.md"
        outcome_line = next(line for line in sop.read_text().splitlines() if '`"unreachable"` means' in line)
        self.assertIn("Any other `\"unreachable\"` entry — a `get-credentials` failure on a running cluster — is worth one", outcome_line)

    def test_dump_failure_is_gate_failed(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(self.CLUSTER, run=run)
        self.assertEqual(entry["outcome"], "gate-failed")

    def test_every_outcome_publishes_the_mode_and_nap(self):
        # `enumerate_clusters` asks `clusters list` for both fields and derives
        # `autopilot`/`has_nap` from them, then used to drop both before
        # writing the manifest -- so a live run spent five `clusters describe`
        # round trips re-deriving them, three of those the identical
        # `value(autoscaling.enableNodeAutoprovisioning)` projection that comes
        # back empty and reads as "my projection is wrong" rather than "false".
        cluster = {**self.CLUSTER, "autopilot": True, "has_nap": True}

        def denied(argv, **kwargs):
            return run_of(1, "", "denied") if "get-credentials" in argv else run_of(0, "")

        def gated(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        entries = [self.run_with(dump_items=[], cluster=cluster)]
        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entries.append(fs.collect_cluster(cluster, run=denied))
                entries.append(fs.collect_cluster(cluster, run=gated))
        self.assertEqual(
            [e["outcome"] for e in entries], ["collected", "unreachable", "gate-failed"]
        )
        for entry in entries:
            with self.subTest(outcome=entry["outcome"]):
                self.assertIs(entry["autopilot"], True)
                self.assertIs(entry["has_nap"], True)

    def test_a_cluster_that_never_ran_still_publishes_the_mode_and_nap(self):
        entry = fs.not_running_entry(
            {"name": "dr-west", "location": "us-west1", "status": "DEGRADED",
             "autopilot": {"enabled": True},
             "autoscaling": {"enableNodeAutoprovisioning": True}},
            "acme",
        )
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertIs(entry["autopilot"], True)
        self.assertIs(entry["has_nap"], True)
        # Absent in the gcloud payload means false, not unknown.
        bare = fs.not_running_entry({"name": "c", "status": "STOPPING"}, "acme")
        self.assertIs(bare["autopilot"], False)
        self.assertIs(bare["has_nap"], False)

    def test_a_dirty_compute_class_is_reported(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-missing-fallbacks", slugs)
        self.assertIn("ccc-no-ondemand-floor", slugs)

    def test_single_zone_nodepool_reported(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 1}
        entry = self.run_with(pools=[pool])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("single-zone-nodepool", slugs)

    def test_near_max_node_count_uses_live_nodes_not_the_stale_initial_field(self):
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}, "initialNodeCount": 9}
        live_nodes = [node(f"n{i}", "p1") for i in range(2)]  # scaled down since creation
        entry = self.run_with(dump_items=live_nodes, pools=[pool])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertNotIn("single-zone-nodepool", slugs)

    def test_near_max_node_count_flagged_from_live_nodes(self):
        # 18 of the pool's real 20-node ceiling: `maxNodeCount` is per
        # location and this pool spans two, while the live count sums both.
        pool = {"name": "p1", "locations": ["us-central1-a", "us-central1-b"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        live_nodes = [node(f"n{i}", "p1") for i in range(18)]
        entry = self.run_with(dump_items=live_nodes, pools=[pool])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("single-zone-nodepool", slugs)

    def test_cluster_level_nap_suppresses_the_finding(self):
        pool = {"name": "p1", "locations": ["us-central1-a"], "autoscaling": {"enabled": True, "maxNodeCount": 10}}
        entry = self.run_with(pools=[pool], cluster={**self.CLUSTER, "has_nap": True})
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["candidates"]})

    def test_autopilot_skips_single_zone_nodepool(self):
        entry = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})

    def test_autopilot_declares_it_rather_than_leaving_it_absent(self):
        """Absent from `commands` is how a check nobody ran looks too, so §6
        read this as a coverage gap unless the model happened to know GKE well
        enough to excuse it by hand — which made a run's honesty about the gap
        depend on the model rather than on the cluster."""
        entry = self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        declared = {e["check"]: e["reason"] for e in entry.get("checks_not_applicable") or []}
        # `spot-scarcity-risk` rides along because this fixture's cluster asks
        # for no Spot capacity at all, which is its own declared non-applicability.
        self.assertEqual(set(declared), {"single-zone-nodepool", "spot-scarcity-risk"})
        self.assertIn("Autopilot", declared["single-zone-nodepool"])

    def test_autopilot_never_issues_the_node_pools_read(self):
        """The API answers 400 for it, and its only consumers cannot apply to a
        cluster with no user node pools."""
        self.run_with(cluster={**self.CLUSTER, "autopilot": True})
        self.assertEqual(self.issued_node_pools_read(), [])

    def test_a_standard_cluster_still_issues_it_and_declares_nothing(self):
        entry = self.run_with(pools=[{"name": "p1", "locations": ["us-central1-a", "us-central1-b"]}])
        self.assertEqual(len(self.issued_node_pools_read()), 1)
        self.assertNotIn("single-zone-nodepool", self.declared_not_applicable(entry))
        self.assertIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})

    def test_a_failed_pools_read_says_so_instead_of_dropping_the_check(self):
        """`[]` meant both "no pools" and "could not read the pools", so a
        denied read took the check out of the manifest with nothing recording
        that it had been attempted — a coverage gap §6 could name but not
        explain."""
        entry = self.run_with(pools_rc=1, pools_stderr="PERMISSION_DENIED on container.nodePools.list")
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})
        # A read that was refused is a limitation, never a non-applicability:
        # the check applies, nobody could run it.
        self.assertNotIn("single-zone-nodepool", self.declared_not_applicable(entry))
        self.assertIn("single-zone-nodepool", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])
        self.assertIn("rc=1", entry["limitations"])

    def test_a_standard_cluster_with_no_pools_ran_the_check(self):
        """The other half of the same conflation: zero pools is an answer, and
        recording nothing for it made an empty cluster look unaudited."""
        entry = self.run_with(pools=[])
        self.assertIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})
        self.assertNotIn("limitations", entry)
        self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["candidates"]})

    def issued_advice_reads(self):
        return [a for a in self.issued if a[:5] == ["gcloud", "beta", "compute", "advice", "capacity-history"]]

    def test_a_clean_autoscaler_window_records_the_read_it_made(self):
        """"Nothing in 24h" is the answer this check exists to give. Recording
        nothing for it makes a healthy cluster look unaudited."""
        entry = self.run_with()
        self.assertIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["candidates"]})

    def test_a_stockout_in_the_window_is_reported(self):
        entry = self.run_with(log_entries=[ERROR_MSG_ENTRY, HEALTHY_ENTRY])
        hits = [c for c in entry["candidates"] if c["check"] == "autoscaler-out-of-resources"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "critical")
        self.assertIn("scale.up.error.out.of.resources", hits[0]["excerpt"])

    GROUP_URL = (
        "https://www.googleapis.com/compute/v1/projects/acme/zones/"
        "us-central1-b/instanceGroupManagers/gk3-prod-usc1-pool-3-b07eba62-grp"
    )

    def stockout_marker(self, pool_class, dump_items=()):
        pool = {
            "name": "pool-3",
            "locations": ["us-central1-b"],
            "config": {"labels": {fs.COMPUTE_CLASS_LABEL: pool_class} if pool_class else {}},
            "instanceGroupUrls": [self.GROUP_URL],
        }
        entry = self.run_with(dump_items=dump_items, pools=[pool], log_entries=[ERROR_MSG_ENTRY])
        (hit,) = [c for c in entry["candidates"] if c["check"] == "autoscaler-out-of-resources"]
        return hit["needs_triage"]

    def test_a_stockout_in_a_pool_its_compute_class_owns_is_sweepable(self):
        cc = compute_class("burst", [{"machineFamily": "n2"}, {"machineFamily": "n4"}])
        self.assertIsNone(self.stockout_marker("burst", dump_items=[cc]))

    def test_a_stockout_in_a_pool_no_compute_class_owns_is_marked(self):
        self.assertEqual(self.stockout_marker(None), fs.NEW_COMPUTE_CLASS_TRIAGE)

    def test_a_pool_labelled_with_a_class_the_cluster_lacks_is_marked(self):
        self.assertEqual(self.stockout_marker("gone"), fs.NEW_COMPUTE_CLASS_TRIAGE)

    def test_a_stockout_on_an_unread_pool_list_is_marked(self):
        entry = self.run_with(pools_rc=1, log_entries=[ERROR_MSG_ENTRY])
        (hit,) = [c for c in entry["candidates"] if c["check"] == "autoscaler-out-of-resources"]
        self.assertEqual(hit["needs_triage"], fs.NEW_COMPUTE_CLASS_TRIAGE)

    def test_a_refused_logging_read_is_a_limitation_not_a_clean_cluster(self):
        entry = self.run_with(log_rc=1, log_stderr="PERMISSION_DENIED on logging.logEntries.list")
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertIn("autoscaler-out-of-resources", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])

    def test_no_spot_anywhere_asks_no_capacity_history_and_declares_why(self):
        entry = self.run_with()
        self.assertEqual(self.issued_advice_reads(), [])
        self.assertIn("spot-scarcity-risk", self.declared_not_applicable(entry))

    def test_a_spot_shape_is_read_in_the_cluster_region(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice=lambda mt: capacity_history([0.05] * 10, mt))
        reads = self.issued_advice_reads()
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0][reads[0].index("--region") + 1], "us-central1")
        self.assertIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["candidates"]})

    def test_a_zonal_cluster_reads_its_region_not_its_zone(self):
        """`capacity-history` takes `--region` and rejects a zone."""
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        self.run_with(
            dump_items=[cc],
            cluster={**self.CLUSTER, "location": "us-central1-a"},
            advice=lambda mt: capacity_history([0.05] * 10, mt),
        )
        read = self.issued_advice_reads()[0]
        self.assertEqual(read[read.index("--region") + 1], "us-central1")

    def test_a_scarce_spot_shape_is_reported(self):
        cc = compute_class("cc1", [{"machineType": "a2-highgpu-1g", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice=lambda mt: capacity_history([0.4] * 10, mt))
        hits = [c for c in entry["candidates"] if c["check"] == "spot-scarcity-risk"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_each_scarce_shape_carries_the_read_that_found_it(self):
        """`commands` is one record per check per cluster, so two shapes
        overwrite each other there and both findings would cite the last read.
        On the live fleet `spot-capacity-test` asks for two shapes, and the
        record that survived named the one that produced no finding — an
        evidence line a reviewer runs to get data about a different machine
        type. `adopt_collector_evidence` prefers the candidate's own command.
        """
        # One shape each, so both clear §3.8's second arm; two classes rather
        # than two priorities on one, so the findings differ in `object` and
        # survive as two.
        classes = [
            compute_class("gpu", [{"machineType": "a2-highgpu-1g", "spot": True}]),
            compute_class("cpu", [{"machineType": "n2-standard-8", "spot": True}]),
        ]
        entry = self.run_with(dump_items=classes, advice=lambda mt: capacity_history([0.4] * 10, mt))
        hits = {c["object"]: c for c in entry["candidates"] if c["check"] == "spot-scarcity-risk"}
        self.assertEqual(set(hits), {"ComputeClass/gpu", "ComputeClass/cpu"})
        wanted = {"ComputeClass/gpu": "a2-highgpu-1g", "ComputeClass/cpu": "n2-standard-8"}
        for obj, hit in hits.items():
            argv = shlex.split(hit["command"])
            self.assertEqual(argv[argv.index("--machine-type") + 1], wanted[obj])
        # And the single per-slug record cannot serve both: it holds one read.
        recorded = [c for c in entry["commands"] if c["check"] == "spot-scarcity-risk"]
        self.assertEqual(len(recorded), 1)

    def test_the_shape_ceiling_names_what_it_did_not_read(self):
        """A collector that quietly stops looking is the failure this whole
        stream reports on."""
        many = [{"machineType": f"n2-standard-{n}", "spot": True} for n in (2, 4, 8, 16, 32, 48, 64, 80, 96)]
        cc = compute_class("cc1", many)
        entry = self.run_with(dump_items=[cc], advice=lambda mt: capacity_history([0.05] * 10, mt))
        self.assertEqual(len(self.issued_advice_reads()), fs.SPOT_MAX_SHAPES)
        self.assertIn("8 of this cluster's 9 distinct Spot machine shapes", entry["limitations"])

    def test_one_failed_shape_leaves_the_check_unevaluated_and_keeps_the_other_s_finding(self):
        """The regional quota reads' rule: `finish` carries `limitations` only
        for a check listed unevaluated, so recording spot-scarcity-risk as run
        because one shape answered published the failed shape as clean."""
        classes = [
            compute_class("gpu", [{"machineType": "a2-highgpu-1g", "spot": True}]),
            compute_class("cpu", [{"machineType": "n2-standard-8", "spot": True}]),
        ]
        entry = self.run_with(
            dump_items=classes,
            advice=lambda mt: capacity_history([0.4] * 10, mt),
            advice_rc=lambda mt: 1 if mt == "n2-standard-8" else 0,
        )
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        unevaluated = {e["check"]: e["reason"] for e in entry["checks_unevaluated"]}
        self.assertIn("reads failed: n2-standard-8", unevaluated["spot-scarcity-risk"])
        self.assertIn("answered: a2-highgpu-1g", unevaluated["spot-scarcity-risk"])
        hits = {c["object"] for c in entry["candidates"] if c["check"] == "spot-scarcity-risk"}
        self.assertEqual(hits, {"ComputeClass/gpu"})

    def test_shapes_past_the_ceiling_leave_the_check_unevaluated(self):
        many = [{"machineType": f"n2-standard-{n}", "spot": True} for n in (2, 4, 8, 16, 32, 48, 64, 80, 96)]
        entry = self.run_with(dump_items=[compute_class("cc1", many)], advice=lambda mt: capacity_history([0.05] * 10, mt))
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        unevaluated = {e["check"]: e["reason"] for e in entry["checks_unevaluated"]}
        self.assertIn("not read past the 8-shape ceiling: n2-standard-96", unevaluated["spot-scarcity-risk"])

    def test_a_refused_advice_read_is_a_limitation_not_a_clean_shape(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice_rc=1, advice_stderr="API [compute.googleapis.com] not enabled")
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        self.assertIn("n2-standard-8", entry["limitations"])
        self.assertIn("not enabled", entry["limitations"])

    def test_a_truncated_advice_read_is_a_limitation_not_a_clean_shape(self):
        """Same shim cut as the autoscaler read: exit 0, stdout that does not
        parse. `run_and_gate` returns None for it, which read as "no history"
        and published the shape as clean."""
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        truncated = json.dumps(capacity_history([0.05] * 10, "n2-standard-8"))[:-20]
        entry = self.run_with(
            dump_items=[cc], advice_stdout=truncated, advice_stderr="credential proxy output truncated"
        )
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        self.assertIn("n2-standard-8", entry["limitations"])
        self.assertIn("not JSON (rc=0)", entry["limitations"])
        self.assertIn("credential proxy output truncated", entry["limitations"])

    def test_a_family_only_spot_chain_is_a_limitation_not_a_non_applicability(self):
        """The cluster does ask for Spot; the API just cannot be asked about it."""
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        self.assertEqual(self.issued_advice_reads(), [])
        self.assertNotIn("spot-scarcity-risk", self.declared_not_applicable(entry))
        self.assertIn("cc1:c3", entry["limitations"])

    def spot_non_applicability(self, entry):
        return next(
            e["reason"] for e in entry["checks_not_applicable"] if e["check"] == "spot-scarcity-risk"
        )

    def test_a_shape_free_spot_chain_says_so_rather_than_denying_the_cluster_uses_spot(self):
        """Every cluster that can scale up through `autopilot-spot` really does
        leave the shape to GKE, so the wrong reason here is one an operator reads
        once per Autopilot cluster in the fleet."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        entry = self.run_with(cluster={**self.CLUSTER, "autopilot": True}, dump_items=[cc])
        reason = self.spot_non_applicability(entry)
        self.assertIn("ComputeClass/autopilot-spot", reason)
        self.assertIn("leaves the machine shape entirely to GKE", reason)
        # ccc-missing-fallbacks is the one limitation: the chain names no machine.
        self.assertNotIn("spot-scarcity-risk", entry["limitations"])

    def test_node_auto_provisioning_makes_a_standard_cluster_read_the_same_way(self):
        """`brokered` is about whether a node can be created through the class,
        not about the cluster's mode. NAP on Standard can, so the Autopilot
        sentence is the true one there too."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        entry = self.run_with(cluster={**self.CLUSTER, "has_nap": True}, dump_items=[cc])
        self.assertIn("leaves the machine shape entirely to GKE", self.spot_non_applicability(entry))

    def test_a_standard_cluster_without_nap_says_no_shape_was_named(self):
        """GKE pre-installs `autopilot-spot` on Standard clusters. Ten of this
        fleet's sixteen clusters are Standard with cluster-level
        auto-provisioning off, and every one published "Every Spot request on
        this cluster leaves the machine shape entirely to GKE" -- a claim about
        Spot requests a cluster that makes none, displacing the true sentence
        that was already in the code below it.

        The reason says nothing about whether a node could be created, because
        the answer is yes and the first attempt at this fix said no. A
        ComputeClass's own `nodePoolAutoCreation.enabled: true` provisions
        independently of the cluster flag: `spot-capacity-test` has NAP off and
        GKE still built `nap-e2-standard-2-spot-rbu9q0zw` there to place a pod
        that selected `autopilot-spot`. What makes the check inapplicable is
        that no shape was named, which is true either way."""
        cc = compute_class("autopilot-spot", [{"spot": True}])
        entry = self.run_with(dump_items=[cc])
        reason = self.spot_non_applicability(entry)
        self.assertIn("The only Spot priority on this cluster is in ComputeClass/autopilot-spot", reason)
        self.assertIn("names no machine family or machine type", reason)
        self.assertNotIn("Every Spot request", reason)
        self.assertNotIn("auto-provisioning", reason)
        self.assertNotIn("no node can be created", reason)
        # ccc-missing-fallbacks is the one limitation: the chain names no machine.
        self.assertNotIn("spot-scarcity-risk", entry["limitations"])

    def test_a_hand_authored_shape_free_class_is_not_called_pre_installed(self):
        # The collector reads neither the class's origin nor the workloads, so
        # the reason claims neither: `spot-batch` may be selected and provision
        # through its own `nodePoolAutoCreation`.
        cc = compute_class("spot-batch", [{"podFamily": "general-purpose", "spot": True}])
        reason = self.spot_non_applicability(self.run_with(dump_items=[cc]))
        self.assertIn("ComputeClass/spot-batch", reason)
        self.assertNotIn("pre-installed", reason)
        self.assertNotIn("Nothing on this cluster requests Spot", reason)

    def test_dangling_reference_reported(self):
        d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
        entry = self.run_with(dump_items=[d])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("dangling-compute-class", slugs)

    def test_mixed_disk_generation_on_stateful_reported(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="standard-rwo")
        entry = self.run_with(dump_items=[cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-mixed-disk-generations", slugs)

    def test_mixed_disk_generation_on_dynamic_rwo_is_excluded_only_from_1_35_3(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="dynamic-rwo")
        other = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="standard-rwo")
        for version, items, flagged in (
            ("1.35.3-gke.1200", [cc, sts], False),
            ("1.36.0-gke.100", [cc, sts], False),
            ("1.35.2-gke.900", [cc, sts], True),
            ("", [cc, sts], True),
            ("1.35.3-gke.1200", [cc, other], True),
        ):
            with self.subTest(version=version, items=items[1]["spec"]["volumeClaimTemplates"]):
                entry = self.run_with(dump_items=items, cluster={**self.CLUSTER, "version": version})
                slugs = {c["check"] for c in entry["candidates"]}
                self.assertEqual("ccc-mixed-disk-generations" in slugs, flagged)

    def test_mixed_disk_generation_excerpt_names_the_control_plane_version(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="standard-rwo")
        entry = self.run_with(dump_items=[cc, sts], cluster={**self.CLUSTER, "version": "1.35.2-gke.900"})
        (hit,) = [c for c in entry["candidates"] if c["check"] == "ccc-mixed-disk-generations"]
        self.assertIn("control plane 1.35.2-gke.900, before 1.35.3", hit["excerpt"])

    def test_mixed_disk_generation_excerpt_says_when_the_version_is_unknown(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="standard-rwo")
        entry = self.run_with(dump_items=[cc, sts], cluster={**self.CLUSTER, "version": ""})
        (hit,) = [c for c in entry["candidates"] if c["check"] == "ccc-mixed-disk-generations"]
        self.assertIn("control plane version unknown", hit["excerpt"])

    def test_mixed_disk_generation_not_flagged_without_persistent_volumes(self):
        cc = compute_class("cc1", [{"machineFamily": "n2"}, {"machineFamily": "c4"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"})  # no volumeClaimTemplates
        entry = self.run_with(dump_items=[cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertNotIn("ccc-mixed-disk-generations", slugs)

    def test_hyperdisk_incompatible_reported(self):
        sc = storage_class("hd", params={"type": "hyperdisk-balanced"})
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"}, storage_class_name="hd")
        entry = self.run_with(dump_items=[sc, cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-hyperdisk-incompatible", slugs)

    def test_a_claim_naming_no_class_uses_the_default_hyperdisk_class(self):
        sc = storage_class("hd", params={"type": "hyperdisk-balanced"})
        sc["metadata"]["annotations"] = {"storageclass.kubernetes.io/is-default-class": "true"}
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"})
        sts["spec"]["volumeClaimTemplates"] = [{"spec": {}}]
        entry = self.run_with(dump_items=[sc, cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("ccc-hyperdisk-incompatible", slugs)

    def test_an_empty_class_name_is_static_binding_not_the_default(self):
        sc = storage_class("hd", params={"type": "hyperdisk-balanced"})
        sc["metadata"]["annotations"] = {"storageclass.kubernetes.io/is-default-class": "true"}
        cc = compute_class("cc1", [{"machineFamily": "c4"}, {"machineFamily": "e2"}])
        sts = statefulset("db", node_selector={"cloud.google.com/compute-class": "cc1"})
        sts["spec"]["volumeClaimTemplates"] = [{"spec": {"storageClassName": ""}}]
        entry = self.run_with(dump_items=[sc, cc, sts])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertNotIn("ccc-hyperdisk-incompatible", slugs)

    def test_no_ondemand_floor_escalates_for_a_referencing_inference_workload(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        gpu_container = {"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}}
        d = deployment("infer", node_selector={"cloud.google.com/compute-class": "cc1"}, containers=[gpu_container])
        entry = self.run_with(dump_items=[cc, d])
        hit = next(c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor")
        self.assertEqual(hit["severity"], "critical")

    def test_no_ondemand_floor_stays_major_for_an_app_that_only_calls_a_model(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        caller = {"name": "app", "image": "example/web:1", "env": [{"name": "OPENAI_API_KEY", "value": "x"}]}
        d = deployment("chat-ui", node_selector={"cloud.google.com/compute-class": "cc1"}, containers=[caller])
        entry = self.run_with(dump_items=[cc, d])
        hit = next(c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor")
        self.assertEqual(hit["severity"], "major")

    def test_no_ondemand_floor_stays_major_for_a_non_inference_referencing_workload(self):
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])
        d = deployment("web", node_selector={"cloud.google.com/compute-class": "cc1"})
        entry = self.run_with(dump_items=[cc, d])
        hit = next(c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor")
        self.assertEqual(hit["severity"], "major")

    def test_reservation_affinity_reported(self):
        cc = compute_class("cc1", [{"machineFamily": "n4", "reservations": {"affinity": "Automatic"}}])
        entry = self.run_with(dump_items=[cc])
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("reservation-mismatch-risk", slugs)


    def test_a_cluster_with_no_compute_class_records_every_ccc_check(self):
        """No ComputeClass and no StatefulSet is an answer the dump gave, not
        a check nobody ran; leaving the slugs out made the cluster partially
        audited on every run."""
        entry = self.run_with(dump_items=[])
        run = {c["check"] for c in entry["commands"]}
        for slug in (
            "ccc-missing-fallbacks", "ccc-no-ondemand-floor", "ccc-large-vm-scarcity",
            "ccc-priority-starvation", "ccc-mixed-disk-generations",
            "ccc-hyperdisk-incompatible", "reservation-mismatch-risk",
        ):
            self.assertIn(slug, run)

    def test_a_dump_with_no_items_list_is_gate_failed_not_empty(self):
        target = self.CLUSTER

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps({"kind": "Status"}))
            raise AssertionError(argv)

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                entry = fs.collect_cluster(target, run=run)
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("items", entry["error"])

    def test_the_target_name_is_qualified(self):
        entry = self.run_with(dump_items=[])
        c = self.CLUSTER
        self.assertEqual(entry["name"], f"{c['project']}/{c['location']}/{c['name']}")

    def test_a_failed_node_pool_read_is_not_a_cluster_without_spot(self):
        entry = self.run_with(dump_items=[], pools_rc=1)
        self.assertNotIn("spot-scarcity-risk", self.declared_not_applicable(entry))
        self.assertEqual(
            {e["check"] for e in entry["checks_unevaluated"]},
            {"single-zone-nodepool", "spot-scarcity-risk"},
        )
        self.assertIn("spot-scarcity-risk read no Spot node pool", entry["limitations"])

    def test_a_truncated_node_pool_read_is_unevaluated_not_gate_failed(self):
        """A cut or non-list answer at exit 0 used to raise inside the pool
        checks and fail the whole cluster's gate."""
        truncated = json.dumps([{"name": "p1", "locations": ["us-central1-a"]}] * 3)[:-10]
        stray = json.dumps([{"name": "p1", "locations": ["us-central1-a"]}, "p2"])
        for stdout in (truncated, json.dumps({"error": "shim"}), stray):
            with self.subTest(stdout=stdout[:20]):
                entry = self.run_with(
                    dump_items=[], pools_stdout=stdout, pools_stderr="credential proxy output truncated"
                )
                self.assertEqual(entry["outcome"], "collected")
                self.assertNotIn("single-zone-nodepool", {c["check"] for c in entry["commands"]})
                self.assertIn("single-zone-nodepool", {e["check"] for e in entry["checks_unevaluated"]})
                self.assertIn("not a JSON list of node pools (rc=0)", entry["limitations"])

    def test_an_empty_node_pool_read_is_still_a_cluster_with_no_pools(self):
        entry = self.run_with(dump_items=[], pools_stdout="")
        self.assertNotIn("single-zone-nodepool", {e["check"] for e in entry.get("checks_unevaluated", [])})

    def test_a_failed_node_pool_read_beside_a_family_only_spot_class_is_unevaluated(self):
        # The family-only request is a limitation, but it says nothing about
        # the node pools, which were not read; the check landed in no list.
        cc = compute_class("cc1", [{"machineFamily": "n2", "spot": True}])
        entry = self.run_with(dump_items=[cc], pools_rc=1)
        self.assertNotIn("spot-scarcity-risk", self.declared_not_applicable(entry))
        self.assertIn("spot-scarcity-risk", {e["check"] for e in entry["checks_unevaluated"]})
        self.assertIn("name no machine type", entry["limitations"])

    def test_a_family_only_spot_class_beside_read_pools_is_unevaluated(self):
        # Pools read, none Spot, and the only Spot request names no machine
        # type: the check could not run, so it cannot be left out of every list.
        cc = compute_class("cc1", [{"machineFamily": "n2", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        self.assertNotIn("spot-scarcity-risk", self.declared_not_applicable(entry))
        self.assertIn("spot-scarcity-risk", {e["check"] for e in entry["checks_unevaluated"]})

    def test_a_failed_node_pool_read_beside_a_typed_spot_class_is_unevaluated(self):
        # The class's shape was read and answered, so the check looked run,
        # but no Spot node pool's shape was ever asked about.
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        entry = self.run_with(dump_items=[cc], pools_rc=1, advice=capacity_history([0.01] * 7))
        self.assertEqual(len(self.issued_advice_reads()), 1)
        self.assertIn("spot-scarcity-risk", {e["check"] for e in entry["checks_unevaluated"]})
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        reason = {e["check"]: e["reason"] for e in entry["checks_unevaluated"]}["spot-scarcity-risk"]
        self.assertIn("no Spot node pool's shape was asked about", reason)
        self.assertIn("answered: n2-standard-8", reason)

    def test_a_shape_answered_with_no_history_is_unevaluated(self):
        # Exit 0 and nothing measured: an empty stdout, an object with no
        # preemptionHistory, or too few days to average. Each left the check
        # recorded as run over a shape nobody measured.
        hot = compute_class("hot", [{"machineType": "n2-standard-8", "spot": True}])
        cold = compute_class("cold", [{"machineType": "c3-standard-8", "spot": True}])
        # `None` is the fake's empty stdout at exit 0.
        for label, cold_answer in (
            ("empty", None),
            ("no history", {"machineType": "c3-standard-8"}),
            ("thin", capacity_history([0.9] * 2, "c3-standard-8")),
        ):
            with self.subTest(label):
                def advice(machine_type, cold_answer=cold_answer):
                    return capacity_history([0.9] * 7, machine_type) if machine_type == "n2-standard-8" else cold_answer
                entry = self.run_with(dump_items=[hot, cold], pools=[], advice=advice)
                reason = {e["check"]: e["reason"] for e in entry.get("checks_unevaluated", [])}.get("spot-scarcity-risk", "")
                self.assertIn("c3-standard-8", reason)
                self.assertIn("answered: n2-standard-8", reason)
                self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
                # The measured shape's finding still files.
                self.assertIn("spot-scarcity-risk", {c["check"] for c in entry["candidates"]})

    def test_a_family_only_request_beside_a_named_shape_is_unevaluated(self):
        typed = compute_class("typed", [{"machineType": "n2-standard-8", "spot": True}])
        family = compute_class("family", [{"machineFamily": "c3", "spot": True}])
        entry = self.run_with(dump_items=[typed, family], pools=[], advice=capacity_history([0.01] * 7))
        reason = {e["check"]: e["reason"] for e in entry.get("checks_unevaluated", [])}.get("spot-scarcity-risk", "")
        self.assertIn("family:c3", reason)
        self.assertIn("answered: n2-standard-8", reason)
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})

    def test_a_failed_node_pool_read_leaves_the_auto_creation_arm_unevaluated(self):
        # With auto-creation off, the class is dangling only if no node pool
        # carries its label, and the labels were never read.
        cc = compute_class("cc1", [{"machineFamily": "n4"}], node_pool_auto_creation=False)
        workload = deployment("web", node_selector={fs.COMPUTE_CLASS_LABEL: "cc1"})
        entry = self.run_with(dump_items=[cc, workload], pools_rc=1)
        self.assertIn("dangling-compute-class", {e["check"] for e in entry["checks_unevaluated"]})
        self.assertNotIn("dangling-compute-class", {c["check"] for c in entry["commands"]})

    def test_a_missing_class_found_beside_the_unevaluated_arm_carries_its_own_command(self):
        """The missing-class arm still files, and the slug's record is popped."""
        cc = compute_class("cc1", [{"machineFamily": "n4"}], node_pool_auto_creation=False)
        reader = deployment("web", node_selector={fs.COMPUTE_CLASS_LABEL: "cc1"})
        dangling = deployment("api", node_selector={fs.COMPUTE_CLASS_LABEL: "missing"})
        entry = self.run_with(dump_items=[cc, reader, dangling], pools_rc=1)
        self.assertNotIn("dangling-compute-class", {c["check"] for c in entry["commands"]})
        hits = [c for c in entry["candidates"] if c["check"] == "dangling-compute-class"]
        self.assertTrue(hits)
        for hit in hits:
            self.assertIn("kubectl get", hit.get("command", ""))

    def test_a_failed_node_pool_read_leaves_an_auto_creating_class_evaluated(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}])
        workload = deployment("web", node_selector={fs.COMPUTE_CLASS_LABEL: "cc1"})
        entry = self.run_with(dump_items=[cc, workload], pools_rc=1)
        self.assertNotIn("dangling-compute-class", {e["check"] for e in entry["checks_unevaluated"]})
        self.assertIn("dangling-compute-class", {c["check"] for c in entry["commands"]})

    def test_a_failed_autoscaler_read_is_unevaluated(self):
        entry = self.run_with(dump_items=[], log_rc=1)
        self.assertIn("autoscaler-out-of-resources", {e["check"] for e in entry["checks_unevaluated"]})

    def test_a_truncated_autoscaler_read_is_unevaluated_not_clean(self):
        """The sandbox's `gcloud` shim cuts stdout at the broker's cap, says so
        on stderr and exits with the child's code, 0. The cut JSON does not
        parse; scoring that as an empty window published the busiest cluster
        -- the one whose log reached the cap -- as free of stockouts."""
        truncated = json.dumps([ERROR_MSG_ENTRY] * 3)[:-40]
        entry = self.run_with(dump_items=[], log_stdout=truncated, log_stderr="credential proxy output truncated")
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertIn("autoscaler-out-of-resources", {e["check"] for e in entry["checks_unevaluated"]})
        self.assertIn("credential proxy output truncated", entry["limitations"])

    def test_an_autoscaler_answer_that_is_not_a_list_of_entries_is_unevaluated(self):
        # `{}` iterated nothing, and a string element was skipped: both read
        # as a clean window.
        for stdout in ("{}", json.dumps([ERROR_MSG_ENTRY, "entry"])):
            with self.subTest(stdout=stdout[:20]):
                entry = self.run_with(dump_items=[], log_stdout=stdout)
                self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
                self.assertIn("autoscaler-out-of-resources", {e["check"] for e in entry["checks_unevaluated"]})
                self.assertIn("not a JSON list of entries", entry["limitations"])

    def test_an_empty_autoscaler_read_is_still_clean(self):
        entry = self.run_with(dump_items=[], log_stdout="\n")
        self.assertIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertNotIn("autoscaler-out-of-resources", {e["check"] for e in entry.get("checks_unevaluated", [])})

    def test_the_autoscaler_read_is_pinned_to_the_location(self):
        self.run_with(dump_items=[])
        read = next(a for a in self.issued if a[:3] == ["gcloud", "logging", "read"])
        self.assertIn(f'resource.labels.location="{self.CLUSTER["location"]}"', read[3])

    def test_a_full_autoscaler_page_leaves_the_check_unevaluated(self):
        """A limitations sentence alone left the check recorded as run, and
        `finish` carries limitations only for an unevaluated check."""
        entry = self.run_with(dump_items=[], log_entries=[{"jsonPayload": {}}] * fs.AUTOSCALER_LOG_LIMIT)
        self.assertIn("older entries", entry["limitations"])
        self.assertIn("autoscaler-out-of-resources", {e["check"] for e in entry["checks_unevaluated"]})
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})

    def test_findings_on_a_full_autoscaler_page_carry_their_own_command(self):
        """The slug's `commands` record is popped, and `adopt_collector_evidence`
        skips a candidate with no command of its own."""
        entry = self.run_with(dump_items=[], log_entries=[ERROR_MSG_ENTRY] * fs.AUTOSCALER_LOG_LIMIT)
        hits = [c for c in entry["candidates"] if c["check"] == "autoscaler-out-of-resources"]
        self.assertTrue(hits)
        for hit in hits:
            self.assertIn("gcloud logging read", hit.get("command", ""))

    def test_a_page_under_the_limit_is_recorded_as_run(self):
        entry = self.run_with(dump_items=[], log_entries=[{"jsonPayload": {}}] * (fs.AUTOSCALER_LOG_LIMIT - 1))
        self.assertIn("autoscaler-out-of-resources", {c["check"] for c in entry["commands"]})
        self.assertNotIn("autoscaler-out-of-resources", {e["check"] for e in entry.get("checks_unevaluated", [])})

    def test_an_unzoned_chain_on_unread_pools_is_unevaluated_not_critical(self):
        """§3.1's own Do-NOT-flag chain, with the pools read failing."""
        span_decides = compute_class("cc1", [{"machineFamily": f} for f in ("c3", "n4", "n2")])
        pinned = compute_class("cc2", [{"machineFamily": "c3"}])
        entry = self.run_with(dump_items=[span_decides, pinned], pools_rc=1)
        fallbacks = [c["object"] for c in entry["candidates"] if c["check"] == "ccc-missing-fallbacks"]
        self.assertEqual(fallbacks, ["ComputeClass/cc2"])
        [reason] = [e["reason"] for e in entry["checks_unevaluated"] if e["check"] == "ccc-missing-fallbacks"]
        self.assertIn("ComputeClass/cc1", reason)
        self.assertIn("node-pools list", reason)
        self.assertNotIn("ccc-missing-fallbacks", {c["check"] for c in entry["commands"]})
        self.assertIn("ccc-missing-fallbacks could not be judged", entry["limitations"])
        # The slug's record is popped, so the filed finding carries its own.
        [filed] = [c for c in entry["candidates"] if c["check"] == "ccc-missing-fallbacks"]
        self.assertIn("kubectl get", filed.get("command", ""))

    def test_a_chain_naming_no_machine_files_no_critical(self):
        pools_only = compute_class("cc1", [{"nodepools": ["a"]}, {"nodepools": ["b"]}])
        gpu_only = compute_class("cc3", [{"gpu": {"type": "nvidia-l4", "count": 1}}])
        pinned = compute_class("cc2", [{"machineFamily": "c3"}])
        pools = [{"name": "p1", "locations": ["us-central1-a"]}]
        entry = self.run_with(dump_items=[pools_only, gpu_only, pinned], pools=pools)
        fallbacks = [c["object"] for c in entry["candidates"] if c["check"] == "ccc-missing-fallbacks"]
        self.assertEqual(fallbacks, ["ComputeClass/cc2"])
        [reason] = [e["reason"] for e in entry["checks_unevaluated"] if e["check"] == "ccc-missing-fallbacks"]
        self.assertIn("ComputeClass/cc1, ComputeClass/cc3", reason)
        self.assertIn("node pools, a pod family, an accelerator or nothing", reason)
        self.assertIn("does not read the machines those resolve to", entry["limitations"])
        self.assertNotIn("ccc-missing-fallbacks", {c["check"] for c in entry["commands"]})

    def test_a_regional_cluster_s_node_locations_clear_an_unzoned_chain(self):
        cc = compute_class("cc1", [{"machineFamily": "n4"}, {"machineFamily": "n4", "spot": True}])
        cluster = {**self.CLUSTER, "locations": ["us-central1-a", "us-central1-b", "us-central1-c"]}
        pools = [{"name": "gpu", "locations": ["us-central1-a"]}]
        entry = self.run_with(dump_items=[cc], pools=pools, cluster=cluster)
        self.assertIn("ccc-missing-fallbacks", {c["check"] for c in entry["commands"]})
        self.assertNotIn("ccc-missing-fallbacks", {c["check"] for c in entry["candidates"]})

    def test_an_unzoned_chain_on_read_multi_zone_pools_is_run_and_clean(self):
        span_decides = compute_class("cc1", [{"machineFamily": f} for f in ("c3", "n4", "n2")])
        pools = [{"name": "p1", "locations": ["us-central1-a", "us-central1-b"]}]
        entry = self.run_with(dump_items=[span_decides], pools=pools)
        self.assertIn("ccc-missing-fallbacks", {c["check"] for c in entry["commands"]})
        self.assertNotIn("ccc-missing-fallbacks", {c["check"] for c in entry["candidates"]})

    def test_every_capacity_read_failing_leaves_spot_unevaluated(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        entry = self.run_with(dump_items=[cc], advice_rc=1)
        self.assertNotIn("spot-scarcity-risk", {c["check"] for c in entry["commands"]})
        self.assertIn("spot-scarcity-risk", {e["check"] for e in entry["checks_unevaluated"]})

    def test_two_hot_shapes_in_one_class_are_one_candidate(self):
        cc = compute_class(
            "cc1",
            [{"machineType": "n2-standard-8", "spot": True}, {"machineType": "n2-standard-16", "spot": True}],
        )
        entry = self.run_with(
            dump_items=[cc], advice=lambda mt: [capacity_history([0.5] * 10, machine_type=mt)]
        )
        spot = [c for c in entry["candidates"] if c["check"] == "spot-scarcity-risk"]
        self.assertEqual(len(spot), 1)
        self.assertIn("n2-standard-8", spot[0]["excerpt"])
        self.assertIn("n2-standard-16", spot[0]["excerpt"])

    def test_a_system_namespace_workload_is_excluded(self):
        bad = deployment("dns", ns="kube-system", node_selector={"cloud.google.com/compute-class": "missing"})
        entry = self.run_with(dump_items=[bad])
        self.assertEqual([c for c in entry["candidates"] if c["check"] == "dangling-compute-class"], [])

    def test_each_standard_exclusion_drops_a_dangling_reference(self):
        cases = {
            "S2": {"metadata": {"labels": {fs.ADDON_MANAGER_LABEL: "Reconcile"}}},
            "S3": {"metadata": {"ownerReferences": [{"kind": "Operator", "name": "x"}]}},
            "S4": {"metadata": {"labels": {fs.OPT_OUT_LABEL: fs.OPT_OUT_VALUE}}},
            "S5": {"spec": {"replicas": 0}},
        }
        for rule, patch_ in cases.items():
            with self.subTest(rule=rule):
                d = deployment("api", node_selector={"cloud.google.com/compute-class": "missing"})
                d["metadata"].update(patch_.get("metadata", {}))
                d["spec"].update(patch_.get("spec", {}))
                self.assertEqual(fs.standard_excluded(d), rule)
                entry = self.run_with(dump_items=[d])
                self.assertEqual(entry["candidates"], [])

    def test_an_excluded_workload_does_not_make_a_class_inference_referenced(self):
        cc = compute_class("gpu-spot", [{"machineFamily": "a2", "spot": True}])
        gpu = deployment(
            "vllm", ns="kube-system", node_selector={"cloud.google.com/compute-class": "gpu-spot"},
            containers=[{"name": "m", "image": "vllm/vllm-openai", "resources": {"limits": {"nvidia.com/gpu": 1}}}],
        )
        entry = self.run_with(dump_items=[cc, gpu])
        floor = [c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor"]
        self.assertEqual([c["severity"] for c in floor], ["major"])

    def test_a_non_production_workload_does_not_make_a_class_inference_referenced(self):
        cc = compute_class("gpu-spot", [{"machineFamily": "a2", "spot": True}])
        for name, ns, labels in (("vllm", "ml-dev", {}), ("serve-staging", "default", {}), ("vllm", "default", {"env": "qa"})):
            with self.subTest(name=name, ns=ns, labels=labels):
                gpu = deployment(
                    name, ns=ns, node_selector={"cloud.google.com/compute-class": "gpu-spot"},
                    containers=[{"name": "m", "image": "vllm/vllm-openai", "resources": {"limits": {"nvidia.com/gpu": 1}}}],
                )
                gpu["metadata"].setdefault("labels", {}).update(labels)
                entry = self.run_with(dump_items=[cc, gpu])
                floor = [c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor"]
                self.assertEqual([c["severity"] for c in floor], ["major"])

    def test_a_non_production_class_is_not_flagged_for_its_spot_floor(self):
        cc = compute_class("batch-staging", [{"machineFamily": "n2", "spot": True}])
        entry = self.run_with(dump_items=[cc])
        self.assertEqual([c for c in entry["candidates"] if c["check"] == "ccc-no-ondemand-floor"], [])


class StockoutExclusionHelpersTest(unittest.TestCase):
    def test_non_production_reads_name_tokens_not_substrings(self):
        self.assertTrue(fs.is_non_production("api-dev"))
        self.assertTrue(fs.is_non_production("qa_runner"))
        self.assertFalse(fs.is_non_production("developer-portal"))
        self.assertFalse(fs.is_non_production("latest"))

    def test_non_production_reads_environment_labels(self):
        self.assertTrue(fs.is_non_production("api", {"env": "Staging"}))
        self.assertFalse(fs.is_non_production("api", {"env": "prod"}))

    def test_a_gke_prefixed_namespace_is_a_system_namespace(self):
        self.assertEqual(fs.standard_excluded({"metadata": {"namespace": "gke-managed-cim"}}), "S1")


class LargeVmIdentityTest(unittest.TestCase):
    def test_two_large_priorities_are_one_candidate(self):
        cc = compute_class("big", [{"machineType": "n2-standard-64"}, {"machineType": "n2-standard-48"}])
        hits = fs.check_ccc_large_vm_scarcity(cc)
        self.assertEqual(len(hits), 1)
        self.assertIn("n2-standard-64", hits[0]["excerpt"])
        self.assertIn("n2-standard-48", hits[0]["excerpt"])


class SpotFamiliesPerOwnerTest(unittest.TestCase):
    def test_a_single_family_pool_is_not_excused_by_another_owners_chain(self):
        cc = compute_class("wide", [{"machineType": "n2-standard-8", "spot": True}, {"machineFamily": "c3"}])
        pool = {"name": "p1", "config": {"spot": True, "machineType": "n2-standard-8"}}
        shape = fs.spot_shapes([cc], [pool])["n2-standard-8"]
        hit, _ = fs.check_spot_scarcity("n2-standard-8", shape, "us-central1", capacity_history([0.5] * 10))
        self.assertEqual(hit["object"], "NodePool/p1")

    def test_a_non_production_pool_is_not_a_spot_owner(self):
        pool = {"name": "ci-test", "config": {"spot": True, "machineType": "n2-standard-8"}}
        self.assertEqual(fs.spot_shapes([], [pool]), {})


class EnumerationFailureTest(unittest.TestCase):
    def test_a_failed_cluster_list_sets_the_manifest_error(self):
        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        manifest = fs.collect_fleet("acme", run=run)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("denied", manifest["error"])

    def test_non_json_cluster_list_sets_the_manifest_error(self):
        def run(argv, **kwargs):
            return run_of(0, "WARNING: something")

        manifest = fs.collect_fleet("acme", run=run)
        self.assertIn("parseable JSON", manifest["error"])


def fleet_run(clusters_by_project, *, projects="acme\nbeta\n", active="acme", cluster_list=None):
    """Discovery lists `projects`; each project's `clusters list` answers from
    `clusters_by_project`, or `cluster_list(project)` when given."""
    def run(argv, **kwargs):
        if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
            return run_of(0, active + "\n")
        if argv[:2] == ["gcloud", "projects"] and "list" in argv:
            return run_of(0, projects)
        if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
            project = argv[argv.index("--project") + 1]
            if cluster_list:
                return cluster_list(project)
            return run_of(0, json.dumps([{"name": n, "location": "us-central1", "status": "RUNNING"} for n in clusters_by_project.get(project, [])]))
        if "get-credentials" in argv:
            return run_of(0)
        if argv[:2] == ["kubectl", "get"]:
            return run_of(0, json.dumps(dump_of()))
        if argv[:3] == ["gcloud", "container", "node-pools"]:
            return run_of(0, "[]")
        if argv[:3] == ["gcloud", "compute", "reservations"]:
            return run_of(0, "[]")
        if argv[:3] == ["gcloud", "compute", "regions"]:
            return run_of(0, json.dumps({"quotas": []}))
        return run_of(0, "")
    return run


class ProjectDiscoveryTest(unittest.TestCase):
    def collect(self, run, project=None):
        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                return fs.collect_fleet(project, run=run)

    def test_every_listed_project_is_read_not_only_the_active_one(self):
        # One project's clusters, all `collected`, certified a whole fleet.
        manifest = self.collect(fleet_run({"acme": ["c1"], "beta": ["c2"]}))
        self.assertEqual(
            {c["name"] for c in manifest["clusters"]},
            {"acme/us-central1/c1", "beta/us-central1/c2", "project/acme", "project/beta"},
        )

    def test_a_project_holding_no_cluster_is_still_read_for_its_reservations(self):
        # §3.10's idle reservation needs no cluster beside it, and a shared
        # reservation often lives in a project that holds none.
        manifest = self.collect(fleet_run({"acme": ["c1"]}))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(set(by_name), {"acme/us-central1/c1", "project/acme", "project/beta"})
        self.assertEqual([c["check"] for c in by_name["project/beta"]["commands"]], ["reservation-mismatch-risk"])
        self.assertEqual([c["check"] for c in by_name["project/beta"]["checks_not_applicable"]], ["quota-exhaustion-risk"])

    def test_a_cluster_free_project_is_not_a_coverage_gap(self):
        # Its quota check has no region to read; undeclared, `coverage_gaps`
        # counted it as a check that did not run and pinned the run partial.
        import audit_report

        manifest = self.collect(fleet_run({"acme": ["c1"]}))
        data = {
            "audit": "stockout-prevention",
            "scope": {
                "clusters": [
                    {
                        "name": e["name"],
                        "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]],
                        "checks_not_applicable": e.get("checks_not_applicable", []),
                    }
                    for e in manifest["clusters"]
                ]
            },
        }
        self.assertEqual([g for g in audit_report.coverage_gaps(data) if "project/beta" in g], [])

    def test_a_project_whose_only_cluster_is_not_running_still_reads_its_quota(self):
        # A DEGRADED cluster is unaudited but draws on its region's quota; the
        # project does not hold "no cluster".
        def cluster_list(project):
            return run_of(0, json.dumps([{"name": "c1", "location": "us-east1-b", "status": "DEGRADED"}]))

        manifest = self.collect(fleet_run({}, cluster_list=cluster_list, projects="acme\n"))
        acme = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertEqual(acme.get("checks_not_applicable", []), [])
        self.assertIn("quota-exhaustion-risk", [c["check"] for c in acme["commands"]])
        self.assertIn("us-east1", " ".join(c["command"] for c in acme["commands"]))

    def test_the_active_project_is_read_when_the_listing_omits_it(self):
        manifest = self.collect(fleet_run({"acme": ["c1"], "beta": ["c2"]}, projects="beta\n"))
        self.assertIn("acme/us-central1/c1", {c["name"] for c in manifest["clusters"]})
        self.assertNotIn("error", manifest)
        # A listing that omits the active project is filtered, so the run is partial.
        entry = next(c for c in manifest["clusters"] if c["name"] == fs.UNENUMERATED_PROJECTS_TARGET)
        self.assertIn("did not name the active project 'acme'", entry["error"])

    def test_a_cluster_free_project_without_compute_engine_leaves_no_target(self):
        inner = fleet_run({"acme": ["c1"]})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"] and "beta" in argv:
                return run_of(1, "", "ERROR: SERVICE_DISABLED: Compute Engine API has not been used in project beta")
            return inner(argv, **kwargs)

        manifest = self.collect(run)
        self.assertEqual({c["name"] for c in manifest["clusters"]}, {"acme/us-central1/c1", "project/acme"})

    def test_a_quota_project_s_compute_refusal_is_a_failed_read_not_an_absent_project(self):
        inner = fleet_run({"acme": ["c1"]})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"] and "beta" in argv:
                return run_of(1, "", "ERROR: SERVICE_DISABLED: Compute Engine API has not been used in project quota-proj")
            return inner(argv, **kwargs)

        beta = next(c for c in self.collect(run)["clusters"] if c["name"] == "project/beta")
        self.assertIn("reservation-mismatch-risk", {e["check"] for e in beta.get("checks_unevaluated", [])})

    def test_a_fleet_that_yields_no_target_is_an_error_not_an_empty_manifest(self):
        # `finish` rejects an empty `scope.clusters`, so an empty manifest
        # without an error left the agent nothing to publish and no rule.
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                project = argv[argv.index("--project") + 1]
                return run_of(1, "", f"ERROR: SERVICE_DISABLED: Compute Engine API has not been used in project {project}")
            return fleet_run({})(argv, **kwargs)

        manifest = self.collect(run)
        self.assertEqual(manifest["clusters"], [])
        self.assertTrue(manifest["error"].startswith("nothing collected"))

    @staticmethod
    def compute_off_run(**fleet_kwargs):
        """No cluster anywhere, and every project's Compute Engine API off."""
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                project = argv[argv.index("--project") + 1]
                return run_of(1, "", f"ERROR: SERVICE_DISABLED: Compute Engine API has not been used in project {project}")
            return fleet_run({}, **fleet_kwargs)(argv, **kwargs)
        return run

    def test_a_failed_project_listing_is_named_when_nothing_is_collected(self):
        """Without `--project`, the discovery entry is a real failure: a
        `projects list` that failed took the rest of the fleet with it, and the
        run error names it rather than saying nothing recorded an error."""
        inner = self.compute_off_run()

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "ERROR: PERMISSION_DENIED")
            return inner(argv, **kwargs)

        error = self.collect(run)["error"]
        self.assertIn(f"First: {fs.UNENUMERATED_PROJECTS_TARGET}: `gcloud projects list` rc=1", error)

    def test_a_failed_listing_is_named_when_the_fallback_project_fails_too(self):
        """A failed `projects list` falls back to the active project; when
        that project's `clusters list` fails as well, the run ends before any
        target is built, and the error still names the listing that left the
        run holding one project."""
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "ERROR: PERMISSION_DENIED")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(1, "", "ERROR: UNAUTHENTICATED")
            return fleet_run({})(argv, **kwargs)

        error = self.collect(run)["error"]
        self.assertIn("1 project(s) could not be listed", error)
        self.assertIn("project discovery also failed: `gcloud projects list` rc=1", error)

    def test_a_scoped_project_whose_listing_fails_names_no_discovery(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(1, "", "ERROR: UNAUTHENTICATED")
            return fleet_run({})(argv, **kwargs)

        error = self.collect(run, project="acme")["error"]
        self.assertIn("1 project(s) could not be listed", error)
        self.assertNotIn("project discovery also failed", error)

    def test_every_cluster_unreachable_and_no_project_entry_is_nothing_collected(self):
        """An unreachable cluster evaluated no check: counting it as read
        emitted a manifest with nothing `finish` could take as a cluster."""
        inner = fleet_run({"acme": ["c1"]}, projects="acme\n")

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(1, "", "ERROR: credential broker unavailable")
            if argv[:2] == ["gcloud", "compute"]:
                raise RuntimeError("project read crashed")
            return inner(argv, **kwargs)

        manifest = self.collect(run)
        self.assertIn("nothing collected", manifest.get("error", ""))
        # The template names every way to yield nothing; `First:` is what the
        # run itself records, the cluster and the read that left it unreached.
        self.assertIn("First: acme/us-central1/c1: get-credentials rc=1", manifest["error"])

    def test_a_filtered_listing_is_not_named_as_why_nothing_was_collected(self):
        """A `projects list` that succeeded without naming the active project
        was read in full; its note says what the run may have missed, so the
        error gives the reason nothing was collected instead."""
        inner = self.compute_off_run()

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "other\n")
            return inner(argv, **kwargs)

        error = self.collect(run)["error"]
        self.assertNotIn(fs.UNENUMERATED_PROJECTS_TARGET, error)
        self.assertIn(f"First: {fs.NO_TARGET_REASON}", error)

    def test_a_scoped_project_that_yields_nothing_says_why_not_what_was_skipped(self):
        error = self.collect(self.compute_off_run(), project="acme")["error"]
        self.assertNotIn(fs.UNENUMERATED_PROJECTS_TARGET, error)
        self.assertIn(f"First: {fs.NO_TARGET_REASON}", error)

    def test_a_project_override_is_recorded_as_unenumerated(self):
        manifest = self.collect(fleet_run({"acme": ["c1"]}), project="acme")
        entry = next(c for c in manifest["clusters"] if c["name"] == fs.UNENUMERATED_PROJECTS_TARGET)
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("--project", entry["error"])

    def test_a_failed_project_listing_is_recorded_as_unenumerated(self):
        inner = fleet_run({"acme": ["c1"]})

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "projects"]:
                return run_of(1, "", "PERMISSION_DENIED: resourcemanager.projects.list")
            return inner(argv, **kwargs)

        manifest = self.collect(run)
        entry = next(c for c in manifest["clusters"] if c["name"] == fs.UNENUMERATED_PROJECTS_TARGET)
        self.assertIn("PERMISSION_DENIED", entry["error"])
        self.assertIn("acme/us-central1/c1", {c["name"] for c in manifest["clusters"]})

    def test_a_credential_that_sees_no_project_is_an_error(self):
        manifest = self.collect(fleet_run({}, projects="", active=""))
        self.assertEqual(manifest["error"], fs.NO_PROJECT_IN_SCOPE_ERROR)

    def test_one_unlistable_project_is_a_gate_failed_target_not_a_run_error(self):
        def cluster_list(project):
            if project == "beta":
                return run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
            return run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}]))

        manifest = self.collect(fleet_run({}, cluster_list=cluster_list))
        self.assertNotIn("error", manifest)
        beta = next(c for c in manifest["clusters"] if c["name"] == "project/beta")
        self.assertEqual(beta["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", beta["error"])

    def test_a_project_with_the_gke_api_off_holds_no_cluster(self):
        def cluster_list(project):
            if project == "beta":
                return run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project beta")
            return run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}]))

        manifest = self.collect(fleet_run({}, cluster_list=cluster_list))
        self.assertNotIn("error", manifest)
        self.assertEqual({c["name"] for c in manifest["clusters"]}, {"acme/us-central1/c1", "project/acme", "project/beta"})
        self.assertEqual(next(c for c in manifest["clusters"] if c["name"] == "project/beta")["outcome"], "collected")

    def test_a_project_numbered_refusal_matching_the_project_holds_no_cluster(self):
        # gcloud names the consumer project by number; the collector resolves the listed project's.
        base = fleet_run({}, cluster_list=lambda project: run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project 123456789"))

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return run_of(0, "123456789\n" if argv[3] == "acme" else "222222222\n")
            return base(argv, **kwargs)

        by_name = {c["name"]: c for c in self.collect(run)["clusters"]}
        self.assertEqual(by_name["project/acme"]["outcome"], "collected")
        self.assertEqual(by_name["project/beta"]["outcome"], "gate-failed")
        self.assertIn("quota project", by_name["project/beta"]["error"])

    def test_a_refusal_whose_project_number_cannot_be_read_names_the_describe(self):
        """A describe the credential may not make is not a quota project; the
        error said "quota project" for both and sent the operator after the
        wrong setting."""
        def cluster_list(project):
            if project == "acme":
                return run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project 123456789")
            return run_of(0, "[]")

        base = fleet_run({}, cluster_list=cluster_list)

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return run_of(1, "", "PERMISSION_DENIED: resourcemanager.projects.get")
            return base(argv, **kwargs)

        by_name = {c["name"]: c for c in self.collect(run)["clusters"]}
        error = by_name["project/acme"]["error"]
        self.assertIn("`gcloud projects describe acme` failed (rc=1)", error)
        self.assertIn("resourcemanager.projects.get", error)
        self.assertNotIn("quota project", error)

    def test_a_project_with_both_apis_off_is_described_once(self):
        # Kubernetes Engine and Compute Engine both refuse, each naming the
        # project by number; the second refusal is answered from the first describe.
        refusal = "ERROR: SERVICE_DISABLED: {api} API has not been used in project 123456789"
        base = fleet_run({}, cluster_list=lambda project: run_of(1, "", refusal.format(api="Kubernetes Engine")))
        describes = []

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "describe"]:
                describes.append(argv[3])
                return run_of(0, "123456789\n" if argv[3] == "acme" else "222222222\n")
            if argv[:4] == ["gcloud", "compute", "reservations", "list"]:
                return run_of(1, "", refusal.format(api="Compute Engine"))
            return base(argv, **kwargs)

        manifest = self.collect(run)
        self.assertEqual(describes.count("acme"), 1)
        # With both APIs off `acme` holds nothing and yields no target, not a
        # failed one: the run's error cites only `beta`'s failure.
        self.assertIn("First: project/beta: cluster enumeration refused", manifest["error"])
        self.assertNotIn("project/acme", manifest["error"])

    def test_the_listing_runs_under_its_own_timeout_not_the_default(self):
        timeouts = []
        base = fleet_run({})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "list"]:
                timeouts.append(kwargs.get("timeout"))
            return base(argv, **kwargs)

        self.collect(run)
        self.assertEqual(timeouts, [fs.PROJECTS_LIST_TIMEOUT_S])
        self.assertGreater(fs.PROJECTS_LIST_TIMEOUT_S, fs.DEFAULT_TIMEOUT_S)
        self.assertLess(fs.PROJECTS_LIST_TIMEOUT_S, fs.PROJECT_READ_DEADLINE_S)

    def test_projects_are_enumerated_in_the_pool(self):
        # Both listings have to be in flight at once to pass the barrier; one
        # project at a time breaks it instead of hanging.
        barrier = threading.Barrier(2, timeout=5)

        def cluster_list(project):
            barrier.wait()
            return run_of(0, "[]")

        manifest = fs.collect_fleet(None, run=fleet_run({}, cluster_list=cluster_list), max_workers=2)
        self.assertNotIn("error", manifest)
        self.assertEqual({c["name"] for c in manifest["clusters"]}, {"project/acme", "project/beta"})

    def test_project_reads_share_the_cluster_pool(self):
        # The reservation read and the cluster's node-pool read have to be in
        # flight together to pass the barrier.
        barrier = threading.Barrier(2, timeout=5)
        inner = fleet_run({"acme": ["c1"]}, projects="acme\n")

        def run(argv, **kwargs):
            if argv[:3] in (["gcloud", "compute", "reservations"], ["gcloud", "container", "node-pools"]):
                barrier.wait()
            return inner(argv, **kwargs)

        manifest = self.collect_with(run, max_workers=2)
        self.assertEqual({c["outcome"] for c in manifest["clusters"]}, {"collected"})

    def test_one_capacity_history_read_answers_every_cluster_in_the_region(self):
        # The advice is per region and shape; three clusters asking about one
        # shape paid three identical calls.
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        inner = fleet_run({"acme": ["c1", "c2", "c3"]}, projects="acme\n")
        advice = []

        def run(argv, **kwargs):
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(cc)))
            if argv[: len(fs.CAPACITY_HISTORY_ARGV)] == fs.CAPACITY_HISTORY_ARGV:
                advice.append(argv)
                return run_of(0, json.dumps(capacity_history([0.01] * 7)))
            return inner(argv, **kwargs)

        manifest = self.collect_with(run, max_workers=3)
        self.assertEqual(len(advice), 1)
        clusters = [c for c in manifest["clusters"] if not c["name"].startswith("project/")]
        self.assertEqual(len(clusters), 3)
        for c in clusters:
            self.assertIn("spot-scarcity-risk", {x["check"] for x in c["commands"]})

    def test_a_failed_capacity_history_read_is_asked_again(self):
        cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
        inner = fleet_run({"acme": ["c1", "c2"]}, projects="acme\n")
        advice = []

        def run(argv, **kwargs):
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(cc)))
            if argv[: len(fs.CAPACITY_HISTORY_ARGV)] == fs.CAPACITY_HISTORY_ARGV:
                advice.append(argv)
                if len(advice) == 1:
                    return run_of(1, "", "UNAVAILABLE")
                return run_of(0, json.dumps(capacity_history([0.01] * 7)))
            return inner(argv, **kwargs)

        manifest = self.collect_with(run, max_workers=1)
        self.assertEqual(len(advice), 2)
        clusters = [c for c in manifest["clusters"] if not c["name"].startswith("project/")]
        ran = ["spot-scarcity-risk" in {x["check"] for x in c["commands"]} for c in clusters]
        self.assertEqual(sorted(ran), [False, True])

    def test_capacity_history_rc0_answers_are_kept_only_when_they_parse(self):
        """Garbled output is a failed read and is asked again; an empty answer
        is a read that measured nothing and is kept."""
        for first, asked in (("{not json", 2), ("", 1), ("[]", 1), ("{}", 1)):
            with self.subTest(first=first):
                cc = compute_class("cc1", [{"machineType": "n2-standard-8", "spot": True}])
                inner = fleet_run({"acme": ["c1", "c2"]}, projects="acme\n")
                advice = []

                def run(argv, **kwargs):
                    if argv[:2] == ["kubectl", "get"]:
                        return run_of(0, json.dumps(dump_of(cc)))
                    if argv[: len(fs.CAPACITY_HISTORY_ARGV)] == fs.CAPACITY_HISTORY_ARGV:
                        advice.append(argv)
                        if len(advice) == 1:
                            return run_of(0, first)
                        return run_of(0, json.dumps(capacity_history([0.01] * 7)))
                    return inner(argv, **kwargs)

                self.collect_with(run, max_workers=1)
                self.assertEqual(len(advice), asked)

    def collect_with(self, run, **kwargs):
        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                return fs.collect_fleet(None, run=run, **kwargs)

    def test_a_run_past_the_deadline_names_every_project_it_did_not_start(self):
        manifest = self.collect_with(fleet_run({"acme": ["c1"]}), project_budget_s=0)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("2 project(s) could not be listed", manifest["error"])
        self.assertIn("not read:", manifest["error"])

    def test_a_deadline_between_listing_and_project_reads_fails_only_those_reads(self):
        # Two listings admitted, then the clock has run out.
        admitted = iter([True, True])
        with patch.object(fs, "_before", lambda deadline: next(admitted, False)):
            manifest = self.collect_with(fleet_run({"acme": ["c1"]}))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/c1"]["outcome"], "collected")
        for name in ("project/acme", "project/beta"):
            self.assertEqual(by_name[name]["outcome"], "gate-failed")
            self.assertTrue(by_name[name]["error"].startswith("not read:"))

    def test_a_slow_project_listing_spends_the_read_budget(self):
        """The clock starts before discovery: a `projects list` that takes the
        whole budget leaves none for the reads, or listing plus reads overrun
        the terminal timeout and the run is killed with no manifest. Taking
        the deadline after discovery would collect this fleet."""
        clock = [0.0]
        base = fleet_run({"acme": ["c1"]})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "list"]:
                clock[0] += fs.PROJECT_READ_DEADLINE_S + 1
            return base(argv, **kwargs)

        with patch.object(fs.time, "monotonic", lambda: clock[0]):
            manifest = self.collect_with(run)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("2 project(s) could not be listed", manifest["error"])
        self.assertIn("not read:", manifest["error"])

    def test_an_error_across_many_projects_quotes_one_and_counts_the_rest(self):
        projects = "".join(f"p{i}\n" for i in range(200))

        def cluster_list(project):
            return run_of(1, "", "ERROR: Reauthentication failed. " + "x" * 250)

        manifest = self.collect_with(fleet_run({}, projects=projects, active="p0", cluster_list=cluster_list))
        self.assertIn("200 project(s)", manifest["error"])
        self.assertLess(len(manifest["error"]), 2 * fs.ERROR_EXCERPT_CHARS)


class CollectProjectTest(unittest.TestCase):
    def test_reservation_and_quota_findings(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, json.dumps([{"name": "r1", "specificReservation": {"count": 10, "inUseCount": 1}}]))
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": [{"metric": "N4_CPUS", "limit": 100, "usage": 95}]}))
            return run_of(0, "")

        entry = fs.collect_project("acme", {"us-central1"}, run=run)
        slugs = {c["check"] for c in entry["candidates"]}
        self.assertIn("reservation-mismatch-risk", slugs)
        self.assertIn("quota-exhaustion-risk", slugs)

    def test_no_data_is_unevaluated_not_absent(self):
        """Both reads failing used to drop the project entry, which reads as
        a project with nothing to report."""
        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        entry = fs.collect_project("acme", {"us-central1"}, run=run)
        self.assertEqual(entry["commands"], [])
        self.assertEqual(
            {e["check"] for e in entry["checks_unevaluated"]},
            {"quota-exhaustion-risk", "reservation-mismatch-risk"},
        )
        self.assertIn("us-central1", entry["limitations"])

    def test_one_failed_region_is_named_rather_than_passed(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:4] == ["gcloud", "compute", "regions", "describe"] and argv[4] == "europe-west1":
                return run_of(1, "", "PERMISSION_DENIED")
            return run_of(0, json.dumps({"quotas": []}))

        entry = fs.collect_project("acme", {"us-central1", "europe-west1"}, run=run)
        # Unevaluated, not run: the document carries `limitations` only for a
        # check the collector lists unevaluated, so a recorded command here
        # published the silent region as clean.
        self.assertNotIn("quota-exhaustion-risk", {c["check"] for c in entry["commands"]})
        [unevaluated] = entry["checks_unevaluated"]
        self.assertEqual(unevaluated["check"], "quota-exhaustion-risk")
        self.assertIn("europe-west1", unevaluated["reason"])
        self.assertIn("answered: us-central1", unevaluated["reason"])
        self.assertIn("europe-west1", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])

    def test_a_region_answer_without_a_quotas_list_is_unevaluated(self):
        """rc 0 and a JSON object is not a region: a real one always carries
        `quotas`, so `{}` or a relay's error object did not answer."""
        for label, stdout in (("empty object", "{}"), ("error object", json.dumps({"error": "shim"}))):
            with self.subTest(label):
                def run(argv, **kwargs):
                    if argv[:3] == ["gcloud", "compute", "reservations"]:
                        return run_of(0, "[]")
                    if argv[:4] == ["gcloud", "compute", "regions", "describe"] and argv[4] == "europe-west1":
                        return run_of(0, stdout)
                    return run_of(0, json.dumps({"quotas": []}))

                entry = fs.collect_project("acme", {"us-central1", "europe-west1"}, run=run)
                self.assertNotIn("quota-exhaustion-risk", {c["check"] for c in entry["commands"]})
                [unevaluated] = entry["checks_unevaluated"]
                self.assertEqual(unevaluated["check"], "quota-exhaustion-risk")
                self.assertIn("europe-west1 (rc=0: returned no quotas list)", unevaluated["reason"])
                self.assertIn("answered: us-central1", unevaluated["reason"])

    def test_the_answered_region_s_findings_survive_another_region_failing(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:4] == ["gcloud", "compute", "regions", "describe"] and argv[4] == "europe-west1":
                return run_of(1, "", "PERMISSION_DENIED")
            return run_of(0, json.dumps({"quotas": [{"metric": "CPUS", "limit": 10, "usage": 10}]}))

        entry = fs.collect_project("acme", {"us-central1", "europe-west1"}, run=run)
        self.assertEqual([c["object"] for c in entry["candidates"]], ["Quota/us-central1:CPUS"])

    def test_a_reservations_answer_that_is_not_a_list_is_unevaluated(self):
        for label, stdout in (
            ("object", json.dumps({"error": "shim"})),
            ("unparseable", '[{"name": "trunc'),
            ("empty object", "{}"),
            ("string element", json.dumps([{"name": "r1"}, "r2"])),
        ):
            with self.subTest(label):
                def run(argv, **kwargs):
                    if argv[:3] == ["gcloud", "compute", "reservations"]:
                        return run_of(0, stdout)
                    return run_of(0, json.dumps({"quotas": []}))

                entry = fs.collect_project("acme", {"us-central1"}, run=run)
                self.assertNotIn("reservation-mismatch-risk", {c["check"] for c in entry["commands"]})
                [unevaluated] = entry["checks_unevaluated"]
                self.assertEqual(unevaluated["check"], "reservation-mismatch-risk")
                self.assertIn("returned output that is not a JSON list of reservations (rc=0)", unevaluated["reason"])
                self.assertIn("not a JSON list", entry["limitations"])

    def test_one_metric_over_the_line_in_two_regions_is_two_findings(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            return run_of(0, json.dumps({"quotas": [{"metric": "CPUS", "limit": 10, "usage": 10}]}))

        entry = fs.collect_project("acme", {"us-central1", "us-east4"}, run=run)
        objects = [c["object"] for c in entry["candidates"]]
        self.assertEqual(sorted(objects), ["Quota/us-central1:CPUS", "Quota/us-east4:CPUS"])

    def test_a_non_production_reservation_is_not_flagged(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, json.dumps([{"name": "gpu-staging", "specificReservation": {"count": 10, "inUseCount": 0}}]))
            return run_of(0, json.dumps({"quotas": []}))

        entry = fs.collect_project("acme", {"us-central1"}, run=run)
        self.assertEqual(entry["candidates"], [])


class CrashIsolationTest(unittest.TestCase):
    def test_one_cluster_crashing_costs_that_cluster_and_no_other(self):
        """`future.result()` re-raises, and the SOP redirects this collector's
        stdout into the manifest — so an unmodelled exception on one cluster
        used to leave a zero-byte file and lose the whole fleet."""
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "boom", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                if "boom" in str((kwargs.get("env") or {}).get("KUBECONFIG", "")):
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": []}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet("acme", run=run)

        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"] if not c["name"].startswith("project/")}
        self.assertEqual(outcomes, {"acme/us-central1/c1": "collected", "acme/us-central1/boom": "gate-failed"})
        boom = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/boom")
        self.assertIn("TypeError", boom["error"])


    def test_a_crashing_project_read_costs_that_project_and_no_other(self):
        inner = fleet_run({"acme": ["c1"]})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "compute", "reservations"] and "beta" in argv:
                raise TypeError("unsupported operand type(s) for -: 'str' and 'int'")
            return inner(argv, **kwargs)

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet(None, run=run)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/c1"]["outcome"], "collected")
        self.assertEqual(by_name["project/acme"]["outcome"], "collected")
        self.assertEqual(by_name["project/beta"]["outcome"], "gate-failed")
        self.assertIn("TypeError", by_name["project/beta"]["error"])

    def test_a_crashing_cluster_list_costs_that_project_and_no_other(self):
        def cluster_list(project):
            # A cluster with no name: `enumerate_clusters` indexes it.
            return run_of(0, '[{"status": "RUNNING"}]' if project == "beta" else json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}]))

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet(None, run=fleet_run({}, cluster_list=cluster_list))
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/c1"]["outcome"], "collected")
        self.assertEqual(by_name["project/beta"]["outcome"], "gate-failed")
        self.assertIn("KeyError", by_name["project/beta"]["error"])


class DefaultRunTest(unittest.TestCase):
    def test_a_timed_out_childs_output_arrives_as_str(self):
        exc = subprocess.TimeoutExpired(["gcloud"], 60, output=b"partial", stderr=b"SERVICE_DISABLED")
        with patch.object(fs.subprocess, "run", side_effect=exc):
            result = fs.default_run(["gcloud"])
        self.assertEqual((result.rc, result.stdout, result.stderr), (124, "partial", "SERVICE_DISABLED"))


class ZoneTimeoutTest(unittest.TestCase):
    SILENT = "WARNING: The following zones did not respond: us-east1-b. List results may be incomplete."

    def test_a_silent_zone_audits_the_listed_clusters_and_fails_the_project_target(self):
        """A reservation a silent zone's cluster consumes would read as unused,
        so the project's reads wait for a complete list."""
        def cluster_list(project):
            return run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}]), self.SILENT)

        inner = fleet_run({}, cluster_list=cluster_list, projects="acme\n")

        def run(argv, **kwargs):
            if argv[:3] in (["gcloud", "compute", "reservations"], ["gcloud", "compute", "regions"]):
                raise AssertionError(f"a project read ran over an incomplete cluster list: {argv}")
            return inner(argv, **kwargs)

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet(None, run=run)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/c1"]["outcome"], "collected")
        self.assertEqual(by_name["project/acme"]["outcome"], "gate-failed")
        self.assertIn("did not respond", by_name["project/acme"]["error"])


class ClustersListedMarkerTest(unittest.TestCase):
    """`clusters_listed: 0` is what lets `finish` tell a fleet with no clusters
    from a run that lost them, so only a completed, empty list may set it."""

    def manifest(self, answer):
        # beta completes empty in every run, so each one shows the marker set beside the entry under test.
        run = fleet_run({}, cluster_list=lambda project: answer if project == "acme" else run_of(0, "[]"))
        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                return fs.collect_fleet(None, run=run)

    def project_entry(self, answer):
        return next(c for c in self.manifest(answer)["clusters"] if c["name"] == "project/acme")

    def test_a_completed_empty_list_marks_the_project(self):
        entry = self.project_entry(run_of(0, "[]"))
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry[fs.CLUSTERS_LISTED_KEY], 0)

    def test_the_gke_api_off_is_an_empty_list_and_marks_the_project(self):
        entry = self.project_entry(run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project acme"))
        self.assertEqual(entry[fs.CLUSTERS_LISTED_KEY], 0)

    def test_a_refusal_naming_a_longer_project_id_does_not_mark_the_project(self):
        # A hyphen ends a word, so `\b` after acme matched acme-prod's refusal and marked acme cluster-free.
        manifest = self.manifest(run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project acme-prod before"))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fs.CLUSTERS_LISTED_KEY in c], ["project/beta"])

    def test_a_refusal_naming_another_project_id_says_so(self):
        """The reason must match the stderr quoted beside it: this refusal
        names `acme-prod`, not no project."""
        manifest = self.manifest(run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project acme-prod before"))
        acme = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertIn("names another project ('acme-prod')", acme["error"])
        self.assertNotIn("names no project", acme["error"])

    def test_a_quota_project_s_refusal_does_not_mark_the_project(self):
        # The refusal names a project other than acme (fleet_run's describe answers no number),
        # so acme's clusters are unknown, not absent.
        manifest = self.manifest(run_of(1, "", "ERROR: SERVICE_DISABLED: Kubernetes Engine API has not been used in project 987654321"))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fs.CLUSTERS_LISTED_KEY in c], ["project/beta"])
        acme = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertEqual(acme["outcome"], "gate-failed")

    def test_a_failed_list_does_not_mark_the_project(self):
        manifest = self.manifest(run_of(1, "", "PERMISSION_DENIED: container.clusters.list"))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fs.CLUSTERS_LISTED_KEY in c], ["project/beta"])

    def test_an_empty_list_with_a_silent_zone_does_not_mark_the_project(self):
        manifest = self.manifest(run_of(0, "[]", ZoneTimeoutTest.SILENT))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fs.CLUSTERS_LISTED_KEY in c], ["project/beta"])

    def test_a_project_holding_a_cluster_is_not_marked(self):
        entry = self.project_entry(run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])))
        self.assertNotIn(fs.CLUSTERS_LISTED_KEY, entry)

    def test_a_project_whose_only_cluster_is_not_running_is_not_marked(self):
        entry = self.project_entry(run_of(0, json.dumps([{"name": "c1", "location": "us-east1-b", "status": "DEGRADED"}])))
        self.assertNotIn(fs.CLUSTERS_LISTED_KEY, entry)


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_the_renames_this_collector_makes_needed_an_id_scheme_bump(self):
        """Qualified cluster names and the region- and message-id-bearing
        objects re-spell every stockout finding on the collector's first run.
        Without a new `ID_SCHEME` a ledger stamped by the previous scheme joins
        against the new ids, and the delta reads every rename as a fix."""
        import audit_report

        self.assertGreaterEqual(audit_report.ID_SCHEME, 6)

    def test_checks_run_copied_from_a_collected_entry_survives_cross_check(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])
        cc = compute_class("cc1", [{"machineFamily": "c3", "spot": True}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(cc)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": []}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet("acme", run=run)

        data = {
            "audit": "stockout-prevention",
            "scope": {
                "clusters": [
                    {
                        "name": e["name"],
                        "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]],
                        # The family-only Spot class leaves spot-scarcity-risk unevaluated, which the report owes a sentence.
                        **({"limitations": e["limitations"]} if e.get("checks_unevaluated") else {}),
                    }
                    for e in manifest["clusters"]
                    if e["outcome"] == "collected"
                ],
                # `--project` skipped discovery, and the manifest says so.
                "skipped": [{"cluster": fs.UNENUMERATED_PROJECTS_TARGET, "reason": "scope narrowed by --project"}],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise
        # `finish` also checks each command names an inspection binary, before
        # it opens the manifest; a copied command has to pass that too.
        for target in data["scope"]["clusters"]:
            for entry in target["checks_run"]:
                audit_report.validate_check_command(entry["command"], target["name"], entry["check"])

    def test_a_region_whose_quota_read_failed_reaches_finish_as_a_gap(self):
        """One silent region used to leave the check in `commands` with only a
        `limitations` string, which the SOP carries only for an unevaluated
        check -- so the copied document named the check run and `finish`
        published the region as clean."""
        import audit_report

        clusters_json = json.dumps([
            {"name": "c1", "location": "us-central1", "status": "RUNNING"},
            {"name": "c2", "location": "europe-west1", "status": "RUNNING"},
        ])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:4] == ["gcloud", "compute", "regions", "describe"] and argv[4] == "europe-west1":
                return run_of(1, "", "PERMISSION_DENIED")
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": []}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet("acme", run=run)

        # Copied as the SOP says and as the survives-cross-check test copies.
        data = {
            "audit": "stockout-prevention",
            "scope": {
                "clusters": [
                    {
                        "name": e["name"],
                        "location": e.get("location", "global"),
                        "project": "acme",
                        "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]],
                        **({"limitations": e["limitations"]} if e.get("checks_unevaluated") else {}),
                    }
                    for e in manifest["clusters"]
                    if e["outcome"] == "collected"
                ],
                "skipped": [{"cluster": fs.UNENUMERATED_PROJECTS_TARGET, "reason": "scope narrowed by --project"}],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise
        [gap] = [g for g in audit_report.coverage_gaps(data) if g.startswith("project/acme")]
        self.assertIn("europe-west1", gap)

        # And the check cannot be claimed as run over the silent region.
        project = next(c for c in data["scope"]["clusters"] if c["name"] == "project/acme")
        project["checks_run"].append({"check": "quota-exhaustion-risk", "command": "gcloud compute regions describe us-central1"})
        with self.assertRaisesRegex(audit_report.ValidationError, "as run or not applicable"):
            audit_report.cross_check_manifest(data, manifest)

    def test_a_check_whose_read_failed_is_rejected_as_run(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "logging", "read"]:
                return run_of(1, "", "PERMISSION_DENIED")
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "reservations"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "regions"]:
                return run_of(0, json.dumps({"quotas": []}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fs, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fs.collect_fleet("acme", run=run)

        cluster_entry = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/c1")
        self.assertEqual(cluster_entry["outcome"], "collected")
        self.assertNotIn("autoscaler-out-of-resources", {c["check"] for c in cluster_entry["commands"]})
        self.assertEqual(
            [e["check"] for e in cluster_entry["checks_unevaluated"]], ["autoscaler-out-of-resources"]
        )

        # Every manifest entry, copied as the sibling test above copies them,
        # so no other rejection fires first -- a document naming only the
        # cluster trips the "scope.clusters omits project/acme" check -- plus
        # the unevaluated check claimed as run on the cluster.
        clusters = [
            {"name": e["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]]}
            for e in manifest["clusters"]
            if e["outcome"] == "collected"
        ]
        for entry in clusters:
            if entry["name"] == "acme/us-central1/c1":
                entry["checks_run"].append({"check": "autoscaler-out-of-resources", "command": "x"})
                entry["limitations"] = "the Cloud Logging read failed"
        skipped = [{"cluster": fs.UNENUMERATED_PROJECTS_TARGET, "reason": "scope narrowed by --project"}]
        data = {"audit": "stockout-prevention", "scope": {"clusters": clusters, "skipped": skipped}}
        with self.assertRaisesRegex(audit_report.ValidationError, "as run or not applicable"):
            audit_report.cross_check_manifest(data, manifest)


class RefusedProjectIdTest(unittest.TestCase):
    """Which project id a refusal names, read only from gcloud's phrasings."""

    def owner(self, stderr):
        def run(argv, **kwargs):
            raise AssertionError(argv)

        return fs.refusal_owner("acme", stderr, run=run)

    def test_english_words_after_project_are_not_project_ids(self):
        for stderr in (
            "ERROR: Kubernetes Engine API is not enabled on this project either.",
            "ERROR: the quota project should be set",
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(self.owner(stderr), (False, "the refusal names no project, so it cannot be tied to 'acme'"))

    def test_a_capitalised_project_keyword_names_its_id(self):
        owned, reason = self.owner("ERROR: Project acme-prod is not found")
        self.assertFalse(owned)
        self.assertIn("names another project ('acme-prod')", reason)

    def test_a_capitalised_project_keyword_naming_this_project_owns_it(self):
        """The ownership test read only a lowercase keyword, so this refusal
        was "names no project" although it names acme."""
        self.assertEqual(self.owner("ERROR: SERVICE_DISABLED: Project acme is not found"), (True, ""))

    def test_a_quoted_project_id_naming_this_project_owns_it(self):
        """`REFUSED_PROJECT_ID_RE` admits a quote or bracket before the id and
        the ownership test did not, so the id was subtracted as this
        project's and the refusal read as naming none."""
        def run(argv, **kwargs):
            raise AssertionError(argv)

        for stderr in (
            "ERROR: SERVICE_DISABLED: project 'acme-prod' is not found",
            'ERROR: SERVICE_DISABLED: project "acme-prod" is not found',
            "ERROR: SERVICE_DISABLED: project [acme-prod] is not found",
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(fs.refusal_owner("acme-prod", stderr, run=run), (True, ""))


if __name__ == "__main__":
    unittest.main()
