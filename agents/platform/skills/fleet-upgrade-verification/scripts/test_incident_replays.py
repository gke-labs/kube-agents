#!/usr/bin/env python3
"""Incident replays: one test per historical upgrade failure of one production fleet, each
rebuilding the incident's precondition from the recorded inputs under testdata/ and asserting
the exact finding and action a readiness rule files for it through the registry's fold
(`upgrade_readiness.evaluate_extra_rules`), the path the report runs. Named by date and
scenario; the fleet, its projects and its clusters are not named, and the recorded dev
fleet's `seeded-b` stands in for the cluster each incident hit."""

import copy
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
from readiness_rules import cluster_status_age, control_plane_upgrade_history, known_issues_matrix, lb_connection_draining, orphan_node_objects, target_patch_valid  # noqa: E402
from test_readiness_rules import DEV_PROJECT, RECORDED_AT, context_for, project_reads, recorded, seeded_b, seeded_b_read  # noqa: E402

CLUSTER_TARGET = "https://container.googleapis.com/v1/projects/196584622296/zones/us-central1-a/clusters/seeded-b"
PAGE = "https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/known-issues"
DRAIN_SECONDS = 36000  # the fleet's TCP backend services drained for ten hours; GKE waits 600 s per node
CAPACITY_MESSAGE_FORMAT = "Try a different location, or try again later: Google Compute Engine does not have enough resources available to fulfill request: {location}."
OFF_PEAK_ACTION = "set a maintenance window in an off-peak hour for the region and retry the upgrade in it; keep upgrade notifications consumed so a failed attempt is a message the same day"
DESCRIBE_COMMAND = ["gcloud", "container", "clusters", "describe", "seeded-b", "--location", "us-central1-a", "--project", DEV_PROJECT, "--format=json(status,statusMessage)"]
RELIST_COMMAND = ["gcloud", "container", "operations", "list", f"--project={DEV_PROJECT}", "--location=us-central1-a", "--filter=targetLink~/clusters/seeded-b($|/nodePools/)", "--format=json(name,operationType,status,startTime,endTime,targetLink)"]


def fold(module, record, read_value=None, target_text=None, at=RECORDED_AT, reads=None, context=None):
    """One rule through the registry's fold, as the report runs it: the folded lists, each
    blocker and risk stamped with the rule's id, entry and `text` (the finding's description)."""
    context = context if context is not None else context_for(record, target_text=target_text, at=at, reads=reads)
    return r.evaluate_extra_rules(record, {}, read_value, None, context, rules=[module])


def instance_group(record, pool_name):
    pool = next(p for p in record["nodePools"] if p["name"] == pool_name)
    return pool["instanceGroupUrls"][0].replace("instanceGroupManagers", "instanceGroups")


def backend_service(name, groups, timeout=DRAIN_SECONDS, region=None):
    """A backend service as the fleet ran them: a TCP service draining `timeout` seconds on the pools' instance groups."""
    service = {"name": name, "selfLink": f"https://www.googleapis.com/compute/v1/projects/{DEV_PROJECT}/global/backendServices/{name}", "connectionDraining": {"drainingTimeoutSec": timeout}, "backends": [{"balancingMode": "UTILIZATION", "group": group} for group in groups]}
    if region:
        service["region"] = f"https://www.googleapis.com/compute/v1/projects/{DEV_PROJECT}/regions/{region}"
    return service


def services_read(*services):
    return project_reads(**{shared.PROJECT_READ_BACKEND_SERVICES: recorded("project_backend_services.json") + list(services)})


def drain_text(name, pools, scope):
    return f"BackendService {name} drains for {DRAIN_SECONDS} s on pool(s) {pools}; GKE waits 600 s per node during an upgrade, so each surge node's drain times out; set the timeout at or under the wait: `gcloud compute backend-services update {name} {scope} --connection-draining-timeout=600`"


