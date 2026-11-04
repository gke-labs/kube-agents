"""Tests for kcc_health.py, the Config Connector read the collectors share."""

import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
import urllib.parse
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


# The group every fake Config Connector kind is in, and the kind a cluster
# that runs Config Connector lists when it holds no object.
FAKE_GROUP = "test.cnrm.cloud.google.com"
DEFAULT_KIND = "ComputeFirewall"


def crd_line(kind, group=FAKE_GROUP, categories="gcp", shortnames=""):
    """One line of `kubectl api-resources -o wide --no-headers`, as kubectl
    aligns it (SHORTNAMES can be empty)."""
    return "   ".join(
        word for word in (f"{kind.lower()}s", shortnames, f"{group}/v1beta1", "true", kind,
                          "delete,deletecollection,get,list,patch,create,update,watch", categories) if word
    )


def is_discovery(argv):
    return argv[:2] == ["kubectl", "api-resources"]


def serve_kcc(argv, answer):
    """The Config Connector read of one cluster, from one `answer`.

    `listing(*items)`: the cluster runs Config Connector and holds `items`,
    and its CRDs are the kinds of `items` (`DEFAULT_KIND` when it holds
    none). A Result that says the type is not served: no Config Connector
    CRD. Any other Result: the answer to every kind's list request.
    """
    absent = answer.rc != 0 and "doesn't have a resource type" in answer.stderr
    items = json.loads(answer.stdout)["items"] if answer.rc == 0 and answer.stdout.startswith("{") else []
    if is_discovery(argv):
        if absent:
            return kcc_health.Result(0, "deployments   deploy   apps/v1   true   Deployment   get,list   all\n", "")
        kinds = sorted({item["kind"] for item in items} or {DEFAULT_KIND})
        return kcc_health.Result(0, "".join(crd_line(kind) + "\n" for kind in kinds), "")
    if argv[:3] == ["kubectl", "get", "--raw"]:
        if answer.rc != 0 or not answer.stdout.startswith("{"):
            return answer
        plural = argv[3].partition("?")[0].rsplit("/", 1)[1]
        return listing(*(item for item in items if f"{item['kind'].lower()}s" == plural))
    raise AssertionError(f"unexpected Config Connector command {argv}")


class FakeRun:
    """Answers `get-credentials` and the Config Connector read per cluster
    (`serve_kcc`), and records the calls."""

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
                return serve_kcc(argv, answer)
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

    def test_a_project_ref_gives_the_project_and_the_namespace_does_not(self):
        """Review finding: a kind with `spec.projectRef` was given its
        namespace's name as its project. `external` gives the project; `name`
        gives it only through a `Project` object the read listed."""
        def topic(name, ref):
            return kcc_object("PubSubTopic", name, namespace="config-control", status="False", reason="UpdateFailed",
                              spec={"projectRef": ref})

        listed = {"apiVersion": "resourcemanager.cnrm.cloud.google.com/v1beta1", "kind": "Project",
                  "metadata": {"name": "app", "namespace": "config-control"}, "spec": {"resourceID": "app-123"}}
        records = kcc_health.unready_objects([
            topic("ext", {"external": "projects/proj-x"}),
            topic("bare", {"external": "proj-y"}),
            topic("listed", {"name": "app"}),
            topic("unlisted", {"name": "elsewhere", "namespace": "projects"}),
            listed,
        ])
        found = {record["name"]: record for record in records}
        self.assertEqual(found["ext"]["project"], "proj-x")
        self.assertEqual(found["bare"]["project"], "proj-y")
        self.assertEqual(found["listed"]["project"], "app-123")
        self.assertNotIn("project", found["unlisted"])
        namespaces = [{"metadata": {"name": "config-control"}}]
        kcc_health.fill_namespace_projects(records, namespaces)
        self.assertNotIn("project", found["unlisted"])
        self.assertFalse(kcc_health._needs_namespace_projects({"kcc_unready": [found["unlisted"]]}))

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


