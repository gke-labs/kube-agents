"""Tests for kcc_health.py, the Config Connector read the collectors share."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import kcc_health  # noqa: E402


def kcc_object(kind, name, *, namespace="infra", status=None, reason="", message="", spec=None):
    item = {"apiVersion": "x.cnrm.cloud.google.com/v1beta1", "kind": kind, "metadata": {"name": name, "namespace": namespace}}
    if spec is not None:
        item["spec"] = spec
    if status is not None:
        item["status"] = {"conditions": [{"type": "Ready", "status": status, "reason": reason, "message": message}]}
    return item


STALLED_TOPIC = kcc_object(
    "PubSubTopic", "fleet-events", status="False", reason="UpdateFailed", message="Error 403: roles/pubsub.editor is missing"
)
BLOCKED_CLUSTER = kcc_object(
    "ContainerCluster",
    "prod-usc1",
    status="False",
    reason="DependencyNotReady",
    message="reference PubSubTopic infra/fleet-events is not ready",
)
READY_POOL = kcc_object("ContainerNodePool", "pool-a", status="True", reason="UpToDate")
UNREACHED = kcc_object("ComputeNetwork", "fresh")


class FakeRun:
    """Answers `get-credentials` and the `gcp` read per cluster, and records the calls."""

    def __init__(self, reads, credentials_rc=0):
        self.reads = reads
        self.credentials_rc = credentials_rc
        self.calls = []

    def __call__(self, argv, *, env=None, timeout=None):
        self.calls.append((list(argv), (env or {}).get("KUBECONFIG", "")))
        if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            return kcc_health.Result(self.credentials_rc, "", "" if self.credentials_rc == 0 else "Permission denied")
        cluster = Path((env or {})["KUBECONFIG"]).name
        for key, answer in self.reads.items():
            if f"_{key}_" in cluster:
                return answer
        raise AssertionError(f"unexpected read with kubeconfig {cluster}")


def listing(*items):
    return kcc_health.Result(0, json.dumps({"apiVersion": "v1", "kind": "List", "items": list(items)}), "")


class UnreadyObjectsTest(unittest.TestCase):
    def test_only_an_explicit_false_counts(self):
        found = kcc_health.unready_objects([STALLED_TOPIC, BLOCKED_CLUSTER, READY_POOL, UNREACHED, "junk"])
        self.assertEqual(
            found,
            [
                {
                    "kind": "ContainerCluster",
                    "namespace": "infra",
                    "name": "prod-usc1",
                    "why": "DependencyNotReady: reference PubSubTopic infra/fleet-events is not ready",
                },
                {
                    "kind": "PubSubTopic",
                    "namespace": "infra",
                    "name": "fleet-events",
                    "why": "UpdateFailed: Error 403: roles/pubsub.editor is missing",
                },
            ],
        )

    def test_a_node_pool_carries_its_cluster_and_resource_id(self):
        pool = kcc_object(
            "ContainerNodePool",
            "prod-usc1-default",
            status="False",
            reason="UpdateFailed",
            spec={"resourceID": "default-pool", "clusterRef": {"external": "projects/p/locations/l/clusters/prod-usc1"}},
        )
        self.assertEqual(
            kcc_health.unready_objects([pool]),
            [
                {
                    "kind": "ContainerNodePool",
                    "namespace": "infra",
                    "name": "prod-usc1-default",
                    "why": "UpdateFailed",
                    "resource": "default-pool",
                    "cluster": "prod-usc1",
                }
            ],
        )

    def test_the_reason_is_one_clipped_line(self):
        long_message = "Error 403:\n" + "x" * 1000
        found = kcc_health.unready_objects(
            [kcc_object("ComputeFirewall", "fw", status="False", reason="UpdateFailed", message=long_message)]
        )
        self.assertNotIn("\n", found[0]["why"])
        self.assertEqual(len(found[0]["why"]), kcc_health.MAX_WHY_CHARS)
        bare = kcc_health.unready_objects([kcc_object("ComputeFirewall", "fw", status="False")])
        self.assertEqual(bare[0]["why"], "no reason given")


class EntryFieldsTest(unittest.TestCase):
    def test_a_served_read_lists_the_unready_objects(self):
        fields = kcc_health.entry_fields({"items": [STALLED_TOPIC, READY_POOL]}, 0, "")
        self.assertEqual([r["name"] for r in fields["kcc_unready"]], ["fleet-events"])

    def test_a_served_read_with_nothing_unready_is_an_empty_list(self):
        self.assertEqual(kcc_health.entry_fields({"items": [READY_POOL]}, 0, ""), {"kcc_unready": []})

    def test_an_unserved_category_adds_nothing(self):
        stderr = 'error: the server doesn\'t have a resource type "gcp"'
        self.assertEqual(kcc_health.entry_fields(None, 1, stderr), {})

    def test_a_failed_read_is_unknown_never_healthy(self):
        for rc, stderr, parsed in (
            (124, "killed", None),
            (1, "Error from server (Forbidden)", None),
            (0, "", None),
            (0, "", {"kind": "List"}),
        ):
            with self.subTest(rc=rc, stderr=stderr):
                fields = kcc_health.entry_fields(parsed, rc, stderr)
                self.assertEqual(list(fields), ["kcc_unread"])
                self.assertIn("not known", fields["kcc_unread"])


class AnnotateManifestTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)

    def manifest(self):
        return {
            "clusters": [
                {"name": "acme/us-central1/hub", "project": "acme", "location": "us-central1", "outcome": "collected"},
                {"name": "acme/us-east1/edge", "project": "acme", "location": "us-east1", "outcome": "collected"},
                {"name": "acme/us-west1/slow", "project": "acme", "location": "us-west1", "outcome": "collected"},
                {"name": "acme/us-west2/gone", "project": "acme", "location": "us-west2", "outcome": "unreachable"},
                {
                    "name": "acme/europe-west1/read",
                    "project": "acme",
                    "location": "europe-west1",
                    "outcome": "collected",
                    "kcc_unready": [],
                },
            ]
        }

    def test_each_collected_cluster_is_read_once(self):
        run = FakeRun(
            {
                "hub": listing(STALLED_TOPIC, BLOCKED_CLUSTER, READY_POOL),
                "edge": kcc_health.Result(1, "", 'the server doesn\'t have a resource type "gcp"'),
                "slow": kcc_health.Result(124, "", "killed"),
            }
        )
        manifest = self.manifest()
        kcc_health.annotate_manifest(manifest, run=run, kubeconfig_dir=self.dir)
        hub, edge, slow, gone, read = manifest["clusters"]
        self.assertEqual([r["name"] for r in hub["kcc_unready"]], ["prod-usc1", "fleet-events"])
        self.assertNotIn("kcc_unready", edge)
        self.assertNotIn("kcc_unread", edge)
        self.assertIn("did not finish", slow["kcc_unread"])
        self.assertNotIn("kcc_unread", gone)
        self.assertEqual(read["kcc_unready"], [])
        reads = [argv for argv, _ in run.calls if argv[0] == "kubectl"]
        self.assertEqual(len(reads), 3)
        self.assertTrue(all(argv == ["kubectl", "get", "gcp", "-A", "-o", "json"] for argv in reads))
        for argv, kubeconfig in run.calls:
            self.assertTrue(kubeconfig.startswith(str(self.dir)), kubeconfig)

    def test_the_reads_go_through_the_callers_pool(self):
        run = FakeRun({"hub": listing(READY_POOL), "edge": listing(), "slow": listing()})
        mapped = []

        def mapper(fn, items):
            items = list(items)
            mapped.extend(entry["name"] for entry in items)
            return map(fn, items)

        manifest = self.manifest()
        kcc_health.annotate_manifest(manifest, run=run, kubeconfig_dir=self.dir, mapper=mapper)
        self.assertEqual(mapped, ["acme/us-central1/hub", "acme/us-east1/edge", "acme/us-west1/slow"])

    def test_a_credential_failure_is_unknown(self):
        run = FakeRun({}, credentials_rc=1)
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, run=run, kubeconfig_dir=self.dir)
        self.assertIn("get-credentials exited 1", manifest["clusters"][0]["kcc_unread"])

    def test_a_read_that_raises_is_unknown_and_keeps_the_manifest(self):
        def run(argv, *, env=None, timeout=None):
            raise RuntimeError("boom")

        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, run=run, kubeconfig_dir=self.dir)
        self.assertIn("RuntimeError", manifest["clusters"][0]["kcc_unread"])


if __name__ == "__main__":
    unittest.main()