def capacity_failure(kind, started, location="us-central1-a", name="operation-capacity-failure"):
    message = CAPACITY_MESSAGE_FORMAT.format(location=location)
    return {"name": name, "operationType": kind, "status": "DONE", "startTime": started, "endTime": started, "targetLink": CLUSTER_TARGET, "zone": location, "statusMessage": message, "error": {"code": 14, "message": message}}


def capacity_text(kind, date, window, location="us-central1-a"):
    return f"{kind} on {date} failed on compute capacity in {location} ({CAPACITY_MESSAGE_FORMAT.format(location=location)}); maintenance window: {window}; {OFF_PEAK_ACTION}"


def with_window(record, window):
    cluster = copy.deepcopy(record)
    if window is None:
        cluster.pop("maintenancePolicy", None)
    else:
        cluster["maintenancePolicy"] = {"window": window}
    return cluster


def answering(context, status, message, operations):
    """A runner answering the status-age rule's `clusters describe` and its `operations list`, recording the calls."""
    calls = []

    def run(cmd, timeout=None, env=None):
        calls.append(cmd)
        if cmd[:4] == RELIST_COMMAND[:4]:
            return 0, json.dumps(operations), ""
        return 0, json.dumps({"status": status, "statusMessage": message}), ""

    context[shared.CONTEXT_RUN_CMD] = run
    return calls


class DrainTimeoutReplays(unittest.TestCase):
    def test_2025_06_26_drain_timeout_on_the_default_pool_holds_every_node_removal(self):
        """A node pool upgrade spent the whole 600 s deregistration wait on every node behind a TCP backend service draining for 36,000 s; read before the window, the service is a blocker with the one command that sets its timeout under the wait."""
        record = seeded_b()
        service = backend_service("tcp-edge-ingest", [instance_group(record, "default-pool")])
        folded = fold(lb_connection_draining, record, seeded_b_read(), reads=services_read(service))
        self.assertEqual([(row["rule"], list(row["holds"])) for row in folded["blocking"]], [("lb-connection-draining", [shared.HOLDS_NODE_POOL])])
        row = folded["blocking"][0]
        self.assertEqual(row["action"], "gcloud compute backend-services update tcp-edge-ingest --global --connection-draining-timeout=600")
        self.assertEqual(row["text"], drain_text("tcp-edge-ingest", "default-pool", "--global"))
        self.assertEqual((folded["risks"], folded["unknown"]), ([], []))

    def test_2025_07_03_the_same_service_shape_on_a_second_pool_is_found_by_the_same_read(self):
        """A week later another pool's upgrade hit the same shape, a service draining for 36,000 s on that pool's instance group; the rule reads the project's backend services once and finds the service whichever pool it fronts."""
        record = seeded_b()
        service = backend_service("tcp-edge-batch", [instance_group(record, "no-surge-pool")])
        folded = fold(lb_connection_draining, record, seeded_b_read(), reads=services_read(service))
        self.assertEqual([row["text"] for row in folded["blocking"]], [drain_text("tcp-edge-batch", "no-surge-pool", "--global")])
        self.assertEqual(folded["blocking"][0]["pools"], ["no-surge-pool"])

    def test_2026_03_31_a_regional_service_across_both_pools_and_the_earlier_two_in_one_read(self):
        """The third occurrence was a regional service fronting both pools, with the earlier two still in place; one read of the project's backend services files one blocker per service, each naming its pools and its own command, so the fleet's whole set is found before any window."""
        record = seeded_b()
        default_group, batch_group = instance_group(record, "default-pool"), instance_group(record, "no-surge-pool")
        services = (backend_service("tcp-edge-ingest", [default_group]), backend_service("tcp-edge-batch", [batch_group]), backend_service("tcp-edge-regional", [default_group, batch_group], region="us-central1"))
        folded = fold(lb_connection_draining, record, seeded_b_read(), reads=services_read(*services))
        self.assertEqual(sorted(row["text"] for row in folded["blocking"]), sorted([
            drain_text("tcp-edge-ingest", "default-pool", "--global"),
            drain_text("tcp-edge-batch", "no-surge-pool", "--global"),
            drain_text("tcp-edge-regional", "default-pool, no-surge-pool", "--region us-central1"),
        ]))
        self.assertEqual({row["action"] for row in folded["blocking"] if row["backend_service"] == "tcp-edge-regional"}, {"gcloud compute backend-services update tcp-edge-regional --region us-central1 --connection-draining-timeout=600"})
        self.assertEqual(folded["rules"]["lb-connection-draining"]["blocking"], 3)