class ReadObjectsTest(unittest.TestCase):
    """`read_objects`: discovery, then paged list requests per Config Connector
    kind (every kind for the compliance collector, `CONTAINER_KINDS` for the
    others), with no more than `proxy_slots()` requests in flight."""

    def read(self, run, **kwargs):
        return kcc_health.read_objects(Path("kubeconfig"), run=run, deadline=kwargs.pop("deadline", 1e9), clock=kwargs.pop("clock", lambda: 0.0), **kwargs)

    def test_the_kinds_come_from_discovery_and_other_resources_are_ignored(self):
        lines = "\n".join((
            crd_line("PubSubTopic", group="pubsub.cnrm.cloud.google.com", shortnames="gcppubsubtopic,gcppubsubtopics"),
            crd_line("ConfigConnectorContext", group="core.cnrm.cloud.google.com", categories=""),
            crd_line("Widget", group="example.com"),
            "computeaddresses   compute.cnrm.cloud.google.com/v1beta1   true   ComputeAddress   get,list,watch   all,gcp",
            "bindings   v1   true   Binding   create",
            "nodes   no   v1   false   Node   get,list",
            "computenolists   compute.cnrm.cloud.google.com/v1beta1   true   ComputeNoList   get   gcp",
        ))
        self.assertEqual(
            kcc_health.kcc_kinds(lines),
            [
                kcc_health.KccKind("compute.cnrm.cloud.google.com", "v1beta1", "computeaddresses", "ComputeAddress"),
                kcc_health.KccKind("pubsub.cnrm.cloud.google.com", "v1beta1", "pubsubtopics", "PubSubTopic"),
            ],
        )
        self.assertEqual(
            kcc_health.kind_argv(kcc_health.KccKind("pubsub.cnrm.cloud.google.com", "v1beta1", "pubsubtopics", "PubSubTopic")),
            ["kubectl", "get", "--raw", f"/apis/pubsub.cnrm.cloud.google.com/v1beta1/pubsubtopics?limit={kcc_health.KIND_PAGE_LIMIT}"],
        )

    def test_a_large_kind_is_read_in_pages(self):
        """Each list request carries `limit=`, so no reply holds a whole
        large kind across every namespace, and the read follows
        `metadata.continue` to the end."""
        kind = kcc_health.KccKind("pubsub.cnrm.cloud.google.com", "v1beta1", "pubsubtopics", "PubSubTopic")
        self.assertEqual(
            kcc_health.kind_argv(kind, "a b/c")[-1],
            f"/apis/pubsub.cnrm.cloud.google.com/v1beta1/pubsubtopics?limit={kcc_health.KIND_PAGE_LIMIT}&continue=a%20b%2Fc",
        )
        pages = {"": ("t1", ["one"]), "t1": ("t2", ["two"]), "t2": ("", ["three"])}
        urls = []

        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, crd_line("PubSubTopic", group="pubsub.cnrm.cloud.google.com") + "\n", "")
            urls.append(argv[3])
            query = urllib.parse.parse_qs(argv[3].partition("?")[2])
            after = query.get("continue", [""])[0]
            token, names = pages[after]
            items = [{"kind": "PubSubTopic", "metadata": {"name": name}} for name in names]
            return kcc_health.Result(0, json.dumps({"items": items, "metadata": {"continue": token}}), "")

        read = kcc_health.read_objects(Path("k"), run=run, deadline=time.monotonic() + 60, workers=1)
        self.assertEqual(read.status, kcc_health.READ_LISTED)
        self.assertEqual([item["metadata"]["name"] for item in read.items], ["one", "two", "three"])
        self.assertEqual(len(urls), 3)
        self.assertTrue(all(f"limit={kcc_health.KIND_PAGE_LIMIT}" in url for url in urls))

    def test_every_list_path_is_the_bare_api_path_on_every_page(self):
        """Each kind's list is a bare absolute `--raw /apis/...` path, on the
        first page and on each continue page. The sandbox's kubectl (1.34 or
        later, tests/test_sandbox_kubectl_pin.py) keeps the kube-proxy's
        `/clusters/<id>` server path on it, so a prefix here would double it."""
        pages = {"": ("t1", ["one"]), "t1": ("", ["two"])}
        raws = []

        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, crd_line("PubSubTopic", group="pubsub.cnrm.cloud.google.com") + "\n", "")
            self.assertEqual(argv[:3], ["kubectl", "get", "--raw"])
            raws.append(argv[3])
            after = urllib.parse.parse_qs(argv[3].partition("?")[2]).get("continue", [""])[0]
            token, names = pages[after]
            items = [{"kind": "PubSubTopic", "metadata": {"name": name}} for name in names]
            return kcc_health.Result(0, json.dumps({"items": items, "metadata": {"continue": token}}), "")

        read = kcc_health.read_objects(Path("k"), run=run, deadline=time.monotonic() + 60, workers=1)
        self.assertEqual(read.status, kcc_health.READ_LISTED)
        self.assertEqual(len(raws), 2)
        for raw in raws:
            self.assertTrue(raw.startswith("/apis/pubsub.cnrm.cloud.google.com/v1beta1/pubsubtopics?limit="), raw)

    def test_a_repeated_continue_token_makes_the_read_unknown(self):
        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, crd_line("PubSubTopic", group="pubsub.cnrm.cloud.google.com") + "\n", "")
            return kcc_health.Result(0, json.dumps({"items": [], "metadata": {"continue": "same"}}), "")

        read = kcc_health.read_objects(Path("k"), run=run, deadline=time.monotonic() + 60, workers=1)
        self.assertEqual(read.status, kcc_health.READ_UNREADABLE)

    def test_the_lists_of_every_kind_are_merged(self):
        calls = []

        def run(argv, *, env=None, timeout=None):
            calls.append(argv)
            return serve_kcc(argv, listing(STALLED_TOPIC, BLOCKED_CLUSTER, READY_POOL))

        read = self.read(run)
        self.assertEqual(read.status, kcc_health.READ_LISTED)
        self.assertEqual(read.kinds, 3)
        self.assertEqual(sorted(item["metadata"]["name"] for item in read.items), ["fleet-events", "pool-a", "prod-usc1"])
        self.assertEqual(len([argv for argv in calls if argv[:3] == ["kubectl", "get", "--raw"]]), 3)
        self.assertFalse([argv for argv in calls if "gcp" in argv], "no category read: it lists the kinds one after another")
        self.assertFalse([argv for argv in calls if "customresourcedefinitions" in argv], "discovery needs no RBAC; a CRD list does")
        self.assertEqual([r["name"] for r in kcc_health.read_fields(read)["kcc_unready"]], ["prod-usc1", "fleet-events"])

    def test_an_item_with_no_kind_takes_the_kind_of_its_crd(self):
        bare = {key: value for key, value in STALLED_TOPIC.items() if key != "kind"}

        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, crd_line("PubSubTopic") + "\n", "")
            return listing(bare)

        self.assertEqual(self.read(run).items[0]["kind"], "PubSubTopic")

    def test_the_kinds_are_listed_in_parallel_on_a_bounded_pool(self):
        lock = threading.Lock()
        running = [0, 0]
        kinds = [f"Kind{n}" for n in range(3 * kcc_health.proxy_slots())]

        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, "".join(crd_line(kind) + "\n" for kind in kinds), "")
            with lock:
                running[0] += 1
                running[1] = max(running[1], running[0])
            time.sleep(0.02)
            with lock:
                running[0] -= 1
            return listing()

        read = self.read(run)
        self.assertEqual(read.status, kcc_health.READ_LISTED)
        self.assertGreater(running[1], 1)
        self.assertLessEqual(running[1], kcc_health.proxy_slots())

    def test_one_kind_that_does_not_list_makes_the_read_unknown(self):
        for answer, status, words in (
            (kcc_health.Result(1, "", "Error from server (Forbidden): pubsubtopics is forbidden"), kcc_health.READ_FAILED, "Forbidden"),
            (kcc_health.Result(kcc_health.TIMEOUT_RC, "", "killed"), kcc_health.READ_TIMED_OUT, "did not finish"),
            (kcc_health.Result(0, '{"items": [{"kind": "Pub', ""), kcc_health.READ_UNREADABLE, "not a list"),
        ):
            with self.subTest(status=status):
                def run(argv, *, env=None, timeout=None):
                    if is_discovery(argv):
                        return kcc_health.Result(0, crd_line("PubSubTopic") + "\n" + crd_line("ComputeFirewall") + "\n", "")
                    if argv[3].partition("?")[0].endswith("pubsubtopics"):
                        return answer
                    return listing(kcc_object("ComputeFirewall", "fw", status="False", reason="UpdateFailed"))

                read = self.read(run)
                self.assertEqual(read.status, status)
                fields = kcc_health.read_fields(read)
                self.assertEqual(list(fields), ["kcc_unread"])
                self.assertIn("not known", fields["kcc_unread"])
                self.assertIn(words, fields["kcc_unread"] if status != kcc_health.READ_TIMED_OUT else fields["kcc_unread"] + read.detail)

    def test_discovery_that_misses_an_aggregated_api_still_finds_the_kinds(self):
        """`kubectl api-resources` exits 1 when an aggregated API (often
        metrics.k8s.io) does not answer, and still prints what it found."""
        partial = "error: unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1: the server is currently unable to handle the request"

        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(1, crd_line("PubSubTopic") + "\n", partial)
            return listing(STALLED_TOPIC)

        self.assertEqual(self.read(run).status, kcc_health.READ_LISTED)

        def missing_kcc(argv, *, env=None, timeout=None):
            return kcc_health.Result(1, "", partial.replace("metrics.k8s.io", "pubsub.cnrm.cloud.google.com"))

        self.assertEqual(self.read(missing_kcc).status, kcc_health.READ_FAILED)

    def test_a_refused_discovery_is_unknown_never_absent(self):
        fields = kcc_health.read_fields(self.read(lambda argv, **kw: kcc_health.Result(1, "", "Unauthorized")))
        self.assertIn("not known", fields["kcc_unread"])

    def test_no_more_than_the_proxy_slots_run_at_once_across_clusters(self):
        """As many clusters read at once as the broker's width, each with its
        own kind pool: the requests in flight never exceed `proxy_slots()`,
        the broker's sweep width."""
        lock = threading.Lock()
        running = [0, 0]
        kinds = [f"Kind{n}" for n in range(12)]

        def run(argv, *, env=None, timeout=None):
            with lock:
                running[0] += 1
                running[1] = max(running[1], running[0])
            time.sleep(0.01)
            with lock:
                running[0] -= 1
            if is_discovery(argv):
                return kcc_health.Result(0, "".join(crd_line(kind) + "\n" for kind in kinds), "")
            return listing()

        width = kcc_health.proxy_slots()
        with ThreadPoolExecutor(max_workers=width) as pool:
            reads = list(pool.map(lambda _: self.read(run), range(width)))
        self.assertTrue(all(read.status == kcc_health.READ_LISTED for read in reads))
        self.assertGreater(running[1], 1)
        self.assertLessEqual(running[1], kcc_health.proxy_slots())

    def test_a_step_that_gets_no_slot_before_the_deadline_does_not_start(self):
        calls = []
        slots = kcc_health._proxy_slot_pool()
        slots.acquire()
        try:
            for _ in range(kcc_health.proxy_slots() - 1):
                slots.acquire()
            now = [0.0]
            read = kcc_health.read_objects(
                Path("kubeconfig"), run=lambda argv, **kw: calls.append(argv), deadline=0.05, clock=lambda: now[0]
            )
        finally:
            for _ in range(kcc_health.proxy_slots()):
                slots.release()
        self.assertEqual(calls, [])
        self.assertEqual(read.status, kcc_health.READ_TIMED_OUT)
        self.assertIn("no proxy slot", read.detail)

    def test_the_flag_collectors_list_only_the_container_kinds(self):
        """From the agent each request takes seconds, so the drift, patch
        readiness and stockout collectors list only the kinds their findings
        can own."""
        calls = []
        served = ("ContainerCluster", "ContainerNodePool", "PubSubTopic", "ComputeFirewall")

        def run(argv, *, env=None, timeout=None):
            calls.append(argv)
            if is_discovery(argv):
                return kcc_health.Result(0, "".join(
                    crd_line(kind, group="container.cnrm.cloud.google.com" if kind.startswith("Container") else FAKE_GROUP) + "\n"
                    for kind in served
                ), "")
            return listing()

        read = self.read(run, kinds=kcc_health.CONTAINER_KINDS)
        self.assertEqual(read.status, kcc_health.READ_LISTED)
        self.assertEqual(read.kinds, 2)
        listed = sorted(argv[3].partition("?")[0] for argv in calls if argv[:3] == ["kubectl", "get", "--raw"])
        self.assertEqual(listed, [
            "/apis/container.cnrm.cloud.google.com/v1beta1/containerclusters",
            "/apis/container.cnrm.cloud.google.com/v1beta1/containernodepools",
        ])
        # With no kinds named, the compliance collector's read lists them all.
        calls.clear()
        self.assertEqual(self.read(run).kinds, len(served))

    def test_a_cluster_with_no_container_kind_is_absent_for_the_flag_collectors(self):
        """Config Connector runs there, but serves no container kind: no object
        a drift, patch readiness or stockout note can name, so neither key."""
        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, crd_line("PubSubTopic") + "\n", "")
            return listing()

        read = self.read(run, kinds=kcc_health.CONTAINER_KINDS)
        self.assertEqual(read.status, kcc_health.READ_ABSENT)
        self.assertEqual(kcc_health.read_fields(read), {})

    def test_a_kind_that_went_away_after_the_discovery_is_skipped(self):
        def run(argv, *, env=None, timeout=None):
            if is_discovery(argv):
                return kcc_health.Result(0, crd_line("PubSubTopic") + "\n" + crd_line("ComputeFirewall") + "\n", "")
            if argv[3].endswith("pubsubtopics"):
                return kcc_health.Result(1, "", "Error from server (NotFound): the server could not find the requested resource")
            return listing(STALLED_TOPIC)

        self.assertEqual(self.read(run).status, kcc_health.READ_LISTED)

    def test_no_config_connector_kind_served_adds_nothing(self):
        read = self.read(lambda argv, **kw: serve_kcc(argv, kcc_health.Result(1, "", "doesn't have a resource type")))
        self.assertEqual(read.status, kcc_health.READ_ABSENT)
        self.assertEqual(kcc_health.read_fields(read), {})

    def test_a_failed_discovery_is_unknown_never_absent(self):
        for answer in (kcc_health.Result(1, "", "Forbidden"), kcc_health.Result(kcc_health.TIMEOUT_RC, "", "killed")):
            with self.subTest(rc=answer.rc):
                fields = kcc_health.read_fields(self.read(lambda argv, **kw: answer))
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
        discoveries = [argv for argv, _ in run.calls if is_discovery(argv)]
        self.assertEqual(len(discoveries), 3, "one discovery per read cluster")
        self.assertTrue(all(argv == kcc_health.discovery_argv() for argv in discoveries))
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

    def test_the_credentials_come_from_collects_fetch(self):
        """`collect.fetch_credentials` builds the `get-credentials` command,
        not a copy of it, and its run takes a proxy slot."""
        run = FakeRun({"hub": listing()})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        with mock.patch.object(collect, "fetch_credentials", wraps=collect.fetch_credentials) as fetch, mock.patch.object(
            kcc_health, "_with_proxy_slot", wraps=kcc_health._with_proxy_slot
        ) as slot:
            kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock)
        self.assertEqual(fetch.call_args.args, ("acme", "hub", "us-central1"))
        self.assertIn("get-credentials", slot.call_args_list[0].args[1])
        self.assertEqual(manifest["clusters"][0]["kcc_unready"], [])
        source = Path(kcc_health.__file__).read_text()
        self.assertNotIn('"get-credentials"', source)

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

        bare = kcc_object("ContainerCluster", "prod", status="False", reason="UpdateFailed")
        manifest = {
            "clusters": [
                self.manifest()["clusters"][0],
                {"name": "acme/us-east1/hub-2", "project": "acme", "location": "us-east1", "outcome": "unreachable"},
            ]
        }
        root = Path(self.dir).parent / "repo"
        (root / "clusters").mkdir(parents=True)
        # The file declares the cluster's Namespace with its project, and the
        # finding is about acme's `prod`: with two possible hosts, the
        # object's project must be known, and only the namespaces read gives it.
        (root / "clusters" / "prod.yaml").write_text(
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: infra\n  annotations:\n"
            "    cnrm.cloud.google.com/project-id: acme\n---\n"
            "apiVersion: container.cnrm.cloud.google.com/v1beta1\nkind: ContainerCluster\nmetadata:\n  name: prod\n  namespace: infra\n"
        )

        def finding():
            return {"id": "f", "check": "release-channel-unenrolled", "cluster": "acme/us-central1/prod", "object": "Cluster/prod",
                    "remediation": {"kind": "manifest", "path": "clusters/prod.yaml", "note": "x"}}

        for namespaces, expected in ((None, []), (listing(self.namespace("infra", "acme")), ["f"])):
            with self.subTest(namespaces=bool(namespaces)):
                read = json.loads(json.dumps(manifest))
                run = FakeRun({"hub": listing(bare)}, namespaces=namespaces)
                kcc_health.annotate_manifest(read, deadline=self.deadline, run=run, clock=self.clock)
                self.assertEqual(audit_report.disclose_stalled_kcc_remediations([finding()], read, root), expected)

    def test_a_read_of_some_kinds_names_them_on_the_entry(self):
        run = FakeRun({"hub": listing(kcc_object("ContainerCluster", "prod", status="False", reason="UpdateFailed"))})
        manifest = {"clusters": [self.manifest()["clusters"][0]]}
        kinds = frozenset({(FAKE_GROUP, "ContainerCluster"), (FAKE_GROUP, "ContainerNodePool")})
        kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, clock=self.clock, kinds=kinds)
        entry = manifest["clusters"][0]
        self.assertEqual([r["name"] for r in entry["kcc_unready"]], ["prod"])
        self.assertEqual(entry["kcc_kinds_read"], ["ContainerCluster", "ContainerNodePool"])

    def test_credential_fetches_take_a_proxy_slot_too(self):
        """`get-credentials` goes through the broker as the reads do, so the
        peak of commands in flight, fetches included, stays inside the slots."""
        lock = threading.Lock()
        running = [0, 0]
        kinds = [f"Kind{n}" for n in range(6)]

        def run(argv, *, env=None, timeout=None):
            with lock:
                running[0] += 1
                running[1] = max(running[1], running[0])
            time.sleep(0.01)
            with lock:
                running[0] -= 1
            if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                Path(env["KUBECONFIG"]).write_text("apiVersion: v1\n")
                return kcc_health.Result(0, "", "")
            if is_discovery(argv):
                return kcc_health.Result(0, "".join(crd_line(kind) + "\n" for kind in kinds), "")
            return listing()

        width = kcc_health.proxy_slots()
        manifest = {"clusters": [
            {"name": f"acme/us-central1/c{n}", "project": "acme", "location": "us-central1", "outcome": "collected"}
            for n in range(2 * width)
        ]}
        with ThreadPoolExecutor(max_workers=width) as pool:
            kcc_health.annotate_manifest(manifest, deadline=self.deadline, run=run, mapper=pool.map, clock=self.clock)
        self.assertTrue(all(entry.get("kcc_unready") == [] for entry in manifest["clusters"]))
        self.assertGreater(running[1], 1)
        self.assertLessEqual(running[1], width)

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

    def slow_run(self, credentials_s, crd_s, kind_s, kinds=(DEFAULT_KIND,), deadline=None):
        """A reader that takes `credentials_s`, `crd_s` or `kind_s` of the fake
        clock per call, or its whole timeout when that is shorter. It checks
        that no step is given more time than is left before `deadline`."""
        timeouts = []

        def run(argv, *, env=None, timeout=None):
            step = "gcloud" if argv[0] == "gcloud" else ("crd" if is_discovery(argv) else "kind")
            timeouts.append((step, timeout))
            if deadline is not None:
                self.assertLessEqual(self.now + timeout, deadline, f"{step} was given more than the time left")
            seconds = {"gcloud": credentials_s, "crd": crd_s, "kind": kind_s}[step]
            if timeout is not None and seconds >= timeout:
                self.now += timeout
                return kcc_health.Result(kcc_health.TIMEOUT_RC, "", "killed")
            self.now += seconds
            if step == "gcloud":
                Path(env["KUBECONFIG"]).write_text("apiVersion: v1\n")
                return kcc_health.Result(0, "", "")
            if step == "crd":
                return kcc_health.Result(0, "".join(crd_line(kind) + "\n" for kind in kinds), "")
            return listing()

        return run, timeouts

    def test_the_reads_end_by_the_deadline_and_the_rest_say_the_budget_was_spent(self):
        # The sweep took seven minutes, so two minutes are left.
        self.now = 420.0
        deadline = kcc_health.run_deadline(0.0)
        run, timeouts = self.slow_run(10, 5, 20, deadline=deadline)
        manifest = self.entries("a", "b", "c", "d")
        kcc_health.annotate_manifest(manifest, deadline=deadline, run=run, clock=self.clock)
        self.assertLessEqual(self.now, deadline)
        self.assertLessEqual(deadline, kcc_health.TERMINAL_TIMEOUT_S - kcc_health.PRINT_MARGIN_S)
        a, b, c, d = manifest["clusters"]
        for entry in (a, b, c):
            self.assertEqual(entry["kcc_unready"], [])
        # c's steps were cut to the time left; d's credentials ended with less
        # than a step left, so its read did not start.
        self.assertEqual(timeouts[6:9], [("gcloud", 50), ("crd", 40), ("kind", 35)])
        self.assertIn("time budget", d["kcc_unread"])
        self.assertIn(str(kcc_health.RUN_DEADLINE_S), d["kcc_unread"])

    def test_many_kinds_end_inside_the_read_limit(self):
        """A cluster with many kinds, listed one at a time here: the read stops
        starting kinds when less than a step is left, and ends inside
        `READ_TIMEOUT_S`."""
        run, timeouts = self.slow_run(0, 1, 10, kinds=[f"Kind{n}" for n in range(50)], deadline=kcc_health.READ_TIMEOUT_S)
        manifest = self.entries("a")
        with mock.patch.object(kcc_health, "proxy_slots", return_value=1), mock.patch.object(
            kcc_health, "_slot_pool", threading.BoundedSemaphore(1)
        ):
            kcc_health.annotate_manifest(manifest, deadline=kcc_health.run_deadline(0.0), run=run, clock=self.clock)
        self.assertLessEqual(self.now, kcc_health.READ_TIMEOUT_S)
        self.assertIn(f"did not finish in {kcc_health.READ_TIMEOUT_S}s", manifest["clusters"][0]["kcc_unread"])
        self.assertLess(len([t for t in timeouts if t[0] == "kind"]), 50)

    def test_a_read_is_never_given_more_than_the_time_left(self):
        self.now = 500.0
        deadline = kcc_health.run_deadline(0.0)
        run, timeouts = self.slow_run(1, 1, 1, deadline=deadline)
        manifest = self.entries("a")
        kcc_health.annotate_manifest(manifest, deadline=deadline, run=run, clock=self.clock)
        self.assertEqual(timeouts, [("gcloud", 40), ("crd", 39), ("kind", 38)])
        self.assertEqual(manifest["clusters"][0]["kcc_unready"], [])

    def test_a_failure_around_the_reads_keeps_the_manifest(self):
        def mapper(fn, items):
            raise RuntimeError("pool shut down")

        manifest = self.entries("a")
        kcc_health.annotate_manifest(
            manifest, deadline=kcc_health.run_deadline(0.0), run=self.slow_run(1, 1, 1)[0], mapper=mapper, clock=self.clock
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
            "KCC_CATEGORY": collect.KCC_CATEGORY,
            "KCC_READY_CONDITION": collect.KCC_READY_CONDITION,
        }
        for name, source in pairs.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(kcc_health, name), source)


    def test_the_in_flight_note_equals_audit_reports(self):
        import audit_report  # noqa: PLC0415

        with mock.patch.dict(os.environ, {kcc_health.SCRATCH_DIR_ENV: audit_report.SCRATCH_DIR}):
            self.assertEqual(str(kcc_health.inflight_note_path("stockout-prevention")),
                             audit_report.inflight_path_for("stockout-prevention"))
        self.assertEqual(kcc_health.INFLIGHT_TTL_SECONDS, audit_report.INFLIGHT_TTL_SECONDS)
        with mock.patch.dict(os.environ, {kcc_health.SCRATCH_DIR_ENV: ""}):
            self.assertEqual(str(kcc_health.inflight_note_path("a").parent), kcc_health.DEFAULT_SCRATCH_DIR)


