"""Tests for kcc_health.py, the Config Connector read the collectors share."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import collect  # noqa: E402
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

    def __init__(self, reads, credentials_rc=0, namespaces=None):
        # `namespaces`: the answer to `kubectl get namespaces`, else a failure.
        self.namespaces = namespaces
        self.reads = reads
        self.credentials_rc = credentials_rc
        self.calls = []
        self.timeouts = []

    def __call__(self, argv, *, env=None, timeout=None):
        self.calls.append((list(argv), (env or {}).get("KUBECONFIG", "")))
        self.timeouts.append(timeout)
        if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            if self.credentials_rc == 0:
                Path(env["KUBECONFIG"]).write_text("apiVersion: v1\n")
            return kcc_health.Result(self.credentials_rc, "", "" if self.credentials_rc == 0 else "Permission denied")
        if argv[:3] == ["kubectl", "get", "namespaces"]:
            return self.namespaces if self.namespaces is not None else kcc_health.Result(1, "", "Forbidden")
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

    def test_the_location_is_recorded(self):
        located = kcc_object("ContainerCluster", "prod", status="False", reason="UpdateFailed", spec={"location": "us-central1"})
        self.assertEqual(kcc_health.unready_objects([located])[0]["location"], "us-central1")
        self.assertNotIn("location", kcc_health.unready_objects([STALLED_TOPIC])[0])

    def test_the_target_project_comes_from_the_objects_annotation(self):
        annotated = kcc_object("ContainerCluster", "prod", namespace="config-control", status="False", reason="UpdateFailed")
        annotated["metadata"]["annotations"] = {"cnrm.cloud.google.com/project-id": "pa"}
        bare = kcc_object("ContainerCluster", "other", namespace="config-control", status="False", reason="UpdateFailed")
        found = {r["name"]: r for r in kcc_health.unready_objects([annotated, bare])}
        self.assertEqual(found["prod"]["project"], "pa")
        self.assertNotIn("project", found["other"])

    def test_a_transient_reason_is_not_unready(self):
        """`Updating` and `Deleting` mean Config Connector still applies the
        object; the note must not say it is not applying."""
        items = [
            kcc_object("ComputeFirewall", "rolling", status="False", reason="Updating", message="Update in progress"),
            kcc_object("ComputeNetwork", "going", status="False", reason="Deleting"),
            STALLED_TOPIC,
        ]
        self.assertEqual([r["name"] for r in kcc_health.unready_objects(items)], ["fleet-events"])

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
        self.dir = Path(scratch.name) / ".kubeconfigs"
        # collect.py owns the directory and the file name.
        patcher = mock.patch.object(collect, "KUBECONFIG_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 0.0
        self.deadline = kcc_health.run_deadline(0.0)

    def clock(self):
        return self.now

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
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        hub, edge, slow, gone, read = manifest["clusters"]
        self.assertEqual([r["name"] for r in hub["kcc_unready"]], ["prod-usc1", "fleet-events"])
        self.assertNotIn("kcc_unready", edge)
        self.assertNotIn("kcc_unread", edge)
        self.assertIn("did not finish", slow["kcc_unread"])
        self.assertNotIn("kcc_unread", gone)
        self.assertEqual(read["kcc_unready"], [])
        reads = [argv for argv, _ in run.calls if argv[:3] == ["kubectl", "get", "gcp"]]
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
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, mapper=mapper, clock=self.clock)
        self.assertEqual(mapped, ["acme/us-central1/hub", "acme/us-east1/edge", "acme/us-west1/slow"])

    def test_a_credential_failure_is_unknown(self):
        run = FakeRun({}, credentials_rc=1)
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertIn("get-credentials exited 1", manifest["clusters"][0]["kcc_unread"])

    def test_a_read_that_raises_is_unknown_and_keeps_the_manifest(self):
        def run(argv, *, env=None, timeout=None):
            raise RuntimeError("boom")

        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertIn("RuntimeError", manifest["clusters"][0]["kcc_unread"])


    def test_the_kubeconfig_directory_goes_through_the_shared_rule(self):
        """collect.py's `usable_kubeconfig_dir` is the one rule for the
        directory; a refusal is `kcc_unread`, and nothing is run."""
        run = FakeRun({"hub": listing()})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        with mock.patch("collect.usable_kubeconfig_dir", return_value="owned by uid 0") as rule:
            kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        rule.assert_called_once_with(self.dir)
        self.assertIn("owned by uid 0", manifest["clusters"][0]["kcc_unread"])
        self.assertEqual(run.calls, [])

    def test_each_kubeconfig_is_prepared_by_the_shared_rule(self):
        run = FakeRun({"hub": listing()})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        with mock.patch("collect.prepare_kubeconfig", return_value=None) as prepare:
            kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        prepare.assert_called_once_with(self.dir / "kubeconfig_acme_hub_us-central1.yaml")
        self.assertEqual(manifest["clusters"][0]["kcc_unready"], [])

    def test_the_kubeconfig_path_is_collects(self):
        """`collect.kubeconfig_path` names the file, not a copy of its rule."""
        run = FakeRun({"hub": listing()})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        named = self.dir / "named_by_collect_hub_.yaml"
        with mock.patch.object(collect, "kubeconfig_path", return_value=named) as path:
            kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        path.assert_called_once_with("acme", "hub", "us-central1")
        self.assertEqual({kubeconfig for _, kubeconfig in run.calls}, {str(named)})
        self.assertFalse(hasattr(kcc_health, "KUBECONFIG_NAME"))

    def test_a_kubeconfig_the_collector_fetched_is_reused(self):
        run = FakeRun({"hub": listing(STALLED_TOPIC)})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        swept = self.dir / "kubeconfig_acme_hub_us-central1.yaml"
        swept.parent.mkdir(parents=True)
        swept.write_text("apiVersion: v1\n")
        kcc_health.annotate_manifest(
            manifest, deadline=self.deadline, run=run, clock=self.clock, kubeconfig_for=lambda entry: swept
        )
        self.assertEqual({kubeconfig for _, kubeconfig in run.calls}, {str(swept)})
        self.assertNotIn("get-credentials", [argv[3] for argv, _ in run.calls if len(argv) > 3])
        self.assertEqual([r["name"] for r in manifest["clusters"][0]["kcc_unready"]], ["fleet-events"])

    def test_a_missing_kubeconfig_file_runs_no_read(self):
        """kubectl reads the defaults or the in-cluster account when the
        `KUBECONFIG` file is not there, which is another cluster."""
        run = FakeRun({"hub": listing(STALLED_TOPIC)})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        missing = self.dir / "kubeconfig_acme_hub_us-central1.yaml"
        kcc_health.annotate_manifest(
            manifest, deadline=self.deadline, run=run, clock=self.clock, kubeconfig_for=lambda entry: missing
        )
        self.assertEqual(run.calls, [])
        self.assertIn("no kubeconfig", manifest["clusters"][0]["kcc_unread"])

    def test_a_project_entry_is_not_a_cluster_and_is_not_read(self):
        """Stockout's `project/<id>` entries are `collected` at location
        `global`; they hold project reads and have no kubeconfig."""
        run = FakeRun({})
        manifest = {
            "clusters": [
                {"name": "project/acme", "project": "acme", "location": "global", "outcome": "collected"},
                {"name": "acme/global/odd", "project": "acme", "location": "global", "outcome": "collected"},
            ]
        }
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertEqual(run.calls, [])
        for entry in manifest["clusters"]:
            self.assertNotIn("kcc_unread", entry)
            self.assertNotIn("kcc_unready", entry)

    def test_a_cluster_that_is_not_running_is_not_read_and_is_not_a_host(self):
        """The drift collector marks every listed cluster `collected`. A hub
        with an unannotated stalled ContainerNodePool gives the note; a
        STOPPING cluster beside it must not take the note away, and no read
        runs on it."""
        import audit_report  # noqa: PLC0415

        pool = kcc_object(
            "ContainerNodePool", "gpu-pool", status="False", reason="UpdateFailed",
            spec={"clusterRef": {"name": "prod-usc1"}},
        )
        run = FakeRun({"hub": listing(pool)})
        manifest = {
            "clusters": [
                {"name": "acme/us-central1/hub", "project": "acme", "location": "us-central1", "outcome": "collected", "cluster_status": "RUNNING"},
                {"name": "acme/us-east1/going", "project": "acme", "location": "us-east1", "outcome": "collected", "cluster_status": "STOPPING"},
            ]
        }
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        hub, going = manifest["clusters"]
        self.assertNotIn("kcc_unread", going)
        self.assertNotIn("kcc_unready", going)
        self.assertFalse([kc for _, kc in run.calls if "_going_" in kc])
        root = Path(self.dir).parent / "repo"
        (root / "pools").mkdir(parents=True)
        (root / "pools" / "gpu.yaml").write_text(
            "apiVersion: container.cnrm.cloud.google.com/v1beta1\nkind: ContainerNodePool\nmetadata:\n  name: gpu-pool\n"
            "  namespace: infra\nspec:\n  location: us-central1\n  clusterRef:\n    name: prod-usc1\n"
        )
        finding = {
            "id": "f", "cluster": "acme/us-central1/prod-usc1", "object": "NodePool/gpu-pool",
            "remediation": {"kind": "manifest", "path": "pools/gpu.yaml", "note": "Raise the ceiling."},
        }
        self.assertEqual(audit_report.disclose_stalled_kcc_remediations([finding], manifest, root), ["f"])

    def namespace(self, name, project=None):
        meta = {"name": name}
        if project:
            meta["annotations"] = {"cnrm.cloud.google.com/project-id": project}
        return {"kind": "Namespace", "metadata": meta}

    def test_the_namespace_annotation_gives_an_object_its_project(self):
        """Config Connector takes the project from the namespace when the
        object names none; one namespaces read per such host gives it."""
        bare = kcc_object("PubSubTopic", "events", status="False", reason="UpdateFailed")
        run = FakeRun({"hub": listing(bare, STALLED_TOPIC)}, namespaces=listing(self.namespace("infra", "acme")))
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertEqual({r["name"]: r.get("project") for r in manifest["clusters"][0]["kcc_unready"]}, {"events": "acme", "fleet-events": "acme"})
        self.assertEqual(len([argv for argv, _ in run.calls if argv[:3] == ["kubectl", "get", "namespaces"]]), 1)

    def test_a_namespace_with_no_annotation_gives_its_name_as_the_project(self):
        """Config Connector uses the namespace's name as the project ID when
        neither the object nor its namespace names one. A namespace the read
        did not return leaves the project unknown."""
        bare = kcc_object("PubSubTopic", "events", namespace="proj-c", status="False", reason="UpdateFailed")
        lost = kcc_object("PubSubTopic", "lost", namespace="gone", status="False", reason="UpdateFailed")
        run = FakeRun({"hub": listing(bare, lost)}, namespaces=listing(self.namespace("proj-c")))
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertEqual({r["name"]: r.get("project") for r in manifest["clusters"][0]["kcc_unready"]}, {"events": "proj-c", "lost": None})

    def test_a_failed_namespaces_read_leaves_the_project_unknown(self):
        bare = kcc_object("PubSubTopic", "events", status="False", reason="UpdateFailed")
        run = FakeRun({"hub": listing(bare)})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertNotIn("project", manifest["clusters"][0]["kcc_unready"][0])

    def test_no_namespaces_read_when_every_object_names_its_project(self):
        annotated = kcc_object("PubSubTopic", "events", status="False", reason="UpdateFailed")
        annotated["metadata"]["annotations"] = {"cnrm.cloud.google.com/project-id": "acme"}
        run = FakeRun({"hub": listing(annotated)})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertFalse([argv for argv, _ in run.calls if argv[:3] == ["kubectl", "get", "namespaces"]])

    def test_the_namespace_project_keeps_the_note_beside_an_unreachable_cluster(self):
        """hub-2 is unreachable, so a match needs a known project; the
        namespace gives it, and the note comes back."""
        import audit_report  # noqa: PLC0415

        bare = kcc_object("PubSubTopic", "events", status="False", reason="UpdateFailed")
        manifest = {
            "clusters": [
                self.manifest()["clusters"][0],
                {"name": "acme/us-east1/hub-2", "project": "acme", "location": "us-east1", "outcome": "unreachable"},
            ]
        }
        root = Path(self.dir).parent / "repo"
        (root / "topics").mkdir(parents=True)
        (root / "topics" / "events.yaml").write_text(
            "apiVersion: pubsub.cnrm.cloud.google.com/v1beta1\nkind: PubSubTopic\nmetadata:\n  name: events\n  namespace: infra\n"
        )

        def finding():
            return {"id": "f", "cluster": "acme/us-central1/hub", "object": "PubSubTopic/events",
                    "remediation": {"kind": "manifest", "path": "topics/events.yaml", "note": "x"}}

        for namespaces, expected in ((None, []), (listing(self.namespace("infra", "acme")), ["f"])):
            with self.subTest(namespaces=bool(namespaces)):
                read = json.loads(json.dumps(manifest))
                run = FakeRun({"hub": listing(bare)}, namespaces=namespaces)
                kcc_health.annotate_manifest(read, deadline=self.deadline, run=run, clock=self.clock)
                self.assertEqual(audit_report.disclose_stalled_kcc_remediations([finding()], read, root), expected)

    def test_clusters_with_the_add_on_are_read_first(self):
        run = FakeRun({"hub": listing(), "edge": listing(), "slow": listing()})
        mapped = []

        def mapper(fn, items):
            items = list(items)
            mapped.extend(entry["name"] for entry in items)
            return map(fn, items)

        manifest = self.manifest()
        manifest["clusters"][2]["config_connector_addon"] = True
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, mapper=mapper, clock=self.clock)
        self.assertEqual(mapped, ["acme/us-west1/slow", "acme/us-central1/hub", "acme/us-east1/edge"])

    def test_the_add_on_is_read_from_the_clusters_list_record(self):
        self.assertTrue(kcc_health.addon_enabled({"addonsConfig": {"configConnectorConfig": {"enabled": True}}}))
        self.assertFalse(kcc_health.addon_enabled({"addonsConfig": {"configConnectorConfig": {}}}))
        self.assertFalse(kcc_health.addon_enabled({}))


class DeadlineTest(unittest.TestCase):
    """The reads end inside the collector's time budget, so the manifest
    prints before the terminal call is killed."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name) / ".kubeconfigs"
        # collect.py owns the directory and the file name.
        patcher = mock.patch.object(collect, "KUBECONFIG_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 0.0

    def clock(self):
        return self.now

    def entries(self, *names):
        return {
            "clusters": [
                {"name": f"acme/us-central1/{n}", "project": "acme", "location": "us-central1", "outcome": "collected"}
                for n in names
            ]
        }

    def slow_run(self, credentials_s, read_s):
        """A reader that takes `credentials_s` or `read_s` of the fake clock
        per call, or its whole timeout when that is shorter."""
        timeouts = []

        def run(argv, *, env=None, timeout=None):
            timeouts.append((argv[0], timeout))
            seconds = read_s if argv[0] == "kubectl" else credentials_s
            if timeout is not None and seconds >= timeout:
                self.now += timeout
                return kcc_health.Result(kcc_health.TIMEOUT_RC, "", "killed")
            self.now += seconds
            if argv[0] != "kubectl":
                Path(env["KUBECONFIG"]).write_text("apiVersion: v1\n")
            return listing() if argv[0] == "kubectl" else kcc_health.Result(0, "", "")

        return run, timeouts

    def test_the_reads_end_by_the_deadline_and_the_rest_say_the_budget_was_spent(self):
        # The sweep took seven minutes, so two minutes are left.
        self.now = 420.0
        deadline = kcc_health.run_deadline(0.0)
        run, timeouts = self.slow_run(10, 105)
        manifest = self.entries("a", "b", "c")
        kcc_health.annotate_manifest(manifest, deadline=deadline, run=run, clock=self.clock)
        self.assertLessEqual(self.now, deadline)
        self.assertLessEqual(deadline, kcc_health.TERMINAL_TIMEOUT_S - kcc_health.PRINT_MARGIN_S)
        a, b, c = manifest["clusters"]
        # The read had the 110 s left, not its own 240 s.
        self.assertEqual(timeouts, [("gcloud", kcc_health.CREDENTIALS_TIMEOUT_S), ("kubectl", 110)])
        self.assertEqual(a["kcc_unready"], [])
        for entry in (b, c):
            self.assertIn("time budget", entry["kcc_unread"])
            self.assertIn(str(kcc_health.RUN_DEADLINE_S), entry["kcc_unread"])

    def test_a_read_that_runs_out_the_time_left_ends_at_the_deadline(self):
        self.now = 420.0
        deadline = kcc_health.run_deadline(0.0)
        run, timeouts = self.slow_run(59, 1000)
        manifest = self.entries("a")
        kcc_health.annotate_manifest(manifest, deadline=deadline, run=run, clock=self.clock)
        self.assertEqual(timeouts, [("gcloud", 60), ("kubectl", 61)])
        self.assertIn("did not finish in 61s", manifest["clusters"][0]["kcc_unread"])
        self.assertLessEqual(self.now, deadline)

    def test_a_read_is_never_given_more_than_the_time_left(self):
        self.now = 500.0
        deadline = kcc_health.run_deadline(0.0)
        run, timeouts = self.slow_run(1, 1)
        manifest = self.entries("a")
        kcc_health.annotate_manifest(manifest, deadline=deadline, run=run, clock=self.clock)
        self.assertEqual(timeouts, [("gcloud", 40), ("kubectl", 39)])
        self.assertEqual(manifest["clusters"][0]["kcc_unready"], [])

    def test_a_failure_around_the_reads_keeps_the_manifest(self):
        def mapper(fn, items):
            raise RuntimeError("pool shut down")

        manifest = self.entries("a")
        kcc_health.annotate_manifest(
            manifest, deadline=kcc_health.run_deadline(0.0), run=self.slow_run(1, 1)[0], mapper=mapper, clock=self.clock
        )
        self.assertIn("RuntimeError", manifest["clusters"][0]["kcc_unread"])


class CopiedConstantsTest(unittest.TestCase):
    """kcc_health.py cannot import collect.py at load time (collect.py imports
    it), so it copies these constants; this holds each copy equal."""

    def test_the_copies_equal_collects(self):
        pairs = {
            "READ_TIMEOUT_S": collect.KCC_READ_TIMEOUT_S,
            "CREDENTIALS_TIMEOUT_S": collect.DEFAULT_TIMEOUT_S,
            "MAX_WHY_CHARS": collect.MAX_KCC_WEDGE_MESSAGE_CHARS,
            "TIMEOUT_RC": collect.TIMEOUT_RC,
            "RESOURCE_TYPE_ABSENT_MARKER": collect.RESOURCE_TYPE_ABSENT_MARKER,
            "KCC_CATEGORY": collect.KCC_CATEGORY,
            "KCC_READY_CONDITION": collect.KCC_READY_CONDITION,
        }
        for name, source in pairs.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(kcc_health, name), source)


if __name__ == "__main__":
    unittest.main()