class OrphanNodeReplay(unittest.TestCase):
    def test_2025_12_09_a_cordoned_node_whose_instance_is_gone_holds_the_pool_upgrade(self):
        """A pool upgrade hung on Nodes left `Ready,SchedulingDisabled` after an interrupted operation had deleted their instances; joined to the project's instances before the window, such a Node is a blocker with the delete that unblocks the upgrade."""
        record = seeded_b()
        ghost = copy.deepcopy(recorded("nodes_seeded_b.json")[0])
        name = "gke-seeded-b-default-pool-4176890d-7x2k"
        ghost["metadata"]["name"] = name
        ghost["metadata"]["creationTimestamp"] = "2025-11-30T08:12:41Z"  # created long before the instance list was read
        ghost["spec"]["providerID"] = f"gce://{DEV_PROJECT}/us-central1-a/{name}"
        ghost["spec"]["unschedulable"] = True
        reads = project_reads()
        reads[shared.PROJECT_READ_INSTANCES][shared.PROJECT_READ_AT_KEY] = "2026-10-20T11:58:00Z"
        folded = fold(orphan_node_objects, record, seeded_b_read([ghost]), reads=reads)
        self.assertEqual([(row["rule"], list(row["holds"]), row["action"]) for row in folded["blocking"]], [("orphan-node-objects", [shared.HOLDS_NODE_POOL], f"kubectl delete node {name}")])
        self.assertEqual(folded["blocking"][0]["text"], f"Node {name} names no instance in the project (providerID gce://{DEV_PROJECT}/us-central1-a/{name}; Ready=True; unschedulable=true): a pool upgrade waits on it; delete the Node object: `kubectl delete node {name}`")
        self.assertEqual(folded["notes"], ["orphan-node-objects: 1 orphan Node(s) (3 nodes, 19 instances in the project)"])


class StuckClusterReplay(unittest.TestCase):
    def test_2025_06_17_a_cluster_reconciling_for_six_days_with_nothing_running_is_a_blocker(self):
        """A cluster sat RECONCILING for six days with no operation running and only a statusMessage to go on, and the upgrade waited behind it; confirmed by a describe and a re-list of its own operations, the status is a blocker that says to freeze changes and open a support case quoting the status, its message and the newest operation."""
        message = "Cluster is reconciling; node pool default-pool resize is pending"
        record = {**seeded_b(), "status": "RECONCILING", "statusMessage": message}
        settled = {"name": "operation-reconcile-start", "operationType": "UPDATE_CLUSTER", "status": "DONE", "startTime": "2026-10-14T12:00:00.000000Z", "endTime": "2026-10-14T12:01:00.000000Z", "targetLink": CLUSTER_TARGET}
        operations = recorded("project_operations.json") + [settled]  # the newest operation of the cluster, done six days before the run
        context = context_for(record, reads=project_reads(**{shared.PROJECT_READ_OPERATIONS: operations}))
        calls = answering(context, "RECONCILING", message, operations)
        folded = fold(cluster_status_age, record, context=context)
        self.assertEqual(calls, [DESCRIBE_COMMAND, RELIST_COMMAND])
        self.assertEqual([(row["rule"], list(row["holds"]), row["operation"]) for row in folded["blocking"]], [("cluster-status-age", list(shared.HOLDS_BOTH), None)])
        self.assertEqual(folded["blocking"][0]["text"], f"seeded-b is RECONCILING with no operation RUNNING on it or its node pools (statusMessage: {message}; newest operation: operation-reconcile-start (UPDATE_CLUSTER, DONE, started 2026-10-14T12:00:00.000000Z, 6d ago)); {cluster_status_age.NO_HOLDER_ACTION}")
        self.assertIn("open a support case quoting the status, its message and the newest operation id", folded["blocking"][0]["text"])