class ReadWantedTest(unittest.TestCase):
    """The read runs with the flag, or while the stream's audit run is in
    flight, so it does not depend on the model copying the flag."""

    STREAM = "stockout-prevention"

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        patcher = mock.patch.dict(os.environ, {kcc_health.SCRATCH_DIR_ENV: scratch.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def note(self, stream=STREAM, text='{"audit": "x", "started_at": 1}', age=0):
        path = self.scratch / f"inflight_{stream}.json"
        path.write_text(text, encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def test_the_flag_alone_reads(self):
        self.assertTrue(kcc_health.read_wanted(True, self.STREAM))
        self.assertEqual(kcc_health.read_reason(True, self.STREAM), kcc_health.READ_BY_FLAG)

    def test_no_note_does_not_read(self):
        self.assertFalse(kcc_health.read_wanted(False, self.STREAM))
        self.assertIs(kcc_health.read_reason(False, self.STREAM), False)

    def test_a_fresh_note_of_the_stream_reads(self):
        self.note()
        self.assertTrue(kcc_health.read_wanted(False, self.STREAM))
        self.assertEqual(kcc_health.read_reason(False, self.STREAM), kcc_health.READ_IN_FLIGHT)

    def test_another_streams_note_does_not_read(self):
        self.note(stream="fleet-consistency-drift")
        self.assertFalse(kcc_health.read_wanted(False, self.STREAM))

    def test_a_stale_note_does_not_read(self):
        self.note(age=kcc_health.INFLIGHT_TTL_SECONDS + 60)
        self.assertFalse(kcc_health.read_wanted(False, self.STREAM))

    def test_a_note_that_does_not_parse_still_reads(self):
        self.note(text="{not json")
        self.assertTrue(kcc_health.read_wanted(False, self.STREAM))

if __name__ == "__main__":
    unittest.main()