class ControlPlaneCapacityReplays(unittest.TestCase):
    def replay(self, kind, started, window, at):
        record = with_window(seeded_b(), window)
        folded = fold(control_plane_upgrade_history, record, at=at, reads=project_reads(**{shared.PROJECT_READ_OPERATIONS: [capacity_failure(kind, started)]}))
        self.assertEqual(folded["blocking"], [])  # a lagging signal: never a blocker
        self.assertEqual([row["rule"] for row in folded["risks"]], ["control-plane-upgrade-history"])
        return folded

    def test_2025_06_19_control_plane_upgrade_failed_on_capacity_with_no_window(self):
        """An automated control-plane upgrade fired in a busy hour and failed because the zone had no capacity, on a cluster with no maintenance window; the failed operation is a risk before the next attempt, with the window that moves the odds."""
        folded = self.replay("UPGRADE_MASTER", "2025-06-19T14:03:12.000000Z", None, datetime(2025, 6, 19, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(folded["risks"][0]["text"], capacity_text("UPGRADE_MASTER", "2025-06-19", "none"))
        self.assertEqual(folded["risks"][0]["action"], OFF_PEAK_ACTION)
        self.assertEqual(folded["notes"], ["control-plane-upgrade-history: 1 capacity failure(s) among 1 upgrade or repair operation(s) in the last 90 days; maintenance window none"])

    def test_2026_04_15_control_plane_upgrade_failed_on_capacity_in_an_afternoon_window(self):
        """The same failure under a daily maintenance window set in the region's busy afternoon; the risk names that window so the operator moves it off-peak before the retry."""
        window = {"dailyMaintenanceWindow": {"startTime": "14:00", "duration": "PT4H0M0S"}}
        folded = self.replay("UPGRADE_MASTER", "2026-04-15T14:41:07.000000Z", window, datetime(2026, 4, 15, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(folded["risks"][0]["text"], capacity_text("UPGRADE_MASTER", "2026-04-15", "daily at 14:00 UTC for PT4H0M0S"))

    def test_2026_10_05_control_plane_upgrade_failed_on_capacity_in_a_weekday_window(self):
        """The same failure under a weekday recurring window that opens mid-afternoon; the risk quotes the recurrence and its start so the window is the thing changed, not the version."""
        window = {"recurringWindow": {"window": {"startTime": "2026-01-05T15:00:00Z", "endTime": "2026-01-05T19:00:00Z"}, "recurrence": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"}}
        folded = self.replay("UPGRADE_MASTER", "2026-10-05T15:20:33.000000Z", window, datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(folded["risks"][0]["text"], capacity_text("UPGRADE_MASTER", "2026-10-05", "recurring FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR from 2026-01-05T15:00:00Z"))

    def test_2026_01_14_a_control_plane_resize_failed_on_the_same_capacity_message(self):
        """A control-plane repair that resized it failed on the same capacity message outside any window; the rule counts a repair as it counts an upgrade, so the next window is set off-peak before the retry."""
        folded = self.replay("REPAIR_CLUSTER", "2026-01-14T13:12:50.000000Z", None, datetime(2026, 1, 14, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(folded["risks"][0]["text"], capacity_text("REPAIR_CLUSTER", "2026-01-14", "none"))


class WithdrawnPatchReplay(unittest.TestCase):
    def test_2026_05_05_a_target_patch_the_channel_withdrew_before_the_window_is_a_blocker(self):
        """A pool upgrade was planned to a 1.34 patch the channel had withdrawn between the plan and the window, and it failed on an image the provider no longer served; read against the location's server config before the window, the target is a blocker naming the newest active patch of its minor."""
        config = recorded("server_config_us_central1_a.json")
        regular = next(channel for channel in config["channels"] if channel["channel"] == "REGULAR")
        self.assertNotIn("1.34.7-gke.1000000", regular["validVersions"] + config["validNodeVersions"])
        self.assertIn("1.34.11-gke.1209000", regular["validVersions"])  # a newer 1.34 patch the channel still serves
        record = {**seeded_b(), "currentMasterVersion": "1.34.11-gke.1209000"}
        folded = fold(target_patch_valid, record, target_text="1.34.7-gke.1000000")
        self.assertEqual([(row["rule"], list(row["holds"]), row["kind"], row["move"]) for row in folded["blocking"]], [("target-patch-valid", list(shared.HOLDS_BOTH), "target", "pool move")])
        self.assertEqual(folded["blocking"][0]["text"], "target 1.34.7-gke.1000000 is in neither the REGULAR channel's validVersions nor validNodeVersions at us-central1-a: a withdrawn patch stays selectable and a node pool upgrade to it fails on an image the provider no longer serves; target the newest active patch of the minor on the REGULAR channel: 1.34.11-gke.1209000")
        self.assertEqual(folded["notes"], ["target-patch-valid: channel REGULAR; target 1.34.7-gke.1000000 withdrawn; control plane 1.34.11-gke.1209000 listed in validVersions at us-central1-a; the target's move: pool move"])


class AddonKnownIssueReplay(unittest.TestCase):
    def test_2026_01_13_a_logging_addon_bug_on_a_1_34_patch_is_a_risk_naming_the_fixed_patch(self):
        """Every cluster on one 1.34 patch lost logs to a fluent-bit add-on bug the known-issues page already listed with a fix in a later patch; the add-on inventory against the table files the risk, naming the fix, before the fleet moves to that patch."""
        table = {"generated": "2026-10-20", "source": PAGE, "newest_gke_version": "1.37.1-gke.1187000", "issues": [
            {"id": "fluent-bit-log-loss-1-34", "category": "Logging", "summary": "fluent-bit drops logs under back-pressure", "identified": "1.34", "fixed_in": "1.34.6-gke.1000000 and later",
             "gke_versions": [{"minor": "1.34", "to": "1.34.6-gke.1000000", "to_exclusive": True}], "addon": "fluent-bit", "addon_versions": [], "url": f"{PAGE}#fluent-bit-log-loss-1-34"},
        ]}
        inventory = [(addon["image"], addon["tag"]) for addon in known_issues_matrix.inventory(seeded_b_read())]
        self.assertIn(("fluent-bit", "v0.0.422-gke.21"), inventory)
        record = {**seeded_b(), "currentMasterVersion": "1.34.5-gke.1200000"}
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            folded = fold(known_issues_matrix, record, seeded_b_read())
        self.assertEqual([(row["rule"], row["kind"], row["issue"]) for row in folded["risks"]], [("known-issues-matrix", "running", "fluent-bit-log-loss-1-34")])
        self.assertEqual(folded["risks"][0]["text"], f"known issue fluent-bit-log-loss-1-34 (fluent-bit drops logs under back-pressure) applies to GKE 1.34.5-gke.1200000 with fluent-bit v0.0.422-gke.21; fixed in 1.34.6-gke.1000000 and later: {PAGE}#fluent-bit-log-loss-1-34")
        self.assertEqual(folded["blocking"], [])
        # On the fixed patch the row no longer applies: the risk is the running patch's, and the fix is the move.
        with patch.object(known_issues_matrix, "load_table", return_value=(table, None)):
            fixed = fold(known_issues_matrix, {**record, "currentMasterVersion": "1.34.6-gke.1000000"}, seeded_b_read())
        self.assertEqual(fixed["risks"], [])


if __name__ == "__main__":
    unittest.main()
